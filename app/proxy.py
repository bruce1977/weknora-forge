"""Reverse-proxy awareness: HTTPS is terminated in front of an HTTP origin.

The deployment shape this module is written for::

    public client --https--> Cloudflare --http--> forge --http--> WeKnora
    internal job  -------------------http------> forge

Forge itself always speaks plain HTTP. Two facts therefore have to be reconstructed
from headers - and only when the *immediate peer* is a trusted proxy:

1. **the request scheme.** Starlette builds absolute URLs - most visibly the 307
   trailing-slash redirect, and the Swagger UI links - from the scope scheme. Without
   correction an HTTPS visitor gets bounced back to ``http://...``.
2. **the real client IP**, used for logging and re-emitted to WeKnora as
   ``X-Forwarded-For``. ``CF-Connecting-IP`` wins when present, because Cloudflare
   always overwrites it while ``X-Forwarded-For`` can carry client-supplied junk.

Everything else already survives the scheme boundary: the HMAC second factor signs
``METHOD + FULL_PATH`` and never the scheme or the host, so a signature produced for
``https://forge.example.com/api/v2/publish`` verifies unchanged on the internal
``http://forge:8000/api/v2/publish`` call.

Trust is deliberately conservative: a caller that is NOT a configured proxy may claim
anything it likes in ``X-Forwarded-*`` / ``CF-*`` and Forge ignores all of it. Use
``["*"]`` only when the listening port is unreachable except through the proxy.
"""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Set

from fastapi import Request
from starlette.types import ASGIApp, Receive, Scope, Send

# Scope keys - read them through the accessors below instead of hard-coding names.
SCOPE_CLIENT_IP = "forge.client_ip"
SCOPE_PEER_IP = "forge.peer_ip"
SCOPE_TRUSTED = "forge.trusted_peer"
SCOPE_PROXY = "forge.proxy"

_VALID_SCHEMES = {"http", "https", "ws", "wss"}

