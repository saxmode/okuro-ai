# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Agent memory — cross-agent learnings with embeddings and dedup.
# index: imports | def _looks_like_document | def _find_referenced_file | def write_memory | def read_memory | def memory_stats | def memory_census
# AGENT_HEADER_END -->
"""Agent memory — cross-agent learnings with embeddings and dedup.

Ported from tm-launcher brain/memory.py.
Postgres vector ops replaced with okuro.db vec_search + sqlite-vec binary.
"""

import json
import logging as _logging
import os
import re
import time as _time
import uuid
from datetime import datetime, timezone
from okuro.sense.retrieval import candidate_pool, scoped_vec_search, vec_write

# Query-side instruction for the asymmetric embedding path. Qwen3-Embedding is
# instruction-tuned: a retrieval QUERY wrapped "Instruct: {task}\nQuery: {q}"
# separates from documents (embedded raw) far better than a bare query, which
# collapses everything toward cos~0.6. embed_query()'s DEFAULT instruction is
# role-matching-specific, so memory passes its own. Measured 2026-07-16
# (paired n=400): switching the read path to this wrapper moved MRR 0.450→0.526,
# recall@10 0.590→0.698, junk rejection 0.875→1.000 (junk sim ceiling 0.617→
# 0.456), at +0.3ms. It lost on the L2 index earlier only because the pre-fix
# 0.4 floor demanded cos>0.82 and collapsed both arms — floor arithmetic, not
# ranking. DOCUMENTS stay raw (embed_one); only queries take this wrapper.
_QUERY_INSTRUCTION = (
    "Given a task or question about this system, retrieve the most relevant "
    "stored learnings, gotchas, conventions, decisions, and architecture notes."
)

# Minimum cosine similarity for "relevant" vector matches. vec_memory
# declares distance_metric=cosine, so `1 - distance` IS cosine (okuro.embed
# .repair enforces the metric; see its docstring for why that matters).
#
# CALIBRATED against measurement, and re-calibrated when the measurement
# disagreed — 2026-07-15, this store, Qwen3-0.6B. Two passes, because the
# first two answers were wrong in instructive ways:
#
#   pass 1 — 16 hand-written topical queries vs 12 off-domain. Looked like a
#     clean gap at 0.62. It was an artifact: I picked the queries. Widening
#     to 19 showed "postgres index performance" legitimately retrieving an
#     HNSW ef_search memory at 0.618, i.e. 0.62 was silently dropping real
#     matches. Small calibration sets flatter whoever picks them.
#   pass 2 — settled on 0.60 as a recall/precision trade.
#   pass 3 — okuro.sense.memory_eval (60 sampled memories, round-tripped
#     through a slice of their own text; unbiased because the store picks the
#     queries, not me) showed 0.60 is STRICTLY DOMINATED:
#
#       floor   round-trip recall@10   junk rejected
#       0.55          0.600                0.875
#       0.60          0.517                0.875     <- same junk, worse recall
#       0.62          0.500                1.000
#
#     Junk tops out in a cluster (0.405-0.547) with one outlier at 0.617, so
#     0.60 buys NOTHING over 0.55 in rejection and costs 8 points of recall.
#     0.62 removes that one outlier for another 10 points — not a trade worth
#     making when the failure it guards is amnesia.
#
# The distributions genuinely OVERLAP: real matches reach down to 0.547,
# junk reaches up to 0.617. No threshold separates them, so this is an
# explicit trade, not a separation point. Recall wins: a dropped memory is
# amnesia — the exact failure this subsystem exists to prevent — while a
# stray weak row is one line an agent can ignore, and `limit` bounds how many
# it ever sees.
#
# 2026-07-16 — RECALIBRATED for the asymmetric query wrapper. The read path
# now embeds queries via embed_query (instruction-prefixed) instead of
# embed_one (raw); see _QUERY_INSTRUCTION. That wrapper compresses OFF-domain
# similarity far more than on-domain, opening a real gap where none existed:
# measured junk max 0.453 vs real min 0.459 (n=80). At 0.50: recall@10 0.688,
# junk rejection 1.000 — both better than the pre-wrapper point (recall ~0.55,
# junk 0.875). Validated by a paired n=400 A/B (MRR 0.450→0.526).
#
# Retune ONLY by re-running `python -m okuro.sense.memory_eval --sample 60`,
# never by feel, and never against queries you wrote yourself. The --sample is
# load-bearing: the CLI defaults to 40, so the bare command does NOT reproduce
# the numbers above. The eval embeds queries through the SAME wrapper — if you
# change the read path's embedding, the eval must change with it or the gate
# certifies a convention the system doesn't use. memory_eval is AUTHORITATIVE
# for the shipped read path: since 2026-07-17 that path is HYBRID (this vector
# arm + the BM25 lexical arm, RRF-fused — _lexical_ids/_rrf_fuse below), and the
# eval measures hybrid as the headline number while reporting the pure-vector
# arm as `vector_*`. _SIM_FLOOR governs ONLY the vector arm, so retune it
# against `vector_recall_at_10` / `vector_junk_rejection`, then confirm the
# hybrid headline holds. (There is no separate hybrid eval — memory_eval is the
# single source of truth; the gate asserts hybrid >= vector so the two can't
# silently diverge.)
#
# NOT equal to thoughts._SIM_FLOOR. Do not copy either number to the other:
# different corpora (thoughts are short intent-shaped fragments and score lower
# than memory prose for the same relevance), calibrated against different evals.
#
# This paragraph previously asserted "thoughts still embeds queries with
# embed_one (raw) and keeps its own 0.55". BOTH halves were false when written
# and stayed false for three days: thoughts.py:314,484 use embed_query with
# their own instruction, and thoughts._SIM_FLOOR is 0.40. thoughts.py:45-49
# records the mirror-image rot — its cross-reference to THIS constant read 0.62
# long after this moved — and concludes "cite the reasoning, not the value."
# That conclusion was written in a comment that then restated a value again.
#
# So: no number from the other module appears here anymore. If you need
# thoughts' floor, read thoughts.py. A value quoted across a module boundary is
# a copy no test covers, and this file has now rotted twice proving it.
#
# HISTORY — read before retuning. Introduced 2026-05-09 as 0.4 while
# vec_memory still carried vec0's default L2 metric, where `1 - distance` is
# not cosine at all. The "sim 0.29-0.30 noise" that motivated it was really
# cosine ~0.75 — good matches, misread. Against L2 the value demanded cosine
# > 0.82, so from the 2026-05-10 Qwen3 swap onward it discarded every real
# match and EVERY query fell through to the recency sort below. Two months of
# "the agent doesn't remember" was this line.
#
# 2026-07-19 — lowered 0.50 -> 0.45. This is a RECALL/PRECISION TRADE, not a
# free win, and the distinction cost a round of rework worth recording.
#
# An audit first measured this with a hand-built junk arm (16 off-domain
# queries, max cosine 0.4065) and concluded 0.45 "strictly dominates" 0.50 —
# same perfect junk rejection, 8 more true matches kept. memory_eval disagreed.
# Mean over seeds 7/11/23/42, sample=60, identical junk set at both floors:
#
#   floor  hybrid MRR  hybrid r@10  top-1   junk_rej
#    0.45     0.7225       0.9542   0.6083    0.8750
#    0.50     0.7079       0.9250   0.5833    1.0000   <- previous
#
# 0.45 buys +2.9pp recall@10, +2.5pp top-1, +1.5pp MRR, and costs one admitted
# junk query in eight. The hand-built junk arm simply did not contain a query
# landing between 0.45 and 0.50 — the exact "small calibration sets flatter
# whoever picks them" failure this comment already warns about, two paragraphs
# up, repeated by someone who had read it.
#
# A single-seed reading of this trade was ALSO recorded here and did not
# reproduce three hours later: the store is live (memories written, confidences
# bumped by reads), so the eligible pool shifts under the sampler. Quote means
# across several seeds, never a single run, and re-measure rather than trusting
# a number in this comment — including these.
#
# Taken anyway, on this file's own stated principle: a dropped memory is
# amnesia — the failure this subsystem exists to prevent — while a stray weak
# row is one line an agent can ignore, bounded by `limit`. Same reasoning that
# rejected 0.62 in favour of 0.55 earlier. The lexical-slot cap (_cap_lexical)
# independently bounds how much unsupported material can reach the agent.
#
# Retune ONLY via `python -m okuro.sense.memory_eval --sample 60`. Do not
# substitute a junk arm you wrote yourself; that is how this line was wrong
# twice.
_SIM_FLOOR = 0.45

