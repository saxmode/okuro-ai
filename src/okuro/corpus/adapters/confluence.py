# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Confluence Cloud corpus adapter — enumerate a space's pages via REST
#   API v2 (version numbers = delta tokens), fetch bodies as storage format,
#   convert to markdown. Works anonymously on public spaces; keyring token for
#   private ones.
# index: class ConfluenceAdapter | class ConfluenceAdapter
# AGENT_HEADER_END -->
"""Pull a Confluence Cloud space into a corpus.

Uses REST **API v2** (``/wiki/api/v2/...``). v1 is deprecated and its
``/rest/api/content`` listing paginates by offset, which silently skips pages
when the collection changes mid-crawl; v2 is cursor-paginated and returns the
version number in the LISTING, which is what makes a delta sync possible
without downloading a single body.

Auth is optional by design. A space that allows anonymous access answers the
API unauthenticated — verified against a live public space — so the common
"I can read this wiki in a browser but I own no token" case needs no
credential at all. When a token IS supplied it comes from the keyring by name;
Atlassian Cloud wants Basic auth with the account EMAIL as username, not the
display name (the same trap documented for Bitbucket in repos/lifecycle.py).
"""

from __future__ import annotations

import logging
import time
from typing import Iterable
from urllib.parse import urlparse

from okuro.corpus.adapters.base import CorpusAdapter, CorpusItem, ItemRef
from okuro.corpus.markdown import storage_to_markdown

log = logging.getLogger(__name__)

_PAGE_LIMIT = 250          # v2 hard-caps at 250; asking for more is silently clamped
_TIMEOUT = 30.0
_RETRY_STATUS = {429, 500, 502, 503, 504}
_MAX_RETRIES = 4


