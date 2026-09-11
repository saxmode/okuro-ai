# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: redline store + policy — open a document as a version, comment on
#   that version, resolve by hand. All policy lives here so MCP and HTTP
#   cannot drift.
# index:
#   config + allowlist | tree hash | tokens
#   def artifact_chain | def deck_hash | def materialize
#   def open_document | def add_comment | def list_comments
#   def resolve_comment | def reopen_comment | def reply
#   def versions | def reanchor | def tombstone_document
#   def resolve_served_file | def inject_overlay | def render_markdown
# AGENT_HEADER_END -->
"""redline — comment on an HTML document the way you comment on a design file.

Backed by ``redline_documents`` / ``redline_versions`` / ``redline_comments`` /
``redline_reanchors`` (migration 145).

THE LOAD-BEARING RULE: a comment belongs to ONE VERSION, never to the
document. Measured on the real cockpit-mockup regeneration, 0 of 5 anchors
survived on any tier — v2 carries zero elements with an id and v3 carries 7 of
491 — so a comment pinned to the document would point at nothing after one
regeneration while still claiming to point somewhere. A new version therefore
starts with an EMPTY comment list, and the agent resolves the previous
version's open comments by hand with a note and an after-excerpt.

THREE INPUTS, three ways a version comes into being:

* ``file`` — an html file under ``redline.roots``. The version is the sha256 of
  the served TREE, and only the CURRENT version's bytes exist: a past version
  kept its hash, never its content, so a past file version is not renderable
  and says so.
* ``artifact`` — an okuro html artifact. Every link of its supersede chain is
  one version, materialized to ``~/.okuro/redline/docs/{document_id}/{seq}/``,
  so EVERY version's own bytes are retrievable and a past version renders
  read-only against the bytes its comments were made on.
* ``prism`` — a deck2 DeckDoc. The version is the sha256 of the deck JSON as it
  read at open, and that JSON is snapshotted beside the artifact bodies. The
  deck is NOT html, so it is rendered by the SPA's own deck runtime inside an
  open shadow root rather than served into the sandbox; the anchor records the
  shadow-host chain and the browser resolver runs inside that root.

Distinct from :mod:`okuro.sense.reviews` (migration 111) on purpose, and the
migration header records the four measured reasons — the shortest of which is
that every ``reviews`` row enters an outbound sync queue that a client's mockup
comment must never join.

All policy is HERE — the allowlist, the tree hash, the token, the resolve
rules — so :mod:`okuro.sense.mcp_tools` and
:mod:`okuro.orchestrator.api.redline` are both thin over one implementation
and cannot drift apart, exactly as ``api/reviews.py`` states for reviews.
"""

from __future__ import annotations

import hashlib
import json
import logging
import secrets
import time
import uuid
from pathlib import Path
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from okuro.db.engine import okuro_home
from okuro.sense import redline_anchor

logger = logging.getLogger("okuro.sense.redline")

CONFIG_PATH = okuro_home() / "config.yaml"

KINDS = ("file", "artifact", "prism")
STATUSES = ("open", "done")

# Where an artifact body and a deck snapshot land, one directory per version.
# A file document is served from where it already lives; these two are the
# inputs whose bytes okuro itself owns, so a version can keep its OWN copy and
# a past version renders against the bytes its comments were made on.
#
# FUNCTIONS, not constants, and for the reason ``db/engine.py`` writes down at
# length: a module constant built from ``okuro_home()`` freezes whatever HOME
# was set when this module was first imported, and 94 test files move HOME. The
# first import would decide where every later process wrote.


def docs_dir() -> Path:
    return okuro_home() / "redline" / "docs"


def shots_dir() -> Path:
    """The optional element crops of design v1.1 §11, written by redline_shots."""
    return okuro_home() / "redline" / "shots"


# The entry file inside a materialized version directory, per kind.
ARTIFACT_ENTRY = "index.html"
PRISM_ENTRY = "deck.json"

# A supersede chain is a linked list and a corrupt row could make it a ring.
MAX_CHAIN = 200

# A tree hash over a large docs directory is a real cost, so the root that can
# be served is capped and the refusal says which cap was hit.
MAX_TREE_FILES = 2000
MAX_TREE_BYTES = 200 * 1024 * 1024

# The overlay's own subpath. Reserved so a document tree can never shadow it.
RESERVED_SUBPATH = "__redline__"

# 8 hours, refreshed on each open of the same version. Long enough for a
# working session, short enough that a leaked URL dies on its own.
TOKEN_TTL_SECONDS = 8 * 3600


class RedlineError(ValueError):
    """A policy refusal — an allowlist miss, an empty note, a bad kind."""


class RedlineTokenError(Exception):
    """A serve-route credential failure that already knows its HTTP status."""

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


# ---------------------------------------------------------------------------
# The file allowlist — config, not the project registry
# ---------------------------------------------------------------------------


def redline_roots() -> list[Path]:
    """Directories a file may be opened from, from ``~/.okuro/config.yaml``.

    Read the way ``review_sync.sync_config`` reads its block: yaml.safe_load,
    one top-level key, absent keys degrade to a safe default. The safe default
    here is EMPTY, and empty means no file input is available — never "all
    files".

    Deliberately NOT the project registry. The registry refuses the exact
    files this module was built for: the project holding them has a NULL path,
    and registering a path as a project must not be a precondition for
    commenting on a file.
    """
    try:
        import yaml
        raw = yaml.safe_load(CONFIG_PATH.read_text()) or {}
    except Exception:  # noqa: BLE001 — an unreadable config fails closed
        return []
    block = (raw.get("redline") or {}) if isinstance(raw, dict) else {}
    roots = block.get("roots") or []
    if isinstance(roots, str):
        roots = [roots]
    out: list[Path] = []
    for entry in roots:
        try:
            out.append(Path(str(entry)).expanduser().resolve())
        except Exception:  # noqa: BLE001
            continue
    return out


def check_file_allowed(ref: str) -> Path:
    """Resolve ``ref`` and refuse anything outside ``redline.roots``.

    Fails closed on an empty allowlist, and refuses a ``..`` segment before
    any filesystem call so a traversal never reaches ``resolve()``.
    """
    if not ref or not str(ref).strip():
        raise RedlineError("a file reference is required")
    raw = Path(str(ref)).expanduser()
    if any(part == ".." for part in raw.parts):
        raise RedlineError(f"path contains a '..' segment and is refused: {ref}")

    roots = redline_roots()
    if not roots:
        raise RedlineError(
            "redline.roots is empty in ~/.okuro/config.yaml — no file may be "
            "opened until a root is configured (this fails closed on purpose)"
        )
    resolved = raw.resolve()
    if not any(resolved == root or resolved.is_relative_to(root) for root in roots):
        raise RedlineError(
            f"path is outside redline.roots: {resolved}. Configured roots: "
            + ", ".join(str(r) for r in roots)
        )
    if not resolved.is_file():
        raise RedlineError(f"not a file: {resolved}")
    return resolved


