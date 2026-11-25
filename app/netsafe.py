"""SSRF guard. Search results and links found on scraped pages are untrusted input: before the app (or the
local Firecrawl it drives) fetches one, make sure it points at the public internet and not at this server's
own services (SearXNG :8888, Firecrawl :3002, the app, cloud metadata at 169.254.169.254, the private VCN)."""
from __future__ import annotations

import asyncio
import ipaddress
import socket
from urllib.parse import urlparse

BLOCKED_SUFFIXES = (".local", ".localhost", ".internal", ".lan", ".home", ".corp", ".intranet", ".oraclevcn.com")
_dns_cache: dict[str, bool] = {}


def _ip_ok(ip: str) -> bool:
    try:
        a = ipaddress.ip_address(ip.split("%")[0])
    except ValueError:
        return False
    return a.is_global and not a.is_multicast


async def _resolve(host: str) -> list[str]:
    infos = await asyncio.get_running_loop().getaddrinfo(host, None, type=socket.SOCK_STREAM)
    return [i[4][0] for i in infos]


def syntactically_public(url: str) -> bool:
    """Cheap checks with no DNS: scheme, port, host shape."""
    try:
        p = urlparse(url)
    except ValueError:
        return False
    if p.scheme not in ("http", "https") or not p.hostname or p.username or p.password:
        return False
    if p.port not in (None, 80, 443, 8080, 8443):
        return False
    h = p.hostname.lower().rstrip(".")
    if h == "localhost" or h.endswith(BLOCKED_SUFFIXES) or "." not in h:
        return False
    try:
        return _ip_ok(str(ipaddress.ip_address(h)))       # literal IP: must itself be global
    except ValueError:
        return True


async def is_public_url(url: str) -> bool:
    """Syntax checks plus: every address the hostname resolves to is a global unicast address."""
    if not syntactically_public(url):
        return False
    h = urlparse(url).hostname.lower().rstrip(".")
    try:
        ipaddress.ip_address(h)
        return True                                        # literal IP already checked above
    except ValueError:
        pass
    if h in _dns_cache:
        return _dns_cache[h]
    try:
        ips = await _resolve(h)
        ok = bool(ips) and all(_ip_ok(ip) for ip in ips)
    except (socket.gaierror, UnicodeError, OSError):
        ok = False
    if len(_dns_cache) > 5000:
        _dns_cache.clear()
    _dns_cache[h] = ok
    return ok
