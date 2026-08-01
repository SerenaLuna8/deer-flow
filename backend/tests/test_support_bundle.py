"""Tests for scripts/support_bundle.py."""

from __future__ import annotations

import json
import sys
import zipfile

import pytest
import support_bundle


def test_collect_environment_routes_pnpm_through_shared_runner(
    tmp_path,
    monkeypatch,
):
    calls = []

    def fake_version_command(name, args, cwd):
        calls.append((name, args, cwd))
        return {"name": name, "ok": True, "stdout": "version", "stderr": ""}

    monkeypatch.setattr(support_bundle, "_version_command", fake_version_command)

    support_bundle.collect_environment(tmp_path)

    assert [call for call in calls if call[0] == "pnpm"] == [
        (
            "pnpm",
            [sys.executable, str(tmp_path / "scripts" / "pnpm.py"), "--version"],
            tmp_path / "frontend",
        )
    ]


def _zip_text(zip_path, name: str) -> str:
    with zipfile.ZipFile(zip_path) as zf:
        return zf.read(name).decode("utf-8")


def _bundle_and_sidecar_text(zip_path) -> str:
    with zipfile.ZipFile(zip_path) as zf:
        archive_text = [zf.read(name).decode("utf-8", errors="replace") for name in zf.namelist()]
    sidecar_text = [
        support_bundle._issue_summary_sidecar_path(zip_path).read_text(encoding="utf-8"),
        support_bundle._issue_draft_sidecar_path(zip_path).read_text(encoding="utf-8"),
    ]
    return "\n".join([*archive_text, *sidecar_text])


def _create_hostile_support_bundle(tmp_path, monkeypatch):
    config_sentinel = "OPAQUE-CONFIG-AUTH-7JX9"
    doctor_sentinel = "OPAQUE-DOCTOR-TAG-8KQ4"
    command_sentinel = "OPAQUE-COMMAND-OUTPUT-3VNM"
    path_sentinel = "OPAQUE-PROJECT-PATH-5WRT"
    project_root = tmp_path / f"project-{path_sentinel}"
    project_root.mkdir()
    (project_root / "config.yaml").write_text(
        f"""
config_version: 29
models:
  - name: default
tools:
  - name: web_search
channels:
  slack:
    enabled: true
guardrails:
  provider:
    use: example:Provider
    config:
      auth: {config_sentinel}
""",
        encoding="utf-8",
    )

    try:
        support_bundle.yaml.safe_load(f"value: !{doctor_sentinel} data\n")
    except Exception as exc:  # noqa: BLE001 - real parser error is the payload
        doctor_output = f"config.yaml load failed: {exc}\nStatus: 1 error(s), 0 warning(s)"
    else:  # pragma: no cover - PyYAML must reject an unknown constructor
        raise AssertionError("unknown YAML tag unexpectedly parsed")

    monkeypatch.setattr(
        support_bundle,
        "collect_environment",
        lambda _project_root: {
            "generated_at": "2026-07-30T00:00:00+00:00",
            "platform": {
                "system": "Darwin",
                "release": "25.5.0",
                "machine": "arm64",
                "python": "3.12.11",
            },
            "commands": [
                {
                    "name": "node",
                    "ok": True,
                    "returncode": 0,
                    "stdout": f"v22.17.0 {command_sentinel}",
                    "stderr": "",
                }
            ],
        },
    )
    monkeypatch.setattr(
        support_bundle,
        "collect_git_summary",
        lambda _project_root: {
            "branch": {
                "ok": True,
                "returncode": 0,
                "stdout": f"feature/{path_sentinel}",
                "stderr": "",
            },
            "head": {
                "ok": True,
                "returncode": 0,
                "stdout": "a" * 40,
                "stderr": "",
            },
            "upstream": {
                "ok": True,
                "returncode": 0,
                "stdout": f"origin/{command_sentinel}",
                "stderr": "",
            },
            "status_short": {
                "ok": True,
                "returncode": 0,
                "stdout": f"## feature/test\n M {path_sentinel}.txt",
                "stderr": "",
            },
            "diff_stat": {
                "ok": True,
                "returncode": 0,
                "stdout": f" {path_sentinel}.txt | 1 +",
                "stderr": "",
            },
        },
    )
    monkeypatch.setattr(
        support_bundle,
        "collect_doctor_output",
        lambda _project_root: {
            "ok": False,
            "returncode": 1,
            "stdout": doctor_output,
            "stderr": "",
        },
    )

    output_path = tmp_path / "support.zip"
    support_bundle.create_support_bundle(
        project_root=project_root,
        out_path=output_path,
        include_doctor=True,
    )
    return output_path, {
        config_sentinel,
        doctor_sentinel,
        command_sentinel,
        path_sentinel,
    }


