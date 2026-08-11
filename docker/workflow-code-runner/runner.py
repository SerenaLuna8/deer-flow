"""Fixed stdin/stdout runner embedded in the Workflow Code runtime image.

Only the parent provider chooses this entrypoint.  Project-authored source is
data inside the canonical input envelope; it is never a shell command, argv,
environment value, or mounted file.
"""

from __future__ import annotations

import contextlib
import hashlib
import inspect
import io
import json
import math
import os
import re
import struct
import sys
import time
import unicodedata

RUNTIME_CONTRACT = "python3.12-v1"
MAX_ENVELOPE_BYTES = 2 * 1024 * 1024
MAX_SAFE_INTEGER = 9_007_199_254_740_991
_EXPONENT_MASK = 0x7FF
_FRACTION_MASK = (1 << 52) - 1
_IMPLICIT_BIT = 1 << 52
_ANSI_ESCAPE = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))")


class OutputLimit(Exception):
    pass


class ResourceExhausted(Exception):
    pass


def _normalize_text(value: str) -> str:
    normalized = unicodedata.normalize("NFC", value)
    if any(0xD800 <= ord(character) <= 0xDFFF for character in normalized):
        raise ValueError("text contains an invalid Unicode scalar")
    return normalized


def _number(value: int | float) -> str:
    if isinstance(value, int):
        if abs(value) > MAX_SAFE_INTEGER:
            raise ValueError("integer exceeds the portable JSON range")
        return str(value)
    if not math.isfinite(value):
        raise ValueError("non-finite JSON number")
    bits = int.from_bytes(struct.pack(">d", value), byteorder="big")
    negative = bool(bits >> 63)
    exponent_bits = (bits >> 52) & _EXPONENT_MASK
    fraction = bits & _FRACTION_MASK
    if exponent_bits == 0 and fraction == 0:
        return "0"
    if exponent_bits == 0:
        significand = fraction
        binary_exponent = -1074
    else:
        significand = _IMPLICIT_BIT | fraction
        binary_exponent = exponent_bits - 1023 - 52
    if binary_exponent >= 0:
        integer = significand << binary_exponent
        if integer > MAX_SAFE_INTEGER:
            raise ValueError("integer exceeds the portable JSON range")
        return f"{'-' if negative else ''}{integer}"
    denominator_power = -binary_exponent
    trailing_zero_bits = (significand & -significand).bit_length() - 1
    common_power = min(denominator_power, trailing_zero_bits)
    significand >>= common_power
    denominator_power -= common_power
    if denominator_power == 0:
        if significand > MAX_SAFE_INTEGER:
            raise ValueError("integer exceeds the portable JSON range")
        return f"{'-' if negative else ''}{significand}"
    digits = str(significand * 5**denominator_power)
    exponent = len(digits) - 1 - denominator_power
    coefficient = digits[0] if len(digits) == 1 else f"{digits[0]}.{digits[1:]}"
    return f"{'-' if negative else ''}{coefficient}e{exponent}"


def _canonical(value) -> str:
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, int | float):
        return _number(value)
    if isinstance(value, str):
        return json.dumps(
            _normalize_text(value), ensure_ascii=False, separators=(",", ":")
        )
    if isinstance(value, list):
        return "[" + ",".join(_canonical(item) for item in value) + "]"
    if isinstance(value, dict):
        if not all(isinstance(key, str) for key in value):
            raise TypeError("JSON object keys must be strings")
        normalized = [(_normalize_text(key), nested) for key, nested in value.items()]
        if len({key for key, _ in normalized}) != len(normalized):
            raise ValueError("normalized JSON keys are not unique")
        normalized.sort(key=lambda item: item[0])
        return (
            "{"
            + ",".join(
                _canonical(key) + ":" + _canonical(nested) for key, nested in normalized
            )
            + "}"
        )
    raise TypeError(f"unsupported JSON result type: {type(value).__name__}")


def _clean_log_text(value: str) -> str:
    without_ansi = _ANSI_ESCAPE.sub("", value)
    return "".join(
        character
        for character in without_ansi
        if character in {"\n", "\r", "\t"} or ord(character) >= 0x20
    )


class TailWriter(io.TextIOBase):
    def __init__(self, limit: int) -> None:
        self._limit = limit
        self._value = b""
        self.truncated = False

    def writable(self) -> bool:
        return True

    def write(self, value: str) -> int:
        if not isinstance(value, str):
            value = str(value)
        encoded = _clean_log_text(value).encode("utf-8", errors="replace")
        if len(encoded) >= self._limit:
            self._value = encoded[-self._limit :]
            self.truncated = True
        elif len(self._value) + len(encoded) > self._limit:
            self._value = (self._value + encoded)[-self._limit :]
            self.truncated = True
        else:
            self._value += encoded
        return len(value)

    def text(self) -> str:
        while self._value:
            try:
                return self._value.decode("utf-8")
            except UnicodeDecodeError:
                self._value = self._value[1:]
                self.truncated = True
        return ""