def _check_contained(root: Path, candidate: Path) -> Path:
    """The same containment check, re-applied to every served subpath."""
    root = root.resolve()
    resolved = candidate.resolve()
    if not (resolved == root or resolved.is_relative_to(root)):
        raise RedlineError(f"path escapes the served root: {candidate}")
    return resolved


# ---------------------------------------------------------------------------
# What creates a version
# ---------------------------------------------------------------------------


def tree_hash(root: Path) -> str:
    """sha256 over the served TREE, not just the entry file.

    Every regular file under ``root``, sorted by relative path, hashed as
    ``relpath + NUL + bytes``. The entry file's own hash is not enough: the
    real mockups load ``./fonts/*``, ``./assets/*.png`` and ``./data/*.json``
    by relative path, so a stylesheet edit changes the page while the HTML
    sha does not.
    """
    digest = hashlib.sha256()
    files: list[Path] = []
    total_bytes = 0
    for path in sorted(root.rglob("*")):
        if path.name == RESERVED_SUBPATH or RESERVED_SUBPATH in path.parts:
            raise RedlineError(
                f"the tree contains a reserved '{RESERVED_SUBPATH}' entry "
                f"({path}) — that subpath belongs to the redline overlay and "
                "must not be shadowed by document content"
            )
        if not path.is_file():
            continue
        files.append(path)
        if len(files) > MAX_TREE_FILES:
            raise RedlineError(
                f"tree exceeds {MAX_TREE_FILES} files under {root} — open a "
                "document from a narrower directory"
            )
        total_bytes += path.stat().st_size
        if total_bytes > MAX_TREE_BYTES:
            raise RedlineError(
                f"tree exceeds {MAX_TREE_BYTES // (1024 * 1024)} MB under "
                f"{root} — open a document from a narrower directory"
            )
    for path in files:
        rel = str(path.relative_to(root))
        digest.update(rel.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
    return digest.hexdigest()


def artifact_chain(artifact_id: str) -> list[dict]:
    """The supersede chain holding ``artifact_id``, OLDEST first.

    Each link is one version, which is the whole reason an html artifact is a
    better redline subject than a file: okuro kept every link, so every
    version's bytes are still readable and a past version can be rendered
    against the bytes its comments were actually made on.

    Walking backwards uses the ``supersedes`` column. Walking FORWARD needs the
    reverse lookup, and :mod:`okuro.sense.artifacts` exposes none — so that one
    step is a direct read of the ``artifacts`` table, which is why the query is
    here rather than hidden behind a helper that would suggest otherwise.
    Both directions are cycle-guarded: a corrupt row would otherwise make a
    linked list into a ring and this function into a hang.
    """
    from okuro.sense.artifacts import artifact_get

    head = artifact_get(artifact_id, include_body=False)
    if head is None:
        raise RedlineError(f"unknown artifact: {artifact_id}")

    seen = {head["id"]}
    while head.get("supersedes") and len(seen) < MAX_CHAIN:
        older = artifact_get(head["supersedes"], include_body=False)
        if older is None or older["id"] in seen:
            break
        seen.add(older["id"])
        head = older

    # A FRESH guard for the forward walk. Reusing the backward one would have
    # every link it just visited already marked seen, so walking up from an
    # older link would stop at the root and report a one-version history for a
    # chain that has many.
    seen = {head["id"]}
    chain = [head]
    db = _db()
    while len(chain) < MAX_CHAIN:
        row = db.fetchone(
            "SELECT id FROM artifacts WHERE supersedes = ? ORDER BY created_at LIMIT 1",
            (chain[-1]["id"],),
        )
        if row is None:
            break
        newer_id = dict(row)["id"]
        if newer_id in seen:
            break
        seen.add(newer_id)
        newer = artifact_get(newer_id, include_body=False)
        if newer is None:
            break
        chain.append(newer)
    return chain


def _artifact_body(artifact_id: str) -> str:
    from okuro.sense.artifacts import artifact_get

    row = artifact_get(artifact_id, include_body=True)
    return (row or {}).get("body") or ""


def check_artifact_html(row: dict) -> None:
    """Refuse an artifact that is not an HTML document.

    Checked on the artifact being OPENED only, never on its ancestors. An
    ancestor that was markdown before an html rewrite still has retrievable
    bytes, and showing them is an honest picture of what that version was —
    whereas letting a markdown artifact IN would hand the owner a page of
    literal source to click on and call it a document.

    ``media_type`` is the authority; a missing one falls back to the body
    opening with an html root element, and the refusal says which of the two
    was checked so the answer is never a mystery.
    """
    media_type = (row.get("media_type") or "").lower()
    if "html" in media_type:
        return
    head = _artifact_body(row["id"]).lstrip()[:512].lower()
    if head.startswith("<!doctype html") or head.startswith("<html"):
        return
    raise RedlineError(
        f"artifact {row['id']} is not text/html: media_type is "
        f"{row.get('media_type')!r} and the body does not open with an html "
        "root element. redline comments on a rendered HTML document — a "
        "markdown artifact would be clicked on as literal source"
    )


def deck_hash(deck: dict) -> str:
    """sha256 of the DeckDoc JSON as it reads right now.

    The deck2 store is one flat JSON file per deck with last-write-wins and no
    revision, version or ``updated_at`` field of its own
    (``prism/compiler/deck_store.py``) — so the content IS the identity, and
    ``sort_keys`` with compact separators is what makes reading the same deck
    twice produce the same hash.
    """
    payload = json.dumps(deck, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def version_dir(document_id: str, seq: int) -> Path:
    return docs_dir() / document_id / str(int(seq))


def materialize(document_id: str, seq: int, entry: str, content: str) -> Path:
    """Write one version's own bytes and return the directory they live in.

    That directory becomes the version's ``root_path``, so serving it needs no
    new code: the containment check and the capability token already work on
    any ``root_path``.
    """
    target = version_dir(document_id, seq)
    target.mkdir(parents=True, exist_ok=True)
    (target / entry).write_text(content, encoding="utf-8")
    return target


def entry_name(kind: str, ref: str) -> str:
    """The subpath that serves a version's entry document, per kind."""
    if kind == "file":
        return Path(ref).name
    if kind == "artifact":
        return ARTIFACT_ENTRY
    return PRISM_ENTRY


# ---------------------------------------------------------------------------
# The token — never the global API bearer
# ---------------------------------------------------------------------------

# {token: {"document_id", "seq", "version_id", "expires_at"}}. IN PROCESS, never
# in the DB: a persisted credential is a credential to steal, and this one buys
# nothing but the bytes of one version of one document. Every token dies with
# the API process and the viewer re-opens.
_TOKENS: dict[str, dict[str, Any]] = {}


def mint_token(document_id: str, version_id: str, seq: int) -> tuple[str, str]:
    """Mint (or refresh) the capability token for one document version."""
    _expire_tokens()
    for token, entry in _TOKENS.items():
        if entry["document_id"] == document_id and entry["seq"] == int(seq):
            entry["expires_at"] = time.time() + TOKEN_TTL_SECONDS
            return token, _iso(entry["expires_at"])
    token = secrets.token_urlsafe(32)
    _TOKENS[token] = {
        "document_id": document_id,
        "version_id": version_id,
        "seq": int(seq),
        "expires_at": time.time() + TOKEN_TTL_SECONDS,
    }
    return token, _iso(_TOKENS[token]["expires_at"])


def check_token(token: str, document_id: str, seq: int) -> dict:
    """Validate an in-path token for exactly this document and version.

    401 when the token is unknown or expired — the viewer re-opens.
    403 when the token is real but names a DIFFERENT document version, and
    403 for the global API bearer, which is not a redline capability and must
    never behave like one. That distinction is the whole point of the split:
    the preview route's in-path token IS the global bearer, and a sandboxed
    frame can read it straight out of ``location.pathname``.
    """
    _expire_tokens()
    if not token:
        raise RedlineTokenError(401, "a redline capability token is required")
    try:
        from okuro.orchestrator.api.main import _API_TOKEN
        if _API_TOKEN and secrets.compare_digest(token, _API_TOKEN):
            raise RedlineTokenError(
                403,
                "the global API bearer is not a redline capability token — "
                "call redline_open to mint one scoped to this document version",
            )
    except ImportError:
        pass
    entry = _TOKENS.get(token)
    if entry is None:
        raise RedlineTokenError(401, "unknown or expired redline token")
    if entry["document_id"] != document_id or entry["seq"] != int(seq):
        raise RedlineTokenError(
            403, "this token belongs to a different document version"
        )
    return entry


def _expire_tokens() -> None:
    now = time.time()
    for token in [t for t, e in _TOKENS.items() if e["expires_at"] <= now]:
        _TOKENS.pop(token, None)


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def serve_url(document_id: str, seq: int, token: str, subpath: str) -> str:
    return f"/api/redline/doc/{document_id}/v/{seq}/serve/{token}/{subpath}"


def overlay_src(document_id: str, seq: int, token: str) -> str:
    return serve_url(document_id, seq, token, f"{RESERVED_SUBPATH}/overlay.js")


def inject_overlay(
    html: str, document_id: str, version_id: str, seq: int, token: str
) -> str:
    """Insert the overlay script tag exactly once, before ``</body>``.

    The src is ABSOLUTE because the injected line must work at any subpath
    depth, and it lives inside the same token scope so one regex and one
    credential cover the document and its overlay.
    """
    src = overlay_src(document_id, seq, token)
    if f"{RESERVED_SUBPATH}/overlay.js" in html:
        return html
    tag = (
        f'<script src="{src}" data-redline-doc="{document_id}" '
        f'data-redline-version="{version_id}" data-redline-seq="{seq}"></script>'
    )
    lowered = html.lower()
    idx = lowered.rfind("</body>")
    if idx < 0:
        return html + tag
    return html[:idx] + tag + html[idx:]


# ---------------------------------------------------------------------------
# Documents and versions
# ---------------------------------------------------------------------------


def _db():
    from okuro.db import get_db
    return get_db()


def _row(row) -> Optional[dict]:
    return dict(row) if row else None


def open_document(
    kind: str,
    ref: str,
    *,
    title: Optional[str] = None,
    project: Optional[str] = None,
    reload: bool = False,
    version_seq: Optional[int] = None,
    shots: bool = False,
) -> dict:
    """Open a document and pin the bytes as a version.

    ``reload`` is accepted for callers that want to say "I know it changed".
    It changes nothing: the source is read on EVERY open, never on a timer, so
    a changed byte always produces a version and unchanged bytes never do.

    ``version_seq`` opens an EXISTING version read-only — no hashing, no new
    version, no chain walk beyond finding the document. That is what the
    viewer's version switcher calls, and it is only useful for the two kinds
    whose bytes okuro owns: a past file version kept its hash and never its
    content, so it answers with ``url: None`` and says why rather than serving
    today's bytes under yesterday's number.

    ``shots`` asks for the optional element crops of design v1.1 §11. It is OFF
    by default because it launches a headless browser, and the viewer opens a
    document on every mount — a picture is worth a browser launch when an agent
    asks for one, never as a side effect of looking at a page. The result
    carries what happened, including the reason nothing was made, so the answer
    is given once rather than showing up as a silent ``screenshot: null``.
    """
    if kind not in KINDS:
        raise RedlineError(f"kind must be one of {KINDS}")

    db = _db()
    resolved = _resolve_ref(kind, ref)
    document = _document_for(db, kind, resolved, title=title, project=project)
    document_id = document["id"]

    if version_seq is not None:
        version = _row(db.fetchone(
            "SELECT * FROM redline_versions WHERE document_id = ? AND seq = ?",
            (document_id, int(version_seq)),
        ))
        if version is None:
            raise RedlineError(
                f"document {document_id} has no version {version_seq}"
            )
        return _opened(
            db, document, version,
            created_version=False, previous=None, shots=shots,
        )

    previous = _row(db.fetchone(
        "SELECT * FROM redline_versions WHERE document_id = ? "
        "ORDER BY seq DESC LIMIT 1",
        (document_id,),
    ))
    if kind == "file":
        version, created_version = _version_for_file(db, document, resolved)
    elif kind == "artifact":
        version, created_version = _version_for_artifact(db, document, resolved)
    else:
        version, created_version = _version_for_prism(db, document, resolved)

    return _opened(
        db, document, version,
        created_version=created_version, previous=previous, shots=shots,
    )


def _resolve_ref(kind: str, ref: str) -> dict:
    """Canonicalize a ref per kind, and refuse it here rather than later.

    Returns the facts every later step needs, so the allowlist check, the
    chain walk and the deck read each happen exactly once per open.
    """
    if kind == "file":
        path = check_file_allowed(ref)
        return {"canonical": str(path), "title": path.name, "path": path}

    if kind == "artifact":
        chain = artifact_chain(ref)
        newest = chain[-1]
        # The NEWEST link is what "the document" is now, so that is the one
        # that has to be an html document. An older link is history.
        check_artifact_html(newest)
        return {
            "canonical": chain[0]["id"],
            "title": newest.get("title") or chain[0]["id"],
            "project": newest.get("project"),
            "chain": chain,
        }

    from okuro.prism.compiler import deck_store

    deck = deck_store.get_deck(ref)
    if deck is None:
        raise RedlineError(
            f"deck not found in the deck2 store: {ref} — prism_list names the "
            "decks that exist"
        )
    return {
        "canonical": str(ref),
        "title": deck.get("title") or str(ref),
        "deck": deck,
    }


def _document_for(
    db, kind: str, resolved: dict, *, title: Optional[str], project: Optional[str]
) -> dict:
    document = _row(db.fetchone(
        "SELECT * FROM redline_documents WHERE kind = ? AND ref = ?",
        (kind, resolved["canonical"]),
    ))
    if document is not None:
        return document
    document_id = str(uuid.uuid4())
    db.execute(
        "INSERT INTO redline_documents (id, kind, ref, title, project) "
        "VALUES (?, ?, ?, ?, ?)",
        (
            document_id, kind, resolved["canonical"],
            (title or resolved["title"]),
            (project or resolved.get("project")),
        ),
    )
    return _row(db.fetchone(
        "SELECT * FROM redline_documents WHERE id = ?", (document_id,))) or {}


def _next_seq(db, document_id: str) -> int:
    row = db.fetchone(
        "SELECT COALESCE(MAX(seq), 0) AS n FROM redline_versions WHERE document_id = ?",
        (document_id,),
    )
    return int(dict(row)["n"]) + 1


def _insert_version(
    db, document_id: str, content_hash: str, root_path: Optional[str],
    *, label: Optional[str] = None,
) -> dict:
    version_id = str(uuid.uuid4())
    db.execute(
        "INSERT INTO redline_versions "
        "(id, document_id, seq, content_hash, root_path, label) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (version_id, document_id, _next_seq(db, document_id), content_hash,
         root_path, label),
    )
    return _row(db.fetchone(
        "SELECT * FROM redline_versions WHERE id = ?", (version_id,))) or {}


def _version_by_hash(db, document_id: str, content_hash: str) -> Optional[dict]:
    return _row(db.fetchone(
        "SELECT * FROM redline_versions WHERE document_id = ? AND content_hash = ?",
        (document_id, content_hash),
    ))


def _version_for_file(db, document: dict, resolved: dict) -> tuple[dict, bool]:
    """One version per distinct tree hash, served from where the file lives."""
    root = resolved["path"].parent
    content_hash = tree_hash(root)
    version = _version_by_hash(db, document["id"], content_hash)
    if version is not None:
        return version, False
    return _insert_version(db, document["id"], content_hash, str(root)), True


def _version_for_artifact(db, document: dict, resolved: dict) -> tuple[dict, bool]:
    """One version per link of the supersede chain, oldest first.

    Every link is materialized, not only the newest — that is what makes an
    artifact document the one input whose PAST versions can be rendered
    read-only against the bytes their comments were made on.

    The body is written only when the file is missing. A link's body never
    changes (a change is a new artifact and therefore a new link), so
    re-writing it on every open would be work with no possible effect.
    """
    created_newest = False
    version: Optional[dict] = None
    for link in resolved["chain"]:
        existing = _version_by_hash(db, document["id"], link["id"])
        if existing is None:
            existing = _insert_version(
                db, document["id"], link["id"], None,
                label=(link.get("created_at") or None),
            )
            created_newest = True
        target = version_dir(document["id"], existing["seq"])
        if not (target / ARTIFACT_ENTRY).is_file():
            materialize(
                document["id"], existing["seq"], ARTIFACT_ENTRY,
                _artifact_body(link["id"]),
            )
        if existing.get("root_path") != str(target):
            db.execute(
                "UPDATE redline_versions SET root_path = ? WHERE id = ?",
                (str(target), existing["id"]),
            )
            existing = _row(db.fetchone(
                "SELECT * FROM redline_versions WHERE id = ?",
                (existing["id"],))) or existing
        version = existing
    if version is None:  # pragma: no cover — a chain always has one link
        raise RedlineError(f"artifact {document['ref']} has no chain to open")
    return version, created_newest


def _version_for_prism(db, document: dict, resolved: dict) -> tuple[dict, bool]:
    """One version per distinct DeckDoc JSON, snapshotted beside it.

    The deck2 store is last-write-wins with no revision field, so the JSON as
    it read at open is the only identity available — and the snapshot is what
    keeps that identity honest: without it the hash would name bytes nobody
    could produce again, and a past prism version would be a number with no
    document behind it.
    """
    deck = resolved["deck"]
    content_hash = deck_hash(deck)
    version = _version_by_hash(db, document["id"], content_hash)
    created = version is None
    if version is None:
        version = _insert_version(db, document["id"], content_hash, None)
    target = version_dir(document["id"], version["seq"])
    if not (target / PRISM_ENTRY).is_file():
        materialize(
            document["id"], version["seq"], PRISM_ENTRY,
            json.dumps(deck, sort_keys=True, separators=(",", ":")),
        )
    if version.get("root_path") != str(target):
        db.execute(
            "UPDATE redline_versions SET root_path = ? WHERE id = ?",
            (str(target), version["id"]),
        )
        version = _row(db.fetchone(
            "SELECT * FROM redline_versions WHERE id = ?",
            (version["id"],))) or version
    return version, created


def is_renderable(document: dict, version: dict) -> bool:
    """Whether THIS version's own bytes can still be shown.

    True for every artifact and prism version — okuro wrote their bytes into
    the version's own directory. For a file, true only for the version whose
    hash still matches what is on disk: the others kept a hash and nothing
    else, and rendering today's bytes under an older version's number is the
    cross-version guess this module refuses to make.
    """
    root_path = version.get("root_path")
    if not root_path:
        return False
    if document["kind"] != "file":
        return (Path(root_path) / entry_name(document["kind"], document["ref"])).is_file()
    try:
        return tree_hash(Path(root_path)) == version["content_hash"]
    except (RedlineError, OSError):
        return False


def _opened(
    db, document: dict, version: dict, *,
    created_version: bool, previous: Optional[dict], shots: bool = False,
) -> dict:
    document_id = document["id"]
    kind = document["kind"]
    token, expires_at = mint_token(document_id, version["id"], version["seq"])
    renderable = is_renderable(document, version)
    entry = entry_name(kind, document["ref"])

    url: Optional[str] = None
    deck_url: Optional[str] = None
    note: Optional[str] = None
    if not renderable:
        note = (
            f"v{version['seq']} kept its content hash and not its bytes, so it "
            "cannot be rendered. Its comments are still listed and still carry "
            "their captured excerpt — that excerpt is what to act on."
        )
    elif kind == "prism":
        # A DeckDoc is not html. The SPA's own deck runtime renders it inside an
        # open shadow root, so there is no document to serve into the sandbox —
        # what is served is the deck JSON the runtime reads.
        deck_url = serve_url(document_id, version["seq"], token, entry)
    else:
        url = serve_url(document_id, version["seq"], token, entry)

    carried: Optional[dict] = None
    if created_version and previous is not None:
        carried = {
            "version_seq": previous["seq"],
            "open_comments": _count(db, previous["id"], "open"),
        }

    shot_report: Optional[dict] = None
    if shots:
        # Imported here so the optional browser extra is never a condition of
        # importing this module, and never fatal to an open.
        from okuro.sense import redline_shots
        try:
            shot_report = redline_shots.crop_version(
                document_id, version_id=version["id"]
            )
        except Exception as exc:  # noqa: BLE001 — a crop is never worth an open
            logger.warning("redline: crops failed for %s: %s", document_id, exc)
            shot_report = {
                "made": 0, "skipped": 0, "paths": [],
                "reason": f"the crop run raised {type(exc).__name__}: {exc}",
            }

    return {
        "document_id": document_id,
        "version_id": version["id"],
        "version_seq": version["seq"],
        "content_hash": version["content_hash"],
        "created_version": created_version,
        "url": url,
        "deck_url": deck_url,
        "renderable": renderable,
        "note": note,
        "web": f"/redline/{document_id}?v={version['seq']}",
        "token_expires_at": expires_at,
        "open_comments": _count(db, version["id"], "open"),
        "carried_over_from": carried,
        "shots": shot_report,
    }


def _count(db, version_id: str, status: str) -> int:
    row = db.fetchone(
        "SELECT COUNT(*) AS n FROM redline_comments WHERE version_id = ? "
        "AND status = ? AND parent_id IS NULL AND tombstoned_at IS NULL",
        (version_id, status),
    )
    return int(dict(row)["n"]) if row else 0


def versions(document_id: str) -> dict:
    """Every version of one document, newest first."""
    db = _db()
    document = _row(db.fetchone(
        "SELECT * FROM redline_documents WHERE id = ?", (document_id,)))
    if document is None:
        raise RedlineError(f"unknown document: {document_id}")
    rows = [dict(r) for r in db.fetchall(
        "SELECT * FROM redline_versions WHERE document_id = ? ORDER BY seq DESC",
        (document_id,),
    )]
    current = rows[0]["id"] if rows else None
    return {
        "document": {
            k: document[k] for k in ("id", "kind", "ref", "title", "project")
        },
        "versions": [
            {
                "id": r["id"],
                "seq": r["seq"],
                "content_hash": r["content_hash"],
                "label": r["label"],
                "captured_at": r["captured_at"],
                "is_current": r["id"] == current,
                # Whether this version's OWN bytes can still be shown. The
                # switcher needs it to tell "past and readable" (artifact,
                # prism) from "past and hash-only" (file), which are two
                # different offers to the owner and must not look alike.
                "renderable": is_renderable(document, r),
                "open": _count(db, r["id"], "open"),
                "done": _count(db, r["id"], "done"),
            }
            for r in rows
        ],
    }


def tombstone_document(document_id: str) -> dict:
    """Tombstone, never delete — the comments stay readable forever."""
    db = _db()
    db.execute(
        "UPDATE redline_documents SET tombstoned_at = datetime('now') WHERE id = ?",
        (document_id,),
    )
    return _row(db.fetchone(
        "SELECT * FROM redline_documents WHERE id = ?", (document_id,))) or {}


# ---------------------------------------------------------------------------
# Comments
# ---------------------------------------------------------------------------


def _with_src(db, version: dict, anchor: dict) -> dict:
    """Fill ``anchor.src`` at CAPTURE time, for file-backed documents.

    The DOM has no memory of where a tag was written, so the overlay emits
    ``src: null`` and this is where ``{line, col}`` is added — against the
    served bytes, once, before the anchor is stored. It has to happen here
    rather than at read time: the anchor is immutable after creation, and
    ``redline_list`` only reads it back.

    An anchor that already carries ``src`` (one built server-side by
    ``build_anchor``) is left alone, and an unresolvable element yields
    ``None`` rather than a guessed line — a line number on a file whose one
    line holds 3,000 characters is worse than no line at all.

    File-backed documents ONLY, per design v1.1 §4.1. An artifact's bytes live
    in a materialized copy and a deck has no source lines at all, so a
    ``line:col`` there would point at a file the agent must not edit.
    """
    if anchor.get("src"):
        return anchor
    document = _row(db.fetchone(
        "SELECT * FROM redline_documents WHERE id = ?", (version["document_id"],)))
    if document is None or document["kind"] != "file":
        return anchor
    source = _version_source(document, version)
    if source is None:
        return anchor
    return {**anchor, "src": redline_anchor.src_for_anchor(source, anchor)}


def add_comment(
    version_id: str,
    body: str,
    *,
    anchor: Optional[dict] = None,
    parent_id: Optional[str] = None,
    author: str = "owner",
) -> dict:
    """Write one comment onto ONE version. A reply carries no anchor."""
    if not (body or "").strip():
        raise RedlineError("a comment needs a body")
    db = _db()
    version = _row(db.fetchone(
        "SELECT * FROM redline_versions WHERE id = ?", (version_id,)))
    if version is None:
        raise RedlineError(f"unknown version: {version_id}")
    if parent_id and anchor:
        raise RedlineError("a reply is text about a comment; it has no anchor")
    if anchor is not None:
        redline_anchor.validate_anchor(anchor)
        anchor = _with_src(db, version, anchor)

    row = db.fetchone(
        "SELECT COALESCE(MAX(seq), 0) AS n FROM redline_comments WHERE version_id = ?",
        (version_id,),
    )
    seq = int(dict(row)["n"]) + 1
    comment_id = str(uuid.uuid4())
    db.execute(
        "INSERT INTO redline_comments "
        "(id, version_id, document_id, seq, anchor, body, author, parent_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            comment_id, version_id, version["document_id"], seq,
            (json.dumps(anchor) if anchor is not None else None),
            body.strip(), author, parent_id,
        ),
    )
    return _row(db.fetchone(
        "SELECT * FROM redline_comments WHERE id = ?", (comment_id,))) or {}