# A superseded row (another memory points to it via `supersedes`) is retracted
# knowledge. write_memory drops its confidence to 0.1, so the 0.3 default floor
# hides it — but a caller passing min_confidence < 0.1 does not. The reviewer
# evidence pool (orchestrator/reviewer/side_effect_evidence.py) passes 0.0, so
# without this a retraction can surface ABOVE its own correction in the pool the
# reviewer scores against. Exclude superseded rows structurally at every read
# site so the invariant "a superseded row never appears in any returned top-k,
# at ANY min_confidence" holds independent of the floor. The `IS NOT NULL` guard
# is load-bearing: a NULL inside a NOT IN list makes the whole predicate return
# no rows (SQL three-valued logic), which would silently empty every read.
def _render_row(r: dict, tag: str | None = None) -> str:
    """One memory as a line, carrying the `→handle` needed to act on it.

    WHY THE HANDLE IS HERE. Every read path used to render topic, scope and
    content and drop the id, even though the row carries it. So an agent could
    find a stale memory by meaning and then be unable to correct it —
    `write_memory(supersedes=...)` needs an id, and the only sources were
    `memory_stale`, `memory_audit`, `memory_utility` and bootstrap pointers,
    none of which is a semantic search. Measured 2026-07-28: during a memory
    cleanup, two separate audit agents identified a harmful memory (one of them
    ranked 1 on the obvious query, briefing every agent on a feature that never
    worked) and reported that they could not retrieve its id to supersede it.
    Recall without a handle is read-only knowledge.

    POSITION differs deliberately from the bootstrap pointer block, which puts
    the handle last. There the body is truncated to a line, so the trailing
    handle stays visible; here the body is full length, so a trailing handle
    would sit an unpredictable distance away. Leading keeps it scannable and
    keeps the `→` marker itself consistent, which is what a reader matches on.

    Same 8-char prefix as bootstrap: `read_memory(memory_id=...)` accepts the
    prefix, and the lookup path reports ambiguity rather than guessing.
    """
    scope = f"[{r['project']}]" if r.get("project") else "[system]"
    handle = f"→{str(r['id'])[:8]}"
    meta = f"{scope} ({tag}) {handle}" if tag else f"{scope} {handle}"
    return f"- **{r['topic']}** {meta}: {r['content']}"


_EXCLUDE_SUPERSEDED = (
    " AND id NOT IN "
    "(SELECT supersedes FROM agent_memory WHERE supersedes IS NOT NULL)"
)

# ---------------------------------------------------------------------------
# Hybrid retrieval — BM25 lexical arm fused with the dense vector arm
# ---------------------------------------------------------------------------
# Pure-vector recall@10 ceils at ~0.68-0.72 even unfloored: a third of the
# misses are real (0/120 sibling-absorption, measured) and code-flavored — file
# paths, symbols, error strings, config values the embedding under-ranks but an
# exact-token match nails. agent_memory_fts (migration 101) adds a BM25 arm;
# reciprocal-rank fusion combines the two rankings.
#
# JUNK SAFETY (load-bearing): the lexical arm is consulted ONLY when the floored
# vector arm is NON-EMPTY (the `if matches:` gate at both call sites). An
# off-domain query has an empty vector arm at the 0.50 cosine floor, so it never
# reaches the lexical arm and stays rejected by the SAME floor as before —
# junk rejection is inherited unchanged from the vector path, not re-derived.
# Proven by the canonical gate okuro.sense.memory_eval (reports hybrid as the
# authoritative number + the vector arm as `vector_*`; docs/research/okuro-
# memory-recall-ceiling.md): recall@10 vector 0.63 -> hybrid 0.88 at sample=60,
# junk rejection 1.000 unchanged; +21-24pp across 3 seeds and a harder
# non-contiguous-token protocol.
_RRF_K = 60  # standard reciprocal-rank-fusion constant (cortex/rerank uses 60)
_LEX_TOKEN_RE = re.compile(r"[A-Za-z0-9_.:/-]+")

# Function words stripped from the lexical query so shared English scaffolding
# ("how do I ...") cannot lexically match a memory. The vector-arm gate is the
# hard junk guard; this just keeps the lexical arm's own precision up.
_LEX_STOP = frozenset((
    "a an the of to in on at for and or but is are was were be been being this "
    "that these those it its as by with from into out up down how what when "
    "where who why which do does did done can could should would will shall may "
    "might must i you he she we they me him her us them my your his our their "
    "not no yes if then than so such very more most some any all each both few "
    "many much other another about above below over under again further once "
    "here there"
).split())


def _lexical_ids(db, query: str, limit: int) -> list[str]:
    """BM25-ranked agent_memory ids for the query's content tokens.

    Superseded rows are excluded structurally (same invariant as the vector
    path). Returns [] on any failure or if agent_memory_fts is absent (the
    vector arm still answers) — the lexical arm is strictly additive.
    """
    try:
        toks = [t for t in _LEX_TOKEN_RE.findall(query or "")
                if len(t) > 1 and t.lower() not in _LEX_STOP]
        if not toks:
            return []
        match = " OR ".join('"' + t.replace('"', '') + '"' for t in toks)
        rows = db.fetchall(
            "SELECT m.id AS id FROM agent_memory_fts f "
            "JOIN agent_memory m ON m.rowid = f.rowid "
            "WHERE agent_memory_fts MATCH ?"
            + _EXCLUDE_SUPERSEDED.replace(" AND id NOT IN", " AND m.id NOT IN")
            + " ORDER BY bm25(agent_memory_fts) LIMIT ?",
            (match, limit),
        )
        return [r["id"] for r in rows]
    except Exception:
        return []


def _rrf_fuse(*ranked_lists: list[str], k: int = _RRF_K) -> list[str]:
    """Reciprocal-rank fusion of one or more ranked id lists."""
    score: dict[str, float] = {}
    for lst in ranked_lists:
        for rank, _id in enumerate(lst):
            score[_id] = score.get(_id, 0.0) + 1.0 / (k + rank + 1)
    return sorted(score, key=lambda i: -score[i])


# Ceiling on how much of a fused result may come from the lexical arm ALONE
# (BM25 hits with no vector support). RRF is rank-based, so when the vector arm
# is thin the lexical arm wins on arithmetic alone: lexical rank-1 scores
# 1/(60+1) and beats vector rank-3's 1/(60+3) regardless of how much better
# the vector match actually is.
#
# Measured 2026-07-19 over 10 realistic queries: 22 of 100 top-10 slots were
# lexical-only, and quality tracked vector-arm size almost perfectly. The
# pathology case — "what did we decide about the Hoover archive OCR pipeline" —
# had exactly ONE memory clear the floor (the correct one, at cos 0.674), then
# RRF admitted 40 BM25 hits and 9 of 10 slots went to distractors that merely
# contained the token "pipeline", four of them from an unrelated project.
#
# The existing junk gate (see _lexical_ids) only protects FULLY off-domain
# queries, where the vector arm is empty and the lexical arm is never
# consulted. It does nothing for a NARROW on-domain query — which is the shape
# agents actually ask.
_LEXICAL_SLOT_CAP = 0.30


def _cap_lexical(fused: list[str], vector_ids: set[str], limit: int) -> list[str]:
    """Keep fusion order, but stop lexical-only rows from flooding the result.

    Vector-supported rows are never dropped. Lexical-only rows are admitted in
    fused order up to `_LEXICAL_SLOT_CAP` of `limit`, and the overflow is
    DISCARDED, not appended.

    The first version appended the overflow instead, reasoning that nothing
    should be lost outright. That made the function a no-op in the one case it
    exists for: with a thin vector arm there are not enough supported rows to
    fill the head, so the overflow lands right back in the visible slots.
    Verified — `_cap_lexical(['v1','l1'...'l9'], {'v1'}, 10)` returned all nine
    lexical rows in the top ten despite an allowance of three. The guarantee
    held only while the vector arm happened to be fat, i.e. precisely when it
    was not needed.

    Returning FEWER rows is the correct answer. An unsupported BM25 hit that
    merely shares a token with the query is not a weak match, it is a wrong
    one, and padding a short result with wrong rows is what taught agents the
    memory surface is noise. `limit` is an upper bound, never a quota.
    """
    if not fused:
        return fused
    allowance = max(1, int(limit * _LEXICAL_SLOT_CAP))
    kept: list[str] = []
    used = 0
    for _id in fused:
        if _id in vector_ids:
            kept.append(_id)
        elif used < allowance:
            kept.append(_id)
            used += 1
    return kept


# ---------------------------------------------------------------------------
# Write-time dedup — cosine OR lexical near-verbatim restatement
# ---------------------------------------------------------------------------
# Cosine-only dedup (top-1 vec neighbour > 0.90) misses near-verbatim
# restatements that the embedding happens to rank just below 0.90 — measured 3
# such real dupes in the live store at cos 0.877-0.899. A lexical (Jaccard)
# co-signal catches them: distinct memory pairs never exceed Jaccard 0.357
# (measured over the whole store), so a Jaccard >= 0.55 gate is precision-safe
# (0 false-merges on the labelled fixture, tests/sense/test_memory_dedup.py).
#
# It does NOT catch CROSS-PHRASING semantic duplicates (the 3 "never git push"
# rules sit at cos 0.74-0.82 AND Jaccard 0.13-0.23 — both INSIDE the distinct
# distribution). No surface threshold separates those from distinct memories;
# they require LLM adjudication (the consolidation tool's job). See
# docs/research/okuro-memory-dedup.md.
_DEDUP_COSINE = 0.90
_DEDUP_JACCARD = 0.55
# The lexical arm also requires this much cosine agreement, so two texts that
# are lexically near-identical but differ on a KEY discriminating token (e.g.
# same sentence, different repo/hash/id) are NOT merged — their embeddings
# diverge. The 3 real dupes the lexical arm targets all sit at cosine 0.877-
# 0.899 (just under the primary 0.90 gate), so 0.80 keeps them while rejecting
# the low-cosine false-merge case.
_DEDUP_LEXICAL_MIN_COSINE = 0.80
# Word-level tokenizer for dedup Jaccard — splits on punctuation (unlike the
# retrieval _LEX_TOKEN_RE, which keeps `./:-` so file paths stay one token).
# The 0.55 threshold and the distinct-pair ceiling (max 0.357) were both
# calibrated against THIS tokenization; keep them in lockstep.
_WORD_TOKEN_RE = re.compile(r"[a-z0-9_]+")


