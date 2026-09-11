# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Vector Store for okuro.cortex
# index: imports | class VectorConfig | class SearchResult | class VectorStore
# AGENT_HEADER_END -->
"""
Vector Store for okuro.cortex
sqlite-vec based semantic search over file headers and content.

Replaces the chromadb-based vectorstore from tm-cortex.
Uses okuro.db for storage and okuro.embed for embeddings.
"""

import hashlib
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional, Callable

from okuro.db import get_db
from okuro.embed import embed, embed_one
from okuro.embed.client import to_bytes
from okuro.embed.config import TIERS, TierSpec, load as load_embed_config
from .core import AgentHeader, FileRole, parse_header, IndexEntry
from .exclusions import build_matcher


def _active_tier_spec() -> TierSpec:
    """Resolve the active embedding tier — env var > config file > high.

    Mirrors okuro.embed.server / .client so all three components agree on
    which model is in use without anyone passing the spec explicitly.
    """
    env_tier = os.environ.get("OKURO_EMBED_TIER", "").lower().strip()
    if env_tier in TIERS:
        return TIERS[env_tier]  # type: ignore[index]
    try:
        cfg = load_embed_config()
    except (ValueError, OSError):
        cfg = None
    if cfg is not None:
        return cfg.spec
    return TIERS["high"]


# Conservative default floor on the hybrid score. Low enough that genuine
# weak-but-relevant hits survive; high enough to drop the near-orthogonal
# noise the exhaustive KNN always returns. Tunable per install.
DEFAULT_SCORE_FLOOR = 0.15


def _env_score_floor() -> float:
    """Resolve the cortex score floor — env var > default. 0.0 disables it."""
    raw = os.environ.get("OKURO_CORTEX_SCORE_FLOOR", "").strip()
    if raw:
        try:
            v = float(raw)
            if v >= 0.0:
                return v
        except ValueError:
            pass
    return DEFAULT_SCORE_FLOOR


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if raw:
        try:
            v = int(raw)
            if v > 0:
                return v
        except ValueError:
            pass
    return default


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    return default


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if raw:
        try:
            v = float(raw)
            if v >= 0.0:
                return v
        except ValueError:
            pass
    return default


def reciprocal_rank_fusion(
    ranked_lists: list[list[str]], k0: int = 60
) -> dict[str, float]:
    """Reciprocal Rank Fusion over several ranked id-lists (F24).

    ``score(doc) = Σ_i 1 / (k0 + rank_i)`` where ``rank_i`` is the 0-based
    position of the doc in list ``i`` (only lists that contain the doc
    contribute). RRF fuses on RANK, so it is immune to the incomparable score
    *scales* of dense cosine similarity vs BM25 — the bug in the old
    ``0.6·vec + 0.4·kw`` linear blend. A doc ranked highly in BOTH arms beats
    one ranked highly in only one. Order-/scale-independent of the inputs.

    Returns ``{doc_id: fused_score}`` (higher = better).
    """
    scores: dict[str, float] = {}
    for ranked in ranked_lists:
        for rank, doc_id in enumerate(ranked):
            scores[doc_id] = scores.get(doc_id, 0.0) + 1.0 / (k0 + rank)
    return scores


# What counts as an indexable file. Module-level because index_directory is
# no longer the only caller — the worktree-overlay indexer takes its file list
# from git rather than from a walk, and still has to apply the SAME predicate.
# Two inline copies would drift, and the drift would be invisible: a file type
# indexed by one path and skipped by the other looks like a ranking problem.
INDEXABLE_EXTENSIONS = frozenset({
    ".py", ".js", ".ts", ".jsx", ".tsx",
    ".md", ".yaml", ".yml", ".toml", ".json",
    ".sh", ".bash", ".zsh",
    ".sql", ".css", ".scss",
    ".go", ".rs", ".java", ".c", ".cpp", ".h",
})

SUPPORTED_FILENAMES = frozenset({
    "dockerfile", "caddyfile", "makefile", "gnumakefile",
    "procfile", "jenkinsfile", "vagrantfile", "rakefile", "gemfile",
    ".gitignore", ".dockerignore", ".editorconfig",
})

BINARY_EXTENSIONS = frozenset({
    ".so", ".dylib", ".dll", ".exe", ".bin", ".o", ".a",
    ".jar", ".class", ".pak", ".dat", ".db", ".sqlite", ".sqlite3",
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".ico", ".svg", ".webp",
    ".mp3", ".mp4", ".wav", ".avi", ".mkv",
    ".zip", ".tar", ".gz", ".bz2", ".xz", ".7z", ".rar",
    ".woff", ".woff2", ".ttf", ".otf", ".eot",
    ".pdf", ".doc", ".docx", ".xls", ".xlsx",
    ".pyc", ".pyo", ".wasm", ".lock", ".probe",
})


def is_indexable_file(path: Path) -> bool:
    """Extension-level verdict, shared by the walk and the overlay indexer.

    Path-level exclusion (denylists, gitignore) is a separate question owned by
    okuro.cortex.exclusions; git already applies .gitignore for the overlay.
    """
    suffix = path.suffix.lower()
    if suffix in BINARY_EXTENSIONS:
        return False
    return suffix in INDEXABLE_EXTENSIONS or path.name.lower() in SUPPORTED_FILENAMES


