# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Ingest CodeFacts from cortex.codegraph into the temporal KG and provide ripple queries.
# index:
#   imports
#   CODE_PREDICATES
#   def ingest_code_facts
#   def set_file_layer
#   def file_sha256
#   def ripple
#   def invalidate_file
# AGENT_HEADER_END -->
"""Bridge between cortex.codegraph (Tree-sitter facts) and the temporal KG.

Maps:
    file        -> entity(type=code_ref, name=<relpath>)
    symbol      -> entity(type=code_ref, name=<relpath>::<symbol>)
    import_tgt  -> entity(type=code_ref, name=<raw_target>, kind=import_target)
    layer       -> entity(type=concept, name=<layer_slug>, kind=layer)

Predicates (CODE_PREDICATES):
    defines        file        -> symbol
    imports        file        -> import_target
    calls          caller_sym  -> callee (str)
    inherits_from  child_sym   -> parent (str)
    in_layer       file        -> layer

Idempotency: the file's sha256 is stored in the file entity's properties.
``ingest_code_facts`` skips files whose sha256 is unchanged. When changed,
all code-triples whose subject is the file or any of its symbols are
invalidated before the new facts are written.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Iterable

from .kg import _ensure_entity, _normalize_date, _triple_id, kg_add
from ..cortex.codegraph.tiers import (
    TIER_CONFIDENCE,
    TIER_EXTERNAL,
    TIER_EXTRACTED,
    TIER_INFERRED,
    build_symbol_index,
    classify_edge,
)

log = logging.getLogger(__name__)

# Ingest-time tier confidences. Local-certainty edges (a parsed definition, a
# resolved import, an in-file call) are EXTRACTED here; cross-file calls and
# inherits are written INFERRED provisionally and upgraded by ``resolve_edges``
# once the project-wide symbol index + import evidence are available.
_CONF_EXTRACTED = TIER_CONFIDENCE[TIER_EXTRACTED]
_CONF_INFERRED = TIER_CONFIDENCE[TIER_INFERRED]
_CONF_EXTERNAL = TIER_CONFIDENCE[TIER_EXTERNAL]


CODE_PREDICATES: tuple[str, ...] = (
    "defines",
    "imports",
    "calls",
    "inherits_from",
    "in_layer",
)

_PY_EXTS = (".py", ".pyw")
_JS_EXTS = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs")


def _resolve_python_import(
    target: str, importer: str, is_relative: bool, project_files: set[str]
) -> str | None:
    """Map a Python import target to a project relpath, or None.

    Tries each progressively-shorter dotted prefix so that
    ``src.domain.user.User`` resolves to ``src/domain/user.py`` (the last
    segment ``User`` being a symbol exposed by the module, not a submodule).
    """
    if is_relative:
        # Target shape: leading dots already stripped by extractor; treat
        # importer's parent dir as the package root.
        parts = target.lstrip(".").split(".")
        package = Path(importer).parent
        candidates = parts
    else:
        package = Path()
        candidates = target.split(".")
        if not candidates:
            return None

    for n in range(len(candidates), 0, -1):
        sub = candidates[:n]
        base = package / Path(*sub)
        for ext in _PY_EXTS:
            cand = f"{base}{ext}"
            if cand in project_files:
                return cand
        init = f"{base}/__init__.py"
        if init in project_files:
            return init
    return None


def _resolve_js_import(
    target: str,
    importer: str,
    project_files: set[str],
    ts_aliases: list[tuple[str, list[str]]] | None = None,
) -> str | None:
    """Map a JS/TS bare/relative import spec to a project relpath, or None.

    Resolution order:
      1. tsconfig.compilerOptions.paths alias (e.g., @/foo -> src/foo)
      2. Relative './x' against the importer directory
      3. Absolute '/x' from project root
    Bare module specs (no alias hit, no './' prefix) stay external.
    """
    # 1. Path alias
    if ts_aliases:
        expanded = _expand_ts_alias(target, ts_aliases)
        if expanded is not None:
            hit = _try_js_candidates(expanded, project_files)
            if hit:
                return hit
    if not target.startswith((".", "/")):
        return None
    package_dir = Path(importer).parent
    base = (package_dir / target).as_posix()
    base = str(Path(base))
    return _try_js_candidates(base, project_files)


def _try_js_candidates(base: str, project_files: set[str]) -> str | None:
    """Try base verbatim, then <base>.<ext>, then <base>/index.<ext>."""
    if base in project_files:
        return base
    for ext in _JS_EXTS:
        cand = f"{base}{ext}"
        if cand in project_files:
            return cand
    for ext in _JS_EXTS:
        idx = f"{base}/index{ext}"
        if idx in project_files:
            return idx
    return None


def _expand_ts_alias(
    target: str, aliases: list[tuple[str, list[str]]]
) -> str | None:
    """Apply the first matching tsconfig path alias to ``target``.

    Aliases come from compilerOptions.paths as ``(pattern, [substitutions])``.
    Patterns may contain a single ``*`` wildcard. We expand to the first
    substitution; callers try the candidate files in order.
    """
    for pattern, subs in aliases:
        if "*" in pattern:
            prefix, _, suffix = pattern.partition("*")
            if target.startswith(prefix) and target.endswith(suffix):
                middle = target[len(prefix): len(target) - len(suffix) or None]
                for sub in subs:
                    if "*" in sub:
                        return sub.replace("*", middle, 1)
                    return sub
        elif target == pattern:
            return subs[0] if subs else None
    return None


def load_ts_aliases(root: Path) -> list[tuple[str, list[str]]]:
    """Read tsconfig.json and return [(pattern, [resolved_subs])] for paths.

    Substitutions are joined with baseUrl (default '.') so the strings
    returned are already project-root-relative POSIX paths ready for the
    project_files lookup. Missing or unreadable tsconfig returns []
    silently — the caller treats it as 'no aliases'.
    """
    cfg = root / "tsconfig.json"
    if not cfg.exists():
        return []
    try:
        # tsconfig allows comments and trailing commas; json loads strict
        # configs fine and we degrade silently on anything fancier.
        import json as _json
        data = _json.loads(cfg.read_text())
    except (OSError, ValueError):
        return []
    co = data.get("compilerOptions") or {}
    base_url = co.get("baseUrl") or "."
    paths = co.get("paths") or {}
    base = Path(base_url)
    out: list[tuple[str, list[str]]] = []
    for pattern, subs in paths.items():
        resolved: list[str] = []
        for sub in subs or []:
            joined = (base / sub).as_posix() if not sub.startswith("/") else sub
            resolved.append(str(Path(joined)))
        if resolved:
            out.append((pattern, resolved))
    return out


def _resolve_import(
    target: str,
    *,
    importer: str,
    language: str,
    is_relative: bool,
    project_files: set[str],
    ts_aliases: list[tuple[str, list[str]]] | None = None,
) -> str:
    """Return resolved relpath if the import points to a project file, else
    the original target string. Pure function — easy to unit-test."""
    if language == "python":
        hit = _resolve_python_import(target, importer, is_relative, project_files)
    elif language in ("javascript", "typescript", "tsx"):
        hit = _resolve_js_import(target, importer, project_files, ts_aliases)
    else:
        hit = None
    return hit or target


def _relpath(file: str, root: Path) -> str:
    p = Path(file)
    try:
        return str(p.relative_to(root)) if p.is_absolute() else str(p)
    except ValueError:
        return str(p)


def _file_entity_name(relpath: str) -> str:
    return relpath


def _symbol_entity_name(relpath: str, symbol_name: str, parent: str | None = None) -> str:
    """Stable entity name for a symbol.

    Shape: ``<relpath>::<qualname>`` where qualname includes the parent
    class for methods (``Class.method``). Callers needing collision
    disambiguation across same-named-same-parent symbols within one file
    should call ``_disambiguate_symbol_names`` first to compute a per-line
    suffix map and use that.
    """
    qual = f"{parent}.{symbol_name}" if parent else symbol_name
    return f"{relpath}::{qual}"


def _symbol_line(symbols, name: str) -> int:
    """Return the first matching symbol's line, or -1. Used to look up
    a class's qualname when emitting inherits_from triples."""
    for s in symbols:
        if s.name == name:
            return s.line
    return -1


