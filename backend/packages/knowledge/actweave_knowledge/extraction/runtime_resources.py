"""Audited parsing resources. Build fingerprints are independent of ingestion tokenizers.

The allowlist is package-scoped; never scan a virtualenv, download a resource,
consult an API, or load an NLP model while advertising capabilities.
"""

from __future__ import annotations

import ctypes
import hashlib
import json
import os
import platform
import stat
import subprocess
from functools import lru_cache
from importlib import metadata
from pathlib import Path

from .contracts import ExtractionError

_LOCK_PATH = Path(__file__).with_name("resources.lock.json")
ADAPTER_REVISION = "adapter-v1"
NETWORK_POLICY = "local-input-spacy-load-only-pandoc-pinned-v1;os-deny-required-v1"
PACKAGES = (
    "beautifulsoup4",
    "charset-normalizer",
    "en-core-web-sm",
    "langdetect",
    "lxml",
    "markdown",
    "markdown-it-py",
    "numpy",
    "olefile",
    "openpyxl",
    "pandas",
    "pillow",
    "pypandoc-binary",
    "pypdfium2",
    "python-docx",
    "python-magic",
    "python-oxmsg",
    "python-pptx",
    "spacy",
    "thinc",
    "unstructured",
    "xlrd",
)
# These scopes are the reviewed resource/code branches, not arbitrary dist contents.
# All spaCy model assets are necessary; no .pyc, installation metadata or training
# corpus is fingerprinted. Native codecs and NLP extensions differ by platform.
RESOURCE_SCOPES = {
    "en-core-web-sm": ("en_core_web_sm/",),
    "spacy": ("spacy/lang/en/", "spacy/tokenizer.", "spacy/language.py", "spacy/util.py", "spacy/pipeline/", "spacy/tokens/"),
    "unstructured": ("unstructured/nlp/", "unstructured/partition/", "unstructured/file_utils/", "unstructured/documents/", "unstructured/chunking/"),
    "pypandoc-binary": ("pypandoc/files/pandoc", "pypandoc/__init__.py"),
    "pillow": ("PIL/", "pillow.libs/"),
    "pypdfium2": ("pypdfium2_raw/",),
    "lxml": ("lxml/etree.",),
    "python-magic": ("magic/",),
}
_LOCAL_IDS = frozenset(f"unstructured.{name}" for name in ("pptx", "epub", "markdown", "eml", "msg", "xml"))
_LOCAL_PACKAGES = {"unstructured", "spacy", "en-core-web-sm", "thinc", "python-magic", "lxml", "langdetect"}
_PARSER_PACKAGES = {
    "builtin.text": {"charset-normalizer"},
    "builtin.markdown": {"charset-normalizer", "markdown-it-py"},
    "builtin.html": {"charset-normalizer", "beautifulsoup4"},
    "builtin.csv": {"charset-normalizer"},
    "builtin.word": {"python-docx", "lxml", "pillow"},
    "builtin.pdf": {"pypdfium2", "pillow"},
    "builtin.excel": {"openpyxl", "pandas", "numpy", "xlrd", "pillow"},
    **{
        name: _LOCAL_PACKAGES | extra
        for name, extra in (
            ("unstructured.pptx", {"python-pptx"}),
            ("unstructured.epub", {"pypandoc-binary"}),
            ("unstructured.markdown", {"markdown", "markdown-it-py", "charset-normalizer"}),
            ("unstructured.eml", set()),
            ("unstructured.msg", {"python-oxmsg", "olefile"}),
            ("unstructured.xml", set()),
        )
    },
}


def _platform_key() -> str:
    return f"{platform.system().lower()}-{platform.machine().lower()}"


def _system_paths() -> dict[str, Path]:
    if platform.system() == "Darwin":
        prefix = Path("/opt/homebrew/opt/libmagic") if platform.machine() == "arm64" else Path("/usr/local/opt/libmagic")
        return {"system/libmagic/library": prefix / "lib/libmagic.1.dylib", "system/libmagic/database": prefix / "share/misc/magic.mgc"}
    arch = {"x86_64": "x86_64-linux-gnu", "aarch64": "aarch64-linux-gnu"}.get(platform.machine(), platform.machine() + "-linux-gnu")
    candidates = [Path("/usr/lib") / arch / "libmagic.so.1", Path("/usr/lib/libmagic.so.1"), Path("/lib") / arch / "libmagic.so.1"]
    return {"system/libmagic/library": next((p for p in candidates if p.is_file()), candidates[0]), "system/libmagic/database": Path("/usr/share/misc/magic.mgc")}


