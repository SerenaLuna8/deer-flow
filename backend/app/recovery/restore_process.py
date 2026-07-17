"""Cancellation-settled pg_restore execution with identity-owned credentials."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

from sqlalchemy.engine import make_url

from app.recovery.cleanup import (
    OwnedFile,
    OwnedWorkspace,
    _create_owned_file,
    _remove_owned_file,
    _settle_blocking_cleanup,
    _settle_blocking_result,
    _write_owned_file,
)

_PROCESS_TERM_TIMEOUT_SECONDS = 5.0


class RestoreCommandFailed(RuntimeError):
    def __init__(self) -> None:
        super().__init__("RESTORE_COMMAND_FAILED")


def _database_name(database_url: str) -> str:
    try:
        name = make_url(database_url).database
    except Exception:
        raise RestoreCommandFailed from None
    if not name:
        raise RestoreCommandFailed
    return name


def _pgpass_escape(value: str) -> str:
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise RestoreCommandFailed
    return value.replace("\\", "\\\\").replace(":", "\\:")


def _libpq_environment(database_url: str) -> tuple[dict[str, str], bytes | None]:
    try:
        parsed = make_url(database_url)
        host = parsed.host or ""
        port = str(parsed.port or 5432)
        user = parsed.username or ""
        database = parsed.database or ""
        if not user or not database:
            raise ValueError
        environment = {
            "PATH": os.environ.get("PATH", ""),
            "LANG": os.environ.get("LANG", "C.UTF-8"),
            "PGHOST": host,
            "PGPORT": port,
            "PGUSER": user,
            "PGDATABASE": database,
        }
        for query_key, env_key in {
            "sslmode": "PGSSLMODE",
            "sslrootcert": "PGSSLROOTCERT",
            "sslcert": "PGSSLCERT",
            "sslkey": "PGSSLKEY",
        }.items():
            value = parsed.query.get(query_key)
            if value:
                environment[env_key] = str(value)
        passfile_content: bytes | None = None
        if parsed.password:
            line = ":".join(_pgpass_escape(value) for value in (host or "*", port, database, user, parsed.password))
            passfile_content = (line + "\n").encode("utf-8")
        return environment, passfile_content
    except RestoreCommandFailed:
        raise
    except Exception:
        raise RestoreCommandFailed from None


async def _terminate_process(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return
    try:
        process.terminate()
        await asyncio.wait_for(
            process.wait(),
            timeout=_PROCESS_TERM_TIMEOUT_SECONDS,
        )
    except (TimeoutError, ProcessLookupError):
        if process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            await process.wait()


async def _prepare_passfile(
    workspace: OwnedWorkspace,
    content: bytes,
) -> OwnedFile:
    owned, producer_cancelled = await _settle_blocking_result(
        _create_owned_file,
        workspace.path,
        prefix=".restore-pgpass-",
    )
    workspace.register(owned)
    try:
        if producer_cancelled:
            raise asyncio.CancelledError
        _unused, writer_cancelled = await _settle_blocking_result(
            _write_owned_file,
            owned,
            content,
        )
        if writer_cancelled:
            raise asyncio.CancelledError
    except BaseException as error:
        cleanup_cancelled = await _settle_blocking_cleanup(
            _remove_owned_file,
            owned.path,
            owned.identity,
        )
        workspace.forget(owned)
        if isinstance(error, asyncio.CancelledError) or cleanup_cancelled:
            raise asyncio.CancelledError from None
        raise
    return owned


async def run_pg_restore(
    database_url: str,
    dump_path: Path,
    workspace: OwnedWorkspace,
) -> None:
    database = _database_name(database_url)
    environment, passfile_content = _libpq_environment(database_url)
    passfile: OwnedFile | None = None
    if passfile_content is not None:
        passfile = await _prepare_passfile(workspace, passfile_content)
        environment["PGPASSFILE"] = str(passfile.path)
    process: asyncio.subprocess.Process | None = None
    try:
        process = await asyncio.create_subprocess_exec(
            "pg_restore",
            "--exit-on-error",
            "--no-owner",
            "--no-acl",
            f"--dbname={database}",
            str(dump_path),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            env=environment,
        )
        if await process.wait() != 0:
            raise RestoreCommandFailed
    except asyncio.CancelledError:
        if process is not None:
            cleanup = asyncio.create_task(_terminate_process(process))
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                await cleanup
        raise
    except RestoreCommandFailed:
        raise
    except Exception:
        if process is not None:
            await _terminate_process(process)
        raise RestoreCommandFailed from None
    finally:
        if passfile is not None:
            cancelled = await _settle_blocking_cleanup(
                _remove_owned_file,
                passfile.path,
                passfile.identity,
            )
            workspace.forget(passfile)
            if cancelled:
                raise asyncio.CancelledError


__all__ = ["RestoreCommandFailed", "run_pg_restore"]