def reply(comment_id: str, body: str, *, author: str = "owner") -> dict:
    """One reply level. The schema's trigger refuses a reply to a reply."""
    db = _db()
    parent = _row(db.fetchone(
        "SELECT * FROM redline_comments WHERE id = ?", (comment_id,)))
    if parent is None:
        raise RedlineError(f"unknown comment: {comment_id}")
    return add_comment(
        parent["version_id"], body, parent_id=comment_id, author=author
    )


def resolve_comment(
    comment_id: str,
    note: str,
    *,
    after_excerpt: Optional[str] = None,
    done_by: Optional[str] = None,
) -> dict:
    """Mark one comment done. The NOTE is the evidence.

    The excerpt pair is a hint shown beside it and never a proof: a cloned
    element's outerHTML is byte-identical to the original, and a
    whitespace-only reformat changes every hash. So a suspicious pair produces
    a WARNING and never a refusal.
    """
    if not (note or "").strip():
        raise RedlineError(
            "a resolve needs a note saying what you did — this is refused by "
            "the schema too, not only here"
        )
    db = _db()
    row = _row(db.fetchone(
        "SELECT * FROM redline_comments WHERE id = ?", (comment_id,)))
    if row is None:
        raise RedlineError(f"unknown comment: {comment_id}")

    who = (done_by or "agent").strip() or "agent"
    excerpt = (after_excerpt or None)
    if excerpt:
        excerpt = excerpt[:redline_anchor.OUTER_HTML_CAP]
    db.execute(
        "UPDATE redline_comments SET status = 'done', "
        "done_at = datetime('now'), done_by = ?, done_note = ?, after_excerpt = ? "
        "WHERE id = ?",
        (who, note.strip(), excerpt, comment_id),
    )
    updated = _row(db.fetchone(
        "SELECT * FROM redline_comments WHERE id = ?", (comment_id,))) or {}

    before = (_anchor_of(row) or {}).get("outer_html")
    warning = None
    if excerpt is None:
        warning = "no after-excerpt supplied; the note is the only evidence"
    elif before is not None and excerpt == before:
        warning = "the excerpt is unchanged — did you edit the right element?"

    return {
        "comment_id": comment_id,
        "status": updated.get("status"),
        "done_at": updated.get("done_at"),
        "done_by": updated.get("done_by"),
        "before_excerpt": before,
        "after_excerpt": excerpt,
        "warning": warning,
    }