@dataclass
class VectorConfig:
    """Configuration for vector store."""

    embedding_model: str = field(
        default_factory=lambda: _active_tier_spec().model_id
    )
    embedding_dim: int = field(
        default_factory=lambda: _active_tier_spec().dim
    )
    # Legacy char-window kept only as a fallback knob; the chunker is now
    # TOKEN-based (see chunk_tokens). Retained so old callers don't break.
    chunk_size: int = 512
    include_content: bool = True

    # --- Chunking (F7/F8/F9) ---
    # Token-based window. The old 512-CHAR window was ~90 tokens (~18% of the
    # model's usable budget) with ZERO overlap. We now chunk by an approximate
    # token count (chars/chars_per_token) with a sliding overlap so concepts
    # straddling a boundary survive. chunk_tokens ≤ embed max_seq (2048) with
    # margin for the breadcrumb header.
    chunk_tokens: int = field(
        default_factory=lambda: _env_int("OKURO_CORTEX_CHUNK_TOKENS", 1024)
    )
    # Overlap as a fraction of the window (sliding stride = 1 - overlap).
    chunk_overlap_ratio: float = field(
        default_factory=lambda: _env_float("OKURO_CORTEX_CHUNK_OVERLAP", 0.18)
    )
    # Char/token approximation — avoids loading the tokenizer per chunk. ~4 is
    # the standard English/code heuristic. Override per-corpus if needed.
    chars_per_token: int = field(
        default_factory=lambda: _env_int("OKURO_CORTEX_CHARS_PER_TOKEN", 4)
    )
    # Max chunks per section / per headerless file. Old caps were 3 and 5,
    # which SILENTLY dropped the body of large files (audit F9: 2,276 files lost
    # content). Raised to a high bound that still guards a pathological file;
    # truncation past it is LOGGED, never silent. 0 = unlimited.
    max_chunks_per_unit: int = field(
        default_factory=lambda: _env_int("OKURO_CORTEX_MAX_CHUNKS", 40)
    )
    # Prepend a breadcrumb (file > section / H1 > H2 > leaf) to each chunk
    # before embedding so near-identical chunks ("Introduction" ×5) are
    # disambiguated in vector space (F11).
    contextual_headers: bool = field(
        default_factory=lambda: _env_bool("OKURO_CORTEX_BREADCRUMB", True)
    )
    # AST-aware code chunking (cAST, audit F10). When ON, CODE files whose
    # language has a tree-sitter grammar are chunked on AST structure (whole
    # functions/classes stay intact) instead of by line window; prose/config
    # keep the line chunker. Falls back to the line chunker on unknown language
    # / missing grammar / parse error.
    #
    # DEFAULT OFF — decided by the same-index sandbox A/B: cAST was NEUTRAL on
    # okuro's gold set (MRR 0.7626 both ON and OFF; recall 0.889 / nDCG 0.789
    # identical). okuro files have rich AGENT_HEADER section indexes, so
    # canonical answers live in section docs, not the content chunks cAST
    # reshapes. Shipped as a validated opt-in lever for header-poor corpora
    # (whole-function chunks), not as default since it neither helps nor harms
    # here — same gate-driven call as hybrid-RRF in an earlier package.
    ast_chunking: bool = field(
        default_factory=lambda: _env_bool("OKURO_CORTEX_AST_CHUNKING", False)
    )
    # cAST budget = non-whitespace chars per chunk. 4000 ≈ chunk_tokens(1024) ×
    # chars_per_token(4) and fits the embed window (≤ max_seq 2048 tokens).
    cast_max_chars: int = field(
        default_factory=lambda: _env_int("OKURO_CORTEX_CAST_MAX_CHARS", 4000)
    )

    # Retrieval mode. DEFAULT OFF = pure dense (cosine KNN) + score floor —
    # the measured-best path on okuro's semantic-code corpus (A/B on the
    # sandbox: dense MRR 0.735 vs FTS5+RRF 0.607; the strong dense arm is
    # diluted by equal-weight BM25 fusion). ON = the FTS5 BM25 + RRF hybrid,
    # an opt-in lever for lexical-heavy corpora / exact-symbol search, pending
    # per-corpus validation. Mirrors OKURO_CORTEX_HYBRID.
    hybrid: bool = field(
        default_factory=lambda: _env_bool("OKURO_CORTEX_HYBRID", False)
    )
    # Minimum relevance a result must clear to be returned (F29).
    # Drops low-similarity noise. Conservative default; override via
    # OKURO_CORTEX_SCORE_FLOOR (env) — 0.0 disables the floor entirely.
    # NOTE: RRF scores are small (≈ sum of 1/(k0+rank)); the floor is applied
    # on a normalized fused score (max-scaled to [0,1]) so this stays a
    # meaningful, scale-stable cutoff.
    score_floor: float = field(
        default_factory=lambda: _env_score_floor()
    )
    # RRF rank-fusion constant. score(doc) = Σ 1/(rrf_k0 + rank_i). k0≈60 is
    # the canonical value (Cormack et al.) — large enough that top ranks don't
    # dominate pathologically, small enough that rank still matters.
    rrf_k0: int = field(default_factory=lambda: _env_int("OKURO_CORTEX_RRF_K0", 60))
    # Candidate pool size per retrieval arm before fusion (and the rerank
    # input size). Larger = better recall, more rerank cost.
    candidate_pool: int = field(
        default_factory=lambda: _env_int("OKURO_CORTEX_CANDIDATE_POOL", 30)
    )
    # Cross-encoder rerank (F26) — OFF by default. Enabling also requires the
    # model/dep to be present (see okuro.cortex.rerank); otherwise search
    # silently falls back to fusion ranking. Config opt-in mirrors the
    # OKURO_CORTEX_RERANK / _MODEL env vars.
    rerank: bool = False
    rerank_model: str = ""


@dataclass
class SearchResult:
    """Single search result."""

    file_path: Path
    purpose: str
    relevance: float
    index_entries: list[IndexEntry]
    matched_section: Optional[str] = None
    snippet: Optional[str] = None
    keyword_score: float = 0.0
    project: Optional[str] = None