def _exception_message(exc: BaseException) -> str:
    value = _clean_log_text(str(exc))
    return f"{type(exc).__name__}: {value}" if value else type(exc).__name__


def _structured_result(envelope: dict) -> bytes:
    encoded = _canonical(envelope).encode("utf-8")
    if len(encoded) > MAX_ENVELOPE_BYTES:
        fallback = {
            "duration_ms": envelope.get("duration_ms", 0),
            "exit_code": 1,
            "outcome": "output_limit",
            "result": None,
            "stderr_tail": "runner envelope exceeded its hard limit",
            "stdout_tail": "",
            "truncated": True,
        }
        encoded = _canonical(fallback).encode("utf-8")
    return encoded


def _run(payload: dict) -> dict:
    started = time.monotonic()
    allowed = {"inputs", "limits", "runtime_contract", "source", "source_digest"}
    if set(payload) != allowed:
        raise ValueError("invalid runner envelope fields")
    if payload["runtime_contract"] != RUNTIME_CONTRACT:
        raise ValueError("unsupported runtime contract")
    source = payload["source"]
    inputs = payload["inputs"]
    limits = payload["limits"]
    if (
        not isinstance(source, str)
        or not isinstance(inputs, dict)
        or not isinstance(limits, dict)
    ):
        raise TypeError("invalid runner envelope types")
    if hashlib.sha256(source.encode("utf-8")).hexdigest() != payload["source_digest"]:
        raise ValueError("source digest mismatch")
    if set(limits) != {"result_bytes", "stderr_tail_bytes", "stdout_tail_bytes"}:
        raise ValueError("invalid runner limit fields")
    result_limit = int(limits["result_bytes"])
    stdout = TailWriter(int(limits["stdout_tail_bytes"]))
    stderr = TailWriter(int(limits["stderr_tail_bytes"]))
    namespace = {"__builtins__": __builtins__, "__name__": "__workflow_code__"}
    outcome = "succeeded"
    exit_code = 0
    result = None
    try:
        compiled = compile(
            source, "<workflow-code>", "exec", dont_inherit=True, optimize=0
        )
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            exec(compiled, namespace, namespace)
            entrypoint = namespace.get("main")
            if not callable(entrypoint):
                raise TypeError("main must be a callable")
            candidate = entrypoint(inputs)
            if inspect.isawaitable(candidate):
                if inspect.iscoroutine(candidate):
                    candidate.close()
                raise TypeError("main must be synchronous")
        if not isinstance(candidate, dict):
            raise TypeError("main must return a JSON object")
        canonical_result = _canonical(candidate).encode("utf-8")
        if len(canonical_result) > result_limit:
            raise OutputLimit("canonical result exceeds the byte limit")
        result = candidate
    except SyntaxError as exc:
        outcome = "syntax_error"
        exit_code = 1
        stderr.write(_exception_message(exc))
    except OutputLimit as exc:
        outcome = "output_limit"
        exit_code = 1
        stderr.write(_exception_message(exc))
    except OSError as exc:
        outcome = "resource_exhausted" if exc.errno in {11, 12, 28} else "runtime_error"
        exit_code = 1
        stderr.write(_exception_message(exc))
    except MemoryError as exc:
        outcome = "resource_exhausted"
        exit_code = 1
        stderr.write(_exception_message(exc))
    except BaseException as exc:
        outcome = "runtime_error"
        exit_code = 1
        stderr.write(_exception_message(exc))
    return {
        "duration_ms": max(0, int((time.monotonic() - started) * 1000)),
        "exit_code": exit_code,
        "outcome": outcome,
        "result": result,
        "stderr_tail": stderr.text(),
        "stdout_tail": stdout.text(),
        "truncated": stdout.truncated or stderr.truncated,
    }


def main() -> int:
    # The image entrypoint uses env -i; clearing here also protects direct test
    # invocation and makes the contract explicit.
    os.environ.clear()
    raw = sys.stdin.buffer.read(MAX_ENVELOPE_BYTES + 1)
    if len(raw) > MAX_ENVELOPE_BYTES:
        envelope = {
            "duration_ms": 0,
            "exit_code": 1,
            "outcome": "output_limit",
            "result": None,
            "stderr_tail": "runner input envelope exceeded its hard limit",
            "stdout_tail": "",
            "truncated": True,
        }
    else:
        try:
            payload = json.loads(raw.decode("utf-8"))
            if not isinstance(payload, dict):
                raise TypeError("runner envelope must be a JSON object")
            envelope = _run(payload)
        except BaseException as exc:
            envelope = {
                "duration_ms": 0,
                "exit_code": 1,
                "outcome": "infrastructure_error",
                "result": None,
                "stderr_tail": _exception_message(exc),
                "stdout_tail": "",
                "truncated": False,
            }
    sys.stdout.buffer.write(_structured_result(envelope))
    sys.stdout.buffer.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
