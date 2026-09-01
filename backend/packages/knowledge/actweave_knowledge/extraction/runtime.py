"""Bounded child protocol, OS isolation and cancellation-safe I/O settlement."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import signal
import stat
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path

from actweave_knowledge.contracts import KNOWLEDGE_QUOTA_EXCEEDED, KnowledgeError

from .child import ERROR_CODES, FRAME_BYTES
from .contracts import Attachment, ExtractionError, ExtractionLimits, ExtractionResult, ExtractSetting, LocalAttachment
from .ipc import open_child_regular, receive_asset
from .manifest import canonical_parse_fingerprint, decode_manifest
from .sandbox import parser_environment, sandbox_command


class ParserSlots:
    """One host-owned nonqueuing admission counter, never a second worker pool."""

    def __init__(self, capacity: int):
        if isinstance(capacity, bool) or not isinstance(capacity, int) or capacity <= 0:
            raise ValueError("capacity must be positive")
        self.capacity = capacity
        self.active = 0

    async def __aenter__(self):
        if self.active >= self.capacity:
            raise ExtractionError("PARSER_BUSY", "解析繁忙，请稍后重试")
        self.active += 1
        return self

    async def __aexit__(self, exc_type, exc, tb):
        self.active -= 1


async def stop_process_group(process: asyncio.subprocess.Process) -> None:
    """Reap the leader and kill any remaining same-session conversion children."""
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        # Darwin can return EPERM while the leader is an unreaped zombie.
        # Wait/reap before the mandatory SIGKILL below; a live unkillable group
        # still fails cleanup instead of claiming quiescence.
        pass
    try:
        await asyncio.wait_for(process.wait(), timeout=1)
    except TimeoutError:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        await process.wait()
    finally:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        if sys.platform.startswith("linux"):
            await _wait_linux_process_group_quiescent(process.pid)


def _linux_process_stat(path: Path) -> tuple[str, int, int] | None:
    try:
        payload = path.read_text(encoding="utf-8")
    except (FileNotFoundError, ProcessLookupError):
        return None
    except (OSError, UnicodeError) as error:
        raise RuntimeError("Linux process-group state is unavailable") from error
    _, separator, fields_text = payload.rpartition(")")
    fields = fields_text.split()
    if not separator or len(fields) < 4:
        raise RuntimeError("Linux process-group state is unavailable")
    try:
        return fields[0], int(fields[2]), int(fields[3])
    except ValueError as error:
        raise RuntimeError("Linux process-group state is unavailable") from error


def _linux_live_process_group_pidfds(process_group_id: int) -> tuple[int, ...]:
    """Open stable handles for live members of one start_new_session group."""
    try:
        entries = os.scandir("/proc")
    except OSError as error:
        raise RuntimeError("Linux process-group state is unavailable") from error
    handles: list[int] = []
    try:
        with entries:
            for entry in entries:
                if not entry.name.isdecimal():
                    continue
                current = _linux_process_stat(Path(entry.path, "stat"))
                if current is None or current[0] in {"X", "Z"} or current[1:] != (process_group_id, process_group_id):
                    continue
                try:
                    handle = os.pidfd_open(int(entry.name))
                except ProcessLookupError:
                    continue
                except OSError as error:
                    raise RuntimeError("Linux process-group state is unavailable") from error
                handles.append(handle)
                revalidated = _linux_process_stat(Path(entry.path, "stat"))
                if revalidated is None or revalidated[0] in {"X", "Z"} or revalidated[1:] != (process_group_id, process_group_id):
                    os.close(handles.pop())
        return tuple(handles)
    except BaseException:
        for handle in handles:
            os.close(handle)
        raise


async def _wait_linux_process_group_quiescent(process_group_id: int) -> None:
    """Wait until SIGKILL has taken effect for every live group member."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + 1
    while True:
        handles = await asyncio.to_thread(_linux_live_process_group_pidfds, process_group_id)
        if not handles:
            return
        futures = []
        registered = []
        try:
            for handle in handles:
                future = loop.create_future()
                loop.add_reader(handle, lambda value=future: value.done() or value.set_result(None))
                registered.append(handle)
                futures.append(future)
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise RuntimeError("Parser process group did not terminate")
            try:
                await asyncio.wait_for(asyncio.gather(*futures), timeout=remaining)
            except TimeoutError:
                raise RuntimeError("Parser process group did not terminate") from None
        finally:
            for handle in registered:
                loop.remove_reader(handle)
            for handle in handles:
                os.close(handle)