# Static (non-vector) cortex schema. Kept in sync with migrations
# 009 (deleted_at), 025 (project) and 039 (cortex_meta). Fresh DBs
# created by _ensure_schema converge on the same shape the migrations
# produce for existing DBs.
_CORTEX_BASE_SCHEMA = """
CREATE TABLE IF NOT EXISTS cortex_docs (
    id          TEXT PRIMARY KEY,
    file_path   TEXT NOT NULL,
    file_hash   TEXT,
    doc_type    TEXT,
    role        TEXT,
    purpose     TEXT,
    section     TEXT,
    chunk_index INTEGER,
    document    TEXT,
    indexed_at  TEXT DEFAULT (datetime('now')),
    deleted_at  TEXT DEFAULT NULL,
    project     TEXT
);

CREATE INDEX IF NOT EXISTS idx_cortex_docs_file
    ON cortex_docs(file_path);
CREATE INDEX IF NOT EXISTS idx_cortex_docs_type
    ON cortex_docs(doc_type);
CREATE INDEX IF NOT EXISTS idx_cortex_docs_active
    ON cortex_docs(file_path, deleted_at);
CREATE INDEX IF NOT EXISTS idx_cortex_docs_project
    ON cortex_docs(project, deleted_at);

CREATE TABLE IF NOT EXISTS cortex_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


# Schema version for vec_cortex. Bump when the vec0 DDL shape changes so
# _ensure_schema can detect an old-shape table and the migration knows what to
# drop. v1 = (id, embedding) L2. v2 = (id, project partition key, embedding
# cosine) — pushes the project filter into the KNN scan (F20/F21) and fixes
# the distance metric (F23).
VEC_CORTEX_SCHEMA_VERSION = "2"
_VEC_SCHEMA_META_KEY = "vec_cortex_schema_version"
# Sentinel partition value for unscoped (project-less) vectors. A NULL
# partition key DEFEATS sqlite-vec's chunk packing entirely — vec0 allocates a
# fresh ~chunk_size-slot chunk per inserted row, so N unscoped vectors cost N
# near-empty chunks (~1024x bloat; the 25GB vec_cortex). Coalescing None to a
# non-NULL sentinel makes vec0 pack unscoped vectors into shared chunks like
# any other partition. Proven on sqlite-vec v0.1.9: NULL -> 1 vec/chunk,
# "" -> 1024 vecs/chunk. NEVER insert NULL into the vec_cortex partition key.
NO_PROJECT_PARTITION = ""
# Set by migration 059 ONLY when a v1-shape vec_cortex existed at upgrade time.
# Authorises _ensure_schema to drop+recreate a v1 table; cleared after reshape.
_VEC_RESHAPE_PENDING_KEY = "vec_cortex_reshape_pending"


def _vec_schema(dim: int) -> str:
    """vec0 DDL for vec_cortex.

    Declares a ``project`` *partition key* so a scoped query filters inside the
    KNN scan instead of post-hoc in Python (the pre-fix path collapsed scoped
    recall to 0-1/k because the index is ~90% one project). Declares
    ``distance_metric=cosine`` so ``distance`` is true cosine distance in
    [0, 2] (vectors are unit-normalized by the embed model), making the
    ``similarity = 1 - distance/2`` mapping correct and non-negative (F23).
    Verified on sqlite-vec v0.1.9.
    """
    return (
        f"CREATE VIRTUAL TABLE IF NOT EXISTS vec_cortex USING vec0("
        f"id TEXT PRIMARY KEY, "
        f"project TEXT partition key, "
        f"embedding float[{dim}] distance_metric=cosine);"
    )


def cosine_distance_to_similarity(distance: float) -> float:
    """Map sqlite-vec cosine *distance* (∈ [0, 2]) to a similarity (∈ [0, 1]).

    cosine distance = 1 - cosine_similarity, range [0, 2] (0 identical, 1
    orthogonal, 2 opposite). The old code used ``1 - distance`` which goes
    negative past orthogonal. Correct mapping is ``1 - distance/2``. Clamped
    to [0, 1] for defensiveness against tiny float overshoot.
    """
    sim = 1.0 - (distance / 2.0)
    if sim < 0.0:
        return 0.0
    if sim > 1.0:
        return 1.0
    return sim


def cortex_meta_get(db, key: str) -> Optional[str]:
    """Read a cortex_meta scalar — returns None if the key is absent."""
    row = db.fetchone("SELECT value FROM cortex_meta WHERE key = ?", (key,))
    return row["value"] if row else None


def cortex_meta_upsert(db, key: str, value: str) -> None:
    """Set/replace a cortex_meta value, refreshing updated_at."""
    with db.write():
        db.execute(
            "INSERT INTO cortex_meta (key, value, updated_at) "
            "VALUES (?, ?, datetime('now')) "
            "ON CONFLICT(key) DO UPDATE SET "
            "value = excluded.value, updated_at = excluded.updated_at",
            (key, value),
        )


_QWEN_INSTRUCT = (
    "Given a code or documentation search query, retrieve the most "
    "relevant source files, configuration entries, or notes."
)


def _format_query_for_model(query: str, model_name: str) -> str:
    """Apply model-specific query-side prompts.

    Each embedding family has its own retrieval-tuned prefix. Passages
    stay raw; only queries are wrapped. Without this, retrieval recall
    drops noticeably (the model was trained with the prefix in place).
    """
    name = model_name.lower()
    if "qwen" in name and "embedding" in name:
        return f"Instruct: {_QWEN_INSTRUCT}\nQuery: {query}"
    if "bge" in name:
        return f"Represent this sentence for searching relevant passages: {query}"
    if "e5" in name:
        return f"query: {query}"
    return query


class VectorStore:
    """sqlite-vec based vector store for semantic search."""

    def __init__(self, config: Optional[VectorConfig] = None):
        self.config = config or VectorConfig()
        self._db = get_db()
        self._ensure_schema()

    def _ensure_schema(self):
        """Create cortex tables if they don't exist.

        The vec_cortex column dimension is fixed at CREATE time, so the
        active-tier dim is baked in here. If a vec table from a different
        tier already exists the dim won't match — the API switch-tier
        handler is responsible for dropping it before calling
        _ensure_schema again.

        cortex_meta is seeded with the current tier label + dim on first
        init so future tier switches can compare.
        """
        self._db.conn.executescript(_CORTEX_BASE_SCHEMA)

        # SHAPE-AWARE vec0 guard. The reshape decision is driven by the table's
        # OWN DDL (sqlite_master.sql — does it contain `partition key`?), NOT by
        # the version marker, so an already-v2 table is recognised as v2 even
        # after migration 059 clears the marker. Four cases:
        #
        #   1. absent                       → create v2.
        #   2. present + DDL is v2          → PRESERVE (never drop, even if
        #      populated); stamp version=2; clear any reshape-pending marker.
        #      ← the live install: no drop, no re-embed, no outage.
        #   3. present + DDL is v1 + (reshape-pending marker set OR table empty)
        #                                   → drop + recreate v2; clear marker.
        #      ← deliberate upgrade authorised by migration 059.
        #   4. present + DDL is v1 + no marker + populated
        #                                   → LEAVE IT (don't nuke a populated
        #      table on a mere VectorStore() open); search tolerates a v1 table
        #      via the global-scan fallback. (Preserves the incident fix.)
        #
        # Idempotent: a second construction lands in case 2 and changes nothing.
        ddl_row = self._db.fetchone(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='vec_cortex'"
        )
        table_exists = ddl_row is not None
        is_v2_shape = bool(ddl_row) and "partition key" in (
            (ddl_row["sql"] or "").lower()
        )
        reshape_pending = (
            cortex_meta_get(self._db, _VEC_RESHAPE_PENDING_KEY) == "1"
        )

        if table_exists and not is_v2_shape:
            # v1 table. Reshape only if authorised (marker) or safe (empty).
            row = self._db.fetchone("SELECT COUNT(*) AS c FROM vec_cortex")
            is_empty = (row["c"] if row else 0) == 0
            if reshape_pending or is_empty:
                self._db.conn.execute("DROP TABLE IF EXISTS vec_cortex")
            # else case 4: leave the populated v1 table intact.

        # Create v2 if the table is now absent (cases 1 and 3). A preserved
        # table (cases 2 and 4) is untouched by CREATE ... IF NOT EXISTS.
        self._db.conn.execute(_vec_schema(self.config.embedding_dim))

        # Re-read the live DDL after any create/drop and reconcile the markers.
        cur_ddl_row = self._db.fetchone(
            "SELECT sql FROM sqlite_master WHERE name='vec_cortex'"
        )
        cur_is_v2 = "partition key" in (
            (cur_ddl_row["sql"] if cur_ddl_row else "") or ""
        ).lower()
        if cur_is_v2:
            # Table is v2 now → stamp version and retire the reshape-pending
            # marker (cases 1/2/3). A v1 table left in place (case 4) keeps the
            # absent version marker so a later migration/marker can still act.
            cortex_meta_upsert(
                self._db, _VEC_SCHEMA_META_KEY, VEC_CORTEX_SCHEMA_VERSION
            )
            if reshape_pending:
                self._db.execute(
                    "DELETE FROM cortex_meta WHERE key = ?",
                    (_VEC_RESHAPE_PENDING_KEY,),
                )

        if cortex_meta_get(self._db, "embedding_tier") is None:
            cortex_meta_upsert(
                self._db, "embedding_tier", _active_tier_spec().tier
            )
        if cortex_meta_get(self._db, "embedding_dim") is None:
            cortex_meta_upsert(
                self._db, "embedding_dim", str(self.config.embedding_dim)
            )

    def _make_id(self, file_path: Path, suffix: str = "") -> str:
        path_hash = hashlib.md5(str(file_path).encode()).hexdigest()[:8]
        return f"{path_hash}_{suffix}" if suffix else path_hash

    @staticmethod
    def _extract_purpose(content: str, file_path: Path) -> str:
        """Extract a purpose string from a file without AGENT_HEADER.

        Tries module docstring first, then falls back to filename-derived text.
        """
        # Python module docstring
        if file_path.suffix == ".py":
            match = re.match(r'^(?:#[^\n]*\n)*\s*(?:\'\'\'|""")(.*?)(?:\'\'\'|""")', content, re.DOTALL)
            if match:
                doc = match.group(1).strip().split("\n")[0].strip()
                if doc and len(doc) > 10:
                    return doc

        # Markdown title
        if file_path.suffix == ".md":
            match = re.match(r"^#\s+(.+)", content)
            if match:
                return match.group(1).strip()

        # YAML description field
        if file_path.suffix in (".yaml", ".yml"):
            match = re.search(r"^description:\s*['\"]?(.+?)['\"]?\s*$", content, re.MULTILINE)
            if match:
                return match.group(1).strip()

        # Fallback: filename with path context
        parts = file_path.parts
        # Use last 2-3 meaningful path components
        meaningful = [p for p in parts[-3:] if p not in ("src", "__init__.py")]
        if meaningful:
            return " ".join(meaningful).replace(".py", "").replace("_", " ")

        return ""

    @staticmethod
    def _guess_role(file_path: Path) -> FileRole:
        """Guess the file role from its path and extension."""
        name = file_path.name.lower()
        suffix = file_path.suffix.lower()

        if suffix in (".md", ".rst", ".txt"):
            return FileRole.DOC
        if suffix in (".yaml", ".yml", ".toml", ".json", ".ini", ".cfg"):
            return FileRole.CONFIG
        if suffix == ".sql":
            return FileRole.DATA
        if "test" in name or "spec" in name:
            return FileRole.CODE
        if "template" in name or suffix in (".html", ".jinja", ".j2"):
            return FileRole.TEMPLATE
        return FileRole.CODE

    def index_file(
        self,
        file_path: Path,
        force: bool = False,
        root: Optional[Path] = None,
        project: Optional[str] = None,
    ) -> bool:
        """Index a single file. Returns True if indexed/updated.

        `project` is stored on every row so cortex_search can filter by
        project slug without walking paths. If omitted it's derived from
        okuro.cortex.roots.project_for_path so callers using absolute paths
        still get correct attribution.
        """
        if not file_path.exists():
            return False

        if project is None:
            try:
                from .roots import project_for_path

                project = project_for_path(file_path)
            except Exception:
                project = None

        # Tier gate: air-tier repos (or a host that can't run a model) index
        # into cortex_docs + FTS only — no neural embeddings, no vec_cortex row.
        from .tier_policy import should_embed

        do_embed = should_embed(project, self._db)

        content = file_path.read_text()
        file_hash = hashlib.md5(content.encode()).hexdigest()[:12]

        base_id = self._make_id(file_path, "header")
        existing = self._db.fetchone(
            "SELECT file_hash, project FROM cortex_docs WHERE id = ?", (base_id,)
        )
        # Content-hash is not the whole index state: a file's PROJECT can change
        # with its content unchanged (a root re-registration flips
        # project_for_path). Skipping re-index then leaves cortex_docs.project
        # and the vec_cortex partition to drift apart — the vector cannot be
        # UPDATEd in place (sqlite-vec) and only the full re-index below moves it
        # (via _remove_file + re-insert under the new partition). So the
        # short-circuit must also require the project to match; a mismatch falls
        # through and self-heals the drift. (This is the G11 root: 449
        # vectors stranded when an in-tree clone was retagged to the workspace root.)
        project_unchanged = bool(existing) and existing["project"] == project
        # Why this file is not short-circuiting. Set here (where the state is
        # known) and reported at the write below under
        # OKURO_CORTEX_DEBUG_REINDEX — a file re-indexed on EVERY cycle at a
        # constant count is a cache-miss loop, and the reason names the cause.
        if force:
            reindex_reason = "forced"
        elif not existing:
            reindex_reason = "no_row"
        elif existing["file_hash"] != file_hash:
            reindex_reason = "hash_changed"
        elif not project_unchanged:
            reindex_reason = f"project_changed:{existing['project']}->{project}"
        else:
            reindex_reason = "missing_vector"
        if existing and not force and existing["file_hash"] == file_hash \
                and project_unchanged:
            # Hash unchanged. When embedding, only short-circuit if the vector is
            # present (migration 059 dropped vec_cortex; a post-migration reindex
            # must repopulate). When NOT embedding (air), the doc row alone is the
            # complete index state — short-circuit unconditionally.
            if not do_embed:
                return False
            has_vec = self._db.fetchone(
                "SELECT 1 AS present FROM vec_cortex WHERE id = ?", (base_id,)
            )
            if has_vec:
                return False

        header = parse_header(content)

        # Priority chain: inline header → sidecar → docstring → skip
        if header is None:
            # Try sidecar entry
            from . import sidecar as sidecar_mod

            sidecar_entries = sidecar_mod.load(file_path.parent)
            sidecar_entry = sidecar_entries.get(file_path.name)
            if sidecar_entry and sidecar_entry.purpose:
                header = sidecar_mod.entry_to_header(sidecar_entry)
            else:
                # Docstring / filename fallback
                purpose = self._extract_purpose(content, file_path)
                if not purpose:
                    return False
                header = AgentHeader(
                    role=self._guess_role(file_path),
                    purpose=purpose,
                    index=[],
                )

        if root and file_path.is_relative_to(root):
            display_path = str(file_path.relative_to(root))
        else:
            display_path = file_path.name

        # Collect documents to index
        documents = []
        metadatas = []
        ids = []

        # 1. Purpose
        documents.append(f"File: {display_path}\nPurpose: {header.purpose}")
        metadatas.append(
            {
                "file_path": str(file_path),
                "file_hash": file_hash,
                "doc_type": "header",
                "role": header.role.value,
                "purpose": header.purpose,
                "section": None,
                "chunk_index": None,
            }
        )
        ids.append(base_id)

        # 2. Sections from index
        for i, entry in enumerate(header.index):
            doc_text = (
                f"File: {display_path}\nSection: {entry.description}\n"
                f"Lines: {entry.start_line}-{entry.end_line}"
            )
            documents.append(doc_text)
            metadatas.append(
                {
                    "file_path": str(file_path),
                    "file_hash": file_hash,
                    "doc_type": "section",
                    "role": header.role.value,
                    "purpose": header.purpose,
                    "section": entry.description,
                    "chunk_index": None,
                }
            )
            ids.append(self._make_id(file_path, f"section_{i}"))

        # 3. Content chunks
        if self.config.include_content:
            lines = content.split("\n")
            budget_chars = self.config.chunk_tokens * max(1, self.config.chars_per_token)
            # A section entry is usable for chunking only if it carries a real
            # line range. AGENT_HEADER indexes store DESCRIPTION-ONLY entries
            # (start_line == end_line == 0); those slice to lines[0:0] == "" and
            # embed NO body. Before 2026-07-16 that silently left ~half of
            # indexed files (every headered file) with only their purpose string
            # in the dense index — a file was findable by what it SAYS it does,
            # never by what it contains. Fall back to whole-file chunking in
            # that case, exactly as for a headerless file.
            usable_sections = [
                e for e in header.index if e.end_line > e.start_line
            ]
            if usable_sections:
                # Indexed sections — chunk each section
                for entry in usable_sections:
                    start = max(0, entry.start_line - 1)
                    end = min(len(lines), entry.end_line)
                    section_content = "\n".join(lines[start:end])

                    if len(section_content) > budget_chars:
                        chunks = self._chunk_body(section_content, file_path)
                    else:
                        chunks = (
                            [section_content] if section_content.strip() else []
                        )
                    chunks = self._cap_chunks(
                        chunks, f"{display_path}::{entry.description}"
                    )

                    breadcrumb = self._breadcrumb(
                        display_path, header.purpose, entry.description
                    )
                    for j, chunk in enumerate(chunks):
                        doc_text = self._compose_chunk_doc(
                            breadcrumb, entry.description, chunk
                        )
                        documents.append(doc_text)
                        metadatas.append(
                            {
                                "file_path": str(file_path),
                                "file_hash": file_hash,
                                "doc_type": "content",
                                "role": header.role.value,
                                "purpose": header.purpose,
                                "section": entry.description,
                                "chunk_index": j,
                            }
                        )
                        ids.append(
                            self._make_id(
                                file_path, f"content_{entry.start_line}_{j}"
                            )
                        )
            else:
                # No usable section ranges — chunk the whole file. Covers both
                # headerless files and headered files whose index is
                # description-only (0-0 ranges).
                whole = "\n".join(lines)
                if whole.strip():
                    chunks = (
                        self._chunk_body(whole, file_path)
                        if len(whole) > budget_chars
                        else [whole]
                    )
                    chunks = self._cap_chunks(chunks, display_path)
                    breadcrumb = self._breadcrumb(display_path, header.purpose, None)
                    for j, chunk in enumerate(chunks):
                        doc_text = self._compose_chunk_doc(breadcrumb, None, chunk)
                        documents.append(doc_text)
                        metadatas.append({
                            "file_path": str(file_path),
                            "file_hash": file_hash,
                            "doc_type": "content",
                            "role": header.role.value,
                            "purpose": header.purpose,
                            "section": None,
                            "chunk_index": j,
                        })
                        ids.append(self._make_id(file_path, f"content_0_{j}"))

        # Compute embeddings BEFORE taking the writer lock. embed() is an
        # HTTP call; holding the writer across it blocks every other
        # session's write_memory / log_progress. Skipped entirely on the air
        # tier / model-less host — the doc rows still power FTS/BM25 search.
        embeddings = embed(documents) if (documents and do_embed) else []

        # An embedding tier that produced no (or too few) vectors must NOT
        # half-write. The insert below only writes a vec_cortex row `if
        # embeddings`, so doc rows would land WITHOUT vectors — and the has_vec
        # short-circuit above would then miss on every future cycle, re-embedding
        # this file forever while the file looks indexed. A short return also
        # beats the IndexError that embeddings[i] would raise on a length
        # mismatch. Leave the previous rows intact and report the miss; the next
        # cycle retries once the embedder is healthy again.
        if do_embed and documents and len(embeddings) != len(documents):
            import logging

            logging.getLogger("okuro.cortex.vectorstore").error(
                "embed returned %d vectors for %d documents — skipping %s "
                "(index left unchanged; a partial write would re-embed this "
                "file every cycle)",
                len(embeddings),
                len(documents),
                file_path,
            )
            return False

        if os.environ.get("OKURO_CORTEX_DEBUG_REINDEX", "").strip() not in ("", "0"):
            import logging

            # do_embed with no embeddings writes doc rows but NO vec_cortex row,
            # so the has_vec short-circuit above misses forever: this file
            # re-embeds every cycle until the cause is fixed.
            will_loop = do_embed and not embeddings
            logging.getLogger("okuro.cortex.vectorstore").info(
                "reindex %s: reason=%s project=%s docs=%d embeddings=%d%s",
                file_path,
                reindex_reason,
                project,
                len(documents),
                len(embeddings),
                " WILL-LOOP(no vector written)" if will_loop else "",
            )

        # One short write transaction: remove stale rows, then insert all
        # new doc + vector pairs. DELETE opens an implicit writer txn
        # anyway — make it explicit so the embed call is clearly outside.
        # Never store a NULL vec_cortex partition key — it disables vec0 chunk
        # packing (one chunk per row). Unscoped vectors go to a shared sentinel
        # partition so they pack. cortex_docs.project keeps the real value/NULL
        # (doc semantics, DISTINCT counts) — only the vector partition is
        # coalesced; the query path filters by the same `project or None` logic.
        vec_project = project if project else NO_PROJECT_PARTITION
        with self._db.write():
            self._remove_file(file_path)
            for i, (doc_id, doc, meta) in enumerate(zip(ids, documents, metadatas)):
                self._db.execute(
                    "INSERT INTO cortex_docs "
                    "(id, file_path, file_hash, doc_type, role, purpose, "
                    "section, chunk_index, document, project) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        doc_id,
                        meta["file_path"],
                        meta["file_hash"],
                        meta["doc_type"],
                        meta["role"],
                        meta["purpose"],
                        meta["section"],
                        meta["chunk_index"],
                        doc,
                        project,
                    ),
                )
                # Vector row only on the embedding tiers. Air-tier docs live in
                # cortex_docs + FTS alone — the query path's BM25 arm covers them.
                if embeddings:
                    self._db.execute(
                        "INSERT INTO vec_cortex (id, project, embedding) "
                        "VALUES (?, ?, ?)",
                        (doc_id, vec_project, to_bytes(embeddings[i])),
                    )
        return True

    @staticmethod
    def _breadcrumb(
        display_path: str, purpose: str, section: Optional[str]
    ) -> str:
        """Build the contextual breadcrumb prepended to a chunk before
        embedding (F11): ``path > [purpose] > section``.

        Disambiguates near-identical chunks across the corpus (e.g. five
        "Introduction" sections or five "__init__" methods) by anchoring each
        chunk's vector to its full ancestry rather than just the leaf heading.
        Each part is included once, blank parts dropped.
        """
        parts: list[str] = []
        if display_path:
            parts.append(display_path)
        # Purpose gives semantic context the bare path/section lack.
        if purpose:
            parts.append(purpose.strip())
        if section and section.strip() and section.strip() not in parts:
            parts.append(section.strip())
        return " > ".join(parts)

    def _compose_chunk_doc(
        self, breadcrumb: str, section: Optional[str], chunk: str
    ) -> str:
        """Compose the text actually embedded for a content chunk.

        With contextual_headers ON, the breadcrumb is prepended so the chunk's
        ancestry is part of its embedding. OFF reproduces a minimal header.
        """
        if self.config.contextual_headers and breadcrumb:
            return f"{breadcrumb}\nContent:\n{chunk}"
        if section:
            return f"Section: {section}\nContent:\n{chunk}"
        return f"Content:\n{chunk}"

    def _chunk_body(self, text: str, file_path: Path) -> list[str]:
        """Dispatch body chunking: cAST for code (F10), line window otherwise.

        When ``ast_chunking`` is on AND the file's extension maps to a
        tree-sitter grammar, chunk on AST structure (whole functions/classes
        stay intact, lossless). Any failure — unknown language, missing grammar,
        parse error, or an empty AST result — falls back to the line/token
        chunker so indexing NEVER crashes and prose/config is unaffected.
        """
        if self.config.ast_chunking:
            try:
                from .cast import cast_chunk_code, language_for_path

                lang = language_for_path(file_path)
                if lang:
                    chunks = cast_chunk_code(
                        text, lang, max_chars=self.config.cast_max_chars
                    )
                    if chunks:
                        return chunks
            except Exception:
                pass  # graceful fallback to the line chunker
        return self._chunk_text(text)

    def _approx_tokens(self, text: str) -> int:
        """Approximate token count from char length (avoids per-chunk
        tokenizer loads). ~4 chars/token is the standard English/code ratio."""
        cpt = max(1, self.config.chars_per_token)
        return (len(text) + cpt - 1) // cpt

    def _chunk_text(self, text: str, chunk_size: int | None = None) -> list[str]:
        """Token-windowed, line-aligned chunking with sliding overlap (F7/F8).

        Builds chunks up to ``chunk_tokens`` (approximated via chars_per_token)
        and slides the window forward by ``(1 - chunk_overlap_ratio)`` of the
        budget, so consecutive chunks share an overlap band — concepts that
        straddle a boundary appear in at least one whole chunk. Line-aligned so
        we never split mid-line. ``chunk_size`` is ignored (legacy arg kept for
        call-site compatibility); the budget comes from config.
        """
        budget_tokens = self.config.chunk_tokens
        budget_chars = max(1, budget_tokens * max(1, self.config.chars_per_token))
        overlap = min(max(self.config.chunk_overlap_ratio, 0.0), 0.9)

        lines = text.split("\n")
        # Pre-compute per-line char cost (+1 for the join newline).
        costs = [len(ln) + 1 for ln in lines]

        chunks: list[str] = []
        i = 0
        n = len(lines)
        while i < n:
            cur_chars = 0
            j = i
            while j < n and (cur_chars + costs[j] <= budget_chars or j == i):
                cur_chars += costs[j]
                j += 1
            chunk = "\n".join(lines[i:j]).strip()
            if chunk:
                chunks.append(chunk)
            if j >= n:
                break
            # Slide forward, retaining an overlap band of lines. Compute how
            # many trailing lines of [i:j] fit in the overlap char budget.
            overlap_chars = int(budget_chars * overlap)
            back = 0
            acc = 0
            k = j - 1
            while k > i and acc + costs[k] <= overlap_chars:
                acc += costs[k]
                back += 1
                k -= 1
            next_i = j - back
            # Guarantee forward progress (avoid infinite loop on a huge line).
            i = next_i if next_i > i else j
        return chunks

    def _cap_chunks(self, chunks: list[str], where: str) -> list[str]:
        """Apply the per-unit chunk cap, LOGGING any truncation (F9 — the old
        3/5 caps dropped content silently). 0 = unlimited."""
        cap = self.config.max_chunks_per_unit
        if cap and len(chunks) > cap:
            import logging
            logging.getLogger("okuro.cortex.vectorstore").warning(
                "chunk cap hit: %s produced %d chunks, keeping %d "
                "(raise OKURO_CORTEX_MAX_CHUNKS to keep more)",
                where, len(chunks), cap,
            )
            return chunks[:cap]
        return chunks

    def _remove_file(self, file_path: Path):
        """Remove all entries for a file."""
        rows = self._db.fetchall(
            "SELECT id FROM cortex_docs WHERE file_path = ?",
            (str(file_path),),
        )
        for row in rows:
            self._db.execute(
                "DELETE FROM vec_cortex WHERE id = ?", (row["id"],)
            )
            self._db.execute(
                "DELETE FROM cortex_docs WHERE id = ?", (row["id"],)
            )

    @staticmethod
    def _tokenize_query(query: str) -> list[str]:
        return [
            w
            for w in re.sub(r"[^\w\s]", " ", query.lower()).split()
            if len(w) > 1
        ]

    @classmethod
    def _fts_match_query(cls, query: str) -> str:
        """Build a safe FTS5 MATCH expression from a free-text query.

        Strips FTS5 operator characters and ORs the surviving tokens — so a
        natural-language query behaves as "any of these terms" (BM25 then
        ranks by TF·IDF). Each token is double-quoted to neutralise reserved
        words (AND/OR/NOT/NEAR) and punctuation. Returns "" when no usable
        token remains (caller then skips the BM25 arm).
        """
        tokens = cls._tokenize_query(query)
        if not tokens:
            return ""
        # Quote each token; FTS5 treats a quoted string as a literal term.
        return " OR ".join(f'"{t}"' for t in tokens)

    @staticmethod
    def _keyword_score(
        tokens: list[str],
        purpose: str,
        sections: list[str],
        file_path: str,
        document: str,
    ) -> float:
        if not tokens:
            return 0.0
        purpose_lower = purpose.lower()
        sections_lower = " ".join(sections).lower()
        path_lower = file_path.lower()
        doc_lower = document.lower()

        total_weight = 0.0
        max_weight = 0.0
        per_token_max = 8.0

        for token in tokens:
            max_weight += per_token_max
            if token in purpose_lower:
                total_weight += 3.0
            if token in sections_lower:
                total_weight += 2.0
            if token in path_lower:
                total_weight += 2.0
            if token in doc_lower:
                total_weight += 1.0

        return total_weight / max_weight if max_weight > 0 else 0.0

    @staticmethod
    def _synthesize_snippet(meta: dict, purpose: str) -> Optional[str]:
        """Build a preview snippet for ANY doc type (F32).

        Previously only ``doc_type == content`` rows carried a snippet, so the
        majority of hits (header + section docs) returned ``None`` and the
        agent got no preview. We synthesize from the richest available text:
          * content → the stored document body
          * section → purpose + section description
          * header  → purpose
        Falls back to the stored ``document`` text. Capped at 200 chars.
        """
        doc_type = meta.get("doc_type")
        document = (meta.get("document") or "").strip()
        section = (meta.get("section") or "").strip()

        if doc_type == "content" and document:
            text = document
        elif doc_type == "section":
            parts = [p for p in (purpose, section) if p]
            text = " — ".join(parts) if parts else document
        elif doc_type == "header":
            text = purpose or document
        else:
            text = document or purpose

        text = text.strip()
        if not text:
            return None
        return text[:200]

    def search(
        self,
        query: str,
        n_results: int = 10,
        filter_role: Optional[str] = None,
        filter_path_prefix: Optional[str] = None,
        project: Optional[str] = None,
    ) -> list[SearchResult]:
        """Hybrid-capable search with best-effort query logging (F38).

        Times the inner search and appends a cortex_query_log row (query text +
        result count + zero-result flag + latency + scope). The log write is
        fire-and-forget — it never raises into the hot path. See
        :meth:`_search_impl` for the retrieval logic.
        """
        t0 = time.perf_counter()
        search = (
            self._search_overlay
            if project and self._is_overlay_slug(project)
            else self._search_impl
        )
        results = search(
            query,
            n_results=n_results,
            filter_role=filter_role,
            filter_path_prefix=filter_path_prefix,
            project=project,
        )
        latency_ms = (time.perf_counter() - t0) * 1000.0
        try:
            from .observability import log_query

            log_query(
                query,
                len(results),
                project=project,
                latency_ms=latency_ms,
                hybrid=self.config.hybrid,
                db=self._db,
            )
        except Exception:
            pass  # observability must never break search
        return results

    @staticmethod
    def _is_overlay_slug(project: str) -> bool:
        from .worktrees import parse_overlay_slug

        return parse_overlay_slug(project) is not None

    def _search_overlay(
        self,
        query: str,
        n_results: int = 10,
        filter_role: Optional[str] = None,
        filter_path_prefix: Optional[str] = None,
        project: Optional[str] = None,
    ) -> list[SearchResult]:
        """Search a worktree overlay together with the base it overlays.

        Two partition-scoped searches, merged under one rule:

            when an overlay document and a base document share the same
            RELATIVE path, DROP the base document.

        Same relative path means same logical file, and the overlay copy is
        the one on the agent's disk. Returning both ranked by score is the
        precise hazard this exists to remove — an agent cannot be expected to
        notice that result #2 is the real one. Shadowing is deterministic; a
        score adjustment is not.

        Everything the branch did NOT touch is served from base, which for an
        unchanged file is byte-identical and therefore correct, not stale.
        """
        from .worktrees import parse_overlay_slug, worktree_for_slug

        parsed = parse_overlay_slug(project or "")
        if parsed is None:
            return self._search_impl(
                query, n_results=n_results, filter_role=filter_role,
                filter_path_prefix=filter_path_prefix, project=project,
            )
        base_project, _branch = parsed

        def run(slug):
            return self._search_impl(
                query, n_results=n_results, filter_role=filter_role,
                filter_path_prefix=filter_path_prefix, project=slug,
            )

        overlay_hits = run(project)
        base_hits = run(base_project)

        ctx = worktree_for_slug(project)
        if ctx is None:
            # The worktree is gone, so no relative path can be computed and no
            # shadow decided. Serve base alone rather than risk showing two
            # versions of one file: the overlay is stale by definition here,
            # and reap() will collect it.
            return base_hits[:n_results]

        def relative_to(path, root) -> Optional[str]:
            try:
                return str(Path(path).resolve().relative_to(root))
            except (OSError, ValueError):
                return None

        shadowed = {
            rel for rel in (relative_to(h.file_path, ctx.tree)
                            for h in overlay_hits)
            if rel is not None
        }
        merged = list(overlay_hits) + [
            h for h in base_hits
            if relative_to(h.file_path, ctx.base) not in shadowed
        ]
        merged.sort(key=lambda h: h.relevance, reverse=True)
        return merged[:n_results]

    def _search_impl(
        self,
        query: str,
        n_results: int = 10,
        filter_role: Optional[str] = None,
        filter_path_prefix: Optional[str] = None,
        project: Optional[str] = None,
    ) -> list[SearchResult]:
        """Retrieval logic. DEFAULT = pure dense (cosine KNN) + score floor.

        The default path is dense-only because the measured A/B on okuro's
        semantic-code corpus showed dense alone (MRR 0.735) beats FTS5+RRF
        fusion (0.607) — the strong dense arm is diluted by equal-weight BM25.
        Set ``config.hybrid`` / ``OKURO_CORTEX_HYBRID=1`` to enable the FTS5
        BM25 + RRF fusion path (opt-in lever for lexical-heavy corpora /
        exact-symbol search). The optional cross-encoder reranker (F26) layers
        on either path and is itself off by default.

        ``project`` (if set) scopes retrieval via the vec_cortex partition key
        (F20/F21), and the BM25 arm via its FTS project filter when hybrid.

        THE AIR FLOOR IS READ HERE, NOT ONLY WRITTEN. ``index()`` gates
        embedding behind ``tier_policy.should_embed`` (line ~594), so an
        air-tier managed repo — or any host with no embedding backend — has
        documents in ``cortex_docs`` and ZERO rows in ``vec_cortex``. Until
        2026-09-06 this method asked no such question: it ran the dense arm
        unconditionally, so those corpora returned an empty list on a pro box
        and raised ``EmbeddingsUnavailable`` on an air one. Measured then: 10
        managed repos on tier 'air', ~1969 docs, 0 vectors, 0 results. The FTS5
        table, the BM25 arm and RRF fusion already existed and worked — the
        searcher simply never consulted the gate the indexer obeys.

        Hybrid stays OFF by default. This is not "turn on hybrid": the BM25
        arm is taken ALONE, and only when the dense arm has nothing to search.
        Fusing them by default would regress the measured MRR from 0.735 to
        0.607 on corpora that do have vectors.
        """
        from .tier_policy import should_embed

        pool = max(self.config.candidate_pool, n_results * 3)

        # Ask the indexer's own question before embedding anything: a query
        # embedding is useless against a partition that was never written, and
        # on an air host embed_one() raises rather than returning empty.
        dense_ok = should_embed(project, self._db)

        vec_results: list = []
        dense_rank: list = []
        if dense_ok:
            # --- Dense arm ---
            embed_query = _format_query_for_model(query, self.config.embedding_model)
            query_embedding = embed_one(embed_query)
            emb_bytes = to_bytes(query_embedding)
            partition = ("project", project) if project else None
            try:
                vec_results = self._db.vec_search(
                    "vec_cortex", emb_bytes, limit=pool, partition=partition
                )
            except Exception:
                # Pre-migration vec_cortex (no partition key) rejects the
                # partition filter — fall back to a global scan + Python
                # project filter so search degrades gracefully until the
                # reindex rebuilds the table.
                vec_results = self._db.vec_search("vec_cortex", emb_bytes, limit=pool)
            dense_rank = [vr["id"] for vr in vec_results]

        # BM25 runs when hybrid is on (fusion), or as the SOLE arm whenever the
        # dense arm produced nothing — whether it was gated off, or the corpus
        # is indexed but not yet embedded. Returning [] while a working lexical
        # index sits right there is the same defect wearing a different hat.
        need_bm25 = self.config.hybrid or not dense_rank
        bm25_rank: list = []
        bm25_score: dict = {}
        if need_bm25:
            # --- Sparse arm: BM25 ---
            match_q = self._fts_match_query(query)
            bm25_rows = (
                self._db.fts_search(
                    "cortex_fts", "cortex_docs", match_q, limit=pool, project=project
                )
                if match_q
                else []
            )
            bm25_rank = [r["id"] for r in bm25_rows]
            bm25_score = {r["id"]: r["bm25"] for r in bm25_rows}

        if dense_rank and not self.config.hybrid:
            # --- Pure dense path (default, measured best) ---
            # cosine distance ∈ [0,2] → similarity ∈ [0,1] via 1 - d/2 (F23).
            sims = {
                vr["id"]: cosine_distance_to_similarity(vr["distance"])
                for vr in vec_results
            }
            ordered_ids = dense_rank  # already distance-sorted (best first)
            score_of = sims
            kw_of = {}
        else:
            # Fusion when both arms ran; a single-list RRF when only one did,
            # which keeps scores on one comparable [0,1] scale either way.
            ranked_lists = [r for r in (dense_rank, bm25_rank) if r]
            fused = (
                reciprocal_rank_fusion(ranked_lists, k0=self.config.rrf_k0)
                if ranked_lists
                else {}
            )
            if not fused:
                return []
            ordered_ids = sorted(fused, key=lambda d: fused[d], reverse=True)
            max_score = max(fused.values()) or 1.0
            score_of = {d: fused[d] / max_score for d in ordered_ids}
            kw_of = bm25_score

        # --- Build results (shared; dedup by file) ---
        seen_files: set[str] = set()
        search_results: list[SearchResult] = []
        for doc_id in ordered_ids:
            meta = self._db.fetchone(
                "SELECT * FROM cortex_docs WHERE id = ? AND deleted_at IS NULL",
                (doc_id,),
            )
            if not meta:
                continue  # dangling vec/fts row or tombstoned doc
            file_path_str = meta["file_path"]
            if filter_role and meta["role"] != filter_role:
                continue
            if filter_path_prefix and filter_path_prefix not in file_path_str:
                continue
            doc_project = meta.get("project") or ""
            if project and doc_project != project:
                continue
            # An overlay partition is valid ONLY in its own named scope. An
            # unscoped search asks "what is in the index", and a branch's
            # private copy of a file is not an answer to that — returning it
            # puts BOTH copies in one ranked list, which is the duplicate
            # regression that made canonical files rank below their own stale
            # worktree copies in May 2026 (141,738 of them).
            #
            # Filtered HERE rather than at the vector query because vec_search
            # takes one partition, not an exclusion, and the row is already
            # loaded — so this costs nothing. Pool crowding is bounded: the
            # whole fleet's overlays are ~1.1% of the index.
            if not project and doc_project and self._is_overlay_slug(doc_project):
                continue
            if file_path_str in seen_files:
                continue
            seen_files.add(file_path_str)

            file_path = Path(file_path_str)
            if file_path.exists():
                try:
                    content = file_path.read_text()
                    header = parse_header(content)
                except Exception:
                    header = None
                index_entries = header.index if header else []
                purpose = header.purpose if header else meta.get("purpose", "")
            else:
                index_entries = []
                purpose = meta.get("purpose", "")

            relevance = score_of.get(doc_id, 0.0)
            if relevance < self.config.score_floor:
                continue

            search_results.append(
                SearchResult(
                    file_path=file_path,
                    purpose=purpose,
                    relevance=relevance,
                    index_entries=index_entries,
                    matched_section=meta.get("section"),
                    snippet=self._synthesize_snippet(meta, purpose),
                    keyword_score=kw_of.get(doc_id, 0.0),
                    project=meta.get("project"),
                )
            )
            if len(search_results) >= pool:
                break

        # --- Optional cross-encoder rerank (F26) over the candidates ---
        search_results = self._maybe_rerank(query, search_results)

        return search_results[:n_results]

    def _maybe_rerank(
        self, query: str, candidates: list["SearchResult"]
    ) -> list["SearchResult"]:
        """Cross-encoder rerank seam (F26). No-op unless a reranker is active.

        Delegates to okuro.cortex.rerank.get_reranker(); when reranking is
        disabled or the model/dep/GPU is unavailable the reranker is None and
        we return the fusion order unchanged — never crash, never hard-depend.
        """
        if not candidates:
            return candidates
        try:
            from .rerank import get_reranker

            reranker = get_reranker(self.config)
        except Exception:
            reranker = None
        if reranker is None:
            return candidates
        try:
            passages = [self._rerank_passage(r) for r in candidates]
            scores = reranker.score(query, passages)
            order = sorted(
                range(len(candidates)), key=lambda i: scores[i], reverse=True
            )
            return [candidates[i] for i in order]
        except Exception:
            # Any runtime failure in the reranker must not break search.
            return candidates

    @staticmethod
    def _rerank_passage(r: "SearchResult") -> str:
        """Compose the richest available text for the cross-encoder to judge.

        A bare 200-char snippet is too thin for a cross-encoder. We combine the
        file path (path tokens carry strong relevance signal for code search),
        the purpose, the matched section, and the snippet — deduped and joined.
        """
        parts = [
            str(r.file_path),
            r.purpose or "",
            r.matched_section or "",
            r.snippet or "",
        ]
        seen: set[str] = set()
        out: list[str] = []
        for p in parts:
            p = (p or "").strip()
            if p and p not in seen:
                seen.add(p)
                out.append(p)
        return " \n ".join(out) if out else str(r.file_path)

    def index_directory(
        self,
        root: Path,
        extensions: Optional[set[str]] = None,
        exclude_patterns: Optional[list[str]] = None,
        progress_callback: Optional[Callable[[Path], None]] = None,
        project: Optional[str] = None,
        force: bool = False,
    ) -> tuple[int, int]:
        """Index all files in directory.

        `project` tags every indexed row with this slug. If omitted, it's
        resolved per-file via okuro.cortex.roots.project_for_path so nested
        repos get correctly attributed. `force=True` re-indexes every file even
        when its content hash is unchanged — needed when the chunking/embedding
        config changes (the hash only tracks file content, not chunk params).
        """
        if extensions is None:
            extensions = INDEXABLE_EXTENSIONS

        supported_filenames = SUPPORTED_FILENAMES

        # Exclusion logic delegates to okuro.cortex.exclusions so scanner and
        # vectorstore agree on what to skip. The old fnmatch implementation
        # silently leaked root-level node_modules / .git / dist because
        # fnmatch does not honour ``**`` at path boundaries.
        matcher = build_matcher(root, extra_deny=exclude_patterns or [])

        binary_extensions = BINARY_EXTENSIONS

        total = 0
        indexed = 0

        from .exclusions import DATA_EXHAUST_EXTENSIONS, is_data_exhaust

        # A registered root nested inside this one owns its own files
        # (project_for_path is longest-match). Walking into it here would tag
        # those files with THIS root's slug, and its own pass would tag them
        # back on the next cycle — each re-tag forcing a full re-embed, because
        # a project change cannot UPDATE a vector in place (see index_file).
        # That ping-pong never converges: a workspace repo and its 19 nested roots
        # burned ~2082 re-embeds per cycle, indefinitely. Prune them.
        nested_roots: set[Path] = set()
        try:
            from .roots import registered_roots

            root_resolved = root.resolve()
            for r in registered_roots():
                try:
                    rp = r.path.resolve()
                except (OSError, ValueError):
                    continue
                if rp != root_resolved and rp.is_relative_to(root_resolved):
                    nested_roots.add(rp)
        except Exception:
            nested_roots = set()

        for dirpath, dirnames, filenames in os.walk(root, onerror=lambda _e: None):
            dirnames[:] = [d for d in dirnames if not matcher.is_pruned_dir(d)]
            if nested_roots:
                dirnames[:] = [
                    d for d in dirnames
                    if Path(dirpath, d).resolve() not in nested_roots
                ]

            # Per-directory same-extension census for the data-exhaust
            # heuristic (F3): a directory holding hundreds of same-extension
            # JSON blobs looks machine-generated. Counted once per dir, not
            # per file, so it stays O(files).
            data_ext_counts: dict[str, int] = {}
            for fn in filenames:
                e = Path(fn).suffix.lower()
                if e in DATA_EXHAUST_EXTENSIONS:
                    data_ext_counts[e] = data_ext_counts.get(e, 0) + 1

            for fname in filenames:
                file_path = Path(dirpath) / fname
                ext = file_path.suffix.lower()
                filename = file_path.name.lower()

                if ext in binary_extensions:
                    continue
                if ext not in extensions and filename not in supported_filenames:
                    continue
                try:
                    rel = file_path.relative_to(root)
                except ValueError:
                    continue
                if matcher.is_excluded(rel):
                    continue
                # F3 — skip generated data-exhaust (large/minified/data-lake
                # JSON) while keeping hand-written configs (package.json, …).
                if is_data_exhaust(
                    file_path,
                    sibling_same_ext_count=data_ext_counts.get(ext),
                ):
                    continue

                total += 1
                if progress_callback:
                    progress_callback(file_path)

                try:
                    if self.index_file(
                        file_path, root=root, project=project, force=force
                    ):
                        indexed += 1
                except Exception as e:
                    print(f"Error indexing {file_path}: {e}")

        return total, indexed

    def get_stats(self) -> dict:
        """Get index statistics (global + per-project breakdown)."""
        total = self._db.fetchone(
            "SELECT COUNT(*) as cnt FROM cortex_docs WHERE deleted_at IS NULL"
        )
        headers = self._db.fetchone(
            "SELECT COUNT(*) as cnt FROM cortex_docs "
            "WHERE doc_type = 'header' AND deleted_at IS NULL"
        )
        sections = self._db.fetchone(
            "SELECT COUNT(*) as cnt FROM cortex_docs "
            "WHERE doc_type = 'section' AND deleted_at IS NULL"
        )
        content = self._db.fetchone(
            "SELECT COUNT(*) as cnt FROM cortex_docs "
            "WHERE doc_type = 'content' AND deleted_at IS NULL"
        )
        per_project_rows = self._db.fetchall(
            "SELECT COALESCE(project, '__unassigned__') AS project, "
            "COUNT(*) AS total, "
            "SUM(CASE WHEN doc_type='header' THEN 1 ELSE 0 END) AS files "
            "FROM cortex_docs WHERE deleted_at IS NULL "
            "GROUP BY COALESCE(project, '__unassigned__') "
            "ORDER BY files DESC"
        )
        return {
            "total_documents": total["cnt"] if total else 0,
            "files_indexed": headers["cnt"] if headers else 0,
            "sections_indexed": sections["cnt"] if sections else 0,
            "content_chunks": content["cnt"] if content else 0,
            "embedding_model": self.config.embedding_model,
            "per_project": [
                {
                    "project": r["project"],
                    "files": r["files"] or 0,
                    "documents": r["total"] or 0,
                }
                for r in per_project_rows
            ],
        }

    def clear(self):
        """Clear all indexed data."""
        with self._db.write():
            self._db.execute("DELETE FROM vec_cortex")
            self._db.execute("DELETE FROM cortex_docs")