def _resolve_callee(callee: str, relpath: str, caller_lookup: dict[str, str]) -> str:
    """Map a callee expression to an in-file symbol entity if possible.

    The ingestor already rewrites ``self.x`` / ``this.x`` to
    ``<class>.<x>``. If that string (or the bare callee for top-level fns)
    matches a symbol defined in this file, point the triple at the
    symbol entity. External / unresolved callees stay verbatim.
    """
    if callee in caller_lookup:
        return f"{relpath}::{caller_lookup[callee]}"
    # Strip arguments / generic params if the extractor left any
    cleaned = callee.split("<", 1)[0].split("(", 1)[0]
    if cleaned in caller_lookup:
        return f"{relpath}::{caller_lookup[cleaned]}"
    return callee


def _build_caller_lookup(symbols, qualname_map: dict[tuple[str, int], str]) -> dict[str, str]:
    """Map extractor-style caller strings ('Class.method', 'fn') -> qualname.

    The extractor records caller as ``<class>.<fn>`` for methods and
    ``<fn>`` for top-level functions. We need to invert this against the
    disambiguated qualname table so 'calls' triple subjects line up with
    'defines' triple objects.
    """
    out: dict[str, str] = {}
    for s in symbols:
        key = f"{s.parent}.{s.name}" if s.parent else s.name
        qual = qualname_map.get((s.name, s.line), s.name)
        out[key] = qual
    return out


