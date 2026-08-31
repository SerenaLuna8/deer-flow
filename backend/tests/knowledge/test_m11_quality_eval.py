"""M11 offline quality decisions and explicitly budgeted real-model entry."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import replace
from uuid import UUID

import eval_quality as quality
import pytest

from app.system_settings.models import CreateSystemModel


def _native_summary_template():
    return CreateSystemModel(
        display_name="Native summary template",
        status="active",
        provider_id=UUID(int=7),
        provider_adapter="deepseek",
        provider_model="deepseek-v4-flash",
        max_input_tokens=1_000_000,
        settings={
            "max_tokens": 51200,
            "temperature": 0.7,
            "request_timeout": 600,
            "reasoning_effort": "high",
            "when_thinking_enabled": {"extra_body": {"thinking": {"type": "enabled"}}},
            "when_thinking_disabled": {"extra_body": {"thinking": {"type": "disabled"}}},
        },
        supports_thinking=True,
        supports_reasoning_effort=True,
        supports_vision=False,
    )


class _DiagnosticFakeRuntime:
    def __init__(self, outcome):
        self.outcome = outcome
        self.received = None

    async def ainvoke(self, messages, **kwargs):
        self.received = (messages, kwargs)
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return self.outcome


def test_summary_diagnostics_sink_rejects_unknown_fields_values_and_context_overrides():
    sentinel = "NEVER_SERIALIZE_UNTRUSTED_DIAGNOSTIC_FIELDS"
    diagnostics = quality.SummaryDiagnostics(task_id=UUID(int=1), document_id=UUID(int=2), task_attempt=1, call_index=3)
    diagnostics.record(
        "runtime_response",
        task_id=sentinel,
        document_id=sentinel,
        task_attempt=999,
        call_index=999,
        event_index=999,
        prompt=sentinel,
        metadata={"secret": sentinel},
        endpoint=sentinel,
        raw_object=object(),
        content_kind="string",
        content_length=4,
        content_empty=False,
        finish_reason=sentinel,
        token_usage={"input_tokens": 4, "output_tokens": True, "total_tokens": sentinel, "extra": sentinel},
    )
    diagnostics.record("runtime_error", exception_name=sentinel, exception_category=sentinel, http_status=True, elapsed_ms=float("nan"), content_kind="string")
    diagnostics.record(sentinel, prompt=sentinel)
    first = diagnostics.events[0]
    assert "prompt" not in first and "metadata" not in first and "endpoint" not in first and "raw_object" not in first
    assert (first["event_index"], first["task_id"], first["document_id"], first["task_attempt"], first["call_index"]) == (1, str(UUID(int=1)), str(UUID(int=2)), 1, 3)
    assert first["content_kind"] == "string" and first["content_length"] == 4 and first["content_empty"] is False
    assert first["token_usage"] == {"input_tokens": 4} and "finish_reason" not in first
    assert len(diagnostics.events) == 2
    assert not {"exception_name", "exception_category", "http_status", "elapsed_ms", "content_kind"} & diagnostics.events[1].keys()
    assert sentinel not in json.dumps(diagnostics.events, allow_nan=False)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure,expected_name,expected_status",
    [("timeout", "TimeoutError", None), ("cancelled", "CancelledError", None), ("rate_limit", "RateLimitError", 429), ("malicious_unknown", "UnknownError", None), ("invalid_status", "APIStatusError", None)],
)
async def test_summary_diagnostics_preserve_exception_identity_without_secret_leak(failure, expected_name, expected_status):
    import asyncio

    import httpx
    from openai import APIStatusError, RateLimitError

    sentinel = "NEVER_SERIALIZE_EXCEPTION_PROMPT_OR_KEY"
    response = httpx.Response(429, request=httpx.Request("POST", f"https://example.invalid/{sentinel}"))
    if failure == "timeout":
        original = TimeoutError(sentinel)
    elif failure == "cancelled":
        original = asyncio.CancelledError(sentinel)
    elif failure == "rate_limit":
        original = RateLimitError(sentinel, response=response, body={"message": sentinel})
    elif failure == "invalid_status":
        original = APIStatusError(sentinel, response=response, body={"message": sentinel})
        original.status_code = True
    else:
        original = type(sentinel, (Exception,), {"status_code": sentinel})(sentinel)
    diagnostics = quality.SummaryDiagnostics()
    diagnostics.task_id, diagnostics.document_id = UUID(int=1), UUID(int=2)
    diagnostics.task_attempt, diagnostics.call_index = 1, 1
    fake = _DiagnosticFakeRuntime(original)
    runtime = quality.ObservedSummaryRuntime(fake, diagnostics)
    messages, kwargs = [sentinel], {"profile": "test-profile", "model_overrides": {"max_tokens": 1024}, "provider_max_retries": 0, "deadline_monotonic": 123.0}
    with pytest.raises(type(original)) as caught:
        await runtime.ainvoke(messages, **kwargs)
    assert caught.value is original and fake.received == (messages, kwargs)
    event = diagnostics.events[0]
    assert event["event"] == "runtime_error" and event["exception_name"] == expected_name and event["http_status"] == expected_status
    assert event["task_id"] == str(UUID(int=1)) and event["document_id"] == str(UUID(int=2)) and event["task_attempt"] == 1 and event["call_index"] == 1
    assert event["elapsed_ms"] >= 0
    assert sentinel not in json.dumps(diagnostics.events)


@pytest.mark.asyncio
@pytest.mark.parametrize("content,finish_reason,expected_kind,expected_empty", [("", "length", "string", True), ([{"text": "NEVER_SERIALIZE_RESPONSE_METADATA"}], "NEVER_SERIALIZE_RESPONSE_METADATA", "list", False)])
async def test_summary_diagnostics_preserve_response_and_project_only_safe_metadata(content, finish_reason, expected_kind, expected_empty):
    from langchain_core.messages import AIMessage

    sentinel = "NEVER_SERIALIZE_RESPONSE_METADATA"
    response = AIMessage(
        content=content,
        response_metadata={"finish_reason": finish_reason, "token_usage": {"prompt_tokens": 30, "completion_tokens": 1024, "total_tokens": 1054, "secret": sentinel}, "headers": {"Authorization": sentinel}},
        additional_kwargs={"reasoning_content": sentinel},
    )
    diagnostics = quality.SummaryDiagnostics()
    runtime = quality.ObservedSummaryRuntime(_DiagnosticFakeRuntime(response), diagnostics)
    assert await runtime.ainvoke([sentinel], profile="test") is response
    event = diagnostics.events[0]
    assert event["event"] == "runtime_response" and event["content_kind"] == expected_kind and event["content_empty"] is expected_empty
    assert event["content_length"] == len(content)
    assert event["finish_reason"] == ("length" if finish_reason == "length" else "unknown")
    assert event["token_usage"] == {"input_tokens": 30, "output_tokens": 1024, "total_tokens": 1054}
    assert sentinel not in json.dumps(diagnostics.events)


@pytest.mark.asyncio
async def test_summary_diagnostics_discard_non_numeric_or_unbounded_token_metadata():
    from langchain_core.messages import AIMessage

    sentinel = "NEVER_SERIALIZE_TOKEN_METADATA"
    response = AIMessage(content=sentinel, response_metadata={"finish_reason": {"secret": sentinel}, "token_usage": {"prompt_tokens": sentinel, "completion_tokens": True, "total_tokens": 10**20}})
    diagnostics = quality.SummaryDiagnostics()
    assert await quality.ObservedSummaryRuntime(_DiagnosticFakeRuntime(response), diagnostics).ainvoke([]) is response
    assert diagnostics.events[0]["token_usage"] == {} and diagnostics.events[0]["finish_reason"] == "unknown"
    assert sentinel not in json.dumps(diagnostics.events)


@pytest.mark.asyncio
async def test_summary_diagnostics_project_standard_usage_without_extra_metadata():
    from langchain_core.messages import AIMessage

    sentinel = "NEVER_SERIALIZE_EXTRA_RESPONSE_FIELDS"
    response = AIMessage(content=sentinel, usage_metadata={"input_tokens": 10, "output_tokens": 20, "total_tokens": 30, "output_token_details": {"reasoning": 5}}, response_metadata={"token_usage": sentinel})
    diagnostics = quality.SummaryDiagnostics()
    assert await quality.ObservedSummaryRuntime(_DiagnosticFakeRuntime(response), diagnostics).ainvoke([]) is response
    assert diagnostics.events[0]["token_usage"] == {"input_tokens": 10, "output_tokens": 20, "total_tokens": 30}
    assert sentinel not in json.dumps(diagnostics.events)


@pytest.mark.asyncio
async def test_summary_diagnostics_keep_first_failure_when_later_attempt_is_budget_denied():
    from app.knowledge.model_port import RegistryKnowledgeModelPort
    from deerflow.secrets import SecretKey

    sentinel = "NEVER_SERIALIZE_FIRST_FAILURE"
    diagnostics = quality.SummaryDiagnostics()
    diagnostics.task_id, diagnostics.document_id = UUID(int=3), UUID(int=4)
    diagnostics.task_attempt = 1
    runtime = quality.ObservedSummaryRuntime(_DiagnosticFakeRuntime(TimeoutError(sentinel)), diagnostics)
    port = quality.BudgetedSummaryPort(RegistryKnowledgeModelPort(secret_key=SecretKey(b"t" * 32), model_runtime=runtime), max_calls=1, diagnostics=diagnostics)
    with pytest.raises(quality.KnowledgeError):
        await port.generate_summary(model_ref="test-model", prompt=sentinel)
    first_attempt = list(diagnostics.events)
    assert first_attempt[0]["exception_name"] == "TimeoutError" and first_attempt[0]["call_index"] == 1
    diagnostics.task_attempt = 2
    with pytest.raises(quality.KnowledgeError) as caught:
        await port.generate_summary(model_ref="test-model", prompt=sentinel)
    assert caught.value.message == "评测摘要调用预算已耗尽" and port.calls == 1
    assert diagnostics.events[: len(first_attempt)] == first_attempt
    assert diagnostics.events[-1]["event"] == "budget_denied" and diagnostics.events[-1]["task_attempt"] == 2
    assert sentinel not in json.dumps(diagnostics.events)


def test_incomplete_summary_diagnostics_report_says_retrieval_quality_not_evaluated(tmp_path):
    report = {
        "status": "failed_or_review_pending",
        "corpus_queries": 85,
        "retrieval_quality_evaluated": False,
        "outcomes": [],
        "summary": {},
        "summary_generation": {"summary_rows": 18, "eligible_segments": 24, "failed_tasks": 3},
        "summary_diagnostics": {"events": [{"event": "budget_denied", "call_index": None}]},
        "gates": {"all_passed": False},
        "usage": {"summary_calls": 24},
        "deployment_note": "Test-only report.",
    }
    quality.write_m11_report(report, json_path=tmp_path / "report.json", md_path=tmp_path / "report.md")
    rendered = (tmp_path / "report.md").read_text()
    assert "未执行检索质量评测" in rendered and "不能据此判断召回质量下降" in rendered
    assert "budget_denied" in rendered


def _corpus():
    queries = []
    for split in ("dev", "holdout"):
        for category, count in (("question_style", 10), ("identifier", 1), ("natural_language", 1), ("tail", 1), ("no_answer", 1)):
            queries.extend({"id": f"{split}-{category}-{index}", "split": split, "category": category} for index in range(count))
    return {
        "queries": queries,
        "documents": [{"source_id": "long-document", "segments": [{"content": "长" * 200}, {"content": "短"}]}],
        "gates": {
            "question_recall_candidate_uplift_pp": 5,
            "question_recall_at_10_uplift_pp": 5,
            "overall_ndcg_regression": 0.02,
            "no_answer_false_recall_not_worse": True,
            "existing_category_recall_not_worse": True,
            "p95_regression_review_ratio_m11": 1.2,
        },
    }


def _outcomes():
    rows = []
    for query in _corpus()["queries"]:
        for mode in ("semantic", "hybrid"):
            for enabled in (False, True):
                correct = query["category"] != "question_style" or int(query["id"].rsplit("-", 1)[1]) < (9 if enabled else 8)
                row = quality.QueryOutcome(
                    query["id"], query["split"], query["category"], mode, recall_candidate=correct, recall_at_10=correct, ndcg_at_10=1.0 if correct else 0.0, returned=0 if query["category"] == "no_answer" else 1, non_provider_ms=10.0
                )
                row.summary_enabled = enabled
                rows.append(row)
    return rows


def _baseline():
    metric = {"recall_candidate": 1.0, "recall_at_10": 1.0, "ndcg_at_10": 1.0, "p95_non_provider_ms": 10.0, "count": 1, "errors": 0}
    holdout = {category: {mode: dict(metric) for mode in ("semantic", "hybrid")} for category in ("identifier", "natural_language", "tail")}
    holdout["no_answer"] = {mode: {"false_recall": 0.0, "p95_non_provider_ms": 10.0, "count": 1, "errors": 0} for mode in ("semantic", "hybrid")}
    return {"corpus_queries": 8, "models": {"primary_embedding": quality.PRIMARY_EMBEDDING, "secondary_embedding": quality.SECONDARY_EMBEDDING, "reranker": quality.RERANKER}, "summary": {"holdout": holdout}}


def _decide(rows, **kwargs):
    return quality.evaluate_m11_gates(quality.summarize_m11(rows), _corpus(), baseline_report=_baseline(), **kwargs)


def test_m11_complete_paired_improvement_passes():
    result = _decide(_outcomes())
    assert result["quality_passed"] is True and result["all_passed"] is True


@pytest.mark.parametrize("missing", ["on_axis", "holdout", "single_query"])
def test_m11_rejects_incomplete_or_missing_holdout_evidence(missing):
    rows = _outcomes()
    if missing == "on_axis":
        rows = [row for row in rows if not (row.mode == "hybrid" and row.summary_enabled)]
    elif missing == "holdout":
        rows = [row for row in rows if row.split != "holdout"]
    else:
        rows.pop()
    assert _decide(rows)["all_passed"] is False


@pytest.mark.parametrize("metric", ["recall_candidate", "recall_at_10"])
def test_m11_requires_five_percentage_points_for_both_question_recall_metrics(metric):
    rows = _outcomes()
    for row in rows:
        if row.category == "question_style" and row.summary_enabled:
            setattr(row, metric, int(row.query_id.rsplit("-", 1)[1]) < 8)
    assert _decide(rows)["quality_passed"] is False


def test_m11_no_answer_regression_fails():
    rows = _outcomes()
    for row in rows:
        if row.category == "no_answer" and row.summary_enabled:
            row.returned = 1
    assert _decide(rows)["quality_passed"] is False


def test_m11_two_equally_regressed_no_answer_axes_do_not_hide_m10_regression():
    rows = _outcomes()
    for row in rows:
        if row.category == "no_answer":
            row.returned = 1
    assert _decide(rows)["quality_passed"] is False


def test_m11_existing_category_must_keep_frozen_m10_waterline():
    rows = _outcomes()
    for row in rows:
        if row.category == "identifier":
            row.recall_at_10 = False  # off is worse too, so paired-only checking would miss it
    assert _decide(rows)["quality_passed"] is False


def test_m11_overall_ndcg_cannot_regress_more_than_point_zero_two():
    rows = _outcomes()
    for row in rows:
        if row.summary_enabled and row.category != "no_answer":
            row.ndcg_at_10 = 0.0
    assert _decide(rows)["quality_passed"] is False


def test_m11_errors_are_failed_evidence_not_excluded_successes():
    rows = _outcomes()
    rows[0].error = "KNOWLEDGE_EMBEDDING_FAILED"
    assert _decide(rows)["quality_passed"] is False
    summary = quality.summarize_m11(rows)
    assert summary["dev"]["question_style"]["semantic"]["off"]["recall_at_10"] == 0.7


def test_m11_performance_regression_needs_real_explicit_review():
    rows = _outcomes()
    for row in rows:
        if row.summary_enabled:
            row.non_provider_ms = 20.0
    undecided = _decide(rows)
    assert undecided["quality_passed"] is True and undecided["all_passed"] is False
    assert undecided["p95_review_pending"] is True
    assert _decide(rows, latency_review="   ")["all_passed"] is False
    reviewed = _decide(rows, latency_review="Operator reviewed measured 2x P95; accepted for this deployment.")
    assert reviewed["all_passed"] is True and reviewed["p95_review_pending"] is False


def test_m11_two_equally_slow_axes_still_require_review_against_frozen_m10():
    rows = _outcomes()
    for row in rows:
        row.non_provider_ms = 100.0
    result = _decide(rows)
    assert result["all_passed"] is False and result["p95_review_pending"] is True


@pytest.mark.parametrize("model,budget,opted_in", [(None, 1, True), ("bad model name", 1, True), ("summary", None, True), ("summary", 0, True), ("summary", 1, False)])
def test_m11_preflight_requires_model_and_sufficient_authorized_call_budget(model, budget, opted_in):
    with pytest.raises(ValueError):
        quality.m11_eval_preflight(_corpus(), summary_model=model, max_summary_calls=budget, opted_in=opted_in)


def test_m11_preflight_counts_only_eligible_long_sources():
    assert quality.m11_eval_preflight(_corpus(), summary_model="approved-summary", max_summary_calls=1, opted_in=True) == 1


def test_fresh_m10_equivalent_scope_keeps_original_frozen_sources_and_queries():
    corpus = quality.load_legacy_m10_corpus()
    assert len(corpus["queries"]) == 65 and len(corpus["documents"]) == 20
    expected = {"queries": "fb20703fa950571050300e172ec0457f2250b04340e3f1356401652fda4394e5", "documents": "f27ca1a442f7d4a2bd8726915ebff54f7e1f560840e133ad57ad8301711ea52b"}
    for kind, digest in expected.items():
        assert hashlib.sha256(json.dumps(corpus[kind], ensure_ascii=False, sort_keys=True).encode()).hexdigest() == digest


def test_fresh_m10_equivalent_rejects_modified_legacy_source(monkeypatch):
    corpus = quality.load_corpus()
    corpus["documents"][0]["segments"][0]["content"] += "changed"
    monkeypatch.setattr(quality, "load_corpus", lambda: corpus)
    with pytest.raises(ValueError, match="original frozen corpus"):
        quality.load_legacy_m10_corpus()


def test_m11_cannot_pass_without_frozen_m10_category_waterlines():
    assert quality.evaluate_m11_gates(quality.summarize_m11(_outcomes()), _corpus())["quality_passed"] is False


@pytest.mark.asyncio
async def test_m11_invalid_budget_stops_before_source_embedding(monkeypatch):
    monkeypatch.setenv("ACT_WEAVE_KNOWLEDGE_M11_QUALITY_EVAL", "1")
    monkeypatch.setattr(quality, "load_corpus", _corpus)

    async def forbidden_source_embedding(*args, **kwargs):
        raise AssertionError("source embedding was dispatched before budget approval")

    monkeypatch.setattr(quality, "build_eval_context", forbidden_source_embedding)
    with pytest.raises(ValueError):
        await quality.run_m11_quality_eval("unused-database-url", api_key="test-key", summary_model="approved", max_summary_calls=0)


@pytest.mark.asyncio
async def test_m11_missing_baseline_stops_before_source_embedding(monkeypatch, tmp_path):
    monkeypatch.setenv("ACT_WEAVE_KNOWLEDGE_M11_QUALITY_EVAL", "1")
    monkeypatch.setenv("ACT_WEAVE_KNOWLEDGE_M11_BASELINE_REPORT", str(tmp_path / "not-available.json"))
    monkeypatch.setattr(quality, "load_corpus", _corpus)

    async def forbidden_source_embedding(*args, **kwargs):
        raise AssertionError("provider I/O cannot precede baseline validation")

    monkeypatch.setattr(quality, "build_eval_context", forbidden_source_embedding)
    with pytest.raises(ValueError, match="verified M10 baseline"):
        await quality.run_m11_quality_eval("unused-database-url", api_key="test-key", summary_model="approved", max_summary_calls=1)


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", ["model_mismatch", "unknown_adapter", "invalid_setting", "invalid_capability", "invalid_endpoint"])
async def test_m11_native_summary_invalid_template_stops_before_source_embedding(monkeypatch, tmp_path, invalid):
    monkeypatch.setenv("ACT_WEAVE_KNOWLEDGE_M11_QUALITY_EVAL", "1")
    monkeypatch.setattr(quality, "load_corpus", _corpus)
    baseline = tmp_path / "baseline.json"
    baseline.write_text(json.dumps(_baseline()))
    template = _native_summary_template()
    endpoint = "https://api.deepseek.invalid/v1"
    if invalid == "model_mismatch":
        template = replace(template, provider_model="different-model")
    elif invalid == "unknown_adapter":
        template = replace(template, provider_adapter="unknown")
    elif invalid == "invalid_setting":
        template = replace(template, settings={"request_timeout": 0})
    elif invalid == "invalid_capability":
        template = replace(template, supports_thinking="yes")
    else:
        endpoint = "not-a-url"

    async def forbidden_source_embedding(*args, **kwargs):
        raise AssertionError("native summary configuration must be validated before Provider I/O")

    monkeypatch.setattr(quality, "build_eval_context", forbidden_source_embedding)
    with pytest.raises(ValueError):
        await quality.run_m11_quality_eval(
            "unused-database-url", api_key="retrieval-test-key", summary_model="deepseek-v4-flash", max_summary_calls=1, summary_model_template=template, summary_base_url=endpoint, baseline_report_path=baseline
        )


@pytest.mark.asyncio
@pytest.mark.postgres
async def test_m11_native_summary_template_survives_isolated_catalog_materialization(migrated_postgres_database_url, monkeypatch):
    import base64
    from types import SimpleNamespace

    import httpx
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.system_settings import SystemModelMaterializer
    from deerflow.persistence.system_settings import SystemModelConfigRow

    monkeypatch.setenv("ACT_WEAVE_SECRET_KEY", base64.b64encode(b"n" * 32).decode())

    async def forbidden_http(*args, **kwargs):
        raise AssertionError("catalog creation and materialization must not dispatch HTTP")

    monkeypatch.setattr(httpx.AsyncClient, "request", forbidden_http)
    template = _native_summary_template()
    engine = create_async_engine(migrated_postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        _port, model_id = await quality._configure_m11_summary_model(
            SimpleNamespace(factory=factory), provider_model="deepseek-v4-flash", base_url="https://api.deepseek.invalid/v1", api_key="evaluation-only-test-key", summary_model_template=template
        )
        async with factory() as session:
            row = await session.get(SystemModelConfigRow, model_id)
            assert row is not None and row.provider_id != template.provider_id
            assert row.provider_adapter == "deepseek" and row.provider_model == "deepseek-v4-flash"
            assert "api_key" not in row.settings
        material = await SystemModelMaterializer(factory).materialize_active(str(model_id))
        assert material.use == "deerflow.models.patched_deepseek:PatchedChatDeepSeek"
        assert material.system_provider_adapter == "deepseek"
        assert material.max_input_tokens == 1_000_000
        assert material.supports_thinking and material.supports_reasoning_effort and not material.supports_vision
        assert material.max_tokens == 51200 and material.temperature == 0.7 and material.request_timeout == 600
        assert material.reasoning_effort == "high"
        assert material.when_thinking_disabled == {"extra_body": {"thinking": {"type": "disabled"}}}
        assert material.base_url == "https://api.deepseek.invalid/v1"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_m11_hard_budget_stops_further_summary_provider_calls():
    class Provider:
        async def generate_summary(self, **kwargs):
            return "生成的摘要"

    port = quality.BudgetedSummaryPort(Provider(), max_calls=1)
    assert await port.generate_summary(model_ref="model", prompt="source") == "生成的摘要"
    with pytest.raises(quality.KnowledgeError) as caught:
        await port.generate_summary(model_ref="model", prompt="second-source")
    assert caught.value.code == "KNOWLEDGE_TASK_FAILED"
    assert port.calls == 1


def test_m11_estimated_cost_requires_explicit_approved_rates():
    usage = {"embed_tokens_by_model": {"embed": 1_000_000}, "rerank_tokens": 500_000, "summary_input_tokens_estimated": 200_000, "summary_output_tokens_estimated": 100_000}
    assert quality.m11_cost_estimate(usage, None) is None
    rates = {"approval_reference": "operator-provided test rate", "currency": "CNY", "per_million_tokens": {"embed": 2.0, quality.RERANKER: 4.0, "summary_input": 5.0, "summary_output": 10.0}}
    assert quality.m11_cost_estimate(usage, rates)["estimated_total"] == 6.0
    rates["approval_reference"] = ""
    with pytest.raises(ValueError):
        quality.m11_cost_estimate(usage, rates)


def test_m11_blocked_report_contains_no_measurements_or_passed_quality(tmp_path):
    report = quality.blocked_m11_report(_corpus())
    quality.write_m11_report(report, json_path=tmp_path / "report.json", md_path=tmp_path / "report.md")
    assert report["status"] == "blocked_pending_operator_input"
    assert report["quality_metrics"] is None and report["gates"]["all_passed"] is False
    assert report["usage"] is None
    assert "未调用真实模型" in (tmp_path / "report.md").read_text()


def test_m11_missing_baseline_is_an_explicit_blocker(monkeypatch, tmp_path):
    monkeypatch.setattr(quality, "REPORT_JSON_PATH", tmp_path / "missing-m10.json")
    report = quality.blocked_m11_report(_corpus())
    assert "verified_m10_baseline_unavailable" in report["blocking_reasons"]


@pytest.mark.parametrize("change", ["missing", "wrong_models", "wrong_corpus_count", "missing_latency", "error_rows"])
def test_m11_baseline_preflight_rejects_unverifiable_or_mismatched_m10_evidence(tmp_path, change):
    baseline = _baseline()
    path = tmp_path / "m10.json"
    if change == "wrong_models":
        baseline["models"]["primary_embedding"] = "different-model"
    elif change == "wrong_corpus_count":
        baseline["corpus_queries"] = 99
    elif change == "missing_latency":
        baseline["summary"]["holdout"]["identifier"]["semantic"].pop("p95_non_provider_ms")
    elif change == "error_rows":
        baseline["summary"]["holdout"]["identifier"]["semantic"]["errors"] = 1
    if change != "missing":
        path.write_text(json.dumps(baseline))
    with pytest.raises(ValueError):
        quality.load_m11_baseline(_corpus(), path)


def test_m11_baseline_path_can_select_operator_verified_report(monkeypatch, tmp_path):
    path = tmp_path / "verified.json"
    path.write_text(json.dumps(_baseline()))
    monkeypatch.setenv("ACT_WEAVE_KNOWLEDGE_M11_BASELINE_REPORT", str(path))
    assert quality.load_m11_baseline(_corpus(), quality.resolve_m11_baseline_path()) == _baseline()


@pytest.mark.asyncio
@pytest.mark.postgres
async def test_failed_source_embedding_releases_eval_clients_and_pool(postgres_database_url, monkeypatch):
    clients, engines = [], []
    real_client, real_engine = quality.KnowledgeModelClient, quality.create_async_engine

    def client_factory(*args, **kwargs):
        client = real_client(*args, **kwargs)
        clients.append(client)
        return client

    def engine_factory(*args, **kwargs):
        engine = real_engine(*args, **kwargs)
        engines.append(engine)
        return engine

    async def failing_embedding(*args, **kwargs):
        raise quality.KnowledgeError("KNOWLEDGE_EMBEDDING_FAILED", "受控评测失败")

    monkeypatch.setattr(quality, "KnowledgeModelClient", client_factory)
    monkeypatch.setattr(quality, "create_async_engine", engine_factory)
    monkeypatch.setattr(quality, "_embed_unique", failing_embedding)
    with pytest.raises(quality.KnowledgeError):
        await quality.build_eval_context(postgres_database_url, "local-test-key")
    try:
        assert clients[0]._http.is_closed
        assert engines[0].pool.checkedin() == 0
    finally:
        for client in clients:
            await client.aclose()
        for engine in engines:
            await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.postgres
@pytest.mark.parametrize("provider_failure,summary_adapter", [(False, "openai"), (True, "openai"), (False, "deepseek")])
async def test_m11_runner_generates_once_via_durable_worker_and_keeps_rows_between_axes(postgres_database_url, monkeypatch, tmp_path, provider_failure, summary_adapter):
    from replay_knowledge import KnowledgeReplayState, ReplayKnowledgeProviderServer

    corpus = _corpus()
    source = "深海列车 " + "用于确定性评测的虚构源材料。" * 20
    digest = hashlib.sha256(source.encode()).hexdigest()
    corpus["documents"] = [{"source_id": "source", "base": "large_hybrid", "chunking_mode": "general", "segments": [{"position": 1, "content": source}]}]
    corpus["parameters"] = {"scale_retrieval_units": 4, "top_k": 10, "score_threshold": 0.2}
    for query in corpus["queries"]:
        query["query"] = query["id"]
        query["judgments"] = [] if query["category"] == "no_answer" else [{"source_id": "source", "position": 1, "content_sha256": digest, "grade": 2}]
    monkeypatch.setenv("ACT_WEAVE_KNOWLEDGE_M11_QUALITY_EVAL", "1")
    for key in ("NO_PROXY", "no_proxy"):
        monkeypatch.setenv(key, "127.0.0.1,localhost")
    monkeypatch.setattr(quality, "load_corpus", lambda: corpus)
    monkeypatch.setattr(quality, "M11_REPORT_JSON_PATH", tmp_path / "m11.json")
    monkeypatch.setattr(quality, "M11_REPORT_MD_PATH", tmp_path / "m11.md")
    baseline = tmp_path / "baseline.json"
    baseline.write_text(json.dumps(_baseline()))
    monkeypatch.setattr(quality, "REPORT_JSON_PATH", baseline)
    state = KnowledgeReplayState()
    state.chat_failures_remaining = int(provider_failure)
    provider = ReplayKnowledgeProviderServer(state)
    provider.start()
    monkeypatch.setattr(quality, "PROVIDER_BASE_URL", provider.base_url)
    try:
        template = _native_summary_template() if summary_adapter == "deepseek" else None
        report = await quality.run_m11_quality_eval(
            postgres_database_url,
            api_key="local-replay-test-key",
            summary_model=template.provider_model if template is not None else "replay/summary",
            max_summary_calls=1,
            summary_base_url=provider.base_url,
            summary_model_template=template,
        )
        assert state.snapshot()["chat_calls"] == 1
        assert report["summary_generation"]["summary_rows"] == (0 if provider_failure else 1)
        assert report["summary_generation"]["failed_tasks"] == int(provider_failure)
        assert report["usage"]["summary_calls"] == 1
        assert report["usage"]["estimated_cost"] is None
        assert report["models"]["summary_provider_adapter"] == summary_adapter
        assert len(report["outcomes"]) == (0 if provider_failure else len(corpus["queries"]) * 4)
        if provider_failure:
            assert report["gates"]["quality_passed"] is False
            assert state.snapshot()["rerank_calls"] == 0
        assert all(pair["identical_hit_identities"] and pair["warm_cache_hits"] > 0 and pair["warm_cache_misses"] == 0 for pair in report["cache_pairs"])
        assert (tmp_path / "m11.json").is_file()
    finally:
        provider.stop()


@pytest.mark.asyncio
@pytest.mark.postgres
@pytest.mark.provider_integration
async def test_m11_holdout_quality_gates_against_real_models(postgres_database_url):
    if os.environ.get("ACT_WEAVE_KNOWLEDGE_M11_QUALITY_EVAL") != "1":
        pytest.skip("explicit M11 model and call-budget approval is required")
    model = os.environ.get("ACT_WEAVE_KNOWLEDGE_M11_SUMMARY_MODEL")
    raw_budget = os.environ.get("ACT_WEAVE_KNOWLEDGE_M11_MAX_SUMMARY_CALLS")
    api_key = quality.resolve_provider_api_key()
    if not api_key:
        pytest.fail("M11 opted in without an API key")
    report = await quality.run_m11_quality_eval(postgres_database_url, api_key=api_key, summary_model=model, max_summary_calls=int(raw_budget) if raw_budget else None, latency_review=os.environ.get("ACT_WEAVE_KNOWLEDGE_M11_P95_REVIEW"))
    assert report["gates"]["all_passed"], report["gates"]