def reopen_comment(comment_id: str) -> dict:
    """Uncheck-to-unresolve. All four done columns clear in one UPDATE."""
    db = _db()
    db.execute(
        "UPDATE redline_comments SET status = 'open', done_at = NULL, "
        "done_by = NULL, done_note = NULL, after_excerpt = NULL WHERE id = ?",
        (comment_id,),
    )
    return _row(db.fetchone(
        "SELECT * FROM redline_comments WHERE id = ?", (comment_id,))) or {}


def reanchor(
    comment_id: str,
    version_id: str,
    anchor: dict,
    *,
    note: Optional[str] = None,
    created_by: str = "owner",
) -> dict:
    """Hand-point an old comment at an element in a NEW version.

    The original anchor is never updated — this writes a row whose ``manual``
    column is CHECK-constrained to 1, so a machine-guessed re-anchor cannot be
    written even by the sqlite3 binary.
    """
    redline_anchor.validate_anchor(anchor)
    db = _db()
    db.execute(
        "INSERT INTO redline_reanchors "
        "(comment_id, version_id, anchor, note, created_by, manual) "
        "VALUES (?, ?, ?, ?, ?, 1)",
        (comment_id, version_id, json.dumps(anchor), note, created_by),
    )
    return _row(db.fetchone(
        "SELECT * FROM redline_reanchors WHERE comment_id = ? AND version_id = ?",
        (comment_id, version_id),
    )) or {}