def _disambiguate_symbol_names(symbols) -> dict[tuple[str, int], str]:
    """Return ``{(symbol_name, line): qualname}`` for one file's symbols.

    qualname = ``parent.name`` when parent exists, plus ``#L<line>`` only
    when (parent, name) collides within this file. Keeps the common case
    pretty while guaranteeing uniqueness.
    """
    by_key: dict[tuple[str | None, str], list] = {}
    for sym in symbols:
        by_key.setdefault((sym.parent, sym.name), []).append(sym)
    out: dict[tuple[str, int], str] = {}
    for (parent, name), syms in by_key.items():
        base = f"{parent}.{name}" if parent else name
        if len(syms) == 1:
            out[(name, syms[0].line)] = base
        else:
            for s in syms:
                out[(name, s.line)] = f"{base}#L{s.line}"
    return out


def _list_ingested_files(db, *, project: str) -> set[str]:
    """Return relpaths of file entities (kind=file) for this project.

    Excludes entities already marked deleted so an undeleted file doesn't
    get repeatedly orphaned across runs.
    """
    rows = db.fetchall(
        """SELECT name, properties FROM kg_entities
           WHERE type = 'code_ref' AND project = ?""",
        (project,),
    )
    out: set[str] = set()
    for r in rows:
        try:
            props = json.loads(r["properties"]) or {}
        except (json.JSONDecodeError, TypeError):
            continue
        if props.get("kind") == "file" and not props.get("deleted"):
            out.add(r["name"])
    return out


def _mark_file_deleted(db, relpath: str) -> None:
    """Tag a file entity as deleted (preserves history; no row removal)."""
    from datetime import date
    import hashlib

    eid = hashlib.sha256(f"{relpath}:code_ref".encode("utf-8")).hexdigest()
    with db.write():
        row = db.fetchone("SELECT properties FROM kg_entities WHERE id = ?", (eid,))
        if not row:
            return
        try:
            props = json.loads(row["properties"]) or {}
        except (json.JSONDecodeError, TypeError):
            props = {}
        props["deleted"] = date.today().isoformat()
        db.execute(
            "UPDATE kg_entities SET properties = ? WHERE id = ?",
            (json.dumps(props), eid),
        )