def _content_tokens(s: str) -> set[str]:
    return {t for t in _WORD_TOKEN_RE.findall((s or "").lower()) if len(t) > 2}


def _jaccard(a: str, b: str) -> float:
    A, B = _content_tokens(a), _content_tokens(b)
    if not A or not B:
        return 0.0
    return len(A & B) / len(A | B)


def _find_write_dupe(db, content: str, vec_bytes) -> tuple[str | None, float, str]:
    """Return (existing_id, score, reason) if a live memory is a near-duplicate
    of ``content``, else (None, 0, "").

    Two precision-safe signals:
      * cosine  — top-1 vec neighbour with cosine > 0.90 (unchanged behaviour).
      * lexical — any BM25 neighbour whose token Jaccard vs ``content`` >= 0.55
        (catches near-verbatim restatements the embedding ranks below 0.90).
    Superseded rows are never returned (reinforcing a retraction would un-retract
    it). The lexical arm degrades to nothing if agent_memory_fts is absent.
    """
    # cosine arm (top-1, mirrors the historical dedup)
    try:
        vm = db.vec_search("vec_memory", vec_bytes, limit=1)
        if vm and (1 - vm[0]["distance"]) > _DEDUP_COSINE:
            return vm[0]["id"], 1 - vm[0]["distance"], "cosine"
    except Exception:
        pass
    # lexical arm — BM25 candidates, gated by Jaccard AND cosine agreement.
    # The cosine confirmation needs the new content's vector; skip the arm if it
    # is unavailable (embed failed) or a candidate has no stored vector.
    new_vec = _vec_from_bytes(vec_bytes)
    if new_vec is None:
        return None, 0.0, ""
    for cid in _lexical_ids(db, content, limit=5):
        row = db.fetchone("SELECT content FROM agent_memory WHERE id = ?", (cid,))
        if not row or _jaccard(content, row["content"]) < _DEDUP_JACCARD:
            continue
        crow = db.fetchone("SELECT embedding FROM vec_memory WHERE id = ?", (cid,))
        if not crow:
            continue
        cvec = _vec_from_bytes(crow["embedding"])
        if cvec is None:
            continue
        cos = _cosine(new_vec, cvec)
        if cos >= _DEDUP_LEXICAL_MIN_COSINE:
            return cid, _jaccard(content, row["content"]), "lexical"
    return None, 0.0, ""


def _vec_from_bytes(b):
    """Unit-normalised float32 vector from sqlite-vec bytes, or None."""
    if not b:
        return None
    try:
        import numpy as _np
        v = _np.frombuffer(b, dtype=_np.float32)
        n = float(_np.linalg.norm(v))
        return v / n if n else None
    except Exception:
        return None


def _cosine(a, b) -> float:
    try:
        return float(a @ b) if a.shape == b.shape else -1.0
    except Exception:
        return -1.0


# ---------------------------------------------------------------------------
# Document detection — memory is a POINTER, never a container
# ---------------------------------------------------------------------------

_MAX_MEMORY_CHARS = 800
_HEADER_RE = re.compile(r"^#{1,4}\s", re.MULTILINE)
_CODE_BLOCK_RE = re.compile(r"^```", re.MULTILINE)
_FILE_PATH_RE = re.compile(r"(?:~/|/home/|/etc/|/tmp/)[\w./-]+")

# Opt-in KG claim markup: [[kg: subject | predicate | object]] or
# [[kg: subject | predicate | object | as_of]]. Pipe-delimited so spaces in
# names are fine. Use opt-in markup rather than prose mining because okuro
# has no local-LLM dependency and regex extraction over prose generates
# unacceptable false positives.
_KG_CLAIM_RE = re.compile(
    r"\[\[\s*kg:\s*([^|\]]+?)\s*\|\s*([^|\]]+?)\s*\|\s*([^|\]]+?)"
    r"(?:\s*\|\s*([^\]]+?))?\s*\]\]"
)


def _scan_kg_claims(content: str) -> list[dict]:
    """Extract triple claims from inline ``[[kg: s | p | o | as_of?]]`` markup."""
    claims: list[dict] = []
    for m in _KG_CLAIM_RE.finditer(content):
        s, p, o, aof = m.groups()
        claims.append({
            "subject": s.strip(),
            "predicate": p.strip(),
            "object": o.strip(),
            "as_of": (aof or "").strip() or None,
        })
    return claims


def _validate_kg_claims(claims: list[dict], project: str | None) -> list[dict]:
    """Run kg_assert on each claim, return non-ok verdicts."""
    if not claims:
        return []
    try:
        from okuro.sense.kg_check import kg_assert
    except Exception:
        return []
    flagged: list[dict] = []
    for c in claims:
        try:
            verdict = kg_assert(
                subject=c["subject"], predicate=c["predicate"],
                object=c["object"], as_of=c.get("as_of"),
                project=project,
            )
        except Exception:
            continue
        if verdict.get("verdict") in ("conflict", "warn"):
            flagged.append(verdict)
    return flagged


def _looks_like_document(content: str) -> tuple[bool, str]:
    """Detect if content is a document that should be referenced, not stored.

    Returns (is_document, reason).
    """
    headers = len(_HEADER_RE.findall(content))
    code_blocks = len(_CODE_BLOCK_RE.findall(content))
    file_paths = _FILE_PATH_RE.findall(content)
    lines = content.count("\n")

    # Multiple markdown headers = structured document
    if headers >= 3:
        return True, f"structured document ({headers} markdown headers)"

    # Multiple code blocks = spec/tutorial
    if code_blocks >= 4:
        return True, f"code-heavy document ({code_blocks // 2} code blocks)"

    # Long + has structure = document
    if len(content) > _MAX_MEMORY_CHARS and (headers >= 2 or code_blocks >= 2):
        return True, f"long structured content ({len(content)} chars, {headers} headers)"

    # Very long regardless of structure
    if len(content) > 2000 and lines > 20:
        return True, f"very long content ({len(content)} chars, {lines} lines)"

    return False, ""


def _find_referenced_file(content: str) -> str | None:
    """If the content references an existing file, return its path."""
    for path in _FILE_PATH_RE.findall(content):
        expanded = os.path.expanduser(path)
        if os.path.isfile(expanded):
            return path
    return None


def _title_from(content: str) -> str:
    """A short human title from the first meaningful line of a body.

    Strips markdown heading markers, list bullets and surrounding backticks,
    collapses whitespace, and truncates on a word boundary. Falls back to a
    generic title rather than ever returning something artifact_write would
    refuse (it requires >= 3 chars).
    """
    for raw in content.splitlines():
        line = raw.strip().lstrip("#*->").strip().strip("`").strip()
        if len(line) >= 3:
            if len(line) > 70:
                line = line[:70].rsplit(" ", 1)[0].rstrip(" ,;:—-") + "…"
            return line
    return "Rescued memory body"


def _rescue_oversized_write(
    content: str, *, reason: str, project: str | None, topic: str,
    source_agent: str | None, confidence: float,
) -> tuple[str | None, str | None]:
    """Store a body the pointer guardrail refused, so it is never lost.

    Returns ``(artifact_id, None)`` on success and ``(None, error)`` on
    failure — exactly one is ever set, so a caller cannot cite an id that
    does not exist.

    ``audience='agent'``: this is machine knowledge rescued from a refused
    write, not a deliverable someone asked for. Filing it as 'user' would put
    every rejected fragment into the human artifact gallery, which is the
    clutter this guardrail exists to prevent.

    NOTE ON RETRIES: an agent that re-issues the same over-long body gets a
    second artifact. That is deliberate — deduplicating here would mean a
    content scan on every rejection, and a duplicate artifact is a far
    cheaper failure than a lost one.
    """
    try:
        from okuro.sense.artifacts import artifact_write
        artifact_id = artifact_write(
            kind="report",
            title=_title_from(content),
            summary=(
                f"Rescued from an over-long write_memory call ({reason}). "
                f"Topic {topic!r}. The pointer memory citing this artifact carries the claim."
            ),
            body=content,
            project=project,
            created_by=source_agent,
            confidence=confidence,
            audience="agent",
        )
    except Exception as exc:  # noqa: BLE001 — never let a rescue failure lose the body
        return None, f"{type(exc).__name__}: {exc}"
    # artifact_write reports its own refusals as a "REJECTED: ..." string
    # rather than raising, so a plain truthiness check would hand the caller
    # a refusal message to cite as an artifact id.
    if not isinstance(artifact_id, str) or artifact_id.startswith("REJECTED"):
        return None, str(artifact_id)
    return artifact_id, None