def _anchor_of(row: dict) -> Optional[dict]:
    raw = row.get("anchor")
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return None


def list_comments(
    document_id: str,
    *,
    version_id: Optional[str] = None,
    status: str = "open",
    include_replies: bool = True,
    include_anchor: bool = False,
    format: str = "json",
) -> dict:
    """The comments on ONE version, with the excerpt an agent acts on.

    ``anchor_state`` is computed against the version's own bytes at read time
    when there are any, and is ``unresolved`` when there are not — a prism
    deck, whose anchors only a browser inside the shadow root can resolve, or a
    past file version, which kept its hash and not its content. It is never
    carried across versions: this function will not look at another version's
    bytes, because 0 of 5 anchors survived the real regeneration and an
    automatic mapping would be a guess dressed as a resolution.

    ``format="markdown"`` returns the paste-into-prompt block of design v1.1
    §10 instead of the rows. Same rows, same function, one shape derived from
    the other — a second export path is a second thing to keep in step.
    """
    if status not in ("open", "done", "all"):
        raise RedlineError("status must be open, done or all")
    if format not in ("json", "markdown"):
        raise RedlineError("format must be json or markdown")

    db = _db()
    document = _row(db.fetchone(
        "SELECT * FROM redline_documents WHERE id = ?", (document_id,)))
    if document is None:
        raise RedlineError(f"unknown document: {document_id}")

    if version_id:
        version = _row(db.fetchone(
            "SELECT * FROM redline_versions WHERE id = ? AND document_id = ?",
            (version_id, document_id),
        ))
    else:
        version = _row(db.fetchone(
            "SELECT * FROM redline_versions WHERE document_id = ? "
            "ORDER BY seq DESC LIMIT 1",
            (document_id,),
        ))
    if version is None:
        raise RedlineError(f"no version to list for document {document_id}")

    sql = (
        "SELECT * FROM redline_comments WHERE version_id = ? "
        "AND parent_id IS NULL AND tombstoned_at IS NULL"
    )
    params: list[Any] = [version["id"]]
    if status != "all":
        sql += " AND status = ?"
        params.append(status)
    rows = [dict(r) for r in db.fetchall(sql + " ORDER BY seq", tuple(params))]

    doc_source = _version_source(document, version)
    parsed = (
        redline_anchor.parse_document(doc_source) if doc_source is not None else None
    )

    comments = [
        _comment_shape(
            db, document, version, row, parsed, include_replies, include_anchor
        )
        for row in rows
    ]
    listing = {
        "document": {
            k: document[k] for k in ("id", "kind", "ref", "title", "project")
        },
        "version": {
            "id": version["id"],
            "seq": version["seq"],
            "content_hash": version["content_hash"],
            "captured_at": version["captured_at"],
            "root_path": version["root_path"],
            "renderable": is_renderable(document, version),
        },
        "counts": {
            "open": _count(db, version["id"], "open"),
            "done": _count(db, version["id"], "done"),
        },
        "comments": comments,
    }
    if format == "markdown":
        markdown = render_markdown(listing, status=status)
        # The rows are dropped on purpose (design v1.1 §9.2): the block IS the
        # payload, and shipping both doubles a prompt's cost to say one thing.
        return {**listing, "comments": [], "markdown": markdown}
    return {**listing, "markdown": None}


