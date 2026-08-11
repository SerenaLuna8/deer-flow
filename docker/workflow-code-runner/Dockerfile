# The multi-platform manifest digest is intentionally pinned.  Updating it is
# a reviewed runtime-profile migration, not an operator configuration change.
FROM python:3.12.11-alpine3.22@sha256:efcdfa6a6b2fd2afb9c7dfa9a5b288a6f68338b5cfdebe6b637d986067d85757

ARG RUNNER_DIGEST
LABEL org.opencontainers.image.title="ActWeave Workflow Python Code Runner" \
      org.actweave.workflow-code.contract="python3.12-v1" \
      org.actweave.workflow-code.runner-digest="${RUNNER_DIGEST}"

RUN addgroup -S -g 65532 workflow-code \
    && adduser -S -D -H -u 65532 -G workflow-code workflow-code \
    && mkdir -p /opt/actweave \
    && chown -R 65532:65532 /opt/actweave

COPY --chown=65532:65532 runner.py /opt/actweave/runner.py

USER 65532:65532
WORKDIR /tmp

# ``env -i`` is part of the isolation contract: it execs Python as PID 1 with
# an actually empty process environment (including /proc/self/environ).  The
# absolute interpreter path means no PATH variable is required.
ENTRYPOINT ["/usr/bin/env", "-i", "/usr/local/bin/python3.12", "-I", "-S", "-B", "/opt/actweave/runner.py"]
