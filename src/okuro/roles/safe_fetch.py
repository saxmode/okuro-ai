# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: The guarded outbound fetch — resolve first, refuse anything that
#   points inward, and re-check every redirect hop.
# index:
#   imports
#   class UnsafeURL
#   limits + blocked suffixes
#   def _resolved_addresses
#   def _host_is_registered
#   def assert_safe_url
#   def safe_fetch
# AGENT_HEADER_END -->
"""SSRF containment for URLs an AGENT chose.

**The hole this closes.** ``POST /api/roles/knowledge-fetch`` exists so the
daily sweep can prove it read a page: it hands the server a URL and the server
does the HTTP, because an agent that supplies its own body has certified
itself. That design is right and it creates a new problem — the server now
makes outbound requests to addresses chosen by an agent whose whole job is
reading untrusted web pages. A prompt injected into any of those pages reaches
a fetcher running on the loopback interface of the machine that owns the
keyring, the orchestrator API and the LAN.

Validating the scheme, which is all the first version did, stops none of that.
``http://127.0.0.1:13333/api/...``, ``http://192.168.1.1/``,
``http://api.meridian.lan/`` and a public URL that 302s to any of them are
all well-formed https-or-http.

**So the rule is about the ADDRESS, not the string.** The host is resolved
before anything connects and every address it resolves to must be globally
routable. Loopback, private, link-local, reserved, multicast, unspecified and
IPv6 unique-local all refuse. ``is_private`` in the stdlib covers RFC1918 and
fc00::/7 together, which is why the check reads shorter than the list it
enforces.

**Every address, not the first.** A name with an A record for a public host and
a second for 127.0.0.1 is a one-line rebinding attack against a checker that
resolves and takes ``[0]``. All of them have to be clean.

**Redirects are hops, and each one is a new decision.** Auto-follow is off.
The loop below re-validates the target of every ``Location`` before following
it, because a public URL that redirects to ``169.254.169.254`` is the classic
metadata-service fetch and the first hop looks perfect.

**Residual risk, stated rather than implied.** The resolution that validates
and the resolution the socket performs are two different lookups, so a DNS
entry that changes between them is not caught here (classic TOCTOU rebinding).
Closing that properly means connecting to the pinned address with the original
hostname for SNI and certificate validation, which ``urllib`` does not expose.
The mitigations that ARE in place — every address checked, short timeouts, a
body cap, refused redirects, and a port allowlist — make the window small and
the payoff small. It is written down here so the next person weighing an
``httpx`` dependency knows what buying it would buy.
"""

from __future__ import annotations

import ipaddress
import logging
import socket
import time
from urllib.parse import urlsplit

logger = logging.getLogger("okuro.roles.safe_fetch")


class UnsafeURL(ValueError):
    """A URL that must not be fetched, carrying WHY in machine-readable form."""

    def __init__(self, reason: str, detail: str):
        super().__init__(detail)
        self.reason = reason
        self.detail = detail


#: Ports an agent may name without the URL being registered. Everything else is
#: a service, not a document: 13333 is the orchestrator's own API, and a local
#: database or model runner listens on a port of its own. A registered source
#: may use any port, because a human put it in the registry on purpose.
PUBLIC_PORTS: frozenset[int] = frozenset({80, 443})

#: Name suffixes that are internal by construction. The address check below is
#: the real defence — a host on the operator's own LAN domain resolves to a
#: private address and would refuse anyway — but a split-horizon resolver
#: answering with a PUBLIC address for an internal name would slip past it,
#: and this does not.
INTERNAL_SUFFIXES: tuple[str, ...] = (
    ".lan", ".local", ".internal", ".intranet", ".home.arpa", ".localdomain",
)

#: Hostnames refused outright, before any lookup.
INTERNAL_HOSTS: frozenset[str] = frozenset({"localhost", "localhost.localdomain"})

#: How much of a document is worth storing. Five megabytes is roughly forty
#: times the largest body in the seeded registry; past it the fetch is not a
#: specification any more and the store is not the right place for it.
MAX_BODY_BYTES = 5 * 1024 * 1024

#: Whole-operation deadline, redirects included. The per-socket timeout cannot
#: bound this on its own: a server dribbling one byte per second never trips a
#: read timeout and holds the API worker open indefinitely.
MAX_TOTAL_SECONDS = 30.0

#: Per-connection socket timeout, necessarily smaller than the total.
CONNECT_TIMEOUT = 10.0

#: Redirect hops allowed. Three is enough for the http→https→canonical-path
#: chain every real vendor uses and short enough that a redirect loop is not a
#: denial of service.
MAX_REDIRECTS = 3


def _resolved_addresses(host: str) -> list[str]:
    """Every address this name resolves to, or raise ``UnsafeURL``.

    A resolution failure is a refusal rather than a pass-through: "I could not
    tell what this points at" and "this points somewhere safe" must never be
    the same answer.
    """
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise UnsafeURL(
            "unresolvable", f"host {host!r} does not resolve: {exc}"
        ) from exc
    addresses = sorted({info[4][0] for info in infos})
    if not addresses:
        raise UnsafeURL("unresolvable", f"host {host!r} resolved to nothing")
    return addresses


def _host_is_registered(db, host: str, port: int | None) -> bool:
    """Whether a registered source already names this host and port.

    Registered sources are the nine a human seeded plus whatever
    ``knowledge-fetch`` auto-registered — and the auto-registered ones only
    exist because a fetch already passed this check, so they cannot be used to
    widen it after the fact.
    """
    if db is None:
        return False
    try:
        rows = db.fetchall("SELECT url FROM role_structure_sources")
    except Exception:  # noqa: BLE001 — a missing registry is not an allowlist
        return False
    for row in rows:
        parts = urlsplit((row["url"] or "").strip())
        if (parts.hostname or "").lower() == host and parts.port == port:
            return True
    return False


