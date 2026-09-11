# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: FastAPI middleware that sets baseline security hardening headers on every response
# index:
#   class SecurityHeadersMiddleware
#   SECURITY_HEADERS (module-level mapping)
# AGENT_HEADER_END -->
"""Security headers middleware for the Okuro orchestrator API.

Sets CSP, X-Frame-Options, X-Content-Type-Options, Referrer-Policy and
Permissions-Policy on every response. Registered AFTER the bearer auth
middleware so unauthenticated 401 responses also carry the headers.
"""
from __future__ import annotations

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response


SECURITY_HEADERS: dict[str, str] = {
    # pywebview injects its js_api bridge into every loaded page by calling
    # `new Function(params, funcBody)` at runtime — which is governed by
    # script-src and requires 'unsafe-eval'. Without it, window.pywebview
    # exists but window.pywebview.api is an empty object: every
    # get_bounds / resize_to / close / minimize call from the React chrome
    # silently throws "not a function" and the user's min/max/close/resize
    # interactions look broken. okuro binds to 127.0.0.1 only and all page
    # content is first-party, so the practical XSS surface that
    # 'unsafe-eval' opens is effectively zero here — the trade-off is
    # acceptable for a local app. A stricter CSP would need pywebview to
    # move to a trusted-types / per-origin-script model, which upstream
    # hasn't.
    "Content-Security-Policy": (
        "default-src 'self'; "
        "script-src 'self' 'wasm-unsafe-eval' 'unsafe-eval'; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data: blob:; "
        # media-src for prism's on-box audio/video briefs embedded as data: URIs
        # (audiobrief block) — same-origin generation, nothing external.
        "media-src 'self' data: blob:; "
        "connect-src 'self' ws: wss:; "
        "font-src 'self' data:; "
        # 'self' (not 'none') so okuro can embed its OWN pages — e.g. a live
        # okuro·flow chart inside an okuro·slides deck. Cross-origin framing
        # (clickjacking) stays blocked. okuro binds to 127.0.0.1 only.
        "frame-ancestors 'self'; "
        "frame-src 'self'; "
        "base-uri 'self'; "
        "form-action 'self'"
    ),
    "X-Frame-Options": "SAMEORIGIN",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=()",
}


# CSP for SERVED DELIVERABLES only (GET /api/preview/{id}/file). Agent-
# generated decks/reports are standalone HTML that legitimately use inline
# <script>/<style> and pull fonts/CSS/images from CDNs — the app's strict
# script-src (no 'unsafe-inline') blocks the inline reveal JS, leaving the
# whole deck at opacity:0 (renders BLANK), and style/font-src 'self' blocks
# Google Fonts. This relaxed policy is applied ONLY to the file endpoint, so
# the SPA keeps its strict CSP. Threat model matches the rest of okuro:
# 127.0.0.1-only, first-party artifacts, opened in a separate tab. script-src
# stays inline-only (NOT https:) — we run the deck's own inline JS, not
# arbitrary remote scripts.
DELIVERABLE_CSP: str = (
    "default-src 'self' data: blob:; "
    "script-src 'self' 'unsafe-inline' 'unsafe-eval' blob:; "
    "style-src 'self' 'unsafe-inline' https:; "
    "font-src 'self' data: https:; "
    "img-src 'self' data: blob: https:; "
    "media-src 'self' data: blob: https:; "
    "connect-src 'self' https: data:; "
    "frame-src 'self' https:; "
    "frame-ancestors 'self'; "
    "base-uri 'self'"
)


# CSP for REDLINE-SERVED DOCUMENTS only. Derived from DELIVERABLE_CSP with
# every `https:` source removed: a redline document is served into a sandbox
# that carries a per-document token in its own URL, and DELIVERABLE_CSP's
# `connect-src https:` / `img-src https:` were measured to permit beaconing
# that token to any external host with ZERO violations. connect-src is 'none'
# because the overlay talks to the parent by postMessage and never fetches;
# the PARENT holds the global bearer and performs every write.
# 127.0.0.1/localhost are listed explicitly because the document is served
# into an OPAQUE origin (sandbox without allow-same-origin), where `'self'`
# has no tuple origin to match.
REDLINE_CSP: str = (
    "default-src 'self' data: blob: http://127.0.0.1:* http://localhost:*; "
    "script-src 'self' 'unsafe-inline' 'unsafe-eval' blob: "
    "http://127.0.0.1:* http://localhost:*; "
    "style-src 'self' 'unsafe-inline' http://127.0.0.1:* http://localhost:*; "
    "font-src 'self' data: http://127.0.0.1:* http://localhost:*; "
    "img-src 'self' data: blob: http://127.0.0.1:* http://localhost:*; "
    "media-src 'self' data: blob: http://127.0.0.1:* http://localhost:*; "
    "connect-src 'none'; "
    "frame-src 'none'; "
    "object-src 'none'; "
    "form-action 'none'; "
    "frame-ancestors 'self'; "
    "base-uri 'none'"
)


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Attach defense-in-depth headers to every HTTP response."""

    async def dispatch(self, request: Request, call_next) -> Response:
        response = await call_next(request)
        for name, value in SECURITY_HEADERS.items():
            response.headers.setdefault(name, value)
        return response
