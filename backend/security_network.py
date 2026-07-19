import fnmatch
import ipaddress
import os
import socket
from urllib.parse import urlparse


class UnsafeOutboundUrl(ValueError):
    pass


_BLOCKED_METADATA_HOSTS = {
    "metadata.google.internal",
    "metadata.google",
    "instance-data",
}


def _is_enabled(name: str) -> bool:
    return os.getenv(name, "false").strip().lower() in {"1", "true", "yes", "on"}


def _allowed_host_patterns() -> list[str]:
    raw = os.getenv("SRE_OUTBOUND_HOST_ALLOWLIST", "")
    return [item.strip().lower() for item in raw.split(",") if item.strip()]


def _is_explicitly_allowed(hostname: str) -> bool:
    return any(fnmatch.fnmatch(hostname, pattern) for pattern in _allowed_host_patterns())


def _is_unsafe_address(address: str) -> bool:
    ip = ipaddress.ip_address(address)
    return any(
        (
            ip.is_private,
            ip.is_loopback,
            ip.is_link_local,
            ip.is_multicast,
            ip.is_reserved,
            ip.is_unspecified,
        )
    )


def validate_outbound_url(url: str) -> str:
    """Validate operator-configured outbound HTTP destinations.

    Private destinations require an explicit host allowlist entry, or the broader
    development-only SRE_ALLOW_PRIVATE_NETWORK_TARGETS switch. Cloud metadata
    destinations remain blocked in all modes.
    """
    parsed = urlparse((url or "").strip())
    if parsed.scheme not in {"http", "https"}:
        raise UnsafeOutboundUrl("only http and https URLs are allowed")
    if not parsed.hostname:
        raise UnsafeOutboundUrl("URL hostname is required")
    if parsed.username or parsed.password:
        raise UnsafeOutboundUrl("credentials in URLs are not allowed")

    hostname = parsed.hostname.rstrip(".").lower()
    if hostname in _BLOCKED_METADATA_HOSTS:
        raise UnsafeOutboundUrl("cloud metadata destinations are blocked")

    try:
        direct_ip = ipaddress.ip_address(hostname)
    except ValueError:
        direct_ip = None

    if direct_ip and direct_ip.is_link_local:
        raise UnsafeOutboundUrl("link-local destinations are blocked")

    if _is_explicitly_allowed(hostname):
        return url

    try:
        addresses = {
            item[4][0]
            for item in socket.getaddrinfo(hostname, parsed.port or (443 if parsed.scheme == "https" else 80))
        }
    except socket.gaierror as exc:
        raise UnsafeOutboundUrl("URL hostname could not be resolved") from exc

    if not addresses:
        raise UnsafeOutboundUrl("URL hostname did not resolve to an address")

    if not _is_enabled("SRE_ALLOW_PRIVATE_NETWORK_TARGETS"):
        for address in addresses:
            if _is_unsafe_address(address):
                raise UnsafeOutboundUrl("private or local destinations require an explicit allowlist")

    return url