# Ceiling on confidence an agent may assert about its own findings.
#
# WHY. The no-query path below orders by `confidence DESC, created_at DESC`,
# and reinforcement (+0.1 per near-duplicate write) was dead for two months
# behind the L2/cosine defect — so confidence could only ever be *claimed*,
# never *earned*. On 2026-07-15 one investigating session wrote four findings
# at confidence 1.0; because recall was falling back to that ordering, those
# four became the top of every session's memory for every query, and told each
# new session the system was broken. Sessions then investigated, "confirmed"
# it, and wrote more 1.0 alarms. The same session later falsified one of its
# own claims and filed the correction at 0.5 — where it ranked BELOW the error
# it retracted. A memory whose ranking rewards certainty over correctness
# cannot self-correct.
#
# 1.0 is now reserved for ratified fact. An agent's honest ceiling is "very
# confident", not "certain" — leaving headroom for reinforcement and human
# confirmation to lift a claim above anything self-asserted.
_AGENT_CONFIDENCE_CAP = 0.8


def _derive_source_agent() -> str:
    """Who is writing — the bootstrapped provider, else 'unknown'.

    Best-effort by construction: a memory must never fail to store because
    provenance could not be resolved.
    """
    try:
        from okuro.sense.session_state import get_session_state

        return get_session_state().get("provider") or "unknown"
    except Exception:  # noqa: BLE001
        return "unknown"


def _derive_session_id() -> str | None:
    """The active telemetry session id, or None outside a bootstrapped session."""
    try:
        from okuro.sense.session_state import current_audit_session_id

        return current_audit_session_id()
    except Exception:  # noqa: BLE001
        return None


def _derive_code_version() -> str | None:
    """Git SHA this process booted on — see migration 104."""
    try:
        from okuro.system.code_version import loaded_code_version

        return loaded_code_version()
    except Exception:  # noqa: BLE001
        return None


def _confirming_sessions(db, memory_id: str) -> set[str]:
    """Distinct sessions that have ASSERTED or REINFORCED this memory.

    The independence set for copy-discounting. A reinforce only counts as
    corroboration if it comes from a session not already in here — otherwise
    it is the same session restating itself, which bootstrap's unrequested
    memory injection makes the default flow, not evidence.

    Reads only recorded events (migration 105 onward). A legacy memory with no
    events has an empty set, so the first reinforce under the new code counts
    as independent — consistent with measuring the write path only where it is
    instrumented, never inferring a session that was never recorded.
    """
    try:
        rows = db.fetchall(
            "SELECT DISTINCT session_id FROM memory_confidence_events "
            "WHERE memory_id = ? AND session_id IS NOT NULL "
            "AND mechanism IN ('assert', 'reinforce')",
            (memory_id,),
        )
        return {r["session_id"] for r in rows}
    except Exception:  # noqa: BLE001 — absence of history is an empty set, not an error
        return set()