def test_redact_data_recursively_masks_secret_like_keys():
    data = {
        "models": [
            {
                "name": "default",
                "api_key": "sk-live-secret",
                "nested": {
                    "client_secret": "client-secret-value",
                    "safe": "visible",
                },
            }
        ],
        "headers": {
            "Authorization": "Bearer header-secret",
        },
        "plain": "kept",
    }

    redacted = support_bundle.redact_data(data)

    assert redacted["models"][0]["api_key"] == "<redacted>"
    assert redacted["models"][0]["nested"]["client_secret"] == "<redacted>"
    assert redacted["models"][0]["nested"]["safe"] == "visible"
    assert redacted["headers"]["Authorization"] == "<redacted>"
    assert redacted["plain"] == "kept"


def test_redact_data_masks_url_credentials_and_cli_flag_secrets():
    data = {
        "models": [
            {"name": "m", "base_url": "https://admin:S3cr3tPass@proxy.internal/v1"},
            {"name": "n", "endpoint": "https://host/v1?access_token=AKIA1234567890ABCD"},
            {"name": "h", "default_headers": {"X-My-Auth": "rawsecrettoken123"}},
        ],
        "database_url": "postgres://dfuser:dfpass@db:5432/deer",
        "mcpServers": {
            "svc": {"command": "npx", "args": ["-y", "server", "--api-key", "LIVE-MCP-SECRET-XYZ"]},
        },
    }

    redacted = support_bundle.redact_data(data)

    assert redacted["models"][0]["base_url"] == "https://<redacted>@proxy.internal/v1"
    assert "AKIA1234567890ABCD" not in redacted["models"][1]["endpoint"]
    assert redacted["models"][1]["endpoint"].endswith("access_token=<redacted>")
    assert redacted["models"][2]["default_headers"]["X-My-Auth"] == "<redacted>"
    assert "dfpass" not in redacted["database_url"]
    assert redacted["database_url"] == "postgres://<redacted>@db:5432/deer"
    args = redacted["mcpServers"]["svc"]["args"]
    assert args[:3] == ["-y", "server", "--api-key"]
    assert args[3] == "<redacted>"


def test_redact_data_masks_inline_and_credential_only_url_secrets():
    data = {
        "mcpServers": {
            "svc": {"command": "npx", "args": ["server", "--api-key=LIVE-COMBINED-SECRET"]},
        },
        "cache_url": "redis://:SuperSecretPass@cache:6379/0",
    }

    redacted = support_bundle.redact_data(data)

    assert "LIVE-COMBINED-SECRET" not in json.dumps(redacted)
    assert redacted["mcpServers"]["svc"]["args"][1] == "--api-key=<redacted>"
    assert "SuperSecretPass" not in redacted["cache_url"]
    assert redacted["cache_url"] == "redis://<redacted>@cache:6379/0"


@pytest.mark.parametrize("reader_name", ["_read_yaml", "_read_json"])
def test_config_reader_errors_do_not_expose_exception_secrets(tmp_path, monkeypatch, reader_name):
    sentinel = "SUPPORT-BUNDLE-READER-SECRET"
    config_path = tmp_path / ("config.yaml" if reader_name == "_read_yaml" else "config.json")
    config_path.write_text("present: true", encoding="utf-8")

    if reader_name == "_read_yaml":
        monkeypatch.setattr(
            support_bundle.yaml,
            "safe_load",
            lambda _value: (_ for _ in ()).throw(RuntimeError(f"Authorization: Bearer {sentinel}")),
        )
    else:
        monkeypatch.setattr(
            support_bundle.json,
            "loads",
            lambda _value: (_ for _ in ()).throw(RuntimeError(f"password={sentinel}")),
        )

    result = getattr(support_bundle, reader_name)(config_path)

    assert sentinel not in json.dumps(result)
    expected_error = "yaml_parse_failed" if reader_name == "_read_yaml" else "json_parse_failed"
    assert result == {"present": True, "error": expected_error}