def _invalid() -> ExtractionError:
    return ExtractionError("PARSER_OUTPUT_INVALID")


def _work_directory_bytes(work_dir: Path, limits: ExtractionLimits) -> int:
    # Bound metadata traversal as well as file bytes. Files may disappear while
    # a converter removes its own intermediates; that is not parser corruption.
    total = entries = 0

    def failed(error: OSError) -> None:
        if not isinstance(error, FileNotFoundError):
            raise error

    for root, directories, names in os.walk(work_dir, followlinks=False, onerror=failed):
        entries += len(directories) + len(names)
        if entries > 100_000:
            raise ExtractionError("PARSER_WORK_DIR_LIMIT_EXCEEDED")
        for name in names:
            try:
                info = os.stat(Path(root) / name, follow_symlinks=False)
            except FileNotFoundError:
                continue
            if stat.S_ISREG(info.st_mode):
                total += info.st_size
                if total > limits.max_work_dir_bytes:
                    raise ExtractionError("PARSER_WORK_DIR_LIMIT_EXCEEDED")
    return total


def _check_work_dir(work_dir: Path, limits: ExtractionLimits) -> None:
    _work_directory_bytes(work_dir, limits)


def _prepare(setting: ExtractSetting, work_dir: Path, limits: ExtractionLimits) -> tuple[ExtractSetting, str]:
    work_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    if work_dir.is_symlink():
        raise _invalid()
    # Each invocation gets private, previously unused output directories.
    for name in ("child", "received"):
        (work_dir / name).mkdir(mode=0o700)
    source_dir = work_dir / "source"
    source_dir.mkdir(mode=0o700, exist_ok=True)
    if source_dir.is_symlink():
        raise _invalid()
    original = setting.source_path.resolve(strict=True)
    within = original.is_relative_to(source_dir.resolve())
    target = original if within else source_dir / ("input" + Path(setting.original_name).suffix.lower())
    fd = os.open(original, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as source:
        info = os.fstat(source.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise _invalid()
        if info.st_size > limits.max_source_bytes:
            raise KnowledgeError(KNOWLEDGE_QUOTA_EXCEEDED, "原文件大小超过限制")
        remaining = limits.max_work_dir_bytes - _work_directory_bytes(work_dir, limits)
        if not within and info.st_size > remaining:
            raise ExtractionError("PARSER_WORK_DIR_LIMIT_EXCEEDED")
        digest = hashlib.sha256()
        size = 0
        destination = None if within else target.open("xb")
        try:
            while data := source.read(64 * 1024):
                size += len(data)
                if size > limits.max_source_bytes:
                    raise KnowledgeError(KNOWLEDGE_QUOTA_EXCEEDED, "原文件大小超过限制")
                if not within and size > remaining:
                    raise ExtractionError("PARSER_WORK_DIR_LIMIT_EXCEEDED")
                digest.update(data)
                if destination is not None:
                    destination.write(data)
        finally:
            if destination is not None:
                destination.close()
    _check_work_dir(work_dir, limits)
    return setting.model_copy(update={"source_path": target.resolve()}), digest.hexdigest()


def _read_manifest(work_dir: Path, limits: ExtractionLimits, relative_path: str, accepted: dict[str, Attachment], source_digest: str, setting: ExtractSetting) -> ExtractionResult:
    _check_work_dir(work_dir, limits)
    with os.fdopen(open_child_regular(work_dir, relative_path), "rb") as source:
        if os.fstat(source.fileno()).st_size > limits.max_manifest_bytes:
            raise _invalid()
        payload = source.read(limits.max_manifest_bytes + 1)
    try:
        result = decode_manifest(payload, limits)
    except (KnowledgeError, ValueError):
        raise _invalid() from None
    if {asset.ref: asset for asset in result.attachments} != accepted or result.source_sha256 != source_digest or result.parse_fingerprint != canonical_parse_fingerprint(setting.profile):
        raise _invalid()
    return result


def _decode_frame(data: bytes) -> dict:
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate key")
            result[key] = value
        return result

    if not data or len(data) > FRAME_BYTES or not data.endswith(b"\n"):
        raise _invalid()
    try:
        frame = json.loads(data, object_pairs_hook=unique, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (ValueError, UnicodeError, RecursionError):
        raise _invalid() from None
    if not isinstance(frame, dict):
        raise _invalid()
    return frame


async def _settle(task: asyncio.Task, *, propagate_cancel: bool):
    """Repeated cancellation cannot drop a live child or an already-started PUT."""
    interrupted = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            interrupted = True
    result = task.result()
    if interrupted and propagate_cancel:
        raise asyncio.CancelledError
    return result


async def run_extraction(
    setting: ExtractSetting,
    *,
    work_dir: Path,
    limits: ExtractionLimits,
    timeout_seconds: int,
    on_asset: Callable[[LocalAttachment], Awaitable[None]],
    guard: Callable[[], Awaitable[None]],
) -> ExtractionResult:
    if timeout_seconds <= 0:
        raise ExtractionError("PARSER_TIMEOUT")
    work_dir = work_dir.absolute()
    process = None
    launch = None
    transport = None
    pipe = None
    read_fd = write_fd = None
    pending: set[asyncio.Task] = set()
    monitor = None
    session = None

    async def tracked(awaitable):
        task = asyncio.create_task(awaitable)
        pending.add(task)
        try:
            return await asyncio.shield(task)
        finally:
            if task.done():
                pending.discard(task)

    async def watch_space():
        while True:
            await tracked(asyncio.to_thread(_check_work_dir, work_dir, limits))
            await asyncio.sleep(0.5)

    async def consume(reader, admitted, digest):
        accepted: dict[str, Attachment] = {}
        result = None
        saw_frame = False
        assert process is not None and process.stdin is not None
        request = json.dumps({"setting": admitted.model_dump(mode="json"), "limits": limits.model_dump(mode="json")}, separators=(",", ":")).encode() + b"\n"
        if len(request) > FRAME_BYTES:
            raise _invalid()
        try:
            process.stdin.write(request)
            await process.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            raise _invalid() from None
        while True:
            try:
                data = await reader.readuntil(b"\n")
            except asyncio.IncompleteReadError as error:
                if not saw_frame and not error.partial and await process.wait() != 0:
                    raise ExtractionError("PARSER_SANDBOX_UNAVAILABLE") from None
                if error.partial or result is None:
                    raise _invalid() from None
                break
            except (asyncio.LimitOverrunError, ValueError):
                raise _invalid() from None
            if result is not None:
                raise _invalid()
            saw_frame = True
            frame = _decode_frame(data)
            if frame.get("type") == "asset" and set(frame) == {"type", "asset"}:
                try:
                    asset = LocalAttachment.model_validate(frame["asset"])
                except ValueError:
                    raise _invalid() from None
                if asset.attachment.ref in accepted:
                    raise _invalid()
                await tracked(asyncio.to_thread(_check_work_dir, work_dir, limits))
                local = await tracked(asyncio.to_thread(receive_asset, asset, work_dir=work_dir, limits=limits, accepted=accepted))
                await guard()
                await tracked(on_asset(local))
                await guard()
                try:
                    process.stdin.write(b'{"type":"ack"}\n')
                    await process.stdin.drain()
                except (BrokenPipeError, ConnectionResetError):
                    raise _invalid() from None
            elif frame.get("type") == "result" and set(frame) == {"type", "relative_path"} and frame["relative_path"] == "child/manifest.json":
                result = await tracked(asyncio.to_thread(_read_manifest, work_dir, limits, frame["relative_path"], accepted, digest, admitted))
            elif frame.get("type") == "error" and set(frame) == {"type", "reason_code"} and isinstance(frame["reason_code"], str) and frame["reason_code"] in ERROR_CODES:
                if frame["reason_code"] == "PARSER_QUOTA_EXCEEDED":
                    raise KnowledgeError(KNOWLEDGE_QUOTA_EXCEEDED, "解析资源超限")
                raise ExtractionError(frame["reason_code"])
            else:
                raise _invalid()
        if await process.wait() != 0:
            raise ExtractionError("PARSER_SANDBOX_UNAVAILABLE")
        await guard()
        return result

    async def cleanup():
        nonlocal process
        stop_error: BaseException | None = None
        # Stop dispatch first. Shielded receiver/callback tasks remain alive.
        for task in (session, monitor):
            if task is not None:
                task.cancel()
        # A cancelled spawn still owns its eventual process handle.
        if process is None and launch is not None:
            try:
                process = await launch
            except Exception:
                pass
        if process is not None:
            try:
                await stop_process_group(process)
            except BaseException as error:
                stop_error = error
        await asyncio.gather(*(task for task in (session, monitor) if task is not None), return_exceptions=True)
        # Receiver threads/host callbacks are shielded, retained, and settled only
        # after group termination; the caller may safely remove work_dir afterward.
        await asyncio.gather(*tuple(pending), return_exceptions=True)
        if process is not None and process.stdin is not None:
            process.stdin.close()
        if transport is not None:
            transport.close()
        if pipe is not None:
            pipe.close()
        for fd in (read_fd, write_fd):
            if fd is not None:
                os.close(fd)
        if stop_error is not None:
            raise stop_error

    deadline = asyncio.timeout(timeout_seconds)
    try:
        async with deadline:
            await guard()
            admitted, digest = await tracked(asyncio.to_thread(_prepare, setting, work_dir, limits))
            read_fd, write_fd = os.pipe()
            command = [sys.executable, "-s", "-B", "-m", "actweave_knowledge.extraction.child", "--output-fd", str(write_fd)]
            argv = await tracked(asyncio.to_thread(sandbox_command, command, work_dir=work_dir))
            environment = await tracked(asyncio.to_thread(parser_environment, work_dir))
            await guard()
            launch = asyncio.create_task(
                asyncio.create_subprocess_exec(
                    *argv, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL, cwd=work_dir / "child", env=environment, start_new_session=True, pass_fds=(write_fd,)
                )
            )
            process = await asyncio.shield(launch)
            os.close(write_fd)
            write_fd = None
            pipe = os.fdopen(read_fd, "rb", buffering=0)
            read_fd = None
            reader = asyncio.StreamReader(limit=FRAME_BYTES)
            protocol = asyncio.StreamReaderProtocol(reader)
            transport, _ = await asyncio.get_running_loop().connect_read_pipe(lambda: protocol, pipe)
            monitor = asyncio.create_task(watch_space())
            session = asyncio.create_task(consume(reader, admitted, digest))
            completed, _ = await asyncio.wait({monitor, session}, return_when=asyncio.FIRST_COMPLETED)
            if monitor in completed:
                await monitor
            return await session
    except TimeoutError:
        if deadline.expired():
            raise ExtractionError("PARSER_TIMEOUT") from None
        raise
    finally:
        await _settle(asyncio.create_task(cleanup()), propagate_cancel=sys.exception() is None)