def record_confidence_event(db, memory_id: str, old: float | None,
                            new: float, mechanism: str,
                            note: str = None) -> None:
    """Append one row to the confidence history. Never raises.

    Five mechanisms mutate agent_memory.confidence — write/cap, reinforce,
    supersede, utility decay, and consolidation — and before migration 105 none
    of them left a trace, so the column could not distinguish a confident
    author from a much-reinforced claim from a decayed one. Every mutation
    calls this; see the migration for why session_id on the event is the part
    that matters (independent confirmations vs one session echoing itself).

    Best-effort by construction: losing the audit row must never turn a
    successful confidence update into a failed one.
    """
    try:
        db.execute(
            """INSERT INTO memory_confidence_events
                   (memory_id, old_confidence, new_confidence, mechanism,
                    session_id, source_agent, note)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (memory_id, old, new, mechanism, _derive_session_id(),
             _derive_source_agent(), note),
        )
    except Exception:  # noqa: BLE001 — an audit failure must not block the write
        pass


def _cap_confidence(confidence: float, ratified: bool = False,
                    source_agent: str = "unknown") -> float:
    """Clamp confidence to [0, 1], and to the agent cap unless ratified."""
    asserted, capped = _clamp_and_cap(confidence, ratified=ratified)
    if capped != asserted:
        _logging.getLogger(__name__).info(
            "write_memory: confidence %.2f from %s capped to %.2f (not ratified)",
            asserted, source_agent, _AGENT_CONFIDENCE_CAP,
        )
    return capped


def _clamp_and_cap(confidence: float, ratified: bool = False
                   ) -> tuple[float, float]:
    """Return (asserted, effective) — what was claimed, and what is stored.

    Split out so the asserted value survives. It used to be destroyed at the
    moment of capping: the function returned the clamped float and nothing
    retained the input, making an agent that asserted 0.95 and one that
    asserted 0.80 indistinguishable rows.
    """
    try:
        conf = float(confidence)
    except (TypeError, ValueError):
        conf = 0.7
    asserted = max(0.0, min(1.0, conf))
    if ratified or asserted <= _AGENT_CONFIDENCE_CAP:
        return asserted, asserted
    return asserted, _AGENT_CONFIDENCE_CAP


def write_memory(topic: str, content: str, project: str = None,
                 confidence: float = 0.7, supersedes: str = None,
                 source_agent: str = None, role: str = None,
                 ratified: bool = False, session_id: str = None) -> str:
    """Write a memory with embedding and dedup.

    Args:
        topic: One of 'convention', 'gotcha', 'decision', 'learning', 'architecture'
        content: The memory content.
        project: Optional project slug (None = system-wide).
        confidence: Confidence 0.0-1.0. Agent writes are capped at
            _AGENT_CONFIDENCE_CAP; see that constant.
        supersedes: Optional UUID of memory this supersedes.
        source_agent: Agent identifier. Defaults to the bootstrapped
            provider — see the derivation note below.
        role: Optional role scope.
        ratified: The claim carries human confirmation. Only a ratified write
            may exceed the agent cap. Never set this from an agent's own
            certainty — it is the human's signal, not the writer's.
        session_id: Telemetry session that produced this claim. Defaults to
            the active session; callers should not pass it.

    Provenance is DERIVED, not requested. `source_agent` and `session_id`
    default from session state rather than from the caller because a field an
    agent must remember to pass is a field that ends up unset: measured
    2026-07-19, source_agent was 'unknown' on 96% of 2939 rows and role NULL
    on 100%, because the MCP schema never exposed them at all. An explicit
    argument still wins — a subagent that knows it is something more specific
    than the session provider should say so.
    """
    if source_agent is None or source_agent == "unknown":
        source_agent = _derive_source_agent()
    if session_id is None:
        session_id = _derive_session_id()
    asserted_confidence, confidence = _clamp_and_cap(confidence,
                                                     ratified=ratified)
    if confidence != asserted_confidence:
        _logging.getLogger(__name__).info(
            "write_memory: confidence %.2f from %s capped to %.2f (not ratified)",
            asserted_confidence, source_agent, _AGENT_CONFIDENCE_CAP,
        )
    # ---- Guardrail: reject documents, enforce pointer memories ----
    #
    # PERSIST BEFORE INSTRUCTING. The rejection used to `return` the refusal
    # and drop `content` on the floor, so a rejected write lost its payload
    # whenever the agent did not re-issue it — on a crash, a compaction, a
    # confused retry, or because the very tool the message prescribes
    # (artifact_write) was itself refused by another gate. Measured 2026-09-09:
    # four subagents in one session hit exactly that composition and their
    # findings survived only because a human-facing session rewrote them by
    # hand.
    #
    # The dedup arm of this same function already learned this: it stores the
    # discarded text in `memory_write_discards` (migration 103, whose comment
    # reads "This table makes every future one recoverable"). This arm was
    # added later and reintroduced the defect that migration was written to
    # end. Same class, fixed at one instance and left live at the other.
    is_doc, reason = _looks_like_document(content)
    if is_doc:
        ref_file = _find_referenced_file(content)
        hint = f" The content references `{ref_file}` which exists on disk." if ref_file else ""
        artifact_id, rescue_error = _rescue_oversized_write(
            content, reason=reason, project=project, topic=topic,
            source_agent=source_agent, confidence=confidence,
        )
        head = (
            f"REJECTED: Memory looks like a {reason}.{hint}\n\n"
            f"Memory is a POINTER, not a container. Long-form content belongs in an artifact "
            f"(SYS-DOCS forbids creating standalone .md files for this).\n\n"
        )
        if artifact_id:
            return (
                head
                + f"YOUR CONTENT IS SAFE — it was stored for you as artifact {artifact_id} "
                f"(artifact_get to read it back). Nothing was lost.\n\n"
                f"One thing left: write the POINTER, so the artifact is findable.\n"
                f"  write_memory(topic={topic!r}, content='<the one-line claim, in your own "
                f"words>. Full detail in artifact {artifact_id}.'"
                + (f", project={project!r}" if project else "")
                + ")\n\n"
                f"Keep the pointer to a short paragraph — the claim plus the artifact id. "
                f"Do NOT call artifact_write again; the body is already stored."
            )
        return (
            head
            + f"!! CONTENT NOT SAVED — storing it for you failed: {rescue_error}\n"
            f"It exists only in this conversation. Save it before doing anything else:\n"
            f"1. artifact_write(kind='report'|'evidence'|'plan', title=..., body=<full content>, "
            f"project=..., summary=<one line>) — returns an artifact id.\n"
            f"2. write_memory() with a SHORT pointer citing that id."
        )

    from okuro.db import get_db

    # No topic-based confidence inflation. The previous auto-bump to 0.9 for
    # decision/architecture topics caused those rows to monopolize every
    # confidence-ordered surface (memory_index, read_memory no-query path,
    # decay protection) regardless of merit. Confidence is now exactly what
    # the caller asserts.

    db = get_db()

    # Generate embedding
    try:
        from okuro.embed.client import embed_one, to_bytes
        embedding = embed_one(content)
        vec_bytes = to_bytes(embedding)

        # Check for near-duplicate via vector similarity.
        #
        # An explicit `supersedes` is a correction, not an accident, so it
        # must never take this path. A correction restates the claim it
        # retracts and is therefore a near-duplicate BY CONSTRUCTION: the
        # reinforce branch would return early, drop the supersedes edge, and
        # bump the confidence of the very memory being retracted. Observed
        # 2026-07-15 while superseding a wrong 1.0 claim — the retraction
        # scored 0.95 against it and reinforced it instead. (Latent until
        # now: dedup demanded cosine > 0.995 under the L2 defect, so it never
        # fired at all.) The writer asked to replace a specific row; honour
        # that over a similarity guess.
        # Near-duplicate detection: cosine top-1 > 0.90 OR a near-verbatim
        # lexical restatement (Jaccard >= 0.55). A supersedes write is a
        # deliberate correction and must never be short-circuited into a
        # reinforce (it restates the claim it retracts — see below).
        existing_id, score, reason = (
            (None, 0.0, "") if supersedes
            else _find_write_dupe(db, content, vec_bytes)
        )
        if existing_id:
            # Near duplicate — reinforce existing
            row = db.fetchone(
                "SELECT confidence FROM agent_memory WHERE id = ?",
                (existing_id,),
            )
            if row:
                old_conf = row["confidence"]
                # Copy-discount: the +0.1 is corroboration only if this session
                # has not already asserted or reinforced this memory. bootstrap
                # injects memories unrequested, so read-then-restate is the
                # default flow — counting it would reward echo, not agreement.
                # An unknown session cannot be shown independent, so it does not
                # count either. Same claim, same source = no new evidence.
                this_session = _derive_session_id()
                independent = (
                    this_session is not None
                    and this_session not in _confirming_sessions(db, existing_id)
                )
                new_conf = min(1.0, old_conf + 0.1) if independent else old_conf
                if independent:
                    db.execute(
                        "UPDATE agent_memory SET confidence = ?, last_accessed = datetime('now') WHERE id = ?",
                        (new_conf, existing_id),
                    )
                    record_confidence_event(
                        db, existing_id, old_conf, new_conf, "reinforce",
                        note=f"{reason} {score:.3f} — independent session",
                    )
                else:
                    # Still touch last_accessed (it was read), but do NOT move
                    # confidence. Record the echo so the discount is auditable —
                    # "reinforced 11 times" must be provably distinct sessions.
                    db.execute(
                        "UPDATE agent_memory SET last_accessed = datetime('now') WHERE id = ?",
                        (existing_id,),
                    )
                    record_confidence_event(
                        db, existing_id, old_conf, old_conf, "reinforce",
                        note=f"{reason} {score:.3f} — echo (same/unknown session, no bump)",
                    )
                label = "similarity" if reason == "cosine" else "lexical overlap"
                # The new content is DISCARDED here — only the existing row's
                # confidence moves. Say so, loudly and in the return value.
                # A caller that wrote a refinement (same claim, corrected
                # detail) otherwise reads "Reinforced" as success while the
                # correction is dropped and the STALE row is strengthened.
                # Measured 2026-07-18: 7.67% of the corpus sits close enough
                # to a neighbour to trigger this. Latent before the cosine fix
                # (dedup demanded > 0.995 under the L2 defect), live since.
                _logging.getLogger(__name__).warning(
                    "write_memory: DISCARDED new content (%s %.2f vs existing "
                    "%s) — reinforced instead. Pass supersedes=<id> to replace "
                    "rather than reinforce. Discarded: %r",
                    label, score, existing_id, (content or "")[:160],
                )
                # Persist the discarded text. The warning above goes to a
                # Python logger, and MCP servers run as stdio subprocesses
                # whose stderr reaches no journal — so until this table the
                # loss was unrecoverable AND uncountable. Best-effort: an
                # audit-trail failure must never turn a reinforce into a
                # lost write, but it must not be silent either.
                try:
                    canonical = db.fetchone(
                        "SELECT content FROM agent_memory WHERE id = ?",
                        (existing_id,),
                    )
                    db.execute(
                        "INSERT INTO memory_write_discards ("
                        "  discarded_text, topic, project, role, source_agent,"
                        "  canonical_id, canonical_text, reason, score,"
                        "  old_confidence, new_confidence"
                        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            content, topic, project, role, source_agent,
                            existing_id,
                            canonical["content"] if canonical else None,
                            reason or "cosine", score, old_conf, new_conf,
                        ),
                    )
                    db.conn.commit()
                except Exception:
                    _logging.getLogger(__name__).warning(
                        "write_memory: could not record the discard for %s — "
                        "the text is now lost with no audit trail",
                        existing_id, exc_info=True,
                    )
                return (
                    f"DISCARDED new content — reinforced existing memory "
                    f"instead ({label}: {score:.2f}, confidence: "
                    f"{old_conf:.2f} -> {new_conf:.2f}).\n"
                    f"Existing id: {existing_id}\n"
                    f"Your text was NOT stored. If this was a correction or a "
                    f"refinement rather than a restatement, re-write it with "
                    f"supersedes=\"{existing_id}\" — that path never dedupes."
                )
    except Exception as exc:
        vec_bytes = None
        # A memory stored without a vector is INVISIBLE to semantic recall —
        # forever, silently. That is the exact silent-degradation class this
        # subsystem exists to prevent, so it must be observable. The write
        # still proceeds (text-only beats losing the memory), but it says so.
        _logging.getLogger(__name__).warning(
            "write_memory: embedding failed (%s) — storing %r (topic=%s, "
            "project=%s) WITHOUT a vector; unretrievable by semantic search "
            "until re-embedded (`python -m okuro.embed.repair`).",
            exc, (content or "")[:48], topic, project,
        )

    # Generate memory id outside the write scope so it's available on return.
    memory_id = str(uuid.uuid4())

    # One short write transaction: project upsert + supersedes + memory
    # + vector insert. Embedding was computed before this block so the
    # writer lock is only held for pure DB work.
    with db.write():
        if project:
            from okuro.sense.progress import _ensure_project
            _ensure_project(db, project)

        if supersedes:
            # Resolve an 8-char handle to a full id. Bootstrap pointers print
            # `→<8 chars>`, read_memory accepts that prefix, and supersedes is
            # a FK to the full agent_memory.id — so an agent following okuro's
            # OWN pointer convention hit a foreign-key failure when it tried to
            # retract what it had just read. Found 2026-07-19 by a cross-
            # provider test: claude reported "supersedes link failed (FK
            # constraint — old memory's UUID wasn't resolvable from its
            # truncated handle)" and then, correctly, carried on.
            #
            # Every surface that emits a handle must accept one back, or the
            # handle is decoration.
            resolved = db.fetchall(
                "SELECT id FROM agent_memory WHERE id = ? OR id LIKE ? || '%'",
                (supersedes, supersedes),
            )
            if len(resolved) == 1:
                supersedes = resolved[0]["id"]
            elif not resolved:
                return (
                    f"No memory with id '{supersedes}' — nothing to supersede. "
                    f"Nothing was written. Re-check the handle, or write "
                    f"without `supersedes` if this is a new fact."
                )
            else:
                ids = ", ".join(r["id"][:12] for r in resolved[:5])
                return (
                    f"Ambiguous supersedes prefix '{supersedes}' — matches "
                    f"{len(resolved)}: {ids}. Nothing was written; pass a "
                    f"longer prefix or the full id."
                )
            _old = db.fetchone(
                "SELECT confidence FROM agent_memory WHERE id = ?",
                (supersedes,),
            )
            db.execute(
                "UPDATE agent_memory SET confidence = 0.1 WHERE id = ?",
                (supersedes,),
            )
            record_confidence_event(
                db, supersedes, _old["confidence"] if _old else None, 0.1,
                "supersede", note=f"retired by {memory_id}",
            )

        db.execute(
            """INSERT INTO agent_memory (id, topic, content, project, confidence,
                                         source_agent, supersedes, role,
                                         session_id, code_version,
                                         asserted_confidence)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (memory_id, topic, content, project, confidence, source_agent,
             supersedes, role, session_id, _derive_code_version(),
             asserted_confidence),
        )
        record_confidence_event(db, memory_id, None, asserted_confidence,
                                "assert")
        if confidence != asserted_confidence:
            record_confidence_event(
                db, memory_id, asserted_confidence, confidence, "cap",
                note=f"agent cap {_AGENT_CONFIDENCE_CAP} (not ratified)",
            )

        if vec_bytes is not None:
            try:
                # `project` rides along as the partition value; without it
                # the row is indexed and unreachable from a scoped scan.
                vec_write(
                    db, "vec_memory", memory_id, vec_bytes,
                    partition=("project", project),
                )
            except Exception as exc:
                # Don't block memory storage — but a missing vector is silent
                # amnesia, so log it (same reason as the embed-failure branch).
                _logging.getLogger(__name__).warning(
                    "write_memory: vector insert failed for id=%s (%s) — "
                    "stored but unretrievable by semantic search.",
                    memory_id, exc,
                )

    # AAAK-style pointer index — best effort, never blocks memory storage.
    try:
        from okuro.sense.memory_index import write_pointer
        write_pointer(memory_id, topic, content, project=project,
                      confidence=confidence, supersedes=supersedes)
    except Exception:
        pass

    # KG claim validation (opt-in markup [[kg: s | p | o | as_of?]]).
    # Runs AFTER persistence so the memory always lands; the warnings are
    # informational so callers can correct the KG state in a follow-up.
    flagged = _validate_kg_claims(_scan_kg_claims(content), project=project)

    scope = f"project={project}" if project else "system-wide"
    role_str = f", role={role}" if role else ""
    base = f"Memory stored ({topic}, {scope}{role_str}, confidence={confidence}, id={memory_id})"
    # Report the cap. It used to be applied silently — the only notice was a
    # Python logger line, and MCP servers run as stdio subprocesses whose
    # stderr reaches no journal, so from the caller's side the number simply
    # changed. A writer who asserts 0.95 and is stored at 0.8 should be told
    # by the tool that did it, not left to notice.
    if confidence != asserted_confidence:
        base += (
            f"\n\nNOTE: you asserted {asserted_confidence}; stored at "
            f"{confidence}. Unratified agent writes are capped at "
            f"{_AGENT_CONFIDENCE_CAP} — 1.0 is reserved for human-confirmed "
            f"fact, so certainty cannot be self-declared. Your asserted value "
            f"is preserved in agent_memory.asserted_confidence."
        )
    if flagged:
        warnings = []
        for v in flagged:
            claim = v["claim"]
            warnings.append(
                f"  - [{v['verdict']}] {claim['subject']} -{claim['predicate']}-> {claim['object']}: "
                + "; ".join(v.get("notes") or [])
            )
        return (
            base + "\n\n⚠ KG-claim validation flagged:\n" + "\n".join(warnings)
            + "\nResolve via kg_invalidate / kg_add or correct the memory body."
        )
    return base