def test_redact_text_masks_url_userinfo_and_query_secrets():
    text = "\n".join(
        [
            "base_url: https://admin:S3cr3tPass@proxy.internal/v1",
            "postgres://dfuser:dfpass@db:5432/deer",
            "endpoint: https://host/v1?api_key=LIVE-QUERY-SECRET&model=gpt-4o",
        ]
    )

    redacted = support_bundle.redact_text(text)

    assert "S3cr3tPass" not in redacted
    assert "dfpass" not in redacted
    assert "LIVE-QUERY-SECRET" not in redacted
    assert "https://<redacted>@proxy.internal/v1" in redacted
    assert "model=gpt-4o" in redacted


def test_redact_keeps_non_secret_flags_visible():
    redacted = support_bundle.redact_data(["--model", "gpt-4o", "--verbose"])
    assert redacted == ["--model", "gpt-4o", "--verbose"]


def test_redact_text_masks_env_assignments_and_bearer_tokens():
    text = "\n".join(
        [
            "OPENAI_API_KEY=sk-live-secret",
            "Authorization: Bearer abc.def.ghi",
            "client_secret: very-secret",
            "normal=value",
        ]
    )

    redacted = support_bundle.redact_text(text)

    assert "sk-live-secret" not in redacted
    assert "abc.def.ghi" not in redacted
    assert "very-secret" not in redacted
    assert "OPENAI_API_KEY=<redacted>" in redacted
    assert "Authorization: Bearer <redacted>" in redacted
    assert "normal=value" in redacted


def test_redact_text_masks_home_directory_paths():
    text = "\n".join(
        [
            "/Users/alice/deer-flow/config.yaml",
            "/home/bob/deer-flow/config.yaml",
            r"C:\Users\carol\deer-flow\config.yaml",
        ]
    )

    redacted = support_bundle.redact_text(text)

    assert "alice" not in redacted
    assert "bob" not in redacted
    assert "carol" not in redacted
    assert "/Users/<user>/deer-flow/config.yaml" in redacted
    assert "/home/<user>/deer-flow/config.yaml" in redacted
    assert r"C:\Users\<user>\deer-flow\config.yaml" in redacted


def test_redact_data_masks_non_keyword_env_secrets_but_keeps_var_references():
    data = {
        "mcpServers": {
            "supabase": {
                "command": "npx",
                "env": {
                    "SUPABASE_SERVICE_ROLE_KEY": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.payload.sig",
                    "R2_ACCESS_KEY": "0123456789abcdef0123456789abcdef",
                    "GEMINI_KEY": "AIzaSyA-EXAMPLE-hardcoded-google-key",
                    "PROJECT_REF": "$SUPABASE_PROJECT_REF",
                    "REGION": "${AWS_REGION}",
                },
            }
        }
    }

    redacted = support_bundle.redact_data(data)
    env = redacted["mcpServers"]["supabase"]["env"]

    assert env["SUPABASE_SERVICE_ROLE_KEY"] == "<redacted>"
    assert env["R2_ACCESS_KEY"] == "<redacted>"
    assert env["GEMINI_KEY"] == "<redacted>"
    assert env["PROJECT_REF"] == "$SUPABASE_PROJECT_REF"
    assert env["REGION"] == "${AWS_REGION}"

    dumped = json.dumps(redacted)
    assert "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9" not in dumped
    assert "0123456789abcdef" not in dumped
    assert "AIzaSyA-EXAMPLE-hardcoded-google-key" not in dumped


def test_redact_data_masks_broadened_secret_key_names():
    data = {
        "aws_access_key_id": "AKIAIOSFODNN7EXAMPLE",
        "db_pwd": "hunter2",
        "signing_private_key": "-----BEGIN KEY-----abc-----END KEY-----",
    }

    redacted = support_bundle.redact_data(data)

    assert redacted["aws_access_key_id"] == "<redacted>"
    assert redacted["db_pwd"] == "<redacted>"
    assert redacted["signing_private_key"] == "<redacted>"