def _version_source(document: dict, version: dict) -> Optional[str]:
    """The HTML to resolve THIS version's anchors against, or ``None``.

    * ``file`` — the entry file on disk. Only the current version's bytes exist
      there, so a past version has no source and its comments read
      ``unresolved`` rather than being resolved against the wrong bytes.
    * ``artifact`` — the version's OWN materialized copy. Every version has one,
      so a past artifact version resolves against exactly what it was.
    * ``prism`` — ``None``, always. A DeckDoc is not HTML and this module's
      resolver parses HTML; the deck is resolved in the browser, inside the
      shadow root, and nowhere else.
    """
    kind = document["kind"]
    if kind == "prism":
        return None
    if kind == "file":
        if not is_renderable(document, version):
            return None
        try:
            return Path(document["ref"]).read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None
    root_path = version.get("root_path")
    if not root_path:
        return None
    try:
        return (Path(root_path) / ARTIFACT_ENTRY).read_text(
            encoding="utf-8", errors="replace"
        )
    except OSError:
        return None


def _anchor_identity(anchor: Optional[dict]) -> str:
    """What the anchor CARRIES, for the case where nothing resolved it.

    A resolved comment's ``identified_by`` names the tiers that agreed. When
    there is no source to resolve against — a prism deck, a past file version —
    that label would otherwise default to ``"position"`` and quietly claim the
    weakest identity for an element that in fact carries a unique ``title``.
    This describes the anchor instead of the page, and the state stays
    ``unresolved`` so the two claims can never be confused.
    """
    if not anchor:
        return "position"
    chain = anchor.get("chain") or []
    first = chain[0] if chain else {}
    if first.get("id"):
        return "id"
    attrquote = anchor.get("attrquote") or {}
    if attrquote.get("n"):
        return str(attrquote["n"])
    if first.get("text"):
        return "text"
    return "position"


