"""Report finalization and localized Markdown rendering."""

from __future__ import annotations

from typing import Any, Literal

from deerflow.skills.review.models import REPORT_SCHEMA_VERSION

Readiness = Literal["blocked", "revise", "publish_candidate"]
Locale = Literal["en", "zh"]

_READINESS_LABELS = {
    "en": {
        "blocked": "Not ready",
        "revise": "Needs revision",
        "publish_candidate": "Publish candidate",
    },
    "zh": {
        "blocked": "不可发布",
        "revise": "需修订",
        "publish_candidate": "可作为发布候选",
    },
}

_ASSURANCE_LABELS = {
    "en": {"static_only": "Static review only"},
    "zh": {"static_only": "仅静态审查"},
}


def readiness_from_facts(
    facts: dict[str, Any],
    *,
    scope: list[str] | None = None,
) -> Readiness:
    summary = facts.get("summary", {})
    if int(summary.get("blockers") or 0) > 0:
        return "blocked"
    if int(summary.get("errors") or 0) > 0:
        return "revise"
    if scope and "all" in scope and facts.get("completeness", {}).get("not_assessed"):
        return "revise"
    return "publish_candidate"


def build_static_report(
    facts: dict[str, Any],
    *,
    completed_at: str,
    scope: list[str] | None = None,
    reviewer_model: str = "deterministic-review-core",
) -> dict[str, Any]:
    scope = scope or ["all"]
    readiness = readiness_from_facts(facts, scope=scope)
    issues = [
        {
            "id": f"deterministic.{index + 1}.{finding['rule_id']}",
            "severity": _semantic_severity(finding.get("severity")),
            "confidence": "high",
            "path": finding.get("path"),
            "line": finding.get("line"),
            "problem": finding.get("message"),
            "impact": ("Deterministic finding affects package readiness or maintainability."),
            "remediation": finding.get("remediation"),
            "suggested_replacement": None,
        }
        for index, finding in enumerate(facts.get("findings", []))
        if finding.get("severity") in {"blocker", "error", "warning"}
    ]
    limitations: list[str] = []
    if facts.get("completeness", {}).get("truncated"):
        limitations.append("Package content was truncated; omitted content was not assessed.")
    for error in facts.get("reader_errors", []):
        limitations.append(f"Reader error {error.get('code')}: {error.get('message')}")
    for error in facts.get("analyzer_errors", []):
        limitations.append(f"Analyzer error {error.get('code')}: {error.get('message')}")

    facts_subject = facts.get("subject", {})
    report_subject = {
        "display_ref": facts_subject.get("display_ref"),
        "package_digest": facts_subject.get("package_digest"),
    }
    for key in (
        "skill_id",
        "version_id",
        "version_number",
        "payload_checksum",
    ):
        if key in facts_subject:
            report_subject[key] = facts_subject[key]

    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "subject": report_subject,
        "review": {
            "scope": scope,
            "profile": facts.get("profile", "deerflow"),
            "facts_schema_version": facts.get("schema_version"),
            "reviewer_model": reviewer_model,
            "completed_at": completed_at,
        },
        "readiness": readiness,
        "assurance": "static_only",
        "dimensions": _dimensions_from_facts(facts),
        "issues": issues,
        "evidence": {
            "facts_complete": not facts.get("completeness", {}).get("truncated"),
            "runtime_runs": [],
            "baseline": None,
            "retained_artifacts": [],
            "limitations": limitations,
        },
        "recommended_actions": _recommended_actions(facts, readiness),
    }


def render_report_markdown(
    report: dict[str, Any],
    facts: dict[str, Any] | None = None,
    *,
    locale: Locale = "en",
) -> str:
    labels = _READINESS_LABELS[locale]
    assurance_labels = _ASSURANCE_LABELS[locale]
    zh = locale == "zh"
    lines = [
        "# Skill Review Report" if not zh else "# 技能审查报告",
        "",
        "## Executive Summary" if not zh else "## 摘要",
        f"- Subject: {report.get('subject', {}).get('display_ref')}",
        f"- Digest: {report.get('subject', {}).get('package_digest')}",
        (f"- Readiness: {report.get('readiness')} ({labels.get(report.get('readiness'), report.get('readiness'))})"),
        (f"- Assurance: {report.get('assurance')} ({assurance_labels.get(report.get('assurance'), report.get('assurance'))})"),
        "",
        "## Scope and Completeness" if not zh else "## 范围与完整性",
        f"- Scope: {', '.join(report.get('review', {}).get('scope', []))}",
        f"- Profile: {report.get('review', {}).get('profile')}",
    ]
    if facts:
        completeness = facts.get("completeness", {})
        lines.extend(
            [
                f"- Truncated: {completeness.get('truncated')}",
                (f"- Not assessed: {', '.join(completeness.get('not_assessed') or []) or '(none)'}"),
            ]
        )
    lines.extend(["", "## Findings" if not zh else "## 问题"])
    issues = report.get("issues", [])
    if not issues:
        lines.append("- No deterministic issues were reported.")
    for issue in issues:
        location = issue.get("path") or "<package>"
        if issue.get("line") is not None:
            location = f"{location}:{issue['line']}"
        lines.append(f"- {issue.get('severity')} {issue.get('id')} at {location}: {issue.get('problem')}")
    lines.extend(["", "## Dimension Review" if not zh else "## 维度审查"])
    for dimension in report.get("dimensions", []):
        lines.append(f"- {dimension.get('id')}: {dimension.get('status')} - {dimension.get('summary')}")
    lines.extend(["", "## Evidence" if not zh else "## 证据"])
    evidence = report.get("evidence", {})
    lines.append(f"- Facts complete: {evidence.get('facts_complete')}")
    for limitation in evidence.get("limitations") or []:
        lines.append(f"- Limitation: {limitation}")
    lines.extend(["", "## Recommended Actions" if not zh else "## 建议动作"])
    actions = report.get("recommended_actions") or []
    if not actions:
        lines.append("- No required action within the assessed scope.")
    else:
        lines.extend(f"- {action}" for action in actions)
    return "\n".join(lines).rstrip() + "\n"


def _semantic_severity(severity: Any) -> str:
    if severity == "blocker":
        return "blocker"
    if severity == "error":
        return "major"
    return "minor"


def _dimensions_from_facts(facts: dict[str, Any]) -> list[dict[str, Any]]:
    summary = facts.get("summary", {})
    status = "blocker" if summary.get("blockers") else "concern" if summary.get("errors") or summary.get("warnings") else "pass"
    return [
        {
            "id": "structure",
            "status": status,
            "summary": (f"{summary.get('blockers', 0)} blocker(s), {summary.get('errors', 0)} error(s), {summary.get('warnings', 0)} warning(s)"),
        },
        {
            "id": "evidence_quality",
            "status": ("concern" if facts.get("evals", {}).get("case_count", 0) == 0 else "pass"),
            "summary": (f"{facts.get('evals', {}).get('case_count', 0)} eval case(s) detected"),
        },
    ]


def _recommended_actions(
    facts: dict[str, Any],
    readiness: str,
) -> list[str]:
    if readiness == "publish_candidate":
        return []
    return [f"{finding.get('rule_id')}: {finding.get('remediation')}" for finding in facts.get("findings", [])[:5]]