@pytest.mark.parametrize(
    "key",
    [
        "db_pass",
        "encryption_key",
        "redis_pass",
        "webhook_signing_key",
        "SUPABASE_SERVICE_ROLE_KEY",
        "gh_pat",
        "passphrase",
        "passcode",
        "dsn",
    ],
)
def test_redact_data_masks_open_ended_secret_key_names(key):
    secret = f"must-not-leak-{key}"

    redacted = support_bundle.redact_data({"provider": {"config": {key: secret}}})

    assert redacted["provider"]["config"][key] == "<redacted>"
    assert secret not in json.dumps(redacted)


def test_run_command_redacts_secret_from_unexpected_exception(tmp_path, monkeypatch):
    secret = "command-exception-secret-value"

    def raise_secret_exception(*_args, **_kwargs):
        raise RuntimeError(f"provider failed with Authorization: Bearer {secret}")

    monkeypatch.setattr(support_bundle.subprocess, "run", raise_secret_exception)

    result = support_bundle._run_command(["provider-cli"], cwd=tmp_path)

    assert secret not in json.dumps(result)


def test_file_manifest_redacts_secret_from_stat_exception(tmp_path, monkeypatch):
    secret = "file-exception-secret-value"
    root = tmp_path / "workspace"
    root.mkdir()
    affected_file = root / "report.txt"
    affected_file.write_text("content is never read", encoding="utf-8")
    path_type = type(affected_file)
    original_is_file = path_type.is_file
    original_stat = path_type.stat

    def is_file(path):
        if path == affected_file:
            return True
        return original_is_file(path)

    def stat(path, *args, **kwargs):
        if path == affected_file:
            raise OSError(f"stat failed with Authorization: Bearer {secret}")
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(path_type, "is_file", is_file)
    monkeypatch.setattr(path_type, "stat", stat)

    manifest = support_bundle._file_manifest(root)

    assert secret not in json.dumps(manifest)


def test_create_support_bundle_masks_hardcoded_env_secret(tmp_path):
    project_root = tmp_path / "project"
    project_root.mkdir()
    (project_root / "config.yaml").write_text(
        """config_version: 5
models:
  - name: default
tools:
  - name: supabase
    env:
      SUPABASE_SERVICE_ROLE_KEY: eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.leak.sig
      R2_ACCESS_KEY: 0123456789abcdef0123456789abcdef
      PROJECT_REF: $SUPABASE_PROJECT_REF
""",
        encoding="utf-8",
    )

    output_path = tmp_path / "support.zip"
    support_bundle.create_support_bundle(
        project_root=project_root,
        out_path=output_path,
        include_doctor=False,
    )

    all_text = "\n".join(_zip_text(output_path, name) for name in zipfile.ZipFile(output_path).namelist())
    assert "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.leak.sig" not in all_text
    assert "0123456789abcdef" not in all_text

    config_summary = json.loads(_zip_text(output_path, "config-summary.json"))
    assert config_summary["tools"] == 1
    assert set(config_summary) == {
        "present",
        "config_version",
        "error",
        "tools",
        "channels",
    }


def test_create_support_bundle_writes_sanitized_zip(tmp_path):
    project_root = tmp_path / "project"
    project_root.mkdir()
    (project_root / "config.yaml").write_text(
        """
config_version: 5
models:
  - name: default
    use: langchain_openai:ChatOpenAI
    model: gpt-4o
    api_key: sk-live-secret
tools:
  - name: web_search
    use: deerflow.community.brave.tools:web_search_tool
    api_key: brave-secret
channels:
  slack:
    enabled: true
    bot_token: xoxb-secret
""",
        encoding="utf-8",
    )
    output_path = tmp_path / "support.zip"
    bundle_path = support_bundle.create_support_bundle(
        project_root=project_root,
        out_path=output_path,
        thread_id=None,
        include_doctor=False,
    )

    assert bundle_path == output_path
    with zipfile.ZipFile(bundle_path) as zf:
        names = set(zf.namelist())

    assert {
        "manifest.json",
        "environment.json",
        "config-summary.json",
        "git.json",
    }.issubset(names)

    all_text = "\n".join(_zip_text(bundle_path, name) for name in names if name.endswith(".json"))
    assert "sk-live-secret" not in all_text
    assert "brave-secret" not in all_text
    assert "xoxb-secret" not in all_text

    config_summary = json.loads(_zip_text(bundle_path, "config-summary.json"))
    assert "models" not in config_summary
    assert config_summary["tools"] == 1
    assert config_summary["channels"] == 1

    triage = json.loads(_zip_text(bundle_path, "triage.json"))
    assert "models_missing" not in triage["signals"]
    assert "models_missing" not in triage["active_signals"]


