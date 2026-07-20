# ruff: noqa: E402

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Sequence
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from scripts.release_acceptance.commands import diagnostic_stages
from scripts.release_acceptance.models import ReleaseEvidence
from scripts.release_acceptance.preflight import PreflightFailure
from scripts.release_acceptance.runner import DiagnosticResult, ReleaseRunner, run_host_diagnostic


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the fixed M8 host release acceptance manifest")
    parser.add_argument(
        "--stage",
        action="append",
        choices=("host_setup", "chromium", "deepseek", "recovery"),
        help="Run a fixed non-sealing diagnostic stage prefix",
    )
    args = parser.parse_args(argv)
    if args.stage:
        try:
            diagnostic_stages(tuple(args.stage))
        except ValueError:
            parser.error("diagnostic stages must be the fixed host_setup[/chromium[/deepseek[/recovery]]] prefix")
    return args


async def _run(args: argparse.Namespace) -> int:
    repository = Path(__file__).resolve().parents[2]
    if args.stage:
        result = await run_host_diagnostic(
            repository=repository,
            stages=diagnostic_stages(tuple(args.stage)),
        )
    else:
        result = await ReleaseRunner.default(repository).run()
    if isinstance(result, PreflightFailure):
        print(json.dumps({"status": "failed", "code": result.code}, sort_keys=True))
        return 1
    if isinstance(result, DiagnosticResult):
        print(
            json.dumps(
                {
                    "status": result.status,
                    "code": result.code,
                    "host_setup_passed": result.host_setup_passed,
                    "chromium_passed": result.chromium.passed if result.chromium else 0,
                    "deepseek": (result.deepseek.model_dump() if result.deepseek is not None else None),
                    "recovery": (result.recovery.model_dump() if result.recovery is not None else None),
                    "cleanup": result.cleanup.model_dump(),
                    "sealed": False,
                },
                sort_keys=True,
            )
        )
        return 0 if result.status == "passed" else 1
    assert isinstance(result, ReleaseEvidence)
    print(
        json.dumps(
            {
                "status": result.status.value,
                "acceptance_run_id": str(result.acceptance_run_id),
                "candidate_evidence_digest": result.candidate_evidence_digest,
            },
            sort_keys=True,
        )
    )
    return 0 if result.status.value != "failed" else 1


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