class ConfluenceAdapter(CorpusAdapter):
    source_type = "confluence"
    materializes = True

    def __init__(
        self,
        base_url: str,
        space_key: str,
        token_key: str | None = None,
        username: str | None = None,
        include_archived: bool = False,
    ) -> None:
        """
        Args:
            base_url:   site url, with or without the /wiki suffix
                        (https://acme.atlassian.net or .../wiki).
            space_key:  space key, e.g. 'PS'.
            token_key:  keyring entry holding an Atlassian API token. Omit for
                        an anonymously readable space.
            username:   Atlassian account EMAIL paired with the token.
            include_archived: also pull archived pages (default: current only).
        """
        self.base_url = self._normalize_base(base_url)
        self.space_key = space_key.strip()
        self.token_key = token_key
        self.username = username
        self.include_archived = include_archived
        self._space_id_cache: str | None = None
        self._client = None

    # ── url detection ────────────────────────────────────────────────
    @classmethod
    def from_url(cls, url: str) -> dict | None:
        """Derive {base_url, space_key} from a pasted Confluence url.

        The UI contract is "paste the link you are looking at", so this accepts
        every shape the address bar produces:
          .../wiki/spaces/PS/overview?homepageId=123
          .../wiki/spaces/PS/pages/22351669/Some+Title
          .../wiki/display/PS/Some+Page          (legacy Server/DC)
          .../wiki/spaces/PS
        Returns None when the url is not recognisably Confluence, so the caller
        can try the next adapter.
        """
        import re as _re

        u = (url or "").strip()
        if not u or "://" not in u:
            return None
        # /spaces/<KEY> is Cloud; /display/<KEY> is the legacy Server path.
        m = _re.search(r"/(?:spaces|display)/([^/?#]+)", u)
        if not m:
            return None
        space = m.group(1)
        # The Confluence signal must come from the INPUT, never from the
        # normalized output: _normalize_base APPENDS /wiki when no context path
        # is present, so testing the result for "/wiki" manufactures the very
        # evidence it checks and the guard can never fire. That made every site
        # with /spaces/<KEY> in its url — any site at all — resolve as a
        # Confluence space. Caught by test_confluence_from_url_returns_none.
        parsed_in = urlparse(u)
        host_in = parsed_in.netloc.lower()
        path_in = parsed_in.path or ""
        looks_confluence = (
            "atlassian.net" in host_in           # Cloud
            or path_in.startswith("/wiki/")       # self-hosted with the usual context path
            or "/display/" in path_in             # legacy Server/DC page path
        )
        if not looks_confluence:
            return None
        return {"base_url": cls._normalize_base(u), "space_key": space}

    # ── setup ────────────────────────────────────────────────────────
    @staticmethod
    def _normalize_base(url: str) -> str:
        """Accept a site url, a /wiki url, or a full page url; return the API base.

        Users paste whatever is in the address bar — usually a deep link like
        ``.../wiki/spaces/PS/overview?homepageId=123``. Deriving the base from
        the origin makes every one of those forms work.
        """
        import re as _re

        u = url.strip().rstrip("/")
        parsed = urlparse(u if "://" in u else f"https://{u}")
        origin = f"{parsed.scheme}://{parsed.netloc}"
        # Confluence Cloud always serves under /wiki; Server/DC may not, so keep
        # a non-standard context path if one was given before the space marker.
        path = parsed.path or ""
        if "/wiki" in path:
            return f"{origin}/wiki"
        # Split on EITHER marker. Splitting only on /spaces left the legacy
        # /display/<KEY>/<Page> form with the whole page path as its api base
        # (…/display/DOCS/Page), which 404s every request. The Cloud /display/
        # case hid it: those urls also contain /wiki and take the branch above.
        head = _re.split(r"/(?:spaces|display)/", path, maxsplit=1)[0].rstrip("/")
        if head:
            return f"{origin}{head}"
        # No context path at all. /wiki is a CLOUD convention — atlassian.net
        # always serves there — but a self-hosted install commonly sits at the
        # root, and appending /wiki to it 404s every request.
        return f"{origin}/wiki" if "atlassian.net" in parsed.netloc.lower() else origin

    def _auth(self) -> tuple[str, str] | None:
        if not self.token_key:
            return None
        from okuro.keyring.storage import KeyringStorage

        token = KeyringStorage().get_key(self.token_key)
        if not token:
            raise RuntimeError(f"token_key '{self.token_key}' resolved to an empty keyring entry")
        if not self.username:
            raise RuntimeError(
                "Confluence Cloud needs the Atlassian account EMAIL as the Basic-auth "
                "username alongside the API token — pass username="
            )
        return (self.username, token)

    def _http(self):
        if self._client is None:
            import httpx

            self._client = httpx.Client(
                timeout=_TIMEOUT,
                auth=self._auth(),
                headers={
                    "Accept": "application/json",
                    "User-Agent": "okuro-corpus/1.0 (+https://github.com/okuro)",
                },
                follow_redirects=True,
            )
        return self._client

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    # ── http ─────────────────────────────────────────────────────────
    def _api(self, path: str, params: dict | None = None) -> dict:
        """GET an API path, retrying throttles and transient 5xx with backoff.

        Confluence Cloud throttles per-IP; a 429 during a 363-page crawl is
        normal, not an error. Retry-After is honoured when present because
        Atlassian sets it — guessing a backoff when the server told us the
        answer just gets us throttled again.
        """
        url = path if path.startswith("http") else f"{self.base_url}{path}"
        client = self._http()
        delay = 1.0
        last = ""
        for attempt in range(_MAX_RETRIES):
            resp = client.get(url, params=params)
            if resp.status_code == 200:
                return resp.json()
            last = f"HTTP {resp.status_code}: {resp.text[:200]}"
            if resp.status_code in (401, 403):
                hint = (
                    " — space is not anonymously readable; pass token_key + username"
                    if not self.token_key
                    else " — check the token and that username is your Atlassian account email"
                )
                raise RuntimeError(f"{last}{hint}")
            if resp.status_code == 404:
                raise RuntimeError(f"{last} — check base_url and space key")
            if resp.status_code not in _RETRY_STATUS:
                raise RuntimeError(last)
            wait = float(resp.headers.get("Retry-After") or delay)
            log.warning("confluence %s → retry in %.1fs (attempt %d)", resp.status_code, wait, attempt + 1)
            time.sleep(min(wait, 30.0))
            delay *= 2
        raise RuntimeError(f"confluence request failed after {_MAX_RETRIES} attempts: {last}")

    def _paginate(self, path: str, params: dict) -> Iterable[dict]:
        """Walk a v2 cursor-paginated collection.

        ``_links.next`` is returned as a path relative to the SITE root and
        already carries the cursor, so it is joined to the origin, not to
        base_url — concatenating it onto base_url yields /wiki/wiki/... and a
        404 that looks like a permissions problem.
        """
        page = self._api(path, params)
        while True:
            yield from page.get("results", [])
            nxt = (page.get("_links") or {}).get("next")
            if not nxt:
                return
            parsed = urlparse(self.base_url)
            page = self._api(f"{parsed.scheme}://{parsed.netloc}{nxt}")

    # ── space / tree ─────────────────────────────────────────────────
    def _space_id(self) -> str:
        if self._space_id_cache:
            return self._space_id_cache
        data = self._api("/api/v2/spaces", {"keys": self.space_key, "limit": 1})
        results = data.get("results") or []
        if not results:
            raise RuntimeError(f"space '{self.space_key}' not found at {self.base_url}")
        self._space_id_cache = str(results[0]["id"])
        return self._space_id_cache

    def describe(self) -> dict:
        return {
            "source_type": self.source_type,
            "base_url": self.base_url,
            "space_key": self.space_key,
            "authenticated": bool(self.token_key),
        }

    @staticmethod
    def _build_tree(pages: list[dict]) -> dict[str, tuple[str, ...]]:
        """Map page id → ancestor titles, root-first.

        Walked iteratively with a visited set: a Confluence tree is acyclic in
        principle, but a page whose parent was deleted mid-crawl can present a
        parentId that resolves to a page not in the listing, and a corrupted
        pair could otherwise loop forever during a 363-page walk.
        """
        by_id = {str(p["id"]): p for p in pages}
        out: dict[str, tuple[str, ...]] = {}
        for pid, page in by_id.items():
            chain: list[str] = []
            seen = {pid}
            cur = page.get("parentId")
            while cur and str(cur) in by_id and str(cur) not in seen:
                seen.add(str(cur))
                parent = by_id[str(cur)]
                chain.append(parent.get("title") or "untitled")
                cur = parent.get("parentId")
            out[pid] = tuple(reversed(chain))
        return out

    # ── adapter contract ─────────────────────────────────────────────
    def enumerate(self) -> Iterable[ItemRef]:
        """List every page with its version number. No bodies fetched."""
        params = {"limit": _PAGE_LIMIT}
        if self.include_archived:
            params["status"] = "current,archived"
        pages = list(self._paginate(f"/api/v2/spaces/{self._space_id()}/pages", params))
        tree = self._build_tree(pages)
        base = self.base_url
        refs = []
        for p in pages:
            pid = str(p["id"])
            webui = ((p.get("_links") or {}).get("webui")) or ""
            refs.append(
                ItemRef(
                    key=pid,
                    version=str((p.get("version") or {}).get("number") or "0"),
                    title=p.get("title") or "untitled",
                    segments=tree.get(pid, ()),
                    url=f"{base}{webui}" if webui else None,
                )
            )
        log.info("confluence: enumerated %d pages in space %s", len(refs), self.space_key)
        return refs

    def fetch(self, ref: ItemRef) -> CorpusItem:
        data = self._api(f"/api/v2/pages/{ref.key}", {"body-format": "storage"})
        storage = (((data.get("body") or {}).get("storage")) or {}).get("value") or ""
        body = storage_to_markdown(storage)
        meta = {
            "space": self.space_key,
            "page_id": ref.key,
        }
        # A page whose body converts to almost nothing is usually a redirect
        # stub or a pure-macro page (child index, embedded report). Flagging it
        # keeps a stub from reading as authoritative content in a RAG hit.
        if len(body.strip()) < 80:
            meta["stub"] = True
        return CorpusItem(ref=ref, body=body, metadata=meta)