def test_create_support_bundle_writes_ai_triage_entrypoints(tmp_path, monkeypatch):
    project_root = tmp_path / "project"
    project_root.mkdir()

    monkeypatch.setattr(
        support_bundle,
        "collect_environment",
        lambda _project_root: {
            "platform": {
                "system": "Darwin",
                "release": "25.5.0",
                "machine": "arm64",
                "python": "3.12.11",
            },
            "commands": [
                {"name": "node", "ok": True, "stdout": "v20.19.5", "stderr": ""},
                {"name": "pnpm", "ok": True, "stdout": "11.7.0", "stderr": ""},
                {"name": "uv", "ok": True, "stdout": "uv 0.8.11", "stderr": ""},
                {"name": "nginx", "ok": True, "stdout": "", "stderr": "nginx version: nginx/1.31.1"},
                {"name": "docker", "ok": False, "error": "docker not found"},
            ],
        },
    )
    monkeypatch.setattr(
        support_bundle,
        "collect_git_summary",
        lambda _project_root: {
            "branch": {"ok": True, "stdout": "feat/community-support-bundle", "stderr": ""},
            "head": {"ok": True, "stdout": "abc123", "stderr": ""},
            "upstream": {"ok": True, "stdout": "origin/main", "stderr": ""},
            "status_short": {"ok": True, "stdout": "## feat/community-support-bundle...origin/main\n M README.md", "stderr": ""},
            "diff_stat": {"ok": True, "stdout": " README.md | 1 +", "stderr": ""},
        },
    )
    monkeypatch.setattr(
        support_bundle,
        "collect_doctor_output",
        lambda _project_root: {
            "ok": False,
            "returncode": 1,
            "stdout": "\n".join(
                [
                    "DeerFlow Health Check",
                    "  ✗ Node.js  (v20.19.5)",
                    "      → Node.js 22+ required. Install from https://nodejs.org/",
                    "  ✗ config.yaml found",
                    "      → Run 'make setup' to create it",
                    "Status: 2 error(s), 2 warning(s)",
                ]
            ),
            "stderr": "",
        },
    )

    output_path = tmp_path / "support.zip"
    support_bundle.create_support_bundle(
        project_root=project_root,
        out_path=output_path,
        include_doctor=True,
    )

    with zipfile.ZipFile(output_path) as zf:
        names = set(zf.namelist())

    assert {"README.md", "issue-summary.md", "ai-issue-draft.md", "triage.json"}.issubset(names)

    triage = json.loads(_zip_text(output_path, "triage.json"))
    assert triage["schema_version"] == 1
    assert triage["status"] == "needs_user_setup"
    assert triage["signals"]["config_missing"] is True
    assert triage["signals"]["node_version_too_old"] is True
    assert triage["signals"]["doctor_failed"] is True
    assert triage["signals"]["dirty_worktree"] is True
    assert "doctor_included" not in triage["active_signals"]
    assert triage["versions"]["python"] == "3.12.11"
    assert triage["versions"]["node"] == "v20.19.5"
    assert triage["doctor"]["errors"] == 2
    assert "Run `make setup`" in triage["reporter_next_steps"][0]
    assert any("Node.js 22+" in step for step in triage["reporter_next_steps"])
    evidence_paths = [item["path"] for item in triage["evidence_files"]]
    assert "issue-summary.md" in evidence_paths
    assert "ai-issue-draft.md" in evidence_paths

    issue_summary = _zip_text(output_path, "issue-summary.md")
    assert "Triage status: needs_user_setup" in issue_summary
    assert "config_missing" in issue_summary
    assert "node_version_too_old" in issue_summary
    assert "python=3.12.11" in issue_summary
    assert "Reporter next steps" in issue_summary
    assert "Run `make setup`" in issue_summary
    assert "Attach the zip if a maintainer asks" in issue_summary
    assert "Ask the reporter to complete local setup" in issue_summary

    sidecar_summary = tmp_path / "support-issue-summary.md"
    assert sidecar_summary.exists()
    assert sidecar_summary.read_text(encoding="utf-8") == issue_summary

    issue_draft = _zip_text(output_path, "ai-issue-draft.md")
    assert "AI issue draft" in issue_draft
    assert "Do not invent if unknown" in issue_draft
    assert "Do not file this issue until every REQUIRED placeholder is replaced" in issue_draft
    assert "Issue title" in issue_draft
    assert "[bug] <REQUIRED: one-line problem summary>" in issue_draft
    assert "### Problem summary" in issue_draft
    assert "### Affected area(s)" in issue_draft
    assert "Config / setup (make, config.yaml, env)" in issue_draft
    assert "### What happened?" in issue_draft
    assert "### Expected behavior" in issue_draft
    assert "### Steps to reproduce" in issue_draft
    assert "### Relevant logs" in issue_draft
    assert "DeerFlow Health Check" not in issue_draft
    assert "<REQUIRED: paste key log lines." in issue_draft
    assert "### How are you running DeerFlow?" in issue_draft
    assert "<REQUIRED: choose Local, Docker, CI, or Other>" in issue_draft
    assert "### Operating system" in issue_draft
    assert "macOS" in issue_draft
    assert "### Platform details" in issue_draft
    assert "arm64" in issue_draft
    assert "### Python version" in issue_draft
    assert "3.12.11" in issue_draft
    assert "### Node.js version" in issue_draft
    assert "v20.19.5" in issue_draft
    assert "### Git state" in issue_draft
    assert "branch: unknown" in issue_draft
    assert "commit: unknown" in issue_draft
    assert "feat/community-support-bundle" not in issue_draft
    assert "### Support bundle summary" in issue_draft
    assert "Triage status: needs_user_setup" in issue_draft
    assert "Attach the zip only if a maintainer asks" in issue_draft

    sidecar_draft = tmp_path / "support-issue-draft.md"
    assert sidecar_draft.exists()
    assert sidecar_draft.read_text(encoding="utf-8") == issue_draft

    bundle_readme = _zip_text(output_path, "README.md")
    assert "Start here" in bundle_readme
    assert "ai-issue-draft.md" in bundle_readme
    assert "Attach the zip if a maintainer asks" in bundle_readme


