# Blocking IO static detection

ActWeave keeps one repository-level static detector for finding synchronous IO
inside backend async paths. It is a review aid, not a proof that every runtime
path is non-blocking.

Run it from the repository root or from `backend/`:

```bash
make detect-blocking-io
```

The report is written to:

```text
.deer-flow/blocking-io-findings.json
```

Each finding is a candidate for human review. Confirm that the call is actually
reachable from an event-loop path before changing production code. Production
async paths should offload synchronous filesystem, subprocess, and network work,
then await cancellation-settled cleanup where applicable.

The detector implementation lives under
`scripts/detectors/blocking_io_static.py`. Add a static rule only for a recurring
high-risk pattern that existing rules cannot see.
