from __future__ import annotations

import argparse
import asyncio
import json
from collections.abc import Sequence
from pathlib import Path

from scripts.release_acceptance.models import ReleaseEvidence
from scripts.release_acceptance.preflight import PreflightFailure
from scripts.release_acceptance.runner import ReleaseRunner


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the fixed M8 host release acceptance manifest")
    return parser.parse_args(argv)


async def _run() -> int:
    repository = Path(__file__).resolve().parents[2]
    result = await ReleaseRunner.default(repository).run()
    if isinstance(result, PreflightFailure):
        print(json.dumps({"status": "failed", "code": result.code}, sort_keys=True))
        return 1
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
    _parse_args(argv)
    return asyncio.run(_run())


if __name__ == "__main__":
    raise SystemExit(main())