def file_sha256(db, relpath: str) -> str | None:
    """Return the stored sha256 for a file entity, or None if absent."""
    row = db.fetchone(
        "SELECT properties FROM kg_entities WHERE name = ? AND type = 'code_ref'",
        (relpath,),
    )
    if not row:
        return None
    try:
        return (json.loads(row["properties"]) or {}).get("sha256")
    except (json.JSONDecodeError, TypeError):
        return None


def invalidate_file(db, relpath: str, project: str | None = None) -> int:
    """Invalidate all code-triples touching a file (file or its symbols).

    Returns the number of triples closed. Closes the temporal slice with
    ``valid_to = today`` so history stays queryable.
    """
    from datetime import date

    today = date.today().isoformat()
    subject_prefix = f"{relpath}::"
    with db.write():
        cur = db.execute(
            """UPDATE kg_triples
               SET valid_to = ?, invalidated_at = datetime('now')
               WHERE predicate IN ({preds})
                 AND (valid_to IS NULL OR valid_to = '')
                 AND (subject = ? OR subject LIKE ? OR object = ?)
            """.format(preds=",".join("?" * len(CODE_PREDICATES))),
            (today, *CODE_PREDICATES, relpath, subject_prefix + "%", relpath),
        )
        return cur.rowcount if cur else 0


def _upsert_entity_with_props(
    db, name: str, etype: str, project: str | None, properties: dict
) -> str:
    """Upsert + merge properties on an existing entity."""
    import hashlib

    eid = hashlib.sha256(f"{name}:{etype}".encode("utf-8")).hexdigest()
    with db.write():
        row = db.fetchone(
            "SELECT properties FROM kg_entities WHERE id = ?", (eid,)
        )
        if row:
            try:
                merged = json.loads(row["properties"]) or {}
            except (json.JSONDecodeError, TypeError):
                merged = {}
            merged.update(properties)
            db.execute(
                "UPDATE kg_entities SET properties = ? WHERE id = ?",
                (json.dumps(merged), eid),
            )
        else:
            db.execute(
                """INSERT INTO kg_entities (id, name, type, project, properties)
                   VALUES (?, ?, ?, ?, ?)""",
                (eid, name, etype, project, json.dumps(properties)),
            )
    return eid


def _insert_triple(
    db,
    subject: str,
    predicate: str,
    obj: str,
    *,
    project: str | None,
    valid_from: str | None,
    confidence: float,
) -> bool:
    """Insert a triple if it doesn't already exist. Returns True if inserted."""
    tid = _triple_id(subject, predicate, obj, valid_from)
    with db.write():
        existing = db.fetchone("SELECT id FROM kg_triples WHERE id = ?", (tid,))
        if existing:
            return False
        db.execute(
            """INSERT INTO kg_triples
               (id, subject, predicate, object, valid_from, valid_to,
                project, source_memory_id, source_artifact_id, confidence)
               VALUES (?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?)""",
            (tid, subject, predicate, obj, valid_from, None, project, confidence),
        )
        return True