def assert_safe_url(url: str, *, db=None) -> tuple[str, list[str]]:
    """Refuse anything an agent must not be able to make this server fetch.

    Returns ``(host, addresses)`` on success so a caller can log what it
    validated. Raises :class:`UnsafeURL` otherwise — never returns a boolean,
    because a boolean at a call site is one missing ``not`` away from an open
    proxy.
    """
    parts = urlsplit((url or "").strip())

    if parts.scheme not in ("http", "https"):
        raise UnsafeURL(
            "scheme",
            f"only http(s) is fetchable, got {parts.scheme or 'no scheme'!r}",
        )

    host = (parts.hostname or "").lower().rstrip(".")
    if not host:
        raise UnsafeURL("no_host", "the URL names no host")

    if host in INTERNAL_HOSTS or host.endswith(INTERNAL_SUFFIXES):
        raise UnsafeURL("internal_name", f"{host!r} is an internal name")

    try:
        port = parts.port
    except ValueError as exc:  # a port that is not a number
        raise UnsafeURL("bad_port", f"unreadable port in {url!r}") from exc
    if port is not None and port not in PUBLIC_PORTS:
        if not _host_is_registered(db, host, port):
            raise UnsafeURL(
                "port",
                f"port {port} is not a public web port and {host!r}:{port} is "
                f"not a registered source",
            )

    addresses = _resolved_addresses(host)
    for address in addresses:
        try:
            ip = ipaddress.ip_address(address)
        except ValueError as exc:  # pragma: no cover - getaddrinfo returned junk
            raise UnsafeURL("unresolvable", f"{address!r} is not an address") from exc
        # is_global is the positive form of every case below and is what we
        # actually require; the individual flags are kept in the message so a
        # refusal says which kind of inward it was.
        if not ip.is_global or ip.is_loopback or ip.is_private or ip.is_reserved:
            kind = (
                "loopback" if ip.is_loopback
                else "link-local" if ip.is_link_local
                else "private" if ip.is_private
                else "reserved"
            )
            raise UnsafeURL(
                "address",
                f"{host!r} resolves to {address} ({kind}) — this server does "
                f"not fetch addresses that point inward",
            )
    return host, addresses


def safe_fetch(url: str, *, db=None, user_agent: str | None = None, opener=None):
    """One guarded GET. Redirects are followed BY HAND, one validation each.

    Returns the same ``HttpResponse`` the poller's transport returns, so the
    body lands in the store through exactly the same path.

    ``opener`` is a test seam and nothing else. It exists because the property
    that matters most here — that hop two is validated as strictly as hop one —
    cannot be demonstrated against the real internet without finding a public
    site that redirects into a private range, and a security control nobody can
    test is a security control nobody can trust.
    """
    import urllib.error
    import urllib.request

    from .source_poll import USER_AGENT, HttpResponse

    class _NoRedirects(urllib.request.HTTPRedirectHandler):
        """Turn a redirect into an HTTPError instead of following it.

        The whole point: urllib's default handler follows the Location header
        before any of this module's code runs again, so the second hop would be
        fetched by a validator that only ever saw the first.
        """

        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None

    opener = opener or urllib.request.build_opener(_NoRedirects)
    deadline = time.monotonic() + MAX_TOTAL_SECONDS
    current = (url or "").strip()
    seen: list[str] = []

    for hop in range(MAX_REDIRECTS + 1):
        host, _addresses = assert_safe_url(current, db=db)
        seen.append(current)

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise UnsafeURL(
                "timeout",
                f"exceeded the {MAX_TOTAL_SECONDS}s budget after {hop} hop(s)",
            )

        request = urllib.request.Request(
            current, headers={"User-Agent": user_agent or USER_AGENT}
        )
        try:
            with opener.open(
                request, timeout=min(CONNECT_TIMEOUT, remaining)
            ) as response:
                # Read ONE byte past the cap so an oversize body is detected
                # rather than silently truncated into the evidence store.
                raw = response.read(MAX_BODY_BYTES + 1)
                if len(raw) > MAX_BODY_BYTES:
                    raise UnsafeURL(
                        "too_large",
                        f"body from {host!r} exceeds {MAX_BODY_BYTES} bytes",
                    )
                return HttpResponse(
                    status=int(response.status),
                    body=raw.decode("utf-8", "replace"),
                    final_url=response.geturl(),
                    headers=dict(response.headers.items()),
                )
        except urllib.error.HTTPError as exc:
            location = exc.headers.get("Location") if exc.headers else None
            if exc.code in (301, 302, 303, 307, 308) and location:
                # Resolve the hop against the URL it came from, then go round
                # and validate it exactly as if the agent had named it.
                from urllib.parse import urljoin

                current = urljoin(current, location)
                continue
            body = ""
            try:
                body = exc.read(MAX_BODY_BYTES).decode("utf-8", "replace")
            except Exception:  # noqa: BLE001 — a body-less error is normal
                pass
            return HttpResponse(
                status=int(exc.code),
                body=body,
                final_url=getattr(exc, "url", current) or current,
                headers=dict(exc.headers.items()) if exc.headers else {},
                error=f"HTTP {exc.code}",
            )

    raise UnsafeURL(
        "too_many_redirects",
        f"more than {MAX_REDIRECTS} redirects: {' -> '.join(seen)}",
    )