def _log_surfaces(memory_ids: list[str], context: str) -> None:
    """Log memory surfacings (GAP 3 / P8) + conditionally bump last_accessed.

    Called by read_memory after it resolves rows. Surface-log writes always
    happen (objective-utility substrate). The ``last_accessed`` bump only
    fires for INTENTIONAL retrievals (an agent or human invoked
    ``read_memory`` with a query, or a tool searched memory) — NOT for
    passive bootstrap surfacings. Reason: every bootstrap previously bumped
    last_accessed for whatever happened to land in the top-10, which froze
    the 90d-since-last_accessed decay rule for any memory the surfacer
    kept surfacing — even when no agent ever acted on it. Decay only fired
    for orphans the surfacer already ignored, defeating the whole hygiene
    pass. Bootstrap surfacing now leaves last_accessed alone so genuinely
    unused memories actually decay.
    """
    if not memory_ids:
        return
    from okuro.sense.surface import log_memory_surface

    for mid in memory_ids:
        log_memory_surface(mid, context)

    # Bootstrap surfaces are PASSIVE — agent didn't ask for them. Skip
    # the last_accessed bump for those contexts so decay can do its job.
    if context.startswith("bootstrap_"):
        return

    try:
        from okuro.db import get_db
        db = get_db()
        placeholders = ",".join("?" * len(memory_ids))
        db.execute(
            f"UPDATE agent_memory SET last_accessed = datetime('now') WHERE id IN ({placeholders})",
            tuple(memory_ids),
        )
    except Exception:  # pragma: no cover — defensive
        pass


# Age half-lives for the browse/fallback ranking, in days. A memory's ranking
# weight halves every τ½. None = does not decay.
#
# Why per-topic, and why these values: a gotcha names a defect, and defects get
# fixed — a year on, a gotcha about an already-patched bug is far likelier stale
# than live, so it should fade unless something re-earns it (a fresh reinforce
# resets created_at's effect via recency). A convention or learning drifts as
# the system moves, on a slower clock. A decision or an architecture note is the
# durable record of WHY the system is shaped as it is; it does not become false
# with age, so it does not decay. Measured symptom this fixes (memory 86ba75d8):
# gotchas about fixed bugs kept surfacing in bootstrap for sessions after the
# fix landed, because the browse sort was raw `confidence DESC, created_at DESC`
# and nothing down-weighted age. This honours ORCH-RETIRE-ON-FIX structurally,
# without an agent having to remember to supersede.
_TOPIC_HALFLIFE_DAYS = {
    "gotcha": 365.0,
    "convention": 180.0,
    "learning": 180.0,
    "decision": None,       # durable — the record of why
    "architecture": None,   # durable — how the system is shaped
}
# An unknown topic decays on the middle clock rather than not at all: an
# unclassified memory is more likely ad-hoc than foundational.
_DEFAULT_HALFLIFE_DAYS = 180.0

# Candidate pool the browse path re-ranks by age-decayed weight. Wider than
# `limit` so a fresher, slightly-lower-confidence memory can outrank a stale
# high-confidence one — the whole point — while staying a cheap bounded read.
_AGE_RERANK_POOL = 60


def _parse_sqlite_ts(ts: str) -> float | None:
    """Epoch seconds for a SQLite `datetime('now')` string (UTC), or None."""
    if not ts:
        return None
    try:
        return datetime.strptime(ts, "%Y-%m-%d %H:%M:%S").replace(
            tzinfo=timezone.utc).timestamp()
    except (ValueError, TypeError):
        return None


def _age_decayed_weight(confidence: float, topic: str, created_at: str,
                        now_ts: float | None = None) -> float:
    """`confidence · 2^(−age/τ½)` — the browse-rank weight (memory 86ba75d8).

    Decisions and architecture notes do not decay (τ½ = None). A row whose
    timestamp cannot be parsed keeps its raw confidence rather than being
    penalised for a formatting quirk — decay must never invent staleness.
    """
    conf = float(confidence or 0.0)
    half = _TOPIC_HALFLIFE_DAYS.get(topic, _DEFAULT_HALFLIFE_DAYS)
    if half is None:
        return conf
    created = _parse_sqlite_ts(created_at)
    if created is None:
        return conf
    now = now_ts if now_ts is not None else _time.time()
    age_days = max(0.0, (now - created) / 86400.0)
    return conf * (2.0 ** (-age_days / half))


def _rerank_by_age(rows: list[dict], limit: int,
                   now_ts: float | None = None) -> list[dict]:
    """Re-order a browse candidate pool by age-decayed weight, keep `limit`.

    Stable within equal weights via the incoming order (confidence DESC), so a
    non-decaying topic keeps its confidence order exactly.
    """
    ranked = sorted(
        rows,
        key=lambda r: _age_decayed_weight(
            r.get("confidence"), r.get("topic"), r.get("created_at"), now_ts),
        reverse=True,
    )
    return ranked[:limit]