def _shot_path(document_id: str, version_seq: int, comment_seq: int) -> Path:
    return (
        shots_dir() / document_id / str(int(version_seq))
        / f"{int(comment_seq)}.png"
    )


def _comment_shape(
    db,
    document: dict,
    version: dict,
    row: dict,
    parsed,
    include_replies: bool,
    include_anchor: bool = False,
) -> dict:
    anchor = _anchor_of(row)
    state = "unresolved"
    # Two different questions, and they diverge exactly when resolution fails.
    # `anchor_identity` is what the anchor CARRIES; `identified_by` is what
    # AGREED when it was resolved. An orphan whose element holds a unique title
    # has identity "title" and verdict "position", and collapsing the two would
    # either claim an agreement that did not happen or hide the attribute a
    # reader needs to find the element by hand.
    anchor_identity = _anchor_identity(anchor)
    identified_by = anchor_identity
    if anchor and parsed is not None:
        outcome = redline_anchor.resolve(parsed, anchor)
        state = outcome["state"]
        identified_by = outcome["identified_by"]

    chain = (anchor or {}).get("chain") or []
    # File-backed documents ONLY, whatever the stored anchor happens to carry.
    # An anchor built server-side fills `src` from whichever bytes it parsed,
    # and for an artifact those are a materialized COPY — a line number there
    # points at a file an edit would be thrown away on. The gate belongs here,
    # on the way out, so no path can leak one.
    src = (
        ((anchor or {}).get("src") or {}) if document["kind"] == "file" else {}
    )
    replies: list[dict] = []
    if include_replies:
        replies = [
            {
                "id": r["id"], "body": r["body"],
                "author": r["author"], "created_at": r["created_at"],
            }
            for r in (dict(x) for x in db.fetchall(
                "SELECT * FROM redline_comments WHERE parent_id = ? ORDER BY seq",
                (row["id"],),
            ))
        ]

    re_row = _row(db.fetchone(
        "SELECT r.*, v.seq AS version_seq FROM redline_reanchors r "
        "JOIN redline_versions v ON v.id = r.version_id "
        "WHERE r.comment_id = ? ORDER BY r.created_at DESC LIMIT 1",
        (row["id"],),
    ))
    reanchored_to = None
    if re_row:
        try:
            new_anchor = json.loads(re_row["anchor"])
            reanchored_to = {
                "version_seq": re_row["version_seq"],
                "path": redline_anchor.human_path(new_anchor.get("chain") or []),
            }
        except (TypeError, ValueError):
            reanchored_to = {"version_seq": re_row["version_seq"], "path": None}

    shape = {
        "id": row["id"],
        "seq": row["seq"],
        "status": row["status"],
        "body": row["body"],
        "author": row["author"],
        "created_at": row["created_at"],
        "version_seq": version["seq"],
        "content_hash": version["content_hash"],
        "anchor_state": state,
        "path": redline_anchor.human_path(chain) if chain else None,
        "element": (anchor or {}).get("outer_html"),
        "text": (chain[0].get("text") if chain else None),
        "identified_by": identified_by,
        # What the ANCHOR carries, whatever the resolution concluded. This is
        # the label to read when the state is orphan or unresolved.
        "anchor_identity": anchor_identity,
        # ABSOLUTE — an agent in another working directory needs it. Only a
        # file document has one: an artifact's bytes are a materialized copy
        # and a deck has no source file, so naming a path there would invite
        # an edit that the next regeneration throws away.
        "file": (document["ref"] if document["kind"] == "file" else None),
        "line": src.get("line"),
        "col": src.get("col"),
        # The shadow-host chain from the document root down to the root the
        # element lives in. Empty for an ordinary document; non-empty for a
        # prism deck, and then it is the only thing that says WHERE the
        # element is — no document-level selector can reach it.
        "hosts": (anchor or {}).get("hosts") or [],
        # An optional crop from redline_shots, reported only when the file is
        # actually there. Absent is the normal state, not an error.
        "screenshot": (
            str(shot) if (shot := _shot_path(
                document["id"], version["seq"], row["seq"])).is_file() else None
        ),
        "box": (anchor or {}).get("box"),
        "replies": replies,
        "done_at": row["done_at"],
        "done_by": row["done_by"],
        "done_note": row["done_note"],
        "after_excerpt": row["after_excerpt"],
        "reanchored_to": reanchored_to,
    }
    if include_anchor:
        # The VIEWER needs the whole anchor — its in-browser resolver runs the
        # same five tiers against the live DOM to place the bubble. An agent
        # does not: it acts on `element`, `text` and `file:line:col`, and a
        # chain of up to 24 rungs per comment in an MCP result is context it
        # cannot use. Opt-in for that reason, not because it is optional data.
        shape["anchor"] = anchor
    return shape


# ---------------------------------------------------------------------------
# The paste-into-prompt block
# ---------------------------------------------------------------------------

# The label grid of design v1.1 §10. Two widths, because the header's labels
# are longer than the items' — and a grid whose columns drift is a grid an eye
# stops using.
_HEAD_WIDTH = 11
_ITEM_WIDTH = 10

# What creates the NEXT version, per kind. The note has to name the actual act,
# because "regenerate the file" is not what supersedes an artifact.
_NEXT_VERSION_ACT = {
    "file": "Regenerating the file",
    "artifact": "Superseding the artifact",
    "prism": "Editing the deck",
}


def _field(label: str, value: str, width: int) -> str:
    """One ``label: value`` line, continuation lines aligned under the value.

    Nothing is truncated and no newline is stripped: the element excerpt is
    what the next agent acts on, so it keeps its own shape and only its
    indentation is ours.
    """
    pad = label.ljust(width)
    lines = str(value).split("\n")
    out = [f"{pad}{lines[0]}"]
    out.extend(" " * width + line for line in lines[1:])
    return "\n".join(out)