def ingest_code_facts(
    facts_iter: Iterable,
    *,
    project: str | None,
    root: Path,
    valid_from=None,
    prune_missing: bool = False,
    project_files: set[str] | None = None,
) -> dict:
    """Bulk-ingest CodeFacts into the KG. Idempotent per (file, sha256).

    When ``project_files`` is provided (CLI passes the result of
    ``discover_project_files``), the iterable is consumed lazily — memory
    stays O(symbols-per-file). Otherwise the iterable is materialized so
    the project-file set can be built from it (single-file callers don't
    care about memory).

    When ``prune_missing`` is True (CLI's ``codegraph ingest`` does this),
    previously-ingested files absent from the current batch are treated
    as deletions: their triples are temporally closed and the file entity
    is marked deleted. Default False so partial / single-file ingests
    don't wipe the rest of the project.

    Returns stats: files_ingested, files_skipped, triples_inserted,
    triples_invalidated, orphans_closed.
    """
    from okuro.db import get_db

    db = get_db()
    if project:
        from okuro.sense.progress import _ensure_project
        _ensure_project(db, project)

    vf = _normalize_date(valid_from)
    stats = {
        "files_ingested": 0,
        "files_skipped": 0,
        "triples_inserted": 0,
        "triples_invalidated": 0,
        "orphans_closed": 0,
        "parse_failures": 0,
        "parse_failure_paths": [],
    }

    # Streaming path when project_files is precomputed; otherwise
    # materialize the iterator so we can build it from the facts.
    if project_files is None:
        facts_list = list(facts_iter)
        project_files = {
            _relpath(f.file, root) for f in facts_list if not f.error
        }
        facts_stream: Iterable = facts_list
    else:
        facts_stream = facts_iter
    ts_aliases = load_ts_aliases(root)

    # Orphan cleanup — only when caller signals a full-project ingest.
    if prune_missing and project:
        previously_ingested = _list_ingested_files(db, project=project)
        for old_relpath in previously_ingested - project_files:
            stats["triples_invalidated"] += invalidate_file(db, old_relpath, project=project)
            _mark_file_deleted(db, old_relpath)
            stats["orphans_closed"] += 1

    # Files that already carry extracted facts. A sha-unchanged file is skipped
    # ONLY if it already has facts — so a file that was sha-only under an older,
    # less-capable extractor (e.g. Java before the Java walker landed) is
    # re-parsed once the extractor can handle it, without needing a content edit.
    files_with_facts: set[str] = {
        r["subject"] for r in db.fetchall(
            "SELECT DISTINCT subject FROM kg_triples "
            "WHERE predicate IN ('defines', 'imports') "
            "AND (valid_to IS NULL OR valid_to = '') "
            "AND (project = ? OR (? IS NULL AND project IS NULL))",
            (project, project),
        )
    }

    for facts in facts_stream:
        if facts.error and not facts.symbols and not facts.imports:
            # Track but skip — unsupported extension is benign, parse error
            # is worth surfacing.
            if "unsupported" not in (facts.error or ""):
                stats["parse_failures"] += 1
                stats["parse_failure_paths"].append(_relpath(facts.file, root))
            continue

        relpath = _relpath(facts.file, root)
        existing_sha = file_sha256(db, relpath)
        if existing_sha == facts.sha256 and relpath in files_with_facts:
            stats["files_skipped"] += 1
            continue

        if existing_sha is not None:
            stats["triples_invalidated"] += invalidate_file(db, relpath, project=project)

        # Upsert file entity with current sha + language
        _upsert_entity_with_props(
            db,
            name=relpath,
            etype="code_ref",
            project=project,
            properties={
                "kind": "file",
                "language": facts.language,
                "sha256": facts.sha256,
            },
        )

        # Symbols — disambiguate name collisions within this file
        qualname_map = _disambiguate_symbol_names(facts.symbols)
        for sym in facts.symbols:
            qual = qualname_map.get((sym.name, sym.line), sym.name)
            sym_name = f"{relpath}::{qual}"
            _upsert_entity_with_props(
                db,
                name=sym_name,
                etype="code_ref",
                project=project,
                properties={
                    "kind": sym.kind,
                    "line": sym.line,
                    "end_line": sym.end_line,
                    "parent": sym.parent,
                    "file": relpath,
                    "qualname": qual,
                },
            )
            if _insert_triple(
                db, relpath, "defines", sym_name,
                project=project, valid_from=vf, confidence=_CONF_EXTRACTED,
            ):
                stats["triples_inserted"] += 1

        # Imports — normalize to a project relpath when possible so ripple
        # queries traverse cross-file edges; external imports stay verbatim.
        for imp in facts.imports:
            resolved = _resolve_import(
                imp.target,
                importer=relpath,
                language=facts.language,
                is_relative=imp.is_relative,
                project_files=project_files,
                ts_aliases=ts_aliases,
            )
            is_project_file = resolved in project_files
            kind = "file" if is_project_file else "import_target"
            _ensure_entity(
                db, resolved, "code_ref", project=project,
                properties={"kind": kind},
            )
            # A resolved intra-project import is directly parsed (extracted);
            # an import that leaves the project is external (stdlib/3rd-party).
            imp_conf = _CONF_EXTRACTED if is_project_file else _CONF_EXTERNAL
            if _insert_triple(
                db, relpath, "imports", resolved,
                project=project, valid_from=vf, confidence=imp_conf,
            ):
                stats["triples_inserted"] += 1

        # Inherits — child is always a class defined in this file
        for inh in facts.inherits:
            # The child class is a top-level symbol (parent=None) in this file
            child_qual = qualname_map.get((inh.child, _symbol_line(facts.symbols, inh.child)), inh.child)
            child_name = f"{relpath}::{child_qual}"
            _ensure_entity(
                db, inh.parent, "code_ref", project=project,
                properties={"kind": "parent_class"},
            )
            # Parent is a bare class name here; ``resolve_edges`` upgrades it to
            # extracted/ambiguous once the project symbol index is available.
            if _insert_triple(
                db, child_name, "inherits_from", inh.parent,
                project=project, valid_from=vf, confidence=_CONF_INFERRED,
            ):
                stats["triples_inserted"] += 1

        # Calls — subject is the enclosing symbol if known, else the file.
        # caller from extractor is "<scope_class>.<fn>" or "<fn>" — match it
        # against the disambiguated qualname map so subjects align with
        # the corresponding 'defines' entity. Same lookup also resolves
        # in-file callees (Class.method) to a real symbol entity.
        caller_lookup = _build_caller_lookup(facts.symbols, qualname_map)
        for call in facts.calls:
            subject = relpath
            if call.caller and call.caller in caller_lookup:
                subject = f"{relpath}::{caller_lookup[call.caller]}"
            obj = _resolve_callee(call.callee, relpath, caller_lookup)
            # In-file resolution (obj now ``relpath::sym``) is directly parsed →
            # extracted. Everything else is a provisional cross-file guess →
            # inferred, upgraded/ambiguated by ``resolve_edges``.
            call_conf = (
                _CONF_EXTRACTED if obj.startswith(f"{relpath}::") else _CONF_INFERRED
            )
            if _insert_triple(
                db, subject, "calls", obj,
                project=project, valid_from=vf, confidence=call_conf,
            ):
                stats["triples_inserted"] += 1

        stats["files_ingested"] += 1

    return stats