def test_triage_flags_config_parse_errors(tmp_path):
    project_root = tmp_path / "project"
    project_root.mkdir()
    (project_root / "config.yaml").write_text("models: [", encoding="utf-8")

    output_path = tmp_path / "support.zip"
    support_bundle.create_support_bundle(
        project_root=project_root,
        out_path=output_path,
        include_doctor=False,
    )

    triage = json.loads(_zip_text(output_path, "triage.json"))
    assert triage["status"] == "needs_user_setup"
    assert triage["signals"]["config_error"] is True
    assert "config_error" in triage["active_signals"]


def test_support_bundle_ignores_removed_extensions_file(tmp_path):
    project_root = tmp_path / "project"
    project_root.mkdir()
    (project_root / "config.yaml").write_text(
        "config_version: 5\nmodels:\n  - name: default\n",
        encoding="utf-8",
    )
    (project_root / "extensions_config.json").write_text("{ broken", encoding="utf-8")

    output_path = tmp_path / "support.zip"
    support_bundle.create_support_bundle(
        project_root=project_root,
        out_path=output_path,
        include_doctor=False,
    )

    triage = json.loads(_zip_text(output_path, "triage.json"))
    assert "extensions_config_error" not in triage["signals"]
    assert "extensions_config_missing" not in triage["signals"]
    with zipfile.ZipFile(output_path) as zf:
        assert "extensions-summary.json" not in zf.namelist()


def test_support_bundle_never_embeds_opaque_yaml_parse_error_content(tmp_path):
    project_root = tmp_path / "project"
    project_root.mkdir()
    sentinel = "ZXQ7OPAQUECRED987"
    (project_root / "config.yaml").write_text(
        f"value: !{sentinel} data\n",
        encoding="utf-8",
    )

    output_path = tmp_path / "support.zip"
    support_bundle.create_support_bundle(
        project_root=project_root,
        out_path=output_path,
        include_doctor=False,
    )

    with zipfile.ZipFile(output_path) as zf:
        archive_text = "\n".join(zf.read(name).decode("utf-8", errors="replace") for name in zf.namelist())
    assert sentinel not in archive_text
    config_summary = json.loads(_zip_text(output_path, "config-summary.json"))
    assert config_summary["error"] == "yaml_parse_failed"


