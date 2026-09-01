"""Deny-default OS launcher. Missing/disabled isolation never falls back to Python."""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

from .contracts import ExtractionError


def _readonly_roots() -> tuple[Path, ...]:
    roots = [Path(sys.prefix), Path(sys.base_prefix), Path(sys.base_prefix).resolve(), Path(__file__).resolve().parents[2]]
    from .runtime_resources import parser_resource_roots

    roots.extend(parser_resource_roots())
    return tuple(dict.fromkeys(roots))


def parser_environment(work_dir: Path) -> dict[str, str]:
    child = str(work_dir.resolve() / "child")
    locale = "en_US.UTF-8" if sys.platform == "darwin" else "C.UTF-8"
    return {
        "HOME": child,
        "TMPDIR": child,
        "TMP": child,
        "TEMP": child,
        "LANG": locale,
        "LC_ALL": locale,
        "PATH": str(Path(sys.executable).parent) + ":/usr/bin:/bin",
        "PYTHONPATH": str(Path(__file__).resolve().parents[2]),
        "PYTHONNOUSERSITE": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "DO_NOT_TRACK": "1",
        "SCARF_NO_ANALYTICS": "true",
        "HF_HUB_OFFLINE": "1",
        "TRANSFORMERS_OFFLINE": "1",
        "TOKENIZERS_PARALLELISM": "false",
        "OMP_NUM_THREADS": "1",
        "OPENBLAS_NUM_THREADS": "1",
    }


def sandbox_command(command: list[str], *, work_dir: Path) -> list[str]:
    root = work_dir.resolve()
    roots = _readonly_roots()
    if sys.platform == "darwin":
        executable = Path("/usr/bin/sandbox-exec")
        ancestors = tuple(dict.fromkeys(parent for path in (*roots, root / "source", root / "child") for parent in path.parents))
        if not executable.is_file() or len(roots) > 12 or len(ancestors) > 48:
            raise ExtractionError("PARSER_SANDBOX_UNAVAILABLE")
        argv = [str(executable)]
        for index in range(12):
            path = roots[index] if index < len(roots) else roots[0]
            argv.extend(["-D", f"READ{index}={path}"])
        for index in range(48):
            path = ancestors[index] if index < len(ancestors) else Path("/")
            argv.extend(["-D", f"ANCESTOR{index}={path}"])
        argv.extend(["-D", f"SOURCE={root / 'source'}", "-D", f"CHILD={root / 'child'}"])
        return [*argv, "-f", str(Path(__file__).with_name("sandbox-macos.sb")), *command]
    if sys.platform == "linux":
        executable = shutil.which("bwrap", path="/usr/bin:/bin")
        if executable is None:
            raise ExtractionError("PARSER_SANDBOX_UNAVAILABLE")
        argv = [executable, "--unshare-user", "--unshare-net", "--unshare-pid", "--unshare-ipc", "--unshare-uts", "--die-with-parent", "--proc", "/proc", "--dev", "/dev"]
        for path in (*roots, Path("/usr/lib"), Path("/usr/lib64"), Path("/lib"), Path("/lib64"), Path("/etc/ld.so.cache"), Path("/etc/localtime")):
            if path.exists():
                argv.extend(["--ro-bind", str(path), str(path)])
        argv.extend(["--ro-bind", str(root / "source"), str(root / "source"), "--bind", str(root / "child"), str(root / "child"), "--chdir", str(root / "child"), "--remount-ro", "/"])
        if "--output-fd" in command:
            descriptor = int(command[command.index("--output-fd") + 1])
            argv.extend(["--preserve-fds", str(descriptor - 2)])
        return [*argv, "--", *command]
    raise ExtractionError("PARSER_SANDBOX_UNAVAILABLE")
