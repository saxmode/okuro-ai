# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Forge repository DISCOVERY — ask a stored credential what it can see,
#   so repos can be added by click instead of by hand-typed clone url. Read-only:
#   this module never writes to managed_repos.
# index:
#   def discover_repos
#   def known_workspaces
#   def _bitbucket
#   def _github
#   def _gitlab
# AGENT_HEADER_END -->
"""What can this credential actually see on the forge?

add_repo needs a clone url the user typed from memory. That is the wrong shape
for "index everything I work on": you cannot type what you have forgotten, and a
typo becomes a failed clone instead of a missing repo.

MEASURED CONSTRAINT, Bitbucket Cloud, 2026-09-03 — this is why the API takes a
workspace rather than discovering everything on its own:
  GET /2.0/repositories                  → 410, "CHANGE-2770 deprecated"
  GET /2.0/workspaces                    → 404 for an Atlassian API token
  GET /2.0/user/permissions/workspaces   → 404 for an Atlassian API token
  GET /2.0/repositories/{workspace}      → 200  ← the only listing that works
So a Bitbucket workspace cannot be enumerated from the token; it must be named.
known_workspaces() recovers the ones already in use from managed repo urls, and
the caller may add more. GitHub and GitLab DO have a global listing and need no
workspace at all.
"""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import urlparse

log = logging.getLogger("okuro.repos.discovery")

_TIMEOUT_S = 30
_PAGE = 100
# A runaway pagination loop against a forge is worse than an incomplete list.
_MAX_PAGES = 20


def _norm(url: str) -> str:
    """Compare clone urls the way a forge means them, not the way they're typed.

    The registry stores what the user pasted (often with a trailing slash); a
    forge's clone link ends in `.git`. Without folding both, every discovered
    repo looks new and the whole 'already managed' column reads false.
    """
    u = (url or "").strip().rstrip("/").lower()
    if u.endswith(".git"):
        u = u[:-4]
    return u


def _host_of(url: str) -> str:
    return urlparse(url).netloc.lower() if "://" in (url or "") else ""


def _token(token_key: str) -> str:
    from okuro.keyring.storage import KeyringStorage

    tok = KeyringStorage().get_key(token_key)
    if not tok:
        raise RuntimeError(f"token_key '{token_key}' resolved to an empty keyring entry")
    return tok


def known_workspaces(host: str | None = None) -> list[dict]:
    """Workspaces already represented in managed_repos, with their credential.

    The credential is part of the answer, not a lookup the caller must do: a
    workspace is only listable BY the identity that already reaches it.
    """
    from okuro.db import get_db

    rows = get_db().fetchall(
        "SELECT url, token_key, username, pr_username FROM managed_repos"
    )
    seen: dict[tuple[str, str], dict] = {}
    for r in rows:
        url = r["url"] or ""
        h = _host_of(url)
        if host and h != host.lower():
            continue
        parts = [p for p in urlparse(url).path.split("/") if p]
        if not parts:
            continue
        ws = parts[0]
        key = (h, ws)
        if key in seen:
            continue
        seen[key] = {
            "host": h,
            "workspace": ws,
            "token_key": r["token_key"],
            "username": r["username"],
            "pr_username": r["pr_username"],
        }
    return sorted(seen.values(), key=lambda d: (d["host"], d["workspace"]))


# Hosts whose API lists everything the token can reach in one call — a
# per-workspace sweep there is pure duplicate work, and a workspace with no
# credential of its own (a public repo cloned anonymously) would report a
# spurious "no token_key" error for repos another source already covers.
_GLOBAL_LISTING = ("github", "gitlab")


def _collapse(sources: list[dict]) -> list[dict]:
    """One source per credential on a global-listing host; keep per-workspace elsewhere."""
    out: list[dict] = []
    seen_global: set[tuple[str, str]] = set()
    for s in sources:
        h = (s.get("host") or "").lower()
        if any(g in h for g in _GLOBAL_LISTING):
            if not s.get("token_key"):
                continue  # covered by a credentialled sibling, or genuinely public
            key = (h, s["token_key"])
            if key in seen_global:
                continue
            seen_global.add(key)
            out.append({**s, "workspace": None})
        else:
            out.append(s)
    return out