# Hardcoded proxy defaults (formerly configurable via config.json proxy node)
_TRUSTED_PROXIES = ["127.0.0.1", "::1", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"]
_CLIENT_IP_HEADERS = ["cf-connecting-ip", "x-real-ip"]

# Proxy / CDN bookkeeping headers. They are never forwarded verbatim to WeKnora:
# Forge re-emits its own, so a public caller cannot forge the origin's view of the
# world (a spoofed X-Forwarded-For would otherwise land straight in WeKnora's logs).
PROXY_HEADERS = frozenset(
    {
        "forwarded",
        "x-forwarded-for",
        "x-forwarded-proto",
        "x-forwarded-host",
        "x-forwarded-port",
        "x-forwarded-server",
        "x-real-ip",
        "x-client-ip",
        "true-client-ip",
        "cf-connecting-ip",
        "cf-connecting-ipv6",
        "cf-ipcountry",
        "cf-ray",
        "cf-visitor",
        "cf-worker",
        "cdn-loop",
    }
)


def is_proxy_header(name: str) -> bool:
    """True for headers a reverse proxy / CDN may have injected."""
    lowered = (name or "").lower()
    return lowered in PROXY_HEADERS or lowered.startswith("cf-")


def _first_value(raw: Optional[str]) -> str:
    """Take the leading element of a comma separated header value."""
    if not raw:
        return ""
    return raw.split(",", 1)[0].strip().strip('"')


class TrustedProxies:
    """Membership test over the configured trusted peers: ``*``, IPs, CIDRs, names."""

    def __init__(self, entries: Sequence[str]) -> None:
        self.allow_all = False
        self._names: Set[str] = set()
        self._networks: List[Any] = []
        for raw in entries or ():
            value = str(raw).strip()
            if not value:
                continue
            if value == "*":
                self.allow_all = True
                continue
            try:
                self._networks.append(ipaddress.ip_network(value, strict=False))
            except ValueError:
                # Hostnames are matched literally (docker service names, unix sockets)
                self._names.add(value.lower())

    def __contains__(self, host: str) -> bool:
        if not host:
            return False
        if self.allow_all:
            return True
        if host.lower() in self._names:
            return True
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            return False
        return any(address in network for network in self._networks)


def _header_map(scope: Scope) -> Dict[str, str]:
    """Lower-cased header map; repeated headers are comma joined like the HTTP spec wants."""
    headers: Dict[str, str] = {}
    for key, value in scope.get("headers") or ():
        name = key.decode("latin-1").lower()
        text = value.decode("latin-1").strip()
        headers[name] = f"{headers[name]},{text}" if name in headers else text
    return headers


@dataclass
class ProxyInfo:
    """What the middleware decided for one request (exposed for logs / whoami)."""

    peer_ip: str = ""
    client_ip: str = ""
    scheme: str = "http"
    trusted_proxy: bool = False
    forwarded_proto: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return {
            "peer_ip": self.peer_ip,
            "client_ip": self.client_ip,
            "scheme": self.scheme,
            "trusted_proxy": self.trusted_proxy,
            "forwarded_proto": self.forwarded_proto,
        }


class ProxyHeadersMiddleware:
    """Rebuild scheme / client IP from forwarding headers of a trusted peer."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app
        self.trusted = TrustedProxies(_TRUSTED_PROXIES)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] in ("http", "websocket"):
            self._apply(scope)
        await self.app(scope, receive, send)

    def _apply(self, scope: Scope) -> None:
        peer = scope.get("client") or ("", 0)
        peer_ip = str(peer[0] or "")
        info = ProxyInfo(
            peer_ip=peer_ip,
            client_ip=peer_ip,
            scheme=str(scope.get("scheme") or "http"),
        )
        scope[SCOPE_PEER_IP] = peer_ip

        if peer_ip not in self.trusted:
            # Direct caller: nothing it says about itself is believable.
            scope[SCOPE_TRUSTED] = False
            scope[SCOPE_CLIENT_IP] = peer_ip
            scope[SCOPE_PROXY] = info
            return

        headers = _header_map(scope)
        info.trusted_proxy = True
        info.forwarded_proto = _first_value(headers.get("x-forwarded-proto"))
        if info.forwarded_proto in _VALID_SCHEMES:
            scope["scheme"] = info.forwarded_proto
            info.scheme = info.forwarded_proto

        client_ip = self._resolve_client_ip(headers) or peer_ip
        info.client_ip = client_ip
        scope[SCOPE_CLIENT_IP] = client_ip
        if client_ip != peer_ip:
            scope["client"] = (client_ip, peer[1] if len(peer) > 1 else 0)

        scope[SCOPE_TRUSTED] = True
        scope[SCOPE_PROXY] = info

    def _resolve_client_ip(self, headers: Dict[str, str]) -> str:
        for name in _CLIENT_IP_HEADERS:
            value = _first_value(headers.get(name.lower()))
            if value:
                return value
        return self._resolve_xff(headers.get("x-forwarded-for", ""))

    def _resolve_xff(self, raw: str) -> str:
        """Rightmost entry that is not itself a trusted proxy (spoof resistant)."""
        chain = [part.strip() for part in raw.split(",") if part.strip()]
        for candidate in reversed(chain):
            if candidate not in self.trusted:
                return candidate
        return chain[0] if chain else ""


# --------------------------------------------------------------------------- #
# Accessors
# --------------------------------------------------------------------------- #
def client_ip(request: Request) -> str:
    """Real caller address (proxy aware)."""
    value = request.scope.get(SCOPE_CLIENT_IP)
    if value:
        return str(value)
    return request.client.host if request.client else ""


def request_scheme(request: Request) -> str:
    """``https`` when the caller reached us through an HTTPS terminating proxy."""
    return request.scope.get("scheme") or request.url.scheme


def forwarded_headers(request: Request) -> Dict[str, str]:
    """Freshly built ``X-Forwarded-*`` describing the real caller, for WeKnora."""
    headers: Dict[str, str] = {"X-Forwarded-Proto": request_scheme(request)}
    ip = client_ip(request)
    if ip:
        headers["X-Forwarded-For"] = ip
    host = request.headers.get("host", "")
    if host:
        headers["X-Forwarded-Host"] = host
    return headers
