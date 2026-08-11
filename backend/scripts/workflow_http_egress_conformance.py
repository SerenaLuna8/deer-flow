#!/usr/bin/env python3
"""Offline Docker conformance for the Workflow HTTP egress profile.

The generated topology has two Docker ``--internal`` networks:

* Worker -> controlled gateway only
* controlled gateway -> pinned TLS target only

The Worker is never attached to the target network and proves that both the
target IP and an Internet IP are unreachable directly.  The gateway resolves
once, compares the entire answer set with its frozen pin, connects that IP with
the approved DNS name as TLS SNI, never redirects, never stores cookies and
never reads ambient proxy variables.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import ssl
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from urllib.parse import quote, urlencode

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))

from app.workflows.http_effect_store import (
    PostgresWorkflowHttpEffectStore,
    WorkflowHttpEffectAlreadyDispatching,
    WorkflowHttpEffectExecutor,
    WorkflowHttpExecutionAuthorityLost,
    WorkflowHttpSideEffectUnknown,
)
from app.workflows.http_effects import (
    WorkflowHttpEffectIdentityV1,
    WorkflowHttpJobExecutionFence,
    derive_workflow_http_idempotency_key,
    derive_workflow_http_operation_key,
    derive_workflow_http_request_fingerprint,
)
from app.workflows.http_execution import (
    HttpxWorkflowControlledEgressClient,
    WorkflowHttpControlledEgressError,
    WorkflowHttpEgressDispatchV1,
)

PYTHON_IMAGE = "nikolaik/python-nodejs:python3.11-nodejs20"
RESOURCE_DIR = Path(__file__).with_name("workflow_http_egress_conformance_resources")
EFFECT_DDL = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "workflow_node_effects_g04.sql"


def _run(arguments: list[str], *, output: bool = False) -> str:
    try:
        completed = subprocess.run(
            arguments,
            check=True,
            text=True,
            capture_output=True,
        )
    except subprocess.CalledProcessError as error:
        detail = (error.stderr or "command failed").strip().splitlines()[-1]
        raise RuntimeError(f"conformance command failed: {detail}") from None
    return completed.stdout.strip() if output else ""


def _openssl(arguments: list[str]) -> None:
    _run(["openssl", *arguments])


def _generate_certificates(directory: Path) -> None:
    for name, common_name, subject_alt_name in (
        (
            "target",
            "target.workflow.test",
            "DNS:target.workflow.test",
        ),
        (
            "gateway",
            "egress.gateway.test",
            "DNS:egress.gateway.test,DNS:localhost,IP:127.0.0.1",
        ),
    ):
        _openssl(
            [
                "req",
                "-x509",
                "-newkey",
                "rsa:2048",
                "-nodes",
                "-subj",
                f"/CN={common_name}",
                "-days",
                "1",
                "-addext",
                f"subjectAltName={subject_alt_name}",
                "-keyout",
                str(directory / f"{name}.key"),
                "-out",
                str(directory / f"{name}.pem"),
            ]
        )
    (directory / "ca.pem").write_bytes((directory / "target.pem").read_bytes() + (directory / "gateway.pem").read_bytes())


def _request(
    path: str,
    *,
    method: str = "GET",
) -> WorkflowHttpEgressDispatchV1:
    write = method not in {"GET", "HEAD"}
    return WorkflowHttpEgressDispatchV1.model_validate_json(
        json.dumps(
            {
                "schema_version": 1,
                "endpoint_policy_id": "target-v1",
                "method": method,
                "path_segments": ["v1", path],
                "query": [],
                "headers": ([{"name": "content-type", "value": "application/json"}] if write else []),
                "body_utf8": "{}" if write else None,
                "idempotency_key": "a" * 64 if write else None,
            }
        )
    )


async def _wait_for_gateway(url: str, context: ssl.SSLContext) -> None:
    deadline = time.monotonic() + 20
    last_error: BaseException | None = None
    while time.monotonic() < deadline:
        try:
            reader, writer = await asyncio.open_connection(
                "localhost",
                int(url.rsplit(":", 1)[1]),
                ssl=context,
                server_hostname="localhost",
            )
            writer.close()
            await writer.wait_closed()
            del reader
            return
        except (OSError, ssl.SSLError) as error:
            last_error = error
            await asyncio.sleep(0.2)
    raise RuntimeError("controlled-egress gateway did not become ready") from last_error


async def _exercise_gateway(gateway_url: str, certificate_dir: Path) -> None:
    context = ssl.create_default_context(cafile=str(certificate_dir / "ca.pem"))
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    await _wait_for_gateway(gateway_url, context)
    client = HttpxWorkflowControlledEgressClient(
        gateway_url=gateway_url,
        tls_context=context,
        timeout_ms=5_000,
        idempotency_headers_by_endpoint={"target-v1": "idempotency-key"},
    )
    previous_proxy = os.environ.get("HTTPS_PROXY")
    os.environ["HTTPS_PROXY"] = "http://127.0.0.1:1"
    try:
        success = await client.dispatch(_request("success"))
        not_found = await client.dispatch(_request("not-found"))
        server_error = await client.dispatch(_request("server-error"))
        invalid = await client.dispatch(_request("invalid"))
        redirect = await client.dispatch(_request("redirect"))
        cookie = await client.dispatch(_request("cookie"))
        cookie_check = await client.dispatch(_request("check-cookie"))
        encoding_request = WorkflowHttpEgressDispatchV1.model_validate_json(
            json.dumps(
                {
                    "schema_version": 1,
                    "endpoint_policy_id": "target-v1",
                    "method": "GET",
                    "path_segments": ["echo", "space /雪"],
                    "query": [{"name": "q name", "value": "a&b/雪"}],
                    "headers": [],
                    "body_utf8": None,
                    "idempotency_key": None,
                }
            )
        )
        encoded = await client.dispatch(encoding_request)
    finally:
        if previous_proxy is None:
            os.environ.pop("HTTPS_PROXY", None)
        else:
            os.environ["HTTPS_PROXY"] = previous_proxy
        await client.aclose()

    if not (
        success.kind == "success"
        and not_found.kind == "http_error"
        and not_found.response.status_code == 404
        and server_error.kind == "http_error"
        and server_error.response.status_code == 503
        and invalid.kind == "response_invalid"
        and redirect.kind == "http_error"
        and redirect.response.status_code == 302
        and cookie.kind == "success"
        and cookie_check.kind == "success"
        and encoded.kind == "success"
    ):
        raise RuntimeError("controlled-egress typed outcomes did not match")
    if any(header.name == "set-cookie" for header in cookie.response.headers):
        raise RuntimeError("controlled-egress persisted a Set-Cookie header")
    expected_path = "/" + "/".join(quote(segment, safe="") for segment in encoding_request.path_segments)
    expected_path += "?" + urlencode([(item.name, item.value) for item in encoding_request.query])
    if not (encoded.response.body.kind == "json" and encoded.response.body.value == {"path": expected_path}):
        raise RuntimeError("controlled-egress path/query encoding changed")

    wrong_context = ssl.create_default_context()
    wrong_client = HttpxWorkflowControlledEgressClient(
        gateway_url=gateway_url,
        tls_context=wrong_context,
        timeout_ms=2_000,
        idempotency_headers_by_endpoint={"target-v1": "idempotency-key"},
    )
    try:
        try:
            await wrong_client.dispatch(_request("success"))
        except (httpx.TransportError, WorkflowHttpControlledEgressError):
            pass
        else:
            raise RuntimeError("controlled-egress accepted an untrusted TLS certificate")
    finally:
        await wrong_client.aclose()

    async with httpx.AsyncClient(
        verify=context,
        follow_redirects=False,
        trust_env=False,
        timeout=5,
    ) as raw:
        raw_payload = _request("success").model_dump(mode="json")
        raw_payload["url"] = "https://169.254.169.254/latest/meta-data"
        response = await raw.post(
            f"{gateway_url}/v1/workflow-http/dispatch",
            json=raw_payload,
        )
        if response.status_code != 400:
            raise RuntimeError("controlled-egress accepted an SSRF URL field")
        endpoint_payload = _request("success").model_dump(mode="json")
        endpoint_payload["endpoint_policy_id"] = "metadata"
        response = await raw.post(
            f"{gateway_url}/v1/workflow-http/dispatch",
            json=endpoint_payload,
        )
        if response.status_code != 403:
            raise RuntimeError("controlled-egress accepted an unapproved endpoint")


async def _expect_dns_rebind_rejection(
    gateway_url: str,
    certificate_dir: Path,
) -> None:
    context = ssl.create_default_context(cafile=str(certificate_dir / "ca.pem"))
    client = HttpxWorkflowControlledEgressClient(
        gateway_url=gateway_url,
        tls_context=context,
        timeout_ms=5_000,
        idempotency_headers_by_endpoint={"target-v1": "idempotency-key"},
    )
    try:
        try:
            await client.dispatch(_request("success"))
        except WorkflowHttpControlledEgressError:
            return
        raise RuntimeError("controlled-egress accepted a rebound DNS address")
    finally:
        await client.aclose()


async def _exercise_postgres_effect_recovery(
    gateway_url: str,
    certificate_dir: Path,
) -> None:
    from postgres_utils import replace_database, temporary_postgres_database
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    database_url = os.environ.get("DATABASE_URL", "").strip()
    if not database_url:
        raise RuntimeError("DATABASE_URL is required for the PostgreSQL effect conformance")
    context = ssl.create_default_context(cafile=str(certificate_dir / "ca.pem"))
    client = HttpxWorkflowControlledEgressClient(
        gateway_url=gateway_url,
        tls_context=context,
        timeout_ms=5_000,
        idempotency_headers_by_endpoint={"target-v1": "idempotency-key"},
    )
    hmac_key = os.urandom(32)
    run_id = uuid.uuid4()
    version_id = uuid.uuid4()
    node_id = uuid.uuid4()
    project_id = uuid.uuid4()
    owner_user_id = str(uuid.uuid4())
    job_id = uuid.uuid4()
    origin_trace_id = f"workflow-http-conformance-{uuid.uuid4()}"
    current_attempt = 1
    current_worker_id = uuid.uuid4()
    current_lease_token = os.urandom(32).hex()

    def fence() -> WorkflowHttpJobExecutionFence:
        return WorkflowHttpJobExecutionFence(
            run_id=run_id,
            project_id=project_id,
            owner_user_id=owner_user_id,
            origin_trace_id=origin_trace_id,
            job_id=job_id,
            execution_epoch=1,
            attempt=current_attempt,
            worker_id=current_worker_id,
            lease_token=current_lease_token,
        )

    def identity(
        activation_key: str,
        request: WorkflowHttpEgressDispatchV1,
    ) -> WorkflowHttpEffectIdentityV1:
        request_fingerprint = derive_workflow_http_request_fingerprint(
            hmac_key=hmac_key,
            canonical_request_material=request.model_dump_json().encode(),
        )
        operation_key = derive_workflow_http_operation_key(
            hmac_key=hmac_key,
            run_id=run_id,
            node_id=node_id,
            activation_key=activation_key,
            request_fingerprint=request_fingerprint,
        )
        idempotency_key = derive_workflow_http_idempotency_key(
            hmac_key=hmac_key,
            operation_key=operation_key,
        )
        return WorkflowHttpEffectIdentityV1(
            schema_version=1,
            effect_id=uuid.uuid4(),
            run_id=run_id,
            workflow_version_id=version_id,
            node_id=node_id,
            activation_key=activation_key,
            operation_key=operation_key,
            method="POST",
            request_fingerprint=request_fingerprint,
            idempotency_key=idempotency_key,
        )

    async def authorize_dispatch() -> bool:
        return True

    admin_url = replace_database(database_url, "postgres")
    try:
        async with temporary_postgres_database(admin_url) as isolated_url:
            engine = create_async_engine(isolated_url)
            try:
                async with engine.begin() as connection:
                    raw_connection = await connection.get_raw_connection()
                    await raw_connection.driver_connection.execute(
                        """
                        CREATE TABLE workflow_runs (
                            id UUID PRIMARY KEY,
                            project_id UUID NOT NULL,
                            owner_user_id VARCHAR(36) NOT NULL,
                            workflow_version_id UUID NOT NULL,
                            status VARCHAR(32) NOT NULL,
                            execution_epoch BIGINT NOT NULL,
                            current_job_id UUID,
                            origin_trace_id VARCHAR(512) NOT NULL,
                            UNIQUE (id,project_id,owner_user_id),
                            UNIQUE (id,project_id,owner_user_id,origin_trace_id)
                        );
                        CREATE TABLE jobs (
                            id UUID PRIMARY KEY,
                            job_type VARCHAR(32) NOT NULL,
                            project_id UUID NOT NULL,
                            owner_user_id VARCHAR(36) NOT NULL,
                            workflow_run_id UUID NOT NULL,
                            workflow_epoch BIGINT NOT NULL,
                            origin_trace_id VARCHAR(512) NOT NULL,
                            status VARCHAR(16) NOT NULL,
                            attempt_count BIGINT NOT NULL,
                            lease_owner_id UUID,
                            lease_token_hash CHAR(64),
                            lease_expires_at TIMESTAMPTZ,
                            cancel_requested_at TIMESTAMPTZ,
                            UNIQUE (
                                id,project_id,owner_user_id,
                                workflow_run_id,workflow_epoch
                            )
                        );
                        CREATE TABLE workflow_run_jobs (
                            workflow_run_id UUID NOT NULL,
                            execution_epoch BIGINT NOT NULL,
                            job_id UUID NOT NULL,
                            project_id UUID NOT NULL,
                            owner_user_id VARCHAR(36) NOT NULL,
                            PRIMARY KEY (workflow_run_id,execution_epoch),
                            UNIQUE (job_id)
                        );
                        """
                    )
                    await connection.execute(
                        text(
                            """INSERT INTO workflow_runs
                               (id,project_id,owner_user_id,workflow_version_id,
                                status,execution_epoch,origin_trace_id)
                               VALUES (:run,:project,:owner,:version,'running',1,:trace)"""
                        ),
                        {
                            "run": run_id,
                            "project": project_id,
                            "owner": owner_user_id,
                            "version": version_id,
                            "trace": origin_trace_id,
                        },
                    )
                    await connection.execute(
                        text(
                            """INSERT INTO jobs
                               (id,job_type,project_id,owner_user_id,
                                workflow_run_id,workflow_epoch,origin_trace_id,
                                status,attempt_count,lease_owner_id,
                                lease_token_hash,lease_expires_at)
                               VALUES (:job,'workflow_run',:project,:owner,:run,1,
                                       :trace,'running',1,:worker,:lease_hash,
                                       now()+interval '10 minutes')"""
                        ),
                        {
                            "job": job_id,
                            "project": project_id,
                            "owner": owner_user_id,
                            "run": run_id,
                            "trace": origin_trace_id,
                            "worker": current_worker_id,
                            "lease_hash": hashlib.sha256(current_lease_token.encode()).hexdigest(),
                        },
                    )
                    await connection.execute(
                        text(
                            """INSERT INTO workflow_run_jobs
                               (workflow_run_id,execution_epoch,job_id,project_id,
                                owner_user_id)
                               VALUES (:run,1,:job,:project,:owner)"""
                        ),
                        {
                            "run": run_id,
                            "job": job_id,
                            "project": project_id,
                            "owner": owner_user_id,
                        },
                    )
                    await connection.execute(
                        text("UPDATE workflow_runs SET current_job_id=:job WHERE id=:run"),
                        {"job": job_id, "run": run_id},
                    )
                    await raw_connection.driver_connection.execute(EFFECT_DDL.read_text(encoding="utf-8"))
                store = PostgresWorkflowHttpEffectStore(engine)
                executor = WorkflowHttpEffectExecutor(store)

                async def take_over() -> WorkflowHttpJobExecutionFence:
                    nonlocal current_attempt, current_worker_id, current_lease_token
                    current_attempt += 1
                    current_worker_id = uuid.uuid4()
                    current_lease_token = os.urandom(32).hex()
                    async with engine.begin() as connection:
                        await connection.execute(
                            text(
                                """UPDATE jobs
                                      SET attempt_count=:attempt,
                                          lease_owner_id=:worker,
                                          lease_token_hash=:lease_hash,
                                          lease_expires_at=now()+interval '10 minutes'
                                    WHERE id=:job"""
                            ),
                            {
                                "attempt": current_attempt,
                                "worker": current_worker_id,
                                "lease_hash": hashlib.sha256(current_lease_token.encode()).hexdigest(),
                                "job": job_id,
                            },
                        )
                    return fence()

                for index, path in enumerate(
                    ("success", "not-found", "server-error", "invalid"),
                    start=1,
                ):
                    base_request = _request(path, method="POST")
                    effect_identity = identity(
                        f"settled-{index}-{uuid.uuid4().hex[:8]}",
                        base_request,
                    )
                    calls = 0

                    async def dispatch(
                        idempotency_key: str | None,
                        *,
                        request: WorkflowHttpEgressDispatchV1 = base_request,
                    ):
                        nonlocal calls
                        calls += 1
                        return await client.dispatch(request.model_copy(update={"idempotency_key": idempotency_key}))

                    async def checkpoint_fault() -> None:
                        raise RuntimeError("checkpoint fault")

                    try:
                        await executor.execute(
                            effect_identity,
                            fence=fence(),
                            authorize_dispatch=authorize_dispatch,
                            dispatch=dispatch,
                            after_settle_before_checkpoint=checkpoint_fault,
                        )
                    except RuntimeError as error:
                        if str(error) != "checkpoint fault":
                            raise
                    else:
                        raise RuntimeError("checkpoint fault injection did not fire")

                    async def must_not_dispatch(_key: str | None):
                        raise RuntimeError("settled outcome was dispatched twice")

                    recovered = await executor.execute(
                        effect_identity.model_copy(update={"effect_id": uuid.uuid4()}),
                        fence=await take_over(),
                        authorize_dispatch=authorize_dispatch,
                        dispatch=must_not_dispatch,
                    )
                    expected_kind = {
                        "success": "success",
                        "not-found": "http_error",
                        "server-error": "http_error",
                        "invalid": "response_invalid",
                    }[path]
                    if recovered.kind != expected_kind or calls != 1:
                        raise RuntimeError("settled HTTP effect did not recover exactly once")

                concurrent_request = _request(
                    f"concurrent-{uuid.uuid4().hex[:8]}",
                    method="POST",
                )
                first_identity = identity("concurrent-effect", concurrent_request)
                second_identity = first_identity.model_copy(update={"effect_id": uuid.uuid4()})
                dispatch_started = asyncio.Event()
                release_dispatch = asyncio.Event()
                concurrent_calls = 0
                concurrent_origin_counts: list[int] = []

                async def real_dispatch(idempotency_key: str | None):
                    nonlocal concurrent_calls
                    concurrent_calls += 1
                    dispatch_started.set()
                    await release_dispatch.wait()
                    outcome = await client.dispatch(concurrent_request.model_copy(update={"idempotency_key": idempotency_key}))
                    assert outcome.kind == "success"
                    count_header = next(
                        (header.value for header in outcome.response.headers if header.name == "x-target-count"),
                        None,
                    )
                    if count_header is None:
                        raise RuntimeError("origin dispatch count was not observable")
                    concurrent_origin_counts.append(int(count_header))
                    return outcome

                async def no_second_dispatch(_key: str | None):
                    raise RuntimeError("concurrent effect dispatched twice")

                takeover_fence: WorkflowHttpJobExecutionFence | None = None

                async def first_worker():
                    try:
                        await executor.execute(
                            first_identity,
                            fence=fence(),
                            authorize_dispatch=authorize_dispatch,
                            dispatch=real_dispatch,
                        )
                    except WorkflowHttpExecutionAuthorityLost:
                        return "lost"
                    raise RuntimeError("stale Worker settled after takeover")

                async def second_worker():
                    nonlocal takeover_fence
                    await dispatch_started.wait()
                    takeover_fence = await take_over()
                    try:
                        await executor.execute(
                            second_identity,
                            fence=takeover_fence,
                            authorize_dispatch=authorize_dispatch,
                            dispatch=no_second_dispatch,
                        )
                    except WorkflowHttpEffectAlreadyDispatching:
                        release_dispatch.set()
                        return "busy"
                    raise RuntimeError("concurrent effect did not preserve one owner")

                if await asyncio.gather(first_worker(), second_worker()) != [
                    "lost",
                    "busy",
                ]:
                    raise RuntimeError("two-Worker effect race produced an invalid result")
                if takeover_fence is None:
                    raise RuntimeError("takeover fence was not installed")
                concurrent_unknown = await store.recover_abandoned_dispatch(
                    first_identity.effect_id,
                    recovery_fence=takeover_fence,
                )
                if concurrent_unknown.state != "unknown" or concurrent_calls != 1 or concurrent_origin_counts != [1]:
                    raise RuntimeError("concurrent effect reached the origin more than once")

                unknown_request = _request(
                    f"unknown-{uuid.uuid4().hex[:8]}",
                    method="POST",
                )
                unknown_identity = identity("unknown-effect", unknown_request)
                old_fence = fence()
                prepared = await store.prepare(unknown_identity, fence=old_fence)
                await store.claim_for_dispatch(
                    prepared.identity.effect_id,
                    fence=old_fence,
                )
                received = await client.dispatch(unknown_request.model_copy(update={"idempotency_key": unknown_identity.idempotency_key}))
                if received.kind != "success":
                    raise RuntimeError("unknown-window origin response was not received")
                current_fence = await take_over()
                try:
                    await store.settle(
                        prepared.identity.effect_id,
                        fence=old_fence,
                        outcome=received,
                    )
                except WorkflowHttpExecutionAuthorityLost:
                    pass
                else:
                    raise RuntimeError("stale Worker settled after losing its lease")
                unknown = await store.recover_abandoned_dispatch(
                    prepared.identity.effect_id,
                    recovery_fence=current_fence,
                )
                if unknown.state != "unknown":
                    raise RuntimeError("uncertain write did not become terminal unknown")
                try:
                    await executor.execute(
                        unknown_identity.model_copy(update={"effect_id": uuid.uuid4()}),
                        fence=current_fence,
                        authorize_dispatch=authorize_dispatch,
                        dispatch=no_second_dispatch,
                    )
                except WorkflowHttpSideEffectUnknown:
                    pass
                else:
                    raise RuntimeError("unknown write exposed a retry path")
            finally:
                await engine.dispose()
    finally:
        await client.aclose()


def run_conformance(*, postgres_effects: bool) -> None:
    image_id = _run(
        ["docker", "image", "inspect", "--format", "{{.Id}}", PYTHON_IMAGE],
        output=True,
    )
    if not image_id.startswith("sha256:"):
        raise RuntimeError("conformance image did not resolve to an immutable image ID")
    prefix = f"wf-http-{uuid.uuid4().hex[:10]}"
    worker_network = f"{prefix}-worker"
    target_network = f"{prefix}-target"
    target = f"{prefix}-origin"
    gateway = f"{prefix}-gateway"
    attacker = f"{prefix}-rebind"
    containers: list[str] = []
    networks: list[str] = []
    with tempfile.TemporaryDirectory(prefix="workflow-http-egress-") as raw_temp:
        certificate_dir = Path(raw_temp)
        _generate_certificates(certificate_dir)
        try:
            for network in (worker_network, target_network):
                _run(["docker", "network", "create", "--internal", network])
                networks.append(network)
            _run(
                [
                    "docker",
                    "run",
                    "-d",
                    "--pull=never",
                    "--name",
                    target,
                    "--network",
                    target_network,
                    "--network-alias",
                    "target.workflow.test",
                    "-v",
                    f"{RESOURCE_DIR}:/app:ro",
                    "-v",
                    f"{certificate_dir}:/certs:ro",
                    PYTHON_IMAGE,
                    "python",
                    "/app/target.py",
                ]
            )
            containers.append(target)
            target_ip = _run(
                [
                    "docker",
                    "inspect",
                    "--format",
                    "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}",
                    target,
                ],
                output=True,
            )
            if not target_ip:
                raise RuntimeError("unable to resolve the conformance target IP")
            _run(
                [
                    "docker",
                    "run",
                    "-d",
                    "--pull=never",
                    "--name",
                    gateway,
                    "--network",
                    "bridge",
                    "-p",
                    "127.0.0.1::8443",
                    "-e",
                    f"PINNED_TARGET_IP={target_ip}",
                    "-e",
                    "HTTPS_PROXY=http://127.0.0.1:1",
                    "-v",
                    f"{RESOURCE_DIR}:/app:ro",
                    "-v",
                    f"{certificate_dir}:/certs:ro",
                    PYTHON_IMAGE,
                    "python",
                    "/app/gateway.py",
                ]
            )
            containers.append(gateway)
            _run(
                [
                    "docker",
                    "network",
                    "connect",
                    "--alias",
                    "egress.gateway.test",
                    worker_network,
                    gateway,
                ]
            )
            _run(["docker", "network", "connect", target_network, gateway])
            published = _run(
                ["docker", "port", gateway, "8443/tcp"],
                output=True,
            )
            gateway_port = int(published.rsplit(":", 1)[1])
            gateway_url = f"https://localhost:{gateway_port}"
            asyncio.run(_exercise_gateway(gateway_url, certificate_dir))
            if postgres_effects:
                asyncio.run(
                    _exercise_postgres_effect_recovery(
                        gateway_url,
                        certificate_dir,
                    )
                )

            probe_output = _run(
                [
                    "docker",
                    "run",
                    "--pull=never",
                    "--network",
                    worker_network,
                    "-e",
                    f"TARGET_IP={target_ip}",
                    "-e",
                    "HTTPS_PROXY=http://127.0.0.1:1",
                    "-v",
                    f"{RESOURCE_DIR}:/app:ro",
                    "-v",
                    f"{certificate_dir}:/certs:ro",
                    PYTHON_IMAGE,
                    "python",
                    "/app/probe.py",
                ],
                output=True,
            )
            if "worker_direct_path=blocked controlled_egress=passed" not in probe_output:
                raise RuntimeError("Worker network-isolation probe did not pass")

            _run(["docker", "network", "disconnect", target_network, target])
            _run(
                [
                    "docker",
                    "run",
                    "-d",
                    "--pull=never",
                    "--name",
                    attacker,
                    "--network",
                    target_network,
                    "--network-alias",
                    "target.workflow.test",
                    PYTHON_IMAGE,
                    "python",
                    "-c",
                    "import time; time.sleep(60)",
                ]
            )
            containers.append(attacker)
            asyncio.run(_expect_dns_rebind_rejection(gateway_url, certificate_dir))
        finally:
            for container in reversed(containers):
                subprocess.run(
                    ["docker", "rm", "-f", container],
                    check=False,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            for network in reversed(networks):
                subprocess.run(
                    ["docker", "network", "rm", network],
                    check=False,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )

    print(
        json.dumps(
            {
                "profile": "docker-internal-controlled-egress-v1",
                "container_image_id": image_id,
                "tls": "passed",
                "direct_path": "blocked",
                "ssrf": "blocked",
                "dns_rebind": "blocked",
                "redirect": "not_followed",
                "cookie": "not_persisted",
                "ambient_proxy": "ignored",
                "path_query_encoding": "passed",
                "job_lease_fence": "passed" if postgres_effects else "not_requested",
                "two_worker_fault_window": "passed" if postgres_effects else "not_requested",
                "typed_outcomes": ["success", "4xx", "5xx", "response_invalid"],
                "postgres_effect_recovery": ("passed" if postgres_effects else "not_requested"),
            },
            sort_keys=True,
        )
    )


def main() -> None:
    global PYTHON_IMAGE
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", default=PYTHON_IMAGE)
    parser.add_argument("--postgres-effects", action="store_true")
    arguments = parser.parse_args()
    PYTHON_IMAGE = arguments.image
    run_conformance(postgres_effects=arguments.postgres_effects)


if __name__ == "__main__":
    main()