def _managed_index() -> dict[str, dict]:
    """Clone url (normalised) → the managed row, so callers can mark duplicates."""
    from okuro.db import get_db

    out: dict[str, dict] = {}
    for r in get_db().fetchall("SELECT id, url, status, last_synced_at FROM managed_repos"):
        out[_norm(r["url"])] = {
            "id": r["id"],
            "status": r["status"],
            "last_synced_at": r["last_synced_at"],
        }
    return out


def _get_json(url: str, auth: tuple[str, str] | None, headers: dict | None = None) -> Any:
    import httpx

    resp = httpx.get(url, auth=auth, headers=headers or {}, timeout=_TIMEOUT_S)
    if resp.status_code != 200:
        raise RuntimeError(f"{url} returned {resp.status_code}: {resp.text[:200]}")
    return resp.json()


# ── per-forge listings ───────────────────────────────────────────────


def _bitbucket(workspace: str, token_key: str, account: str) -> list[dict]:
    """List one workspace. The global listing is deprecated — see module docstring."""
    tok = _token(token_key)
    out: list[dict] = []
    url = (
        f"https://api.bitbucket.org/2.0/repositories/{workspace}"
        f"?pagelen={_PAGE}&fields=next,values.slug,values.full_name,"
        f"values.mainbranch.name,values.updated_on,values.is_private,values.language,"
        f"values.description,values.links.clone"
    )
    for _ in range(_MAX_PAGES):
        data = _get_json(url, (account, tok))
        for v in data.get("values", []):
            https = next(
                (c["href"] for c in (v.get("links", {}).get("clone") or [])
                 if c.get("name") == "https"),
                f"https://bitbucket.org/{v.get('full_name')}",
            )
            # The clone url embeds the account name for Bitbucket; strip it so it
            # matches what add_repo stores and what _managed_index keys on.
            if "@" in https:
                https = "https://" + https.split("@", 1)[1]
            out.append({
                "name": v.get("slug"),
                "full_name": v.get("full_name"),
                "url": https,
                "default_branch": (v.get("mainbranch") or {}).get("name"),
                "updated_on": v.get("updated_on"),
                "private": v.get("is_private"),
                "language": v.get("language") or None,
                "description": v.get("description") or None,
            })
        url = data.get("next")
        if not url:
            break
    return out


def _github(token_key: str, account: str | None = None) -> list[dict]:
    tok = _token(token_key)
    out: list[dict] = []
    headers = {"Authorization": f"Bearer {tok}", "Accept": "application/vnd.github+json"}
    for page in range(1, _MAX_PAGES + 1):
        data = _get_json(
            f"https://api.github.com/user/repos?per_page={_PAGE}&page={page}&affiliation="
            "owner,collaborator,organization_member",
            None,
            headers,
        )
        if not data:
            break
        for v in data:
            out.append({
                "name": v.get("name"),
                "full_name": v.get("full_name"),
                "url": v.get("clone_url"),
                "default_branch": v.get("default_branch"),
                "updated_on": v.get("pushed_at"),
                "private": v.get("private"),
                "language": v.get("language"),
                "description": v.get("description"),
            })
        if len(data) < _PAGE:
            break
    return out


def _gitlab(token_key: str, account: str | None = None) -> list[dict]:
    tok = _token(token_key)
    out: list[dict] = []
    headers = {"PRIVATE-TOKEN": tok}
    for page in range(1, _MAX_PAGES + 1):
        data = _get_json(
            f"https://gitlab.com/api/v4/projects?membership=true&per_page={_PAGE}&page={page}",
            None,
            headers,
        )
        if not data:
            break
        for v in data:
            out.append({
                "name": v.get("path"),
                "full_name": v.get("path_with_namespace"),
                "url": v.get("http_url_to_repo"),
                "default_branch": v.get("default_branch"),
                "updated_on": v.get("last_activity_at"),
                "private": v.get("visibility") != "public",
                "language": None,
                "description": v.get("description"),
            })
        if len(data) < _PAGE:
            break
    return out