def _resource_path(logical_name: str) -> Path:
    if logical_name.startswith("system/"):
        return _system_paths()[logical_name]
    package, relative = logical_name.split("/", 1)
    if package not in RESOURCE_SCOPES or ".." in Path(relative).parts or Path(relative).is_absolute():
        raise ValueError("invalid logical resource")
    return Path(metadata.distribution(package).locate_file(relative))


def _resource_names() -> list[str]:
    names = list(_system_paths())
    for package, prefixes in RESOURCE_SCOPES.items():
        try:
            files = metadata.distribution(package).files or ()
        except metadata.PackageNotFoundError:
            continue
        for file in files:
            value = str(file)
            if not value.startswith(prefixes) or "__pycache__" in value or value.endswith((".pyc", ".pxd", ".pyx", ".h", ".cpp")):
                continue
            if package == "pillow" and not (value.endswith((".so", ".dylib", ".dll")) or value.endswith("Image.py") or value.endswith("features.py")):
                continue
            names.append(f"{package}/{value}")
    return sorted(set(names))


@lru_cache(maxsize=4096)
def _hash_bytes(path: str, identity: tuple[int, ...]) -> str:
    # Cache only immutable file identities; replacing/tampering a resource
    # changes stat identity and is rehashed before every parser invocation.
    with open(path, "rb") as file:
        return hashlib.file_digest(file, "sha256").hexdigest()


def _file_hash(path: Path) -> str:
    resolved = path.resolve(strict=True)
    info = resolved.stat()
    if not stat.S_ISREG(info.st_mode):
        raise ValueError("resource must be a regular file")
    return _hash_bytes(str(resolved), (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns))


def _version(name: str) -> str | None:
    try:
        return metadata.version(name)
    except metadata.PackageNotFoundError:
        return None


def _canonical(value: dict) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def runtime_manifest() -> dict:
    resources = []
    for logical_name in _resource_names():
        try:
            digest = _file_hash(_resource_path(logical_name))
        except (OSError, ValueError, metadata.PackageNotFoundError):
            digest = None
        resources.append({"logical_name": logical_name, "sha256": digest})
    return {
        "format_version": 1,
        "platform": _platform_key(),
        "adapter_revision": ADAPTER_REVISION,
        "network_policy": NETWORK_POLICY,
        "packages": [{"name": name, "version": _version(name)} for name in sorted(PACKAGES)],
        "resources": resources,
    }


def runtime_digest() -> str:
    return hashlib.sha256(_canonical(runtime_manifest())).hexdigest()


def _locked_manifest() -> dict | None:
    try:
        lock = json.loads(_LOCK_PATH.read_text())
        if not isinstance(lock, dict) or type(lock.get("format_version")) is not int or lock["format_version"] != 1:
            return None
        platforms = lock.get("platforms")
        if not isinstance(platforms, dict):
            return None
        manifest = platforms.get(_platform_key())
        if not isinstance(manifest, dict) or not isinstance(manifest.get("packages"), list) or not isinstance(manifest.get("resources"), list):
            return None
        for item in manifest["packages"]:
            if not isinstance(item, dict) or not isinstance(item.get("name"), str) or not isinstance(item.get("version"), str):
                return None
        for item in manifest["resources"]:
            if not isinstance(item, dict) or not isinstance(item.get("logical_name"), str) or not isinstance(item.get("sha256"), str):
                return None
        return manifest
    except (OSError, ValueError, KeyError, TypeError):
        return None


def probe_parser_resources(parser_id: str) -> str | None:
    """Fail closed on unverified platforms or missing/tampered required resources."""
    unavailable = "PARSER_DEPENDENCY_UNAVAILABLE"
    required = _PARSER_PACKAGES.get(parser_id)
    expected = _locked_manifest()
    if required is None or expected is None:
        return unavailable
    if expected.get("network_policy") != NETWORK_POLICY or expected.get("adapter_revision") != ADAPTER_REVISION:
        return unavailable
    pins = {p["name"]: p["version"] for p in expected["packages"]}
    for name in required:
        if _version(name) is None or _version(name) != pins.get(name):
            return unavailable
    for resource in expected["resources"]:
        logical = resource["logical_name"]
        owner = logical.split("/", 1)[0]
        if owner not in required and not (owner == "system" and parser_id in _LOCAL_IDS):
            continue
        try:
            if not resource["sha256"] or _file_hash(_resource_path(logical)) != resource["sha256"]:
                return unavailable
        except (OSError, ValueError, KeyError, metadata.PackageNotFoundError):
            return unavailable
    # Every resource-bearing package must actually have a non-empty lock scope.
    owners = {r["logical_name"].split("/", 1)[0] for r in expected["resources"]}
    if (required & RESOURCE_SCOPES.keys()) - owners:
        return unavailable
    if parser_id in _LOCAL_IDS and "system" not in owners:
        return unavailable
    return None