def test_support_bundle_excludes_opaque_values_from_zip_and_both_sidecars(
    tmp_path,
    monkeypatch,
):
    output_path, sentinels = _create_hostile_support_bundle(
        tmp_path,
        monkeypatch,
    )

    exported_text = _bundle_and_sidecar_text(output_path)

    assert all(sentinel not in exported_text for sentinel in sentinels)


def test_support_bundle_evidence_json_contains_only_closed_summaries(
    tmp_path,
    monkeypatch,
):
    output_path, _sentinels = _create_hostile_support_bundle(
        tmp_path,
        monkeypatch,
    )

    triage = json.loads(_zip_text(output_path, "triage.json"))
    config_summary = json.loads(_zip_text(output_path, "config-summary.json"))
    doctor_summary = json.loads(_zip_text(output_path, "doctor.json"))
    git_summary = json.loads(_zip_text(output_path, "git.json"))
    environment_summary = json.loads(_zip_text(output_path, "environment.json"))

    assert config_summary == triage["config"]
    assert doctor_summary == triage["doctor"]
    assert git_summary == triage["git"]
    assert environment_summary == {
        "platform": triage["platform"],
        "versions": triage["versions"],
    }


@pytest.mark.parametrize(
    ("config_version", "error"),
    [
        (
            "OPAQUE-CONFIG-VERSION-4QZT",
            {"detail": "OPAQUE-CONFIG-ERROR-7NXM"},
        ),
        (
            {"detail": "OPAQUE-CONFIG-VERSION-4QZT"},
            "OPAQUE-CONFIG-ERROR-7NXM",
        ),
    ],
)
def test_support_bundle_closed_config_fast_path_rejects_opaque_version_and_error(
    tmp_path,
    config_version,
    error,
):
    project_root = tmp_path / "project"
    project_root.mkdir()
    (project_root / "config.yaml").write_text(
        json.dumps(
            {
                "present": True,
                "config_version": config_version,
                "error": error,
                "models": 1,
                "tools": 2,
                "channels": 3,
            }
        ),
        encoding="utf-8",
    )

    output_path = tmp_path / "support.zip"
    support_bundle.create_support_bundle(
        project_root=project_root,
        out_path=output_path,
        include_doctor=False,
    )

    exported_text = _bundle_and_sidecar_text(output_path)
    assert "OPAQUE-CONFIG-VERSION-4QZT" not in exported_text
    assert "OPAQUE-CONFIG-ERROR-7NXM" not in exported_text

    config_summary = json.loads(_zip_text(output_path, "config-summary.json"))
    triage = json.loads(_zip_text(output_path, "triage.json"))
    assert config_summary == {
        "present": True,
        "config_version": None,
        "error": None,
        "tools": 2,
        "channels": 3,
    }
    assert triage["config"] == config_summary


def test_thread_summary_lists_files_without_file_contents(tmp_path):
    project_root = tmp_path / "project"
    outputs = project_root / ".deer-flow" / "threads" / "thread-123" / "user-data" / "outputs"
    uploads = project_root / ".deer-flow" / "threads" / "thread-123" / "user-data" / "uploads"
    outputs.mkdir(parents=True)
    uploads.mkdir(parents=True)
    (outputs / "report.md").write_text("raw report content with secret-content", encoding="utf-8")
    (outputs / "report-sk-live-secret.txt").write_text("filename token", encoding="utf-8")
    (uploads / "input.csv").write_text("name,value\nsecret,1\n", encoding="utf-8")

    output_manifest = support_bundle._file_manifest(outputs)
    upload_manifest = support_bundle._file_manifest(uploads)
    output_names = [item["path"] for item in output_manifest]
    upload_names = [item["path"] for item in upload_manifest]

    assert "report.md" in output_names
    assert "input.csv" in upload_names

    all_text = json.dumps({"outputs": output_manifest, "uploads": upload_manifest})
    assert "secret-content" not in all_text
    assert "name,value" not in all_text
    assert "sk-live-secret" not in all_text
    assert "report-sk-<redacted>.txt" in all_text


