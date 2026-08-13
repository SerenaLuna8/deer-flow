"""Shared URL safety checks for server-side web tools."""

from __future__ import annotations

import ipaddress
import socket
from collections.abc import Callable
from ipaddress import IPv4Address, IPv6Address, ip_address, ip_network
from urllib.parse import urlparse

_BLOCKED_HOSTNAMES = {"localhost", "metadata.google.internal"}
_NAT64_PREFIX = ip_network("64:ff9b::/96")


def decode_ipv4_literal(host: str) -> IPv4Address | None:
    """Decode integer, hex, octal, and shortened IPv4 URL host forms."""

    parts = host.split(".")
    if not 1 <= len(parts) <= 4:
        return None

    values: list[int] = []
    for part in parts:
        if not part:
            return None
        try:
            if part.startswith(("0x", "0X")):
                values.append(int(part, 16))
            elif part.startswith("0") and len(part) > 1:
                values.append(int(part, 8))
            else:
                values.append(int(part, 10))
        except ValueError:
            return None

    *leading, last = values
    if any(not 0 <= value <= 0xFF for value in leading):
        return None
    max_last = (1 << (8 * (4 - len(leading)))) - 1
    if not 0 <= last <= max_last:
        return None

    result = 0
    for value in leading:
        result = (result << 8) | value
    result = (result << (8 * (4 - len(leading)))) | last
    return IPv4Address(result)


def embedded_ipv4_address(address: IPv6Address) -> IPv4Address | None:
    """Extract an IPv4 address carried by common IPv6 transition formats."""

    if address.ipv4_mapped is not None:
        return address.ipv4_mapped
    if address.sixtofour is not None:
        return address.sixtofour
    if address in _NAT64_PREFIX:
        return IPv4Address(int(address) & 0xFFFFFFFF)
    packed = int(address)
    if packed >> 32 == 0 and packed > 1:
        return IPv4Address(packed & 0xFFFFFFFF)
    return None


def _parse_http_url(value: object) -> tuple[str, str] | None:
    if not isinstance(value, str):
        return None
    url = value.strip()
    try:
        parsed = urlparse(url)
        hostname = parsed.hostname
        # Accessing port is deliberate: malformed ports otherwise survive
        # parsing and fail later in a provider-specific way.
        _ = parsed.port
    except ValueError:
        return None
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or not hostname:
        return None
    normalized_host = hostname.strip().rstrip(".").lower()
    if not normalized_host:
        return None
    return url, normalized_host


def _literal_address(host: str) -> IPv4Address | IPv6Address | None:
    try:
        return ip_address(host)
    except ValueError:
        return decode_ipv4_literal(host)


def is_url_value_present(value: object) -> bool:
    """Return whether a provider supplied a non-empty URL field."""

    return isinstance(value, str) and bool(value.strip())


def sanitize_public_http_reference_url(value: object) -> str:
    """Return a safe public URL reference without performing DNS resolution.

    Search providers return references rather than fetching them locally. This
    guard rejects unsafe literal forms at that output boundary; a later
    downloader must still call :func:`validate_public_http_url` and revalidate
    every redirect target using resolved addresses.
    """

    parsed = _parse_http_url(value)
    if parsed is None:
        return ""
    url, host = parsed
    if host in _BLOCKED_HOSTNAMES or host.endswith(".localhost"):
        return ""
    literal = _literal_address(host)
    if literal is not None and is_blocked_address(literal):
        return ""
    return url


def resolve_host_addresses(hostname: str) -> list[ipaddress._BaseAddress]:
    """Resolve a hostname to all IP addresses for SSRF screening."""
    addresses: list[ipaddress._BaseAddress] = []
    try:
        infos = socket.getaddrinfo(hostname, None)
    except (socket.gaierror, UnicodeError):
        return addresses
    for info in infos:
        sockaddr = info[4]
        try:
            addresses.append(ipaddress.ip_address(sockaddr[0]))
        except ValueError:
            continue
    return addresses


def is_blocked_address(address: ipaddress._BaseAddress) -> bool:
    """Return True for addresses web tools should not reach by default."""

    if isinstance(address, IPv6Address):
        embedded = embedded_ipv4_address(address)
        if embedded is not None and not embedded.is_global:
            return True
    return not address.is_global


def validate_public_http_url(
    url: str,
    *,
    allow_private_addresses: bool = False,
    action: str = "fetch",
    resolver: Callable[[str], list[ipaddress._BaseAddress]] | None = None,
) -> str | None:
    """Validate an http(s) URL before a server-side web tool fetches it.

    Returns an ``"Error: ..."`` string when the URL should be rejected, or
    ``None`` when the caller may proceed.  The check is intentionally conservative
    for self-hosted fetch/render services because those services run inside the
    deployment network and can otherwise reach cloud metadata or private hosts.
    """
    parsed = _parse_http_url(url)
    if parsed is None:
        return "Error: Only http:// and https:// URLs are supported"

    if allow_private_addresses:
        return None

    _, normalized_host = parsed
    if normalized_host in _BLOCKED_HOSTNAMES or normalized_host.endswith(".localhost"):
        return f"Error: Refusing to {action} a private or loopback address"

    literal_ip = _literal_address(normalized_host)

    if literal_ip is not None:
        candidates = [literal_ip]
    else:
        resolve = resolver or resolve_host_addresses
        candidates = resolve(normalized_host)
        if not candidates:
            return "Error: URL host could not be resolved"

    if any(is_blocked_address(addr) for addr in candidates):
        return f"Error: Refusing to {action} a private, loopback, or metadata address"
    return None