def _read_memory_rows(query: str = None, topic: str = None, project: str = None,
                      limit: int = 10, min_confidence: float = 0.3,
                      role: str = None, system_only: bool = False,
                      _surface_context: str = "system_read") -> list[dict]:
    """Same filters as read_memory, but returns raw row dicts.

    Used by callers (notably build_memory) that need both the textual
    body AND the underlying memory IDs — read_memory's string return
    erases IDs by design (LLM-facing) but bootstrap callers need IDs
    to dedupe against the memory_index pointer surface.

    Each row dict mirrors the agent_memory columns plus a "_similarity"
    key when ``query`` was provided (cosine sim, 0..1, None otherwise).

    ``system_only=True`` restricts to ``project IS NULL`` — memories that
    are true regardless of project. This is NOT what ``project=None``
    means: that is "no project filter" (read_memory documents it as
    "include system-wide", and side_effect_evidence.py relies on it to
    search everything). The two were conflated, which is how bootstrap's
    "system-wide" slice came to emit project rows — see build_memory.
    """
    from okuro.db import get_db

    db = get_db()
    rows: list[dict] = []
    # See read_memory: a query that legitimately matches nothing must not be
    # answered from the confidence sort. Tracked separately from `rows`
    # because an EMPTY semantic result and a FAILED one need opposite
    # handling, and `if not rows` cannot tell them apart.
    semantic_ran = False

    if query:
        try:
            from okuro.embed.client import embed_query, to_bytes
            vec_bytes = to_bytes(embed_query(query, instruction=_QUERY_INSTRUCTION))
            matches = scoped_vec_search(
                db, "vec_memory", vec_bytes,
                limit=candidate_pool(limit, scoped=True),
                partition=("project", project) if project else None,
            )
            semantic_ran = True
            n_raw = len(matches)
            best = max((1 - m["distance"] for m in matches), default=None)
            matches = [m for m in matches if (1 - m["distance"]) > _SIM_FLOOR]
            if not matches and n_raw:
                _logging.getLogger(__name__).warning(
                    "_read_memory_rows: %d candidates but best (cosine %.3f) "
                    "is under _SIM_FLOOR=%.2f — returning nothing rather than "
                    "a confidence list (query=%r, ctx=%s)",
                    n_raw, best if best is not None else float("nan"),
                    _SIM_FLOOR, (query or "")[:80], _surface_context,
                )
            if matches:
                distances = {m["id"]: m["distance"] for m in matches}
                # Hybrid: fuse BM25 lexical arm with the vector arm, gated on the
                # non-empty vector arm (junk safety — see read_memory / the
                # _lexical_ids docstring). Fused list is vector ∪ lexical.
                _vector_ids = {m["id"] for m in matches}
                ids = _cap_lexical(
                    _rrf_fuse([m["id"] for m in matches],
                              _lexical_ids(db, query, limit * 4)),
                    _vector_ids,
                    limit,
                )[: limit * 4]
                rank = {i: p for p, i in enumerate(ids)}
                placeholders = ", ".join("?" * len(ids))
                sql = (
                    "SELECT id, topic, content, project, confidence, "
                    "source_agent, created_at FROM agent_memory "
                    f"WHERE id IN ({placeholders}) AND confidence >= ?"
                    + _EXCLUDE_SUPERSEDED
                )
                params: list = list(ids) + [min_confidence]
                if topic:
                    sql += " AND topic = ?"; params.append(topic)
                if project:
                    sql += " AND (project = ? OR project IS NULL)"; params.append(project)
                elif system_only:
                    sql += " AND project IS NULL"
                if role:
                    sql += " AND (role = ? OR role IS NULL)"; params.append(role)
                fetched = db.fetchall(sql, tuple(params))
                fetched = sorted(fetched,
                                 key=lambda r: rank.get(r["id"], len(ids)))[:limit]
                for r in fetched:
                    d = dict(r)
                    # Lex-only rows have no cosine (below/absent the vector
                    # floor); _similarity stays None for them, as for the
                    # recency path — callers already tolerate None.
                    d["_similarity"] = (
                        1 - distances[r["id"]] if r["id"] in distances else None
                    )
                    rows.append(d)
        except Exception as exc:
            semantic_ran = False
            _logging.getLogger(__name__).warning(
                "_read_memory_rows: semantic search failed (query=%r) — "
                "falling back to recency/confidence ordering. Cause: %s",
                (query or "")[:80], exc,
            )

    # A query was asked and answered "nothing relevant" — return that. This is
    # the path bootstrap's "Memories Relevant to This Task" is built from:
    # falling through would hand every session the top rows by ASSERTED
    # confidence under a RELEVANCE header. Measured 2026-07-15 with the
    # fall-through still in place, an unrelated task_hint produced three rows
    # with _similarity=[None, None, None] — GCP region trivia and Swiss data-
    # centre facts — presented to the agent as relevant to its task. That is
    # what taught agents the section was noise, and it is the same shape as
    # the four alarm memories briefing every session.
    #
    # A FAILED embed still falls through (degraded beats dead) and warned.
    if query and semantic_ran and not rows:
        return []

    if not rows:
        sql = (
            "SELECT id, topic, content, project, confidence, source_agent, "
            "created_at FROM agent_memory WHERE confidence >= ?"
            + _EXCLUDE_SUPERSEDED
        )
        params2: list = [min_confidence]
        if topic:
            sql += " AND topic = ?"; params2.append(topic)
        if project:
            sql += " AND (project = ? OR project IS NULL)"; params2.append(project)
        elif system_only:
            sql += " AND project IS NULL"
        if role:
            sql += " AND (role = ? OR role IS NULL)"; params2.append(role)
        # Browse path (no query). Pull a candidate pool by confidence, then
        # re-rank by age-decayed weight so a stale high-confidence gotcha does
        # not outrank a fresh one — the raw `created_at DESC` tiebreak could not
        # do that across confidence tiers. See _age_decayed_weight.
        sql += " ORDER BY confidence DESC, created_at DESC LIMIT ?"
        params2.append(max(limit, _AGE_RERANK_POOL))
        pool = []
        for r in db.fetchall(sql, tuple(params2)):
            d = dict(r)
            d["_similarity"] = None
            pool.append(d)
        rows = _rerank_by_age(pool, limit)

    if rows:
        _log_surfaces([r["id"] for r in rows], _surface_context)
    return rows


