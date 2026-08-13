"""Dependency and compatibility gates for the neutral Memory contract."""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

from deerflow import memory_contract
from deerflow.persistence.private_work import memory_document_repository, memory_dream_prepare_repository


def test_memory_contract_import_does_not_load_runtime_or_persistence_stacks() -> None:
    script = """
import sys
import deerflow.memory_contract
for prefix in ('app', 'sqlalchemy', 'langchain', 'langgraph', 'deerflow.agents', 'deerflow.runtime'):
    if any(name == prefix or name.startswith(prefix + '.') for name in sys.modules):
        raise SystemExit(prefix)
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr or result.stdout


def test_memory_contract_sources_have_only_neutral_dependencies() -> None:
    root = Path(memory_contract.__file__).resolve().parent
    forbidden = (
        "app",
        "sqlalchemy",
        "langchain",
        "langgraph",
        "deerflow.agents",
        "deerflow.persistence",
        "deerflow.runtime",
    )
    for source in root.glob("*.py"):
        tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
        imports = [node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)]
        imports.extend(alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names)
        assert not any(module == prefix or module.startswith(prefix + ".") for module in imports for prefix in forbidden), source


def test_legacy_facade_reexports_the_neutral_contract_identities() -> None:
    declared_reexports = set(memory_document_repository.__all__) & set(memory_contract.__all__)
    required_compatibility_exports = {
        "DEFAULT_EPISODE_RETENTION_DAYS",
        "EPISODE_SEARCH_TAGS",
        "EPISODE_SIMILARITY_FLOOR",
        "MAX_EPISODE_QUERY_CHARS",
        "MAX_REMEMBER_CONTENT_CHARS",
        "REMEMBER_BACKLOG_LIMIT",
        "REMEMBER_PROMPT_VERSION",
        "REMEMBER_RUN_LIMIT",
        "MemoryDreamAdmissionDisposition",
        "MemoryDreamAdmissionKind",
        "MemoryDreamReleaseDisposition",
        "MemoryDreamTrigger",
        "MemoryHistoryActivationStatus",
        "MemoryProposalDisposition",
        "validate_episode_retention_days",
    }
    for name in declared_reexports | required_compatibility_exports:
        assert getattr(memory_document_repository, name) is getattr(
            memory_contract,
            name,
        )


def test_prepare_repository_reexports_the_neutral_contract_identities() -> None:
    for name in (
        "MemoryDreamPrepareAdmission",
        "MemoryDreamPrepareAdmissionDisposition",
        "MemoryDreamPrepareConflict",
        "MemoryDreamPrepareNotFound",
        "MemoryDreamPreparePhase",
        "MemoryDreamPrepareRecord",
        "MemoryDreamPrepareResultDisposition",
        "memory_dream_prepare_idempotency_key",
    ):
        assert getattr(memory_dream_prepare_repository, name) is getattr(
            memory_contract,
            name,
        )


def test_memory_repository_modules_have_one_way_dependencies() -> None:
    repository_root = Path(memory_document_repository.__file__).parent
    sources = (
        *repository_root.glob("memory_*repository.py"),
        *repository_root.glob("memory_*store.py"),
        repository_root / "memory_repository_parts.py",
    )
    for source in sources:
        tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
        imports = [node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)]
        imports.extend(alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names)
        assert not any(module == "deerflow.agents" or module.startswith("deerflow.agents.") for module in imports), source
        if source.name != "memory_document_repository.py":
            assert not any(module == "deerflow.persistence.private_work.memory_document_repository" for module in imports), source


def test_memory_app_services_use_neutral_contract_for_pure_memory_symbols() -> None:
    app_root = Path(__file__).resolve().parents[1] / "app"
    for relative in (
        "private_work/memory_service.py",
        "private_work/memory_authority.py",
        "private_work/memory_dream_service.py",
        "private_work/memory_dream_prepare_service.py",
        "private_work/snapshot_repository.py",
    ):
        source = app_root / relative
        tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
        imports = [node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)]
        assert not any(module == "deerflow.agents.memory.dream" for module in imports), source

    worker_source = app_root / "worker/memory_dream.py"
    worker_tree = ast.parse(
        worker_source.read_text(encoding="utf-8"),
        filename=str(worker_source),
    )
    forbidden_agent_names = {
        "DREAM_PROMPT_VERSION",
        "MemoryDocumentInvalid",
        "validate_memory_document",
    }
    for node in ast.walk(worker_tree):
        if isinstance(node, ast.ImportFrom) and node.module == "deerflow.agents.memory.dream":
            assert forbidden_agent_names.isdisjoint(alias.name for alias in node.names)


def test_memory_repository_facade_import_does_not_load_agent_runtime() -> None:
    script = """
import sys
import deerflow.persistence.private_work.memory_document_repository
for prefix in ('langchain', 'langgraph', 'deerflow.agents'):
    if any(name == prefix or name.startswith(prefix + '.') for name in sys.modules):
        raise SystemExit(prefix)
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr or result.stdout


def test_agent_paths_reexport_neutral_validation_identities() -> None:
    from deerflow.agents.memory import dream, review_policy, snip

    for name in (
        "DREAM_PROMPT_VERSION",
        "MemoryDocumentInvalid",
        "MemoryDocumentOverBudget",
        "estimate_memory_tokens",
        "render_empty_memory_document",
        "validate_memory_document",
        "validate_memory_document_sections",
    ):
        assert getattr(dream, name) is getattr(memory_contract, name)
    for name in (
        "SnipOutputInvalid",
        "compute_snip_content_digest",
        "normalize_snip_output",
        "validate_snip_line",
        "validate_snip_output",
    ):
        assert getattr(snip, name) is getattr(memory_contract, name)
    for name in (
        "memory_document_deletion_ratio",
        "memory_document_needs_review",
    ):
        assert getattr(review_policy, name) is getattr(memory_contract, name)