def resolve_edges(project: str | None) -> dict:
    """Re-tier ``calls`` + ``inherits_from`` edges with the project-wide symbol
    index and import evidence — the post-ingest resolution pass.

    Turns the provisional cross-file edges the ingester writes into
    extracted / inferred / ambiguous / external (see ``codegraph.tiers``), and
    resolves bare callee / parent names to concrete ``file::symbol`` targets
    when the evidence allows:

      - a bare name that resolves to a symbol → the edge is retargeted at that
        symbol (old bare slice temporally closed, resolved slice opened at the
        same ``valid_from``);
      - a same-target retiering (e.g. an in-file call already pointing at its
        symbol) is an in-place confidence correction — no history churn;
      - an ambiguous or external edge keeps its verbatim object, only its
        confidence tier is corrected.

    Idempotent: re-running on an unchanged graph only reconfidences at most and
    resolves nothing new. Returns per-tier counts plus resolved / reconfidenced
    / unchanged tallies.
    """
    from datetime import date

    from okuro.db import get_db

    db = get_db()
    proj_clause = "(project = ? OR (? IS NULL AND project IS NULL))"

    # 1. Symbol index over every definition in the project.
    define_rows = db.fetchall(
        f"""SELECT subject, object FROM kg_triples
            WHERE predicate = 'defines' AND (valid_to IS NULL OR valid_to = '')
              AND {proj_clause}""",
        (project, project),
    )
    symbol_index, project_files = build_symbol_index(
        [(r["subject"], r["object"]) for r in define_rows]
    )

    # 2. Import evidence: caller_file -> {imported project files}.
    imports_by_file: dict[str, set[str]] = {}
    import_rows = db.fetchall(
        f"""SELECT subject, object FROM kg_triples
            WHERE predicate = 'imports' AND (valid_to IS NULL OR valid_to = '')
              AND {proj_clause}""",
        (project, project),
    )
    for r in import_rows:
        if r["object"] in project_files:
            imports_by_file.setdefault(r["subject"], set()).add(r["object"])

    # Full project file set (defines-owners ∪ every classified file). Used to
    # tier imports as intra-project (extracted) vs external — without this,
    # imports pointing at a file that defines no symbol would look external.
    file_set: set[str] = set(project_files)
    for r in db.fetchall(
        f"""SELECT DISTINCT subject FROM kg_triples
            WHERE predicate = 'in_layer' AND (valid_to IS NULL OR valid_to = '')
              AND {proj_clause}""",
        (project, project),
    ):
        file_set.add(r["subject"])

    # defines/imports rows with confidence — re-tiered from the KG so an
    # incremental re-ingest (which sha-skips unchanged files) still tiers them.
    di_rows = db.fetchall(
        f"""SELECT id, predicate, object, confidence FROM kg_triples
            WHERE predicate IN ('defines', 'imports')
              AND (valid_to IS NULL OR valid_to = '')
              AND {proj_clause}""",
        (project, project),
    )

    stats = {t: 0 for t in ("extracted", "inferred", "ambiguous", "external")}
    stats.update(resolved=0, reconfidenced=0, unchanged=0)

    edge_rows = db.fetchall(
        f"""SELECT id, predicate, subject, object, confidence, valid_from
            FROM kg_triples
            WHERE predicate IN ('calls', 'inherits_from')
              AND (valid_to IS NULL OR valid_to = '')
              AND {proj_clause}""",
        (project, project),
    )

    today = date.today().isoformat()
    with db.write() as conn:
        for r in edge_rows:
            subject, obj, pred = r["subject"], r["object"], r["predicate"]
            caller_file = subject.split("::", 1)[0]
            res = classify_edge(
                obj,
                caller_file=caller_file,
                symbol_index=symbol_index,
                imported_files=imports_by_file.get(caller_file, set()),
            )
            stats[res.tier] += 1
            new_conf = res.confidence
            if res.target != obj:
                # Retarget: close the bare slice, open the resolved one. The
                # resolved target is always an existing symbol entity (it came
                # from the defines index), so no entity upsert is needed.
                conn.execute(
                    """UPDATE kg_triples
                       SET valid_to = ?, invalidated_at = datetime('now')
                       WHERE id = ?""",
                    (today, r["id"]),
                )
                vf = r["valid_from"]
                tid = _triple_id(subject, pred, res.target, vf)
                conn.execute(
                    """INSERT OR IGNORE INTO kg_triples
                       (id, subject, predicate, object, valid_from, valid_to,
                        project, source_memory_id, source_artifact_id, confidence)
                       VALUES (?, ?, ?, ?, ?, NULL, ?, NULL, NULL, ?)""",
                    (tid, subject, pred, res.target, vf, project, new_conf),
                )
                stats["resolved"] += 1
            elif abs(new_conf - float(r["confidence"])) > 1e-9:
                conn.execute(
                    "UPDATE kg_triples SET confidence = ? WHERE id = ?",
                    (new_conf, r["id"]),
                )
                stats["reconfidenced"] += 1
            else:
                stats["unchanged"] += 1

        # defines are directly parsed → always extracted; imports are extracted
        # when intra-project, external otherwise. Normalized here (not at parse
        # time) so a sha-skipped file's defines/imports still get tiered.
        for r in di_rows:
            if r["predicate"] == "defines":
                want = _CONF_EXTRACTED
            else:  # imports
                want = _CONF_EXTRACTED if r["object"] in file_set else _CONF_EXTERNAL
            if abs(want - float(r["confidence"])) > 1e-9:
                conn.execute(
                    "UPDATE kg_triples SET confidence = ? WHERE id = ?",
                    (want, r["id"]),
                )
                stats["reconfidenced"] += 1
    return stats