@pytest.mark.parametrize(
    "legacy_owners",
    [(), ("owner-a", "owner-b")],
    ids=["no-legacy-data", "cross-user-collision"],
)
def test_support_bundle_rejects_thread_id_without_trusted_project_owner_scope(tmp_path, legacy_owners):
    project_root = tmp_path / "project"
    project_root.mkdir()
    for owner in legacy_owners:
        outputs = project_root / ".deer-flow" / "users" / owner / "threads" / "shared-thread" / "user-data" / "outputs"
        outputs.mkdir(parents=True)
        (outputs / f"{owner}.txt").write_text(f"private data for {owner}", encoding="utf-8")

    output_path = tmp_path / "support.zip"
    with pytest.raises(ValueError, match=r"(?i)(project.*owner|unscoped|unsupported|disabled)"):
        support_bundle.create_support_bundle(
            project_root=project_root,
            out_path=output_path,
            thread_id="shared-thread",
            include_doctor=False,
        )

    assert not output_path.exists()


def test_support_bundle_rejects_empty_thread_id_instead_of_silently_ignoring_it(
    tmp_path,
):
    project_root = tmp_path / "project"
    project_root.mkdir()
    output_path = tmp_path / "support.zip"

    with pytest.raises(
        ValueError,
        match=r"(?i)(thread|unscoped|unsupported|disabled)",
    ):
        support_bundle.create_support_bundle(
            project_root=project_root,
            out_path=output_path,
            thread_id="",
            include_doctor=False,
        )

    assert not output_path.exists()


def test_collect_thread_summary_rejects_unscoped_lookup(tmp_path):
    project_root = tmp_path / "project"
    project_root.mkdir()

    with pytest.raises(ValueError, match=r"(?i)project.*owner"):
        support_bundle.collect_thread_summary(
            project_root,
            "missing-thread",
        )


def test_thread_summary_rejects_path_like_thread_id(tmp_path):
    project_root = tmp_path / "project"
    project_root.mkdir()

    with pytest.raises(ValueError, match="Invalid thread_id"):
        support_bundle.collect_thread_summary(project_root, "../outside")


@pytest.mark.parametrize("thread_id", ["..", ".", "...", "a..b", "....", "..%2f"])
def test_validate_thread_id_rejects_dot_traversal(thread_id):
    with pytest.raises(ValueError, match="Invalid thread_id"):
        support_bundle._validate_thread_id(thread_id)


def test_validate_thread_id_accepts_safe_ids():
    support_bundle._validate_thread_id("thread-123")
    support_bundle._validate_thread_id("a.b_c-1")


def test_main_reports_invalid_thread_id_without_traceback(tmp_path, capsys):
    project_root = tmp_path / "project"
    project_root.mkdir()

    exit_code = support_bundle.main(
        [
            "--project-root",
            str(project_root),
            "--out",
            str(tmp_path / "support.zip"),
            "--thread-id",
            "../outside",
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 2
    assert "Invalid thread_id" in captured.err
    assert "Traceback" not in captured.err


def test_main_does_not_echo_secret_bearing_invalid_thread_id(tmp_path, capsys):
    project_root = tmp_path / "project"
    project_root.mkdir()
    sentinel = "SUPPORT-BUNDLE-THREAD-SECRET"

    exit_code = support_bundle.main(
        [
            "--project-root",
            str(project_root),
            "--out",
            str(tmp_path / "support.zip"),
            "--thread-id",
            f"password={sentinel}",
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 2
    assert "Invalid thread_id" in captured.err
    assert sentinel not in captured.err


def test_main_prints_reporter_next_steps_and_optional_upload(tmp_path, capsys):
    project_root = tmp_path / "project"
    project_root.mkdir()

    exit_code = support_bundle.main(
        [
            "--project-root",
            str(project_root),
            "--out",
            str(tmp_path / "support.zip"),
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "Issue summary:" in captured.out
    assert "Issue draft:" in captured.out
    assert "Suggested next steps:" in captured.out
    assert "If an AI assistant files the issue, start from the issue draft" in captured.out
    assert "Attach the zip if a maintainer asks" in captured.out