def read_memory(query: str = None, topic: str = None, project: str = None,
                limit: int = 10, min_confidence: float = 0.3, role: str = None,
                memory_id: str = None,
                _surface_context: str = "system_read") -> str:
    """Read memories with optional semantic search.

    Args:
        memory_id: Full id or the 8-char prefix printed as the `→id` handle on
            every bootstrap memory pointer. Takes precedence over `query`.
            Added 2026-07-19: the pointer renderer emitted those handles and NO
            api accepted them, while the packet instructed agents to "call
            read_memory to load the full body of any pointer". Passing a handle
            as `query` ran a semantic search on a hex string and returned an
            unrelated memory with no error — a wrong answer that looked like a
            right one, which is the failure class this whole subsystem is
            currently being repaired for.
        query: Search query (uses embeddings if provided).
        topic: Filter by topic.
        project: Filter by project (None = include system-wide).
        limit: Max results.
        min_confidence: Minimum confidence threshold.
        role: Filter by role scope.
        _surface_context: Internal — how this call is reaching read_memory,
            for the memory-surface log. Only the agent-facing MCP tool passes
            'read_memory' (a session-ful context whose surfacings feed
            memory_utility); bootstrap passes 'bootstrap_system' /
            'bootstrap_relevant'. The default 'system_read' is for internal
            RAG callers (web, orchestrator, grounding, reviewer, intake) that
            run outside any bootstrapped agent session — their session_id is
            unavoidably NULL, so they are kept OUT of the session-ful buckets
            to stop them starving/polluting the utility-loop signal (G6). See
            surface._SESSIONFUL_CONTEXTS.
    """
    from okuro.db import get_db

    db = get_db()

    # Direct fetch by handle. Runs BEFORE any semantic path so a pointer can
    # always be opened, and returns an explicit miss rather than falling
    # through to a search that would answer a lookup with something adjacent.
    if memory_id:
        mid = memory_id.strip()
        rows = db.fetchall(
            "SELECT id, topic, content, project, confidence, source_agent, "
            "created_at FROM agent_memory WHERE id = ? OR id LIKE ? || '%'",
            (mid, mid),
        )
        if not rows:
            return (
                f"No memory with id '{mid}'. The `→id` handles on bootstrap "
                f"pointers are 8-char prefixes of the full id."
            )
        if len(rows) > 1:
            ids = ", ".join(r["id"][:12] for r in rows[:5])
            return f"Ambiguous id prefix '{mid}' — matches {len(rows)}: {ids}"
        r = rows[0]
        _log_surfaces([r["id"]], _surface_context)
        return _render_row(r, "exact id")

    # Did semantic search actually get to run and give a verdict? A query
    # that legitimately matches nothing must NOT be answered with the
    # recency list below — see the guard after this block.
    semantic_ran = False

    if query:
        try:
            from okuro.embed.client import embed_query, to_bytes
            # Widen the pre-filter pool so the sim floor + topic/project
            # post-filters have real material to work with; narrowing to
            # `limit` pre-filter over-prunes (mirrors thoughts.surface_relevant).
            vec_bytes = to_bytes(embed_query(query, instruction=_QUERY_INSTRUCTION))
            matches = scoped_vec_search(
                db, "vec_memory", vec_bytes,
                limit=candidate_pool(limit, scoped=True),
                partition=("project", project) if project else None,
            )
            semantic_ran = True
            # Apply the similarity floor — drop noise before it ever reaches
            # the confidence/topic filters.
            n_raw = len(matches)
            best = max((1 - m["distance"] for m in matches), default=None)
            matches = [m for m in matches if (1 - m["distance"]) > _SIM_FLOOR]
            if not matches and n_raw:
                # The floor emptied a non-empty candidate set. This is the
                # exact shape of the 2026-05→07 outage: not an error, so the
                # except branch never fired, and the caller silently got a
                # recency list instead of an answer. Say so.
                _logging.getLogger(__name__).warning(
                    "read_memory: semantic search found %d candidates but the "
                    "best (cosine %.3f) is under _SIM_FLOOR=%.2f — falling "
                    "back to recency/confidence ordering (query=%r, ctx=%s)",
                    n_raw, best if best is not None else float("nan"),
                    _SIM_FLOOR, (query or "")[:80], _surface_context,
                )
            if matches:
                distances = {m["id"]: m["distance"] for m in matches}
                # Hybrid: fuse the BM25 lexical arm with the vector arm. Gated on
                # the vector arm being non-empty (we are inside `if matches:`),
                # so off-domain junk — empty vector arm — never reaches the
                # lexical arm and stays rejected by the same floor. Fused list is
                # vector ∪ lexical, so lex-only rows get hydrated too.
                _vector_ids = {m["id"] for m in matches}
                ids = _cap_lexical(
                    _rrf_fuse([m["id"] for m in matches],
                              _lexical_ids(db, query, limit * 4)),
                    _vector_ids,
                    limit,
                )[: limit * 4]
                rank = {i: p for p, i in enumerate(ids)}
                placeholders = ", ".join("?" * len(ids))

                sql = f"""SELECT id, topic, content, project, confidence, source_agent, created_at
                          FROM agent_memory
                          WHERE id IN ({placeholders}) AND confidence >= ?""" + _EXCLUDE_SUPERSEDED
                params = list(ids) + [min_confidence]

                if topic:
                    sql += " AND topic = ?"
                    params.append(topic)
                if project:
                    sql += " AND (project = ? OR project IS NULL)"
                    params.append(project)
                if role:
                    sql += " AND (role = ? OR role IS NULL)"
                    params.append(role)

                rows = db.fetchall(sql, tuple(params))

                if rows:
                    # Re-impose the FUSED ranking (SQL IN preserves no order)
                    # and cap at the caller's limit. Widened pool was for
                    # filtering headroom, not output bloat.
                    rows = sorted(
                        rows,
                        key=lambda r: rank.get(r["id"], len(ids)),
                    )[:limit]
                    _log_surfaces([r["id"] for r in rows], _surface_context)
                    lines = []
                    for r in rows:
                        scope = f"[{r['project']}]" if r.get("project") else "[system]"
                        # Lex-only rows (recovered by BM25, below/absent the
                        # vector floor) have no cosine to quote — label them.
                        if r["id"] in distances:
                            tag = f"relevance: {1 - distances[r['id']]:.2f}"
                        else:
                            tag = "keyword match"
                        lines.append(_render_row(r, tag))
                    return "\n".join(lines)
                else:
                    # Cleared the floor, then the confidence/topic/project/
                    # role filters removed everything. Also silent, also ends
                    # in a recency list that ignores the query.
                    _logging.getLogger(__name__).warning(
                        "read_memory: %d matches cleared the floor but the "
                        "confidence/topic/project filters left none — falling "
                        "back to recency/confidence ordering (query=%r, ctx=%s)",
                        len(matches), (query or "")[:80], _surface_context,
                    )
        except Exception as exc:
            # Silent semantic→recency fallback was the third oddity in the
            # 2026-05-09 audit: when embeddings throw, the query is dropped
            # and the result becomes a pure recency/confidence sort, but the
            # caller has no way to tell. Log a warning so the failure is
            # observable. Behavior unchanged — we still fall through.
            _logging.getLogger(__name__).warning(
                "read_memory: semantic search failed (query=%r, ctx=%s) — "
                "falling back to recency/confidence ordering. Cause: %s",
                (query or "")[:80], _surface_context, exc,
            )

    # Semantic search ran, reached a verdict, and the verdict was "nothing
    # relevant". Say that. Do NOT fall through to the recency sort.
    #
    # The fall-through is the mechanism behind the 2026-05→07 incident. With
    # the metric broken, EVERY query landed here and got the same ten
    # confidence-ordered rows — so an agent asking about sourdough, or about
    # cortex, was handed identical unrelated content dressed as recall. That
    # is worse than an empty answer: it is confidently wrong, and it is what
    # let four alarm memories brief every session in the system. The recency
    # list is a fine BROWSE (no query given); it is never an ANSWER.
    #
    # An embedding failure still falls through — degraded is better than dead
    # when the service is down — but it warned above, so it is visible.
    if query and semantic_ran:
        return (
            "No memories found matching this query "
            f"(nothing above the {_SIM_FLOOR:.2f} relevance floor). "
            "This means okuro has no memory on this topic — not that the "
            "topic is unimportant."
        )

    # Direct query (no embedding search)
    sql = """SELECT id, topic, content, project, confidence, source_agent, created_at
             FROM agent_memory WHERE confidence >= ?""" + _EXCLUDE_SUPERSEDED
    params: list = [min_confidence]

    if topic:
        sql += " AND topic = ?"
        params.append(topic)
    if project:
        sql += " AND (project = ? OR project IS NULL)"
        params.append(project)
    if role:
        sql += " AND (role = ? OR role IS NULL)"
        params.append(role)

    # Browse path — same age-decay re-rank as _read_memory_rows, so the two
    # surfaces agree on what "most relevant with no query" means.
    sql += " ORDER BY confidence DESC, created_at DESC LIMIT ?"
    params.append(max(limit, _AGE_RERANK_POOL))

    rows = _rerank_by_age([dict(r) for r in db.fetchall(sql, tuple(params))],
                          limit)

    if not rows:
        return "No memories found."

    _log_surfaces([r["id"] for r in rows], _surface_context)
    return "\n".join(_render_row(r) for r in rows)


def memory_stats(group_by: str = "week", since: str = None, until: str = None,
                 project: str = None, topic: str = None) -> dict:
    """Creation counts per period, split active vs superseded. READ-ONLY.

    WHY THIS EXISTS. Measured 2026-07-29: an audit of the 2026-05-10..07-17
    memory blind window could answer none of six requested measurements —
    weekly creation counts, a census by topic and project, how many in-window
    rows were later retracted — because no typed tool reported a per-date
    count and the store hook correctly refuses raw SQL against the store. The
    census reported "six of the eight requested measurements are unreachable".

    That is a gap in the SANCTIONED path, and a sanctioned path with a hole in
    it does not produce a missing number; it produces an agent that either
    reports the hole or reaches for sqlite3 and gets denied. Neither is a
    measurement.

    ``group_by``: "day" | "week" | "month". ``since`` inclusive, ``until``
    exclusive, both ISO strings. Superseded uses the same structural
    definition every read path uses — another row points at this one via
    ``supersedes`` — not a confidence threshold, because the confidence drop
    is a consequence of supersession rather than its definition.
    """
    from okuro.db import get_db

    fmt = {"day": "%Y-%m-%d", "week": "%Y-W%W", "month": "%Y-%m"}.get(group_by)
    if fmt is None:
        raise ValueError(f"group_by must be day|week|month, got {group_by!r}")

    superseded_expr = (
        "CASE WHEN id IN (SELECT supersedes FROM agent_memory "
        "WHERE supersedes IS NOT NULL) THEN 1 ELSE 0 END"
    )
    sql = (
        f"SELECT strftime('{fmt}', created_at) AS period, "
        "COUNT(*) AS total, "
        f"SUM({superseded_expr}) AS superseded, "
        f"SUM(1 - {superseded_expr}) AS active "
        "FROM agent_memory WHERE 1 = 1"
    )
    params: list = []
    if since:
        sql += " AND created_at >= ?"
        params.append(since)
    if until:
        sql += " AND created_at < ?"
        params.append(until)
    if project:
        sql += " AND project = ?"
        params.append(project)
    if topic:
        sql += " AND topic = ?"
        params.append(topic)
    sql += " GROUP BY period ORDER BY period ASC"

    rows = [dict(r) for r in get_db().fetchall(sql, tuple(params))]
    return {
        "group_by": group_by,
        "since": since,
        "until": until,
        "project": project,
        "topic": topic,
        "periods": rows,
        "total": sum(r["total"] for r in rows),
        "active": sum(r["active"] for r in rows),
        "superseded": sum(r["superseded"] for r in rows),
    }


def memory_census(since: str = None, until: str = None,
                  group_by: str = "topic") -> dict:
    """Row counts over a date window, grouped by topic or project. READ-ONLY.

    The second half of the census gap: "how many" per period is
    ``memory_stats``; "of what kind, and whose" is this. Same window
    semantics — ``since`` inclusive, ``until`` exclusive.
    """
    from okuro.db import get_db

    col = {"topic": "topic", "project": "project",
           "source_agent": "source_agent"}.get(group_by)
    if col is None:
        raise ValueError(
            f"group_by must be topic|project|source_agent, got {group_by!r}"
        )

    superseded_expr = (
        "CASE WHEN id IN (SELECT supersedes FROM agent_memory "
        "WHERE supersedes IS NOT NULL) THEN 1 ELSE 0 END"
    )
    sql = (
        f"SELECT COALESCE({col}, '(none)') AS bucket, COUNT(*) AS total, "
        f"SUM({superseded_expr}) AS superseded, "
        "ROUND(AVG(confidence), 3) AS avg_confidence, "
        "SUM(CASE WHEN confidence >= 1.0 THEN 1 ELSE 0 END) AS at_confidence_1 "
        "FROM agent_memory WHERE 1 = 1"
    )
    params: list = []
    if since:
        sql += " AND created_at >= ?"
        params.append(since)
    if until:
        sql += " AND created_at < ?"
        params.append(until)
    sql += " GROUP BY bucket ORDER BY total DESC"

    rows = [dict(r) for r in get_db().fetchall(sql, tuple(params))]
    return {
        "group_by": group_by,
        "since": since,
        "until": until,
        "buckets": rows,
        "total": sum(r["total"] for r in rows),
        "superseded": sum(r["superseded"] for r in rows),
        "at_confidence_1": sum(r["at_confidence_1"] for r in rows),
    }