def set_file_layer(
    relpath: str,
    layer: str,
    *,
    project: str | None = None,
    confidence: float = 0.8,
    valid_from=None,
) -> str:
    """Tag a file with its architectural layer via an ``in_layer`` triple.

    Convenience wrapper for the layer classifier (task 3) and any manual
    overrides. Layers are stored as ``concept`` entities so they show up
    in faceted KG queries.
    """
    return kg_add(
        subject=relpath,
        predicate="in_layer",
        object=layer,
        subject_type="code_ref",
        object_type="concept",
        project=project,
        confidence=confidence,
        valid_from=valid_from,
    )


def set_file_community(
    relpath: str,
    community: str,
    *,
    project: str | None = None,
    confidence: float = 0.7,
    valid_from=None,
) -> str:
    """Tag a file with its detected subsystem via an ``in_community`` triple.

    Written by cortex.codegraph.insights (Louvain partition of the file import
    graph). Communities are ``concept`` entities so they surface in faceted KG
    queries and color the /knowledge graph, exactly like ``in_layer``.
    """
    return kg_add(
        subject=relpath,
        predicate="in_community",
        object=community,
        subject_type="code_ref",
        object_type="concept",
        project=project,
        confidence=confidence,
        valid_from=valid_from,
    )


def ripple(
    entity: str,
    *,
    predicates: tuple[str, ...] = ("imports", "calls", "defines"),
    max_depth: int = 3,
    direction: str = "incoming",
    project: str | None = None,
    limit_per_hop: int = 200,
) -> list[dict]:
    """BFS along selected predicates from ``entity``. For diff-impact.

    direction:
        - 'incoming' = walk subjects that point AT entity (who depends on me?)
        - 'outgoing' = walk objects pointed AT by entity (what do I depend on?)
        - 'both'     = both
    """
    from okuro.db import get_db

    db = get_db()
    seen: set[str] = {entity}
    frontier: list[tuple[str, int]] = [(entity, 0)]
    results: list[dict] = []

    placeholders = ",".join("?" * len(predicates))

    while frontier:
        node, depth = frontier.pop(0)
        if depth >= max_depth:
            continue

        params: list = list(predicates)
        if direction == "incoming":
            where = f"object = ? AND predicate IN ({placeholders})"
            params = [node, *predicates]
        elif direction == "outgoing":
            where = f"subject = ? AND predicate IN ({placeholders})"
            params = [node, *predicates]
        else:
            where = f"(subject = ? OR object = ?) AND predicate IN ({placeholders})"
            params = [node, node, *predicates]

        if project:
            where += " AND (project = ? OR project IS NULL)"
            params.append(project)
        where += " AND (valid_to IS NULL OR valid_to = '')"

        rows = db.fetchall(
            f"""SELECT subject, predicate, object FROM kg_triples
                WHERE {where}
                LIMIT ?""",
            tuple(params + [limit_per_hop]),
        )

        for r in rows:
            results.append({
                "depth": depth + 1,
                "subject": r["subject"],
                "predicate": r["predicate"],
                "object": r["object"],
            })
            next_node = r["subject"] if direction == "incoming" else r["object"]
            if direction == "both":
                next_node = r["object"] if r["subject"] == node else r["subject"]
            if next_node not in seen:
                seen.add(next_node)
                frontier.append((next_node, depth + 1))

    return results