# ── public entry point ───────────────────────────────────────────────


def discover_repos(
    workspace: str | None = None,
    token_key: str | None = None,
    host: str | None = None,
    account: str | None = None,
) -> dict:
    """List repos a stored credential can see, marking the ones already managed.

    With no arguments, sweeps every (host, workspace, credential) already present
    in managed_repos — "show me everything I have access to" without the user
    naming anything. A per-source failure is reported in `errors` and never
    aborts the sweep: one dead credential must not hide the repos behind the
    healthy ones.

    Returns {sources, repos, managed, new, errors}. Read-only — adding is
    a separate, explicit add_repo call.
    """
    sources: list[dict] = []
    if workspace or token_key or host:
        # An explicit ask. Fill the credential from a sibling repo on the same
        # host when the caller gave only a workspace.
        cred = {"host": host or "", "workspace": workspace or "", "token_key": token_key,
                "username": None, "pr_username": None}
        if not token_key or not host:
            for k in known_workspaces():
                if (workspace and k["workspace"] == workspace) or (host and k["host"] == host):
                    cred = {**k, **{x: y for x, y in
                                    (("workspace", workspace), ("token_key", token_key),
                                     ("host", host)) if y}}
                    break
        sources = [cred]
    else:
        sources = _collapse(known_workspaces())

    managed = _managed_index()
    repos: list[dict] = []
    errors: list[dict] = []
    seen_urls: set[str] = set()

    for src in sources:
        h = (src.get("host") or "").lower()
        tk = src.get("token_key")
        acct = account or src.get("pr_username") or src.get("username")
        try:
            if "bitbucket" in h:
                if not src.get("workspace"):
                    raise RuntimeError(
                        "Bitbucket needs a workspace — the global repository listing is "
                        "deprecated (CHANGE-2770) and workspaces are not enumerable with "
                        "an API token."
                    )
                if not tk:
                    raise RuntimeError("no token_key for this source")
                found = _bitbucket(src["workspace"], tk, acct or "")
            elif "github" in h:
                found = _github(tk) if tk else []
                if not tk:
                    raise RuntimeError("no token_key for this source")
            elif "gitlab" in h:
                if not tk:
                    raise RuntimeError("no token_key for this source")
                found = _gitlab(tk)
            else:
                raise RuntimeError(f"discovery not implemented for host '{h or 'unknown'}'")
        except Exception as exc:  # noqa: BLE001 — one bad source must not kill the sweep
            errors.append({
                "host": h,
                "workspace": src.get("workspace"),
                "token_key": tk,
                "detail": str(exc)[:300],
            })
            log.warning("discovery failed for %s/%s: %s", h, src.get("workspace"), exc)
            continue

        for r in found:
            key = _norm(r.get("url"))
            if key in seen_urls:
                continue
            seen_urls.add(key)
            hit = managed.get(key)
            r["managed"] = bool(hit)
            r["repo_id"] = hit["id"] if hit else None
            r["managed_status"] = hit["status"] if hit else None
            r["last_synced_at"] = hit["last_synced_at"] if hit else None
            r["host"] = h
            # The credential that LISTED this repo is the one that can clone it.
            # Discarding it here is what made one-click adds clone anonymously
            # and fail on every private repo.
            r["token_key"] = tk
            r["username"] = src.get("username")
            r["pr_username"] = src.get("pr_username")
            repos.append(r)

    repos.sort(key=lambda r: (r.get("managed") or False, (r.get("full_name") or "").lower()))
    return {
        "sources": [
            {"host": s.get("host"), "workspace": s.get("workspace"),
             "token_key": s.get("token_key")}
            for s in sources
        ],
        "repos": repos,
        "managed": sum(1 for r in repos if r["managed"]),
        "new": sum(1 for r in repos if not r["managed"]),
        "errors": errors,
    }