def _load_local_spacy_model():
    """Exact replacement for Unstructured 0.21.5's auto-installing loader."""
    import spacy

    try:
        return spacy.load("en_core_web_sm")
    except (OSError, ImportError, ValueError):
        raise ExtractionError("PARSER_DEPENDENCY_UNAVAILABLE") from None


class _DlInfo(ctypes.Structure):
    _fields_ = [("filename", ctypes.c_char_p), ("base", ctypes.c_void_p), ("symbol_name", ctypes.c_char_p), ("symbol_address", ctypes.c_void_p)]


def _loaded_library_path(library) -> Path:
    # Resolve the library that owns the actual loaded symbol. ctypes._name can
    # be only a Linux soname and is not evidence of which file was loaded.
    resolve = ctypes.CDLL(None).dladdr
    resolve.argtypes = (ctypes.c_void_p, ctypes.POINTER(_DlInfo))
    resolve.restype = ctypes.c_int
    info = _DlInfo()
    if not resolve(ctypes.cast(library.magic_version, ctypes.c_void_p), ctypes.byref(info)) or not info.filename:
        raise ExtractionError("PARSER_DEPENDENCY_UNAVAILABLE")
    path = Path(os.fsdecode(info.filename))
    if not path.is_absolute():
        raise ExtractionError("PARSER_DEPENDENCY_UNAVAILABLE")
    return path


def prepare_local_parser(parser_id: str) -> None:
    """Run only in the parsing child, before importing any partition module.

    The narrow loader override disables the inspected installer branch even if
    loading fails after the hash preflight. It is not the OS network sandbox.
    """
    if parser_id not in _LOCAL_IDS or probe_parser_resources(parser_id):
        raise ExtractionError("PARSER_DEPENDENCY_UNAVAILABLE")
    try:
        import magic

        # python-magic loader selection must resolve the same bytes we checked.
        if _file_hash(_loaded_library_path(magic.libmagic)) != _file_hash(_resource_path("system/libmagic/library")):
            raise ExtractionError("PARSER_DEPENDENCY_UNAVAILABLE")
        os.environ["MAGIC"] = str(_resource_path("system/libmagic/database"))
        import unstructured.nlp.tokenize as tokenize

        tokenize._load_spacy_model = _load_local_spacy_model
        if parser_id == "unstructured.epub":
            import pypandoc

            binary = str(_resource_path("pypandoc-binary/pypandoc/files/pandoc"))
            os.environ["PYPANDOC_PANDOC"] = binary
            # A reused process might have cached a different executable. Pinned
            # pypandoc's documented clean helper clears that exact path cache.
            pypandoc.clean_pandocpath_cache()
            if pypandoc.get_pandoc_path() != binary:
                raise ExtractionError("PARSER_DEPENDENCY_UNAVAILABLE")
    except (ImportError, OSError, ValueError, AttributeError):
        raise ExtractionError("PARSER_DEPENDENCY_UNAVAILABLE") from None


def build_manifest() -> dict:
    """Build-time only native probes; output contains versions, never host paths."""
    manifest = runtime_manifest()
    if any(not p["version"] for p in manifest["packages"]) or any(not r["sha256"] for r in manifest["resources"]):
        raise ExtractionError("PARSER_DEPENDENCY_UNAVAILABLE")
    magic = ctypes.CDLL(str(_resource_path("system/libmagic/library")))
    magic.magic_version.restype = ctypes.c_int
    binary = _resource_path("pypandoc-binary/pypandoc/files/pandoc")
    result = subprocess.run([str(binary), "--version"], capture_output=True, text=True, check=True, timeout=10)
    version = result.stdout.splitlines()[0]
    if not version.startswith("pandoc "):
        raise ExtractionError("PARSER_DEPENDENCY_UNAVAILABLE")
    # Version evidence complements binary hashes, and is deliberately outside
    # the runtime fingerprint (which hashes the same binary on each platform).
    return {"format_version": 1, "platforms": {_platform_key(): manifest}, "native_build_probes": {_platform_key(): {"libmagic_api_version": magic.magic_version(), "pandoc": version}}}


def parser_resource_roots() -> tuple[Path, ...]:
    """Additional read-only filesystem roots for the child OS sandbox.

    Python distributions are under the virtualenv, already mounted read-only
    by the caller. Return aliases as well as resolved library/database roots,
    because macOS sandbox path resolution observes both sides of symlinks.
    """
    paths = _system_paths()
    roots = {directory for path in paths.values() for directory in (path.parent, path.resolve().parent)}
    if platform.system() == "Darwin":
        # Audited python-magic loader searches this Homebrew link directory.
        roots.add(Path("/opt/homebrew/lib") if platform.machine() == "arm64" else Path("/usr/local/lib"))
    return tuple(sorted(roots, key=str))