def _text_field(comment: dict) -> str:
    """The ``text:`` line, which is never blank.

    An empty value here is the exact failure F5 measured: a TextQuoteSelector
    with ``exact=""`` matched 131 elements on the real page. So a text-free
    element says so, and says what identifies it instead.

    The label comes from ``anchor_identity`` — what the ANCHOR carries — and not
    from the resolution's verdict. Measured on the real page: a bubble whose
    title repeats four times resolves to an orphan with verdict ``position``
    while still holding that title, and the title is what a reader searching
    the file by hand actually uses.
    """
    if comment.get("text"):
        return str(comment["text"])
    by = comment.get("anchor_identity") or comment.get("identified_by")
    if by and by not in ("position", "text"):
        return (
            f"(none — this element has no text; it is identified by its {by} "
            "attribute)"
        )
    return (
        "(none — this element has no text; it is identified by its position in "
        "the chain)"
    )


def _warning_field(comment: dict, version: dict, kind: str) -> Optional[str]:
    """Why a pointer is weaker than ``exact``, when it is."""
    state = comment.get("anchor_state")
    if state == "exact":
        return None
    if state == "orphan":
        return (
            "the overlay could not place this on the page when the version was "
            "last opened.\nThe excerpt above is what was captured — act on "
            "that, not on a live lookup."
        )
    if kind == "prism":
        return (
            "this element lives inside the deck's shadow root, which only a "
            "browser can\nreach — so the anchor was not resolved here and the "
            "state is unresolved, not exact.\nThe excerpt above is what was "
            "captured."
        )
    if not version.get("renderable"):
        return (
            f"v{version['seq']} kept its content hash and not its bytes, so "
            "this anchor was not\nresolved against anything. The excerpt above "
            "is what was captured."
        )
    return (
        "this version's bytes were not read, so the anchor was not resolved. "
        "The excerpt\nabove is what was captured."
    )


def render_markdown(listing: dict, *, status: str = "open") -> str:
    """The block an agent pastes into a prompt — design v1.1 §10.

    One block per comment carrying the number, the status, the version, WHY the
    pointer can be trusted, the excerpt, and the comment. It is deliberately
    flat text rather than JSON: this is read by a model inside a prompt, and
    the excerpt is the payload because no cross-version re-anchoring is
    attempted anywhere in this module.
    """
    document = listing["document"]
    version = listing["version"]
    counts = listing["counts"]
    comments = listing["comments"]
    kind = document["kind"]

    ident = document.get("title") or document["ref"]
    if document.get("project"):
        ident = f"{document['project']} · {ident}"
    head = [
        f"## redline — {ident} · v{version['seq']} · {counts['open']} open",
        _field("document:", f"{kind} · {document['ref']}", _HEAD_WIDTH),
        _field(
            "version:",
            f"{version['seq']} · hash {str(version['content_hash'])[:8]}… · "
            f"captured {version['captured_at']}",
            _HEAD_WIDTH,
        ),
        _field(
            "resolve:",
            'redline_resolve(comment_id=…, note="what you changed", '
            'after_excerpt="<new outerHTML>")',
            _HEAD_WIDTH,
        ),
        _field(
            "note:",
            f"these comments belong to VERSION {version['seq']}. "
            f"{_NEXT_VERSION_ACT.get(kind, 'Changing the source')} creates "
            f"version\n{version['seq'] + 1} with an empty comment list — "
            "resolve each item below rather than expecting it\nto carry over.",
            _HEAD_WIDTH,
        ),
    ]
    if status != "open":
        head.append(
            _field("listing:", f"status={status} · {len(comments)} shown", _HEAD_WIDTH)
        )

    blocks = ["\n".join(head)]
    if not comments:
        blocks.append(
            f"_No {status if status != 'all' else ''} comments on this version._".replace(
                "  ", " "
            )
        )
    for comment in comments:
        lines = [
            f"### #{comment['seq']} · {comment['status']} · "
            f"{comment['created_at']} · anchor {comment['anchor_state']} · "
            f"identified by {comment['identified_by']}",
            _field("where:", comment.get("path") or "(no chain recorded)", _ITEM_WIDTH),
        ]
        if comment.get("hosts"):
            lines.append(
                _field(
                    "shadow:",
                    " › ".join(str(h) for h in comment["hosts"])
                    + "  (the element is inside this shadow root)",
                    _ITEM_WIDTH,
                )
            )
        if comment.get("file") and comment.get("line"):
            lines.append(
                _field(
                    "file:",
                    f"{comment['file']}:{comment['line']}:{comment.get('col') or 1}",
                    _ITEM_WIDTH,
                )
            )
        element = comment.get("element")
        if element:
            lines.append(
                _field(
                    "element:",
                    str(element)[:redline_anchor.OUTER_HTML_CAP],
                    _ITEM_WIDTH,
                )
            )
        lines.append(_field("text:", _text_field(comment), _ITEM_WIDTH))
        if comment.get("screenshot"):
            lines.append(_field("shot:", str(comment["screenshot"]), _ITEM_WIDTH))
        lines.append(_field("comment:", comment.get("body") or "", _ITEM_WIDTH))
        for reply in comment.get("replies") or []:
            lines.append(
                _field("reply:", f"{reply['author']}: {reply['body']}", _ITEM_WIDTH)
            )
        if comment.get("status") == "done":
            lines.append(
                _field("done:", comment.get("done_note") or "(no note)", _ITEM_WIDTH)
            )
            if comment.get("after_excerpt"):
                lines.append(
                    _field(
                        "after:",
                        str(comment["after_excerpt"])[:redline_anchor.OUTER_HTML_CAP],
                        _ITEM_WIDTH,
                    )
                )
        warning = _warning_field(comment, version, kind)
        if warning:
            lines.append(_field("warning:", warning, _ITEM_WIDTH))
        # Last, and unpadded, because it is the one field the agent must copy
        # verbatim into redline_resolve.
        lines.append(f"comment_id: {comment['id']}")
        blocks.append("\n".join(lines))

    return "\n\n".join(blocks) + "\n"


# ---------------------------------------------------------------------------
# Serving
# ---------------------------------------------------------------------------


def resolve_served_file(document_id: str, seq: int, subpath: str) -> tuple[dict, dict, Path]:
    """(document, version, path) for one served subpath, containment enforced."""
    db = _db()
    document = _row(db.fetchone(
        "SELECT * FROM redline_documents WHERE id = ?", (document_id,)))
    if document is None:
        raise RedlineError(f"unknown document: {document_id}")
    version = _row(db.fetchone(
        "SELECT * FROM redline_versions WHERE document_id = ? AND seq = ?",
        (document_id, int(seq)),
    ))
    if version is None:
        raise RedlineError(f"unknown version seq {seq} for document {document_id}")
    root_path = version.get("root_path")
    if not root_path:
        raise RedlineError("this version is not served from disk")

    if any(part == ".." for part in Path(subpath).parts):
        raise RedlineError(f"subpath contains a '..' segment and is refused: {subpath}")
    target = _check_contained(Path(root_path), Path(root_path) / subpath)
    return document, version, target
