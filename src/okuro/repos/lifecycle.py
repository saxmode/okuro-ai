# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Managed-repository lifecycle — clone/sync/remove cloned repos, register
#   them as projects (→ cortex roots), and run code-graph ingest. State tracked in
#   the managed_repos table (mig 081).
# index:
#   def add_repo
#   def inherit_credential
#   def sync_repo
#   def update_repo
#   def update_credential
#   def probe_credentials
#   def remove_repo
#   def list_repos
#   def get_repo
#   def _clone
#   def _ingest
#   def _register_project
#   def _name_from_url
#   def _auth_url
# AGENT_HEADER_END -->
"""Clone remote repos under the per-user data dir and prepare them for AI work.

Flow (add_repo):
  1. clone <url> → <data-dir>/repos/<workspace>/<name>   (paths.repo_path)
  2. register as a project (projects table) so it becomes a cortex root
  3. code-graph ingest via `python -m okuro codegraph ingest` (reuses the CLI —
     one code path, portable across users)
  4. status transitions: pending → cloning → indexing → ready | error

Security: a git credential is fetched from the keyring by name at clone time and
injected into the https url in-memory only. The token is never written to the DB
and never logged — the original url is stored verbatim.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
import sys
from pathlib import Path

from okuro.repos.paths import repo_path, repo_id, slugify

log = logging.getLogger(__name__)


# ── URL / naming helpers ─────────────────────────────────────────────


def _name_from_url(url: str) -> str:
    """Derive a repo name from a clone url (strip .git, take last path segment)."""
    tail = url.rstrip("/").split("/")[-1]
    return re.sub(r"\.git$", "", tail) or "repo"


def _auth_username_for_host(url: str) -> str:
    """Default Basic-auth username per host when the caller gives only a token.

    Each forge expects a specific sentinel username in the ``user:token`` pair:
      github.com     → x-access-token   (GitHub PAT / installation token)
      gitlab.com     → oauth2           (GitLab PAT)
      bitbucket.org  → x-token-auth     (Bitbucket Access Token)
    A wrong sentinel makes a VALID token fail — this was the Bitbucket bug.

    An Atlassian API token needs TWO DIFFERENT identities, measured 2026-09-03:
      git over https → the Bitbucket USERNAME (the account handle, not the email); the email
                       is rejected with "you may not have access".
      REST API       → the Atlassian EMAIL; the username is rejected with
                       "API token must be used with an atlassian registered email".
    So ``username`` (git) and ``pr_username`` (REST) are not interchangeable.
    x-token-auth also works for git and is the safer default when unsure.
    """
    host = url.split("/", 3)[2].lower() if "://" in url else ""
    if "github" in host:
        return "x-access-token"
    if "gitlab" in host:
        return "oauth2"
    if "bitbucket" in host:
        return "x-token-auth"
    return "git"  # generic; most forges accept any non-empty username with a token


def _auth_url(url: str, token_key: str | None, username: str | None = None) -> str:
    """Inject keyring credentials into an https url. In-memory only — never stored.

    ``username`` overrides the per-host default. For a Bitbucket Atlassian API
    token this is the Bitbucket USERNAME, not the email — see
    _auth_username_for_host. Both the username and token are percent-encoded so
    an ``@`` in an email address can't corrupt the URL's authority section.
    """
    if not token_key or not url.startswith("https://"):
        return url
    try:
        from okuro.keyring.storage import KeyringStorage

        token = KeyringStorage().get_key(token_key)
    except Exception as exc:
        # Surface the real cause. Falling back to a credential-less url here just
        # defers the failure to git as the misleading "could not read Username".
        raise RuntimeError(f"keyring lookup for token_key '{token_key}' failed: {exc}") from exc
    if not token:
        raise RuntimeError(f"token_key '{token_key}' resolved to an empty/missing keyring entry")
    from urllib.parse import quote

    user = username or _auth_username_for_host(url)
    cred = f"{quote(user, safe='')}:{quote(token, safe='')}"
    return url.replace("https://", f"https://{cred}@", 1)


# ── DB helpers ───────────────────────────────────────────────────────


def _row_to_dict(r) -> dict:
    return {
        "id": r["id"],
        "url": r["url"],
        "workspace": r["workspace"],
        "name": r["name"],
        "path": r["path"],
        "tier": r["tier"],
        "status": r["status"],
        "last_indexed_sha": r["last_indexed_sha"],
        "error": r["error"],
        "default_branch": r["default_branch"],
        "project_id": r["project_id"],
        "token_key": r["token_key"],
        "username": r["username"],
        "pr_username": r["pr_username"],
        "last_synced_at": r["last_synced_at"],
        "last_change": json.loads(r["last_change"]) if r["last_change"] else None,
        "created_at": r["created_at"],
        "updated_at": r["updated_at"],
    }


def _set_status(db, rid: str, status: str, error: str | None = None) -> None:
    db.execute(
        "UPDATE managed_repos SET status = ?, error = ?, updated_at = datetime('now') WHERE id = ?",
        (status, error, rid),
    )
    db.conn.commit()


# ── git ──────────────────────────────────────────────────────────────


def _run_git(
    args: list[str], cwd: Path | None = None, timeout: int | None = None
) -> subprocess.CompletedProcess:
    """Run git. ``timeout`` is for network commands — a hung fetch must not wedge
    a daemon tick. GIT_TERMINAL_PROMPT=0 turns a credential prompt into an
    immediate error instead of a process that blocks forever on stdin."""
    import os

    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    try:
        return subprocess.run(
            ["git", *args],
            cwd=str(cwd) if cwd else None,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
            env=env,
        )
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(
            args=["git", *args], returncode=124, stdout="",
            stderr=f"git timed out after {timeout}s",
        )


def _clone(url: str, dest: Path, token_key: str | None, username: str | None = None) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    res = _run_git(["clone", "--depth", "1", _auth_url(url, token_key, username), str(dest)])
    if res.returncode != 0:
        # Scrub any token that may echo back in an error line before surfacing.
        msg = _scrub(res.stderr.strip() or "git clone failed")
        raise RuntimeError(msg)
    # git writes the (credentialled) clone URL into .git/config — strip it back
    # to the clean url so the token never persists on disk. sync re-injects.
    _run_git(["remote", "set-url", "origin", url], cwd=dest)


def _scrub(text: str) -> str:
    """Remove injected credentials from any string before it is stored/logged."""
    return re.sub(r"https://[^@/\s]+@", "https://", text)


def _head_sha(root: Path) -> str | None:
    res = _run_git(["rev-parse", "HEAD"], cwd=root)
    return res.stdout.strip() if res.returncode == 0 else None


def _default_branch(root: Path) -> str | None:
    res = _run_git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=root)
    return res.stdout.strip() if res.returncode == 0 else None


def _change_delta(root: Path, prev_sha: str | None, new_sha: str | None) -> dict:
    """Summarise what changed between two SHAs for the /repos overview.

    Returns {prev_sha, new_sha, commits, files, insertions, deletions}. commits=0
    means "already up to date". Best-effort: a shallow clone or missing prev
    object degrades to zeros rather than failing the sync.
    """
    delta = {
        "prev_sha": prev_sha,
        "new_sha": new_sha,
        "commits": 0,
        "files": 0,
        "insertions": 0,
        "deletions": 0,
    }
    if not prev_sha or not new_sha or prev_sha == new_sha:
        return delta
    rng = f"{prev_sha}..{new_sha}"
    cnt = _run_git(["rev-list", "--count", rng], cwd=root)
    if cnt.returncode == 0 and cnt.stdout.strip().isdigit():
        delta["commits"] = int(cnt.stdout.strip())
    stat = _run_git(["diff", "--shortstat", rng], cwd=root)
    if stat.returncode == 0:
        # e.g. " 12 files changed, 340 insertions(+), 88 deletions(-)"
        for pat, key in (
            (r"(\d+) files? changed", "files"),
            (r"(\d+) insertions?\(\+\)", "insertions"),
            (r"(\d+) deletions?\(-\)", "deletions"),
        ):
            m = re.search(pat, stat.stdout)
            if m:
                delta[key] = int(m.group(1))
    return delta


# ── ingest / registration ────────────────────────────────────────────


def _register_project(db, slug: str, name: str, path: Path, url: str) -> None:
    """Register the clone as a project so it becomes a cortex root."""
    stack = _detect_stack(path)
    db.execute(
        "INSERT OR IGNORE INTO projects (id, name, path, url, stack, active) "
        "VALUES (?, ?, ?, ?, ?, 1)",
        (slug, name, str(path), url, json.dumps(stack)),
    )
    # Keep path/url fresh if the row already existed. Both axes: attaching a
    # managed repo means "scan it" (indexed) and "agents may see it"
    # (active). Explicit since migration 100 split the two.
    db.execute(
        "UPDATE projects SET path = ?, url = ?, active = 1, indexed = 1 "
        "WHERE id = ?",
        (str(path), url, slug),
    )
    db.conn.commit()


def _detect_stack(path: Path) -> list[str]:
    from okuro.sense.projects import _detect_stack as detect

    return detect(str(path))


def _index_cortex(path: Path, slug: str) -> None:
    """Index the repo into cortex (docs → FTS, + vectors on advanced/pro tiers).

    Best-effort: the code graph is the guaranteed layer, so a cortex failure
    (e.g. embed service down) must not fail the whole add — it just means
    semantic search lags until the daemon's next refresh. Tier gating
    (air = no vectors) is handled inside VectorStore via tier_policy.
    """
    try:
        from okuro.cortex.vectorstore import VectorStore

        VectorStore().index_directory(path, project=slug)
    except Exception as exc:  # noqa: BLE001 — cortex is best-effort here
        import logging

        logging.getLogger(__name__).warning(
            "cortex index for %s deferred to daemon (%s)", slug, exc
        )


def _ingest(path: Path, slug: str, full: bool = False) -> None:
    """Run code-graph ingest by invoking the okuro CLI (single code path).

    Uses the running interpreter (`python -m okuro`) so it works for any user
    regardless of whether the `okuro` console script is on PATH. ``full`` forces
    a complete re-walk — correct for the initial clone (no prior marker); sync
    leaves it False for git-incremental re-parsing of changed files only.
    """
    cmd = [sys.executable, "-m", "okuro", "codegraph", "ingest", str(path), "--project", slug]
    if full:
        cmd.append("--full")
    res = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if res.returncode != 0:
        raise RuntimeError((res.stderr or res.stdout or "codegraph ingest failed").strip()[:2000])


# ── public API ───────────────────────────────────────────────────────


def inherit_credential(url: str) -> dict:
    """Find the credential a sibling repo already uses to reach this url's forge.

    A credential belongs to a (host, workspace) pair, not to one repo — every
    repo in a Bitbucket workspace is reached by the same identity. Requiring the
    caller to restate it on each add is how a private repo silently gets cloned
    ANONYMOUSLY: add_repo stores token_key exactly as given and never infers one,
    _auth_url returns the bare url, and the clone fails with a message about the
    repo rather than about the missing credential.

    Prefers a sibling in the same workspace (the exact identity), then any repo
    on the same host (right forge, probably right token). Returns {} when there
    is genuinely nothing to inherit — a public repo needs no credential and must
    not be handed someone else's.
    """
    from okuro.db import get_db
    from urllib.parse import urlparse

    def _parts(u: str) -> tuple[str, str]:
        pu = urlparse(u or "")
        segs = [x for x in pu.path.split("/") if x]
        return pu.netloc.lower(), (segs[0].lower() if segs else "")

    host, ws = _parts(url)
    if not host:
        return {}

    rows = get_db().fetchall(
        "SELECT url, token_key, username, pr_username FROM managed_repos "
        "WHERE token_key IS NOT NULL"
    )
    same_host, same_ws = [], []
    for r in rows:
        h, w = _parts(r["url"] or "")
        if h != host:
            continue
        same_host.append(r)
        if ws and w == ws:
            same_ws.append(r)

    pick = (same_ws or same_host)
    if not pick:
        return {}
    r = pick[0]
    return {
        "token_key": r["token_key"],
        "username": r["username"],
        "pr_username": r["pr_username"],
    }


def add_repo(
    url: str,
    workspace: str = "default",
    tier: str = "air",
    name: str | None = None,
    token_key: str | None = None,
    username: str | None = None,
    pr_username: str | None = None,
    inherit: bool = True,
) -> dict:
    """Clone a repo, register it as a project, and code-graph ingest it.

    Args:
        url:        git clone url (https or ssh).
        workspace:  grouping bucket (dir under repos/). Default 'default'.
        tier:       air | advanced | pro (retrieval depth; air = tree-sitter graph).
        name:       override repo dir name (defaults to the url tail).
        token_key:  keyring entry name holding a git token (https urls only).
        username:   GIT Basic-auth username paired with the token. For a Bitbucket
                    Atlassian API token this is the Bitbucket USERNAME, not the
                    email — the email belongs in pr_username (REST/PR identity).
        pr_username: REST/PR identity. Bitbucket = the Atlassian account email.
        inherit:    when no token_key is given, adopt the credential a sibling
                    repo already uses for this host+workspace. Without this a
                    private repo clones ANONYMOUSLY and fails with a message
                    about the repo instead of about the missing credential.
                    Pass False to force a genuinely anonymous clone.

    Returns the managed_repos row as a dict. Raises on clone/ingest failure
    (status is persisted as 'error' with the message before raising).
    """
    from okuro.db import get_db

    if tier not in ("air", "advanced", "pro"):
        raise ValueError(f"invalid tier '{tier}' (air|advanced|pro)")

    # An explicit token_key always wins; inheritance only fills a blank.
    if inherit and not token_key:
        inherited = inherit_credential(url)
        if inherited:
            token_key = inherited["token_key"]
            username = username or inherited["username"]
            pr_username = pr_username or inherited["pr_username"]
            log.info(
                "add_repo: inheriting credential '%s' from a sibling on this forge",
                token_key,
            )

    name = name or _name_from_url(url)
    ws = slugify(workspace)
    slug = repo_id(ws, name)
    dest = repo_path(ws, name)

    db = get_db()
    existing = db.fetchone("SELECT id FROM managed_repos WHERE id = ?", (slug,))
    if existing:
        raise ValueError(f"repo '{slug}' already managed — use sync_repo to update")
    if dest.exists():
        raise ValueError(f"clone target already exists: {dest}")

    db.execute(
        "INSERT INTO managed_repos "
        "(id, url, workspace, name, path, tier, status, project_id, token_key, username, pr_username) "
        "VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?)",
        (slug, url, ws, name, str(dest), tier, slug, token_key, username, pr_username),
    )
    db.conn.commit()

    try:
        _set_status(db, slug, "cloning")
        _clone(url, dest, token_key, username)
        branch = _default_branch(dest)
        sha = _head_sha(dest)

        _set_status(db, slug, "indexing")
        _register_project(db, slug, name, dest, url)
        _ingest(dest, slug, full=True)  # fresh clone → full walk, no prior marker
        _index_cortex(dest, slug)       # cortex docs/FTS (+ vectors per tier)

        change = json.dumps(_change_delta(dest, None, sha))  # first index → commits=0
        db.execute(
            "UPDATE managed_repos SET status = 'ready', default_branch = ?, "
            "last_indexed_sha = ?, last_synced_at = datetime('now'), last_change = ?, "
            "error = NULL, updated_at = datetime('now') WHERE id = ?",
            (branch, sha, change, slug),
        )
        db.conn.commit()
        _rebuild_cross_repo()  # refresh workspace cross-repo graph (non-fatal)
    except Exception as exc:
        _set_status(db, slug, "error", _scrub(str(exc))[:2000])
        raise

    return get_repo(slug)


def sync_repo(repo_id_or_slug: str, token_key: str | None = None,
              username: str | None = None, full: bool = False) -> dict:
    """git pull the repo, then re-run code-graph ingest.

    ``full=True`` forces a full re-walk (re-parses every file) instead of the
    git-incremental default — needed to re-tier an index built before the
    provenance-tier vocabulary landed (defines/imports on unchanged files keep
    their old confidence under an incremental run). Call/inherit edges are
    re-tiered on every sync regardless (resolve_edges is project-wide)."""
    from okuro.db import get_db

    db = get_db()
    row = db.fetchone("SELECT * FROM managed_repos WHERE id = ?", (repo_id_or_slug,))
    if not row:
        raise ValueError(f"unknown managed repo '{repo_id_or_slug}'")
    rec = _row_to_dict(row)
    root = Path(rec["path"])
    if not root.exists():
        _set_status(db, rec["id"], "error", "clone dir missing on disk")
        raise RuntimeError(f"clone dir missing: {root}")

    try:
        _set_status(db, rec["id"], "indexing")
        # origin is stored credential-free (see _clone) — pull from the auth url
        # in-memory so the token never lands in .git/config. Reuse the credential
        # captured at add time (mig 088) unless the caller overrides it; without
        # this, a private-repo pull falls back to bare `origin` and git prompts
        # for a username (impossible in a background task → "could not read
        # Username" error).
        tk = token_key or rec.get("token_key")
        un = username or rec.get("username")
        pull_url = _auth_url(rec["url"], tk, un) if tk else "origin"
        prev_sha = rec["last_indexed_sha"]
        pull = _run_git(["pull", "--ff-only", pull_url, rec["default_branch"] or "HEAD"], cwd=root)
        if pull.returncode != 0:
            raise RuntimeError(_scrub(pull.stderr.strip() or "git pull failed"))
        _ingest(root, rec["id"], full=full)  # incremental by default (git-marker in the CLI)
        _index_cortex(root, rec["id"])
        sha = _head_sha(root)
        change = json.dumps(_change_delta(root, prev_sha, sha))
        db.execute(
            "UPDATE managed_repos SET status = 'ready', last_indexed_sha = ?, "
            "last_synced_at = datetime('now'), last_change = ?, "
            "error = NULL, updated_at = datetime('now') WHERE id = ?",
            (sha, change, rec["id"]),
        )
        db.conn.commit()
        _rebuild_cross_repo()  # refresh workspace cross-repo graph (non-fatal)
    except Exception as exc:
        _set_status(db, rec["id"], "error", _scrub(str(exc))[:2000])
        raise

    return get_repo(rec["id"])


# ── credential health probe ──────────────────────────────────────────
#
# Repo status was only ever evaluated on demand: a repo stayed 'ready' with a
# green last_synced_at until someone hit Sync. When a forge revokes access, the
# registry keeps reporting a weeks-old success as current state — 12 of 13
# repos read 'ready' while every one of them was dead, and only the one repo
# that happened to be synced flipped to 'error'. That is a proxy trusted as
# truth. This probe is the missing reconciler.
#
# It groups by CREDENTIAL, not by repo: access is granted to an identity, so one
# ls-remote answers for every repo sharing that identity. 13 repos → 2 network
# calls.

# git/forge wording for "your credential was rejected", as distinct from "the
# network is down". Only the former may mark a repo broken — a laptop on a
# plane must not flip the whole registry to error.
_AUTH_FAIL_RE = re.compile(
    r"(403|401|authentication failed|invalid username or password|"
    r"do not have access|not have access|repository not found|"
    r"could not read username|permission denied|access denied|"
    r"requested repository either does not exist)",
    re.I,
)

# Probes are cheap but remote; a wedged forge must not hold a daemon tick.
_PROBE_TIMEOUT_S = 25

_BUSY_STATUSES = ("pending", "cloning", "indexing")


def _credential_key(rec: dict) -> tuple[str, str, str]:
    """Identity a repo authenticates AS: (host, token_key, username).

    Repos sharing this tuple share a fate — that is what makes one probe
    sufficient for the whole group.
    """
    url = rec.get("url") or ""
    host = url.split("/", 3)[2].lower() if "://" in url else ""
    return (host, rec.get("token_key") or "", rec.get("username") or "")


def _probe_one(rec: dict) -> tuple[bool, str]:
    """ls-remote a single repo with its stored credential.

    Returns (ok, message). No clone dir needed — this deliberately talks to the
    remote, not to the working copy, so it detects revocation on a repo that has
    never been synced since.
    """
    try:
        auth = _auth_url(rec["url"], rec.get("token_key"), rec.get("username"))
    except Exception as exc:  # keyring miss IS a credential failure worth showing
        return False, _scrub(str(exc))
    res = _run_git(["ls-remote", "--heads", auth], timeout=_PROBE_TIMEOUT_S)
    if res.returncode == 0:
        return True, "ok"
    return False, _scrub((res.stderr or res.stdout or "git ls-remote failed").strip())


def probe_credentials(mark: bool = True) -> dict:
    """Check every distinct git credential and flag the repos a dead one owns.

    One ls-remote per credential group. On failure the group is re-probed with a
    DIFFERENT repo before anything is marked: one repo can 404 because it was
    renamed or deleted, which says nothing about the credential. Two failures on
    two repos is a credential verdict; one is a repo verdict.

    Transient failures (DNS, timeout, connection refused) are reported but never
    marked — only wording that means "your credential was rejected" counts.

    Args:
        mark: persist status='error' on affected repos. False = report only.

    Returns {checked, probes, healthy, broken, repos_marked, transient}.
    """
    from okuro.db import get_db

    db = get_db()
    rows = [_row_to_dict(r) for r in db.fetchall("SELECT * FROM managed_repos")]
    # A repo mid-clone is being written by another code path; leave it alone.
    live = [r for r in rows if r["status"] not in _BUSY_STATUSES]

    groups: dict[tuple[str, str, str], list[dict]] = {}
    for rec in live:
        groups.setdefault(_credential_key(rec), []).append(rec)

    out = {
        "checked": len(live),
        "probes": 0,
        "healthy": 0,
        "broken": [],
        "repos_marked": [],
        "transient": [],
    }

    for key, members in groups.items():
        first = members[0]
        ok, msg = _probe_one(first)
        out["probes"] += 1
        if ok:
            out["healthy"] += 1
            continue
        if not _AUTH_FAIL_RE.search(msg):
            # Unreachable, not unauthorised. Say so; change nothing.
            out["transient"].append({"credential": key[1] or f"anon@{key[0]}", "detail": msg[:200]})
            log.warning("repo probe unreachable for %s: %s", first["id"], msg[:200])
            continue

        # Confirm before condemning the credential: re-probe a sibling.
        doomed = [first]
        scope = "repo"
        if len(members) > 1:
            second = members[1]
            ok2, msg2 = _probe_one(second)
            out["probes"] += 1
            if not ok2 and _AUTH_FAIL_RE.search(msg2):
                doomed = members
                scope = "credential"

        out["broken"].append({
            "credential": key[1] or f"anon@{key[0]}",
            "host": key[0],
            "username": key[2] or None,
            "scope": scope,
            "repos": [r["id"] for r in doomed],
            "detail": msg[:300],
        })
        log.warning(
            "REPO AUTH — credential '%s' (%s as %s) is rejected; %d repo(s) affected: %s. %s",
            key[1] or "anonymous", key[0], key[2] or "-", len(doomed),
            ", ".join(r["id"] for r in doomed), msg[:200],
        )
        if not mark:
            continue
        for rec in doomed:
            # Name the probed repo. The group verdict is proven against ONE
            # repo's URL, so that URL appears in `msg` — on the other 11 rows it
            # reads as a mismatch unless we say whose response this was.
            via = "" if scope == "repo" else f" via {first['name']}"
            note = (
                f"AUTH — credential '{key[1] or 'anonymous'}' rejected by {key[0]} "
                f"(probed {_probe_stamp()}{via}). Last successful sync: "
                f"{rec['last_synced_at'] or 'never'}. {msg}"
            )
            _set_status(db, rec["id"], "error", note[:2000])
            out["repos_marked"].append(rec["id"])

    return out


def _probe_stamp() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _utc_stamp() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")


def _last_commit_subject(root: Path) -> str | None:
    res = _run_git(["log", "-1", "--pretty=%s"], cwd=root)
    return res.stdout.strip() if res.returncode == 0 and res.stdout.strip() else None


def _open_pull_request(url: str, token_key: str | None, pr_username: str | None,
                       source: str, dest: str, title: str) -> dict:
    """Open a pull request on the repo's forge. Bitbucket Cloud only for now.

    Bitbucket's REST API needs Basic auth with the Atlassian ACCOUNT EMAIL
    (pr_username) + the API token from the keyring — the git push username
    (the GitHub-style handle) 401s here. Raises with a clear message for any
    unsupported host or missing credential so the caller can report that the
    branch was pushed but the PR must be opened manually.
    """
    host = url.split("/", 3)[2].lower() if "://" in url else ""
    if "bitbucket.org" not in host:
        raise RuntimeError(f"PR automation not implemented for host '{host}' — open the PR manually")
    if not token_key:
        raise RuntimeError("no token_key stored — cannot authenticate the PR REST call")
    if not pr_username:
        raise RuntimeError("no PR author configured — Bitbucket REST needs your Atlassian account email (pr_username)")

    from okuro.keyring.storage import KeyringStorage

    token = KeyringStorage().get_key(token_key)
    if not token:
        raise RuntimeError(f"token_key '{token_key}' resolved to an empty keyring entry")

    path = url.split("bitbucket.org/", 1)[1].rstrip("/")
    path = re.sub(r"\.git$", "", path)
    parts = path.split("/")
    if len(parts) < 2:
        raise RuntimeError(f"cannot derive workspace/repo from url: {url}")
    workspace, slug = parts[0], parts[1]

    import httpx

    api = f"https://api.bitbucket.org/2.0/repositories/{workspace}/{slug}/pullrequests"
    body = {
        "title": title,
        "source": {"branch": {"name": source}},
        "destination": {"branch": {"name": dest}},
    }
    resp = httpx.post(api, json=body, auth=(pr_username, token), timeout=30)
    if resp.status_code not in (200, 201):
        raise RuntimeError(f"Bitbucket PR API {resp.status_code}: {_scrub(resp.text)[:200]}")
    data = resp.json()
    return {
        "id": data.get("id"),
        "url": (data.get("links", {}).get("html", {}) or {}).get("href"),
    }


def push_repo(repo_id_or_slug: str, token_key: str | None = None,
              username: str | None = None, pr_username: str | None = None,
              branch: str | None = None, title: str | None = None,
              open_pr: bool = True) -> dict:
    """Push local commits to a NEW branch and open a PR into the default branch.

    Deliberately never pushes to the default branch directly — unreviewed code
    must not land on main. Flow:
      1. compare local HEAD to the remote default-branch tip (ls-remote)
         → identical: nothing to push (no-op, not an error)
         → diverged (remote has commits HEAD lacks): refuse, tell caller to Sync
      2. push HEAD to a fresh branch (default: okuro/<utc-stamp>)
      3. open_pr → open a PR (source branch → default branch) on the forge

    Reuses the git credential captured at add time (mig 088); the PR REST call
    uses pr_username (mig 091) — a DIFFERENT identity on Bitbucket. NEVER
    force-pushes. Does NOT re-ingest. If the branch pushes but the PR call fails,
    returns pr_status='failed' with the branch name so the PR can be opened
    manually — the commits are safe on the remote either way.
    """
    from okuro.db import get_db

    db = get_db()
    row = db.fetchone("SELECT * FROM managed_repos WHERE id = ?", (repo_id_or_slug,))
    if not row:
        raise ValueError(f"unknown managed repo '{repo_id_or_slug}'")
    rec = _row_to_dict(row)
    if rec["status"] in ("pending", "cloning", "indexing"):
        raise RuntimeError(f"repo is busy ({rec['status']}) — wait for it to finish before pushing")
    root = Path(rec["path"])
    if not root.exists():
        raise RuntimeError(f"clone dir missing: {root}")

    dest = rec["default_branch"] or _default_branch(root) or "main"
    tk = token_key or rec.get("token_key")
    un = username or rec.get("username")
    auth = _auth_url(rec["url"], tk, un) if tk else "origin"
    local = _head_sha(root)

    # Is there anything to push, and is it a clean fast-forward over the remote?
    ls = _run_git(["ls-remote", auth, dest], cwd=root)
    remote_sha = ls.stdout.split()[0] if ls.returncode == 0 and ls.stdout.strip() else None
    if remote_sha and remote_sha == local:
        return {
            "id": rec["id"], "source_branch": None, "dest_branch": dest,
            "pushed": False, "up_to_date": True, "pr_status": "skipped",
            "pr_url": None, "pr_error": None, "detail": "no local commits to push",
        }
    if remote_sha:
        anc = _run_git(["merge-base", "--is-ancestor", remote_sha, "HEAD"], cwd=root)
        if anc.returncode != 0:
            raise RuntimeError(
                f"local branch has diverged from origin/{dest} — Sync first, then push"
            )

    src = branch or f"okuro/{_utc_stamp()}"
    push = _run_git(["push", auth, f"HEAD:refs/heads/{src}"], cwd=root)
    pout = _scrub(((push.stderr or "") + "\n" + (push.stdout or "")).strip())
    if push.returncode != 0:
        hint = ""
        if re.search(r"non-fast-forward|\[rejected\]|fetch first", pout, re.I):
            hint = " — remote already has this branch; try again"
        raise RuntimeError((pout or "git push failed") + hint)

    result = {
        "id": rec["id"], "source_branch": src, "dest_branch": dest,
        "pushed": True, "up_to_date": False, "pr_status": "skipped",
        "pr_url": None, "pr_error": None, "detail": f"pushed HEAD → {src}",
    }
    if open_pr:
        pu = pr_username or rec.get("pr_username")
        ttl = title or _last_commit_subject(root) or f"okuro push {src}"
        try:
            pr = _open_pull_request(rec["url"], tk, pu, src, dest, ttl)
            result["pr_status"] = "opened"
            result["pr_url"] = pr.get("url")
        except Exception as exc:  # branch is safely pushed; PR is best-effort
            result["pr_status"] = "failed"
            result["pr_error"] = _scrub(str(exc))[:300]
    return result


def _rebuild_cross_repo() -> None:
    """Rebuild the workspace cross-repo anchor graph. Best-effort.

    Auxiliary to any single repo's ingest — a failure here must never fail the
    repo add/sync, so all exceptions are swallowed (logged only).
    """
    try:
        from okuro.cortex.codegraph import build_cross_repo_graph
        stats = build_cross_repo_graph()
        log.info("cross-repo graph rebuilt: %s", stats)
    except Exception as exc:  # noqa: BLE001 — auxiliary, never fatal
        log.warning("cross-repo rebuild skipped: %s", exc)


# ── credential / metadata update ─────────────────────────────────────

# Columns a caller may rewrite after add_repo. `id`, `path`, `project_id`,
# `workspace` and `name` are excluded on purpose: they are the repo's identity
# on disk and in the cortex index, and changing one means a re-clone, not an
# UPDATE. Everything here is metadata the forge can change under us.
_UPDATABLE = ("url", "tier", "default_branch", "token_key", "username", "pr_username")

# Changing any of these changes who we authenticate AS, so the new identity must
# be proven before it is written. `tier` and `default_branch` do not.
_CREDENTIAL_FIELDS = ("url", "token_key", "username")


def _rest_whoami(url: str, token_key: str | None, account: str | None) -> tuple[bool, str]:
    """Verify the REST identity (pr_username) — a DIFFERENT check from git.

    An Atlassian API token authenticates git as the Bitbucket username and REST
    as the Atlassian email. A git-only probe therefore passes while PR creation
    is still broken, which is exactly how the 2026-09-03 outage hid. Each forge
    exposes a whoami; ask it.
    """
    if not token_key or not account:
        return True, "skipped — no REST identity configured"
    host = url.split("/", 3)[2].lower() if "://" in url else ""
    if "bitbucket" in host:
        api = "https://api.bitbucket.org/2.0/user"
    elif "github" in host:
        api = "https://api.github.com/user"
    elif "gitlab" in host:
        api = "https://gitlab.com/api/v4/user"
    else:
        return True, f"skipped — no known REST whoami for {host or 'this host'}"
    try:
        from okuro.keyring.storage import KeyringStorage

        token = KeyringStorage().get_key(token_key)
    except Exception as exc:
        return False, _scrub(f"keyring lookup for '{token_key}' failed: {exc}")
    if not token:
        return False, f"token_key '{token_key}' resolved to an empty keyring entry"
    try:
        import httpx

        resp = httpx.get(api, auth=(account, token), timeout=_PROBE_TIMEOUT_S)
    except Exception as exc:
        return False, _scrub(f"{api} unreachable: {exc}")
    if resp.status_code == 200:
        return True, "ok"
    return False, _scrub(f"{api} returned {resp.status_code}: {resp.text[:200]}")


def _verify_candidate(cand: dict, check_rest: bool) -> dict:
    """Probe a would-be record. Returns {git: {ok, detail}, rest: {...}}."""
    git_ok, git_msg = _probe_one(cand)
    out = {"git": {"ok": git_ok, "detail": git_msg}}
    if check_rest:
        rest_ok, rest_msg = _rest_whoami(
            cand.get("url") or "", cand.get("token_key"), cand.get("pr_username")
        )
        out["rest"] = {"ok": rest_ok, "detail": rest_msg}
    return out


def _apply_update(db, rid: str, fields: dict) -> None:
    sets = ", ".join(f"{k} = ?" for k in fields)
    db.execute(
        f"UPDATE managed_repos SET {sets}, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
        (*fields.values(), rid),
    )


def update_repo(
    repo_id_or_slug: str,
    *,
    verify: bool = True,
    force: bool = False,
    **fields,
) -> dict:
    """Rewrite mutable columns on one managed repo — the credential-rotation path.

    Identity changes are ROUTINE (job change, workspace migration, token
    rotation, forge account merge). Before this existed the only ways to
    re-point a credential were direct SQL or remove+re-add, which re-clones and
    re-ingests thousands of files to change three metadata columns.

    Credential changes are PROVEN BEFORE THEY ARE WRITTEN: the candidate record
    is probed against the live forge (git ls-remote, plus a REST whoami when
    pr_username is in play) and the UPDATE is refused if the new identity does
    not work. Saving a credential that fails is worse than refusing it — it
    looks green in the UI and breaks at the next sync.

    Args:
        repo_id_or_slug: the managed repo to update.
        verify:  probe the new identity before writing. False = write blind.
        force:   write even when the probe fails (records the failure).
        **fields: any of _UPDATABLE. Pass None to clear a nullable column.

    Returns {id, updated: {...}, verified: {...} | None, status}.
    """
    from okuro.db import get_db

    unknown = set(fields) - set(_UPDATABLE)
    if unknown:
        raise ValueError(
            f"not updatable: {', '.join(sorted(unknown))}. "
            f"Allowed: {', '.join(_UPDATABLE)}. Identity columns (id, path, "
            f"workspace, name, project_id) require remove_repo + add_repo."
        )
    if not fields:
        raise ValueError("no fields given — nothing to update")

    db = get_db()
    rec = get_repo(repo_id_or_slug)
    if not rec:
        raise ValueError(f"unknown repo: {repo_id_or_slug}")

    # Only write what actually differs; a no-op UPDATE still bumps updated_at
    # and would make the audit trail lie about when the credential last moved.
    changed = {k: v for k, v in fields.items() if rec.get(k) != v}
    if not changed:
        return {"id": rec["id"], "updated": {}, "verified": None, "status": rec["status"]}

    cand = {**rec, **changed}
    verified = None
    if verify and (set(changed) & set(_CREDENTIAL_FIELDS) or "pr_username" in changed):
        verified = _verify_candidate(cand, check_rest="pr_username" in cand and bool(cand.get("pr_username")))
        failed = [k for k, v in verified.items() if not v["ok"]]
        if failed and not force:
            return {
                "id": rec["id"],
                "updated": {},
                "verified": verified,
                "status": rec["status"],
                "refused": (
                    f"new credential failed {' and '.join(failed)} verification — "
                    f"nothing written. Re-run with force=True to save it anyway."
                ),
            }

    _apply_update(db, rec["id"], changed)

    # A repo parked in 'error' by the auth alarm must come back on its own once
    # the credential is proven — otherwise the user fixes the cause and the UI
    # still shows red until the next daemon tick.
    status = rec["status"]
    if verified and verified["git"]["ok"] and status == "error":
        _set_status(db, rec["id"], "ready", None)
        status = "ready"
    return {
        "id": rec["id"],
        "updated": changed,
        "verified": verified,
        "status": status,
    }


def update_credential(
    *,
    token_key: str | None = None,
    username: str | None = None,
    host: str | None = None,
    new_token_key: str | None = None,
    new_username: str | None = None,
    new_pr_username: str | None = None,
    verify: bool = True,
    force: bool = False,
) -> dict:
    """Re-point every repo sharing one identity — the class-scope sibling of update_repo.

    probe_credentials already groups repos by (host, token_key, username)
    because repos sharing that tuple share a fate. Repair uses the same grouping
    for the same reason: an identity change hits every repo behind it, so fixing
    them one at a time is the instance fix applied N times.

    Selects by the OLD identity (any of token_key / username / host, ANDed),
    probes ONE member with the new values, and writes the whole group only if
    that probe passes.

    Returns {matched, updated, verified, refused?}.
    """
    from okuro.db import get_db

    if not any((token_key, username, host)):
        raise ValueError("give at least one selector: token_key, username or host")
    new = {
        k: v
        for k, v in (
            ("token_key", new_token_key),
            ("username", new_username),
            ("pr_username", new_pr_username),
        )
        if v is not None
    }
    if not new:
        raise ValueError("nothing to set — give new_token_key, new_username or new_pr_username")

    db = get_db()
    rows = [_row_to_dict(r) for r in db.fetchall("SELECT * FROM managed_repos")]

    def _host(rec: dict) -> str:
        u = rec.get("url") or ""
        return u.split("/", 3)[2].lower() if "://" in u else ""

    members = [
        r
        for r in rows
        if (token_key is None or (r.get("token_key") or "") == token_key)
        and (username is None or (r.get("username") or "") == username)
        and (host is None or _host(r) == host.lower())
    ]
    if not members:
        return {"matched": 0, "updated": [], "verified": None}

    verified = None
    if verify:
        cand = {**members[0], **new}
        verified = _verify_candidate(cand, check_rest=bool(cand.get("pr_username")))
        failed = [k for k, v in verified.items() if not v["ok"]]
        if failed and not force:
            return {
                "matched": len(members),
                "updated": [],
                "verified": verified,
                "refused": (
                    f"new credential failed {' and '.join(failed)} verification against "
                    f"{members[0]['name']} — no repo was touched. force=True to override."
                ),
            }

    updated = []
    for rec in members:
        changed = {k: v for k, v in new.items() if rec.get(k) != v}
        if not changed:
            continue
        _apply_update(db, rec["id"], changed)
        if verified and verified["git"]["ok"] and rec["status"] == "error":
            _set_status(db, rec["id"], "ready", None)
        updated.append(rec["id"])
    return {"matched": len(members), "updated": updated, "verified": verified}


def remove_repo(repo_id_or_slug: str, delete_files: bool = False) -> dict:
    """Remove a managed repo from the registry.

    Deactivates the linked project and drops the managed_repos row. Files on
    disk are deleted ONLY when delete_files=True — deleting files is a
    never-without-asking boundary, so callers must opt in explicitly.
    """
    from okuro.db import get_db

    db = get_db()
    row = db.fetchone("SELECT * FROM managed_repos WHERE id = ?", (repo_id_or_slug,))
    if not row:
        raise ValueError(f"unknown managed repo '{repo_id_or_slug}'")
    rec = _row_to_dict(row)

    if delete_files:
        import shutil

        root = Path(rec["path"])
        # Guard: only ever delete inside the managed repos root.
        from okuro.repos.paths import repos_root

        if repos_root() in root.parents and root.exists():
            shutil.rmtree(root, ignore_errors=True)

    if rec["project_id"]:
        # Both axes: the clone is gone from disk, so there is nothing to scan
        # (indexed) and no live project for agents to be briefed on (active).
        # Explicit since migration 100 split the two — before it, `active`
        # alone gated indexing.
        db.execute(
            "UPDATE projects SET active = 0, indexed = 0 WHERE id = ?",
            (rec["project_id"],),
        )
    db.execute("DELETE FROM managed_repos WHERE id = ?", (rec["id"],))
    db.conn.commit()
    _rebuild_cross_repo()  # prune the removed repo's anchors (non-fatal)

    rec["status"] = "removed"
    rec["files_deleted"] = delete_files
    return rec


def list_repos(workspace: str | None = None) -> list[dict]:
    """List managed repos, optionally filtered by workspace."""
    from okuro.db import get_db

    db = get_db()
    if workspace:
        rows = db.fetchall(
            "SELECT * FROM managed_repos WHERE workspace = ? ORDER BY workspace, name",
            (slugify(workspace),),
        )
    else:
        rows = db.fetchall("SELECT * FROM managed_repos ORDER BY workspace, name")
    return [_row_to_dict(r) for r in rows]


def get_repo(repo_id_or_slug: str) -> dict | None:
    """Fetch one managed repo by id."""
    from okuro.db import get_db

    db = get_db()
    row = db.fetchone("SELECT * FROM managed_repos WHERE id = ?", (repo_id_or_slug,))
    return _row_to_dict(row) if row else None
