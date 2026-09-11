### SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Tier-1 — one cheap facet per session, embedded and clustered into a corpus map.
# index: imports | FACET_KEYS | caps | prompt | parse_facet | extract_facet | facet_summary_text | store_facet | run_tier1 | cluster_corpus
# AGENT_HEADER_END -->
"""Tier-1: what the corpus is made of.

Tier-0 counted. Tier-2 judges, and is expensive and dormant. This tier sits
between them and answers the question that decides how tier-2 is ever
affordable: **what KINDS of session are in here, and how many of each.**

One small-model call per session over the bounded transcript
(:func:`~.facets.render_transcript`), producing a fixed set of facets. The
facet summary is embedded into ``vec_distill_sessions`` and the corpus is
k-means clustered, so tier-2 can sample per cluster instead of per session.

Three things are deliberately NOT model calls
---------------------------------------------
Tools used, token mass, provider, session class, marker counts — all of those
are already on the tier-0 row, derived from the store. Asking a model to
restate them would cost money to produce a worse answer. The model is asked
only for what cannot be counted: what the session was TRYING to do, whether it
got there, and how it failed if it did.

Cost measurement is honest about what it is
-------------------------------------------
Neither bridge path returns token usage — CLI providers parse text
(``bridge/executor.py``) and the local-http path drops the OpenAI ``usage``
block (``bridge/local.py:124``). So this module records EXACT character counts
for prompt and output, and the pilot converts them to a token ESTIMATE at a
declared ratio. Characters are measured; tokens are inferred; the pilot prints
them under those two words and never mixes them.
"""

from __future__ import annotations

import json
import logging
import os
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass, field

from .facets import EXTRACTION_VERSION, render_transcript

log = logging.getLogger(__name__)

VEC_TABLE = "vec_distill_sessions"

# The facet document's shape. Fixed, because a clustering over free-form keys
# clusters the schema drift rather than the sessions.
FACET_KEYS = ("task_type", "outcome", "summary", "topics", "failure_mode")

OUTCOMES = ("completed", "partial", "abandoned", "unclear")

# Per-field caps. distill_facets.facet is CHECKed at 4096 chars; these keep the
# document an order of magnitude under it, so a verbose model produces a
# truncated facet rather than a failed INSERT halfway through a batch.
_CAP_TASK_TYPE = 60
_CAP_SUMMARY = 600
_CAP_TOPIC = 40
_MAX_TOPICS = 8
_CAP_FAILURE = 200
_CAP_FACET_JSON = 4096

# What reaches the embedder. Same reasoning as interaction.turns.EMBED_CHAR_CAP:
# the discriminating signal is at the top and a longer input pulls every vector
# toward a boilerplate centroid.
_EMBED_CHAR_CAP = 2000

# A tool-bridge call: no MCP servers, no CLAUDE.md discovery, no per-machine
# system prompt (~3x faster on claude — invoke.py:52-56). Paired with a minimal
# system prompt, as that docstring instructs.
_SYSTEM_PROMPT = (
    "You classify agent session transcripts. Reply with one JSON object and "
    "nothing else. No preamble, no code fence, no commentary."
)

_PROMPT = """\
Classify this agent session transcript. It contains only the user turns and \
the assistant's text replies — tool calls, tool results and internal reasoning \
have been removed, so judge from what was said, not from what is missing.

Reply with exactly this JSON object:

{{
  "task_type": "<2-4 word kind of work, lowercase, e.g. 'bug fix', 'research \
report', 'schema migration', 'planning conversation'>",
  "outcome": "<one of: completed | partial | abandoned | unclear>",
  "summary": "<one or two sentences: what was asked and what happened>",
  "topics": ["<up to 5 short topic tags>"],
  "failure_mode": "<short phrase if something went wrong, otherwise null>"
}}

TRANSCRIPT ({turns} turns, {kind} session):
{transcript}
"""


@dataclass
class FacetResult:
    session_id: str
    facet: dict | None = None
    model: str | None = None
    prompt_chars: int = 0
    output_chars: int = 0
    duration: float = 0.0
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.facet is not None


@dataclass
class Tier1Run:
    considered: int = 0
    extracted: int = 0
    embedded: int = 0
    prompt_chars: int = 0
    output_chars: int = 0
    duration: float = 0.0
    refused_by_reason: dict[str, int] = field(default_factory=dict)

    def refuse(self, reason: str) -> None:
        self.refused_by_reason[reason] = self.refused_by_reason.get(reason, 0) + 1


def build_prompt(read) -> str:
    """The exact text sent for one session. Pure — the pilot prints it."""
    return _PROMPT.format(
        turns=len(read.turns),
        kind="subagent" if str(read.session_id).startswith("agent-") else "main",
        transcript=render_transcript(read),
    )


def parse_facet(raw: str) -> dict | None:
    """Coerce a model reply into the declared facet shape, or None.

    Returns None rather than a partial dict: a facet missing ``task_type`` or
    ``outcome`` cannot be clustered or sampled, so half a facet is worth less
    than a counted refusal that says the extraction did not work.

    ``json_repair`` is already a dependency (added for prism outline parsing)
    and handles the malformation classes ad-hoc regex cannot — a missing comma
    mid-object, a mismatched quote.
    """
    if not raw or not raw.strip():
        return None
    text = raw.strip()
    # Models fence JSON despite being asked not to.
    if text.startswith("```"):
        text = text.split("\n", 1)[-1]
        if text.rstrip().endswith("```"):
            text = text.rstrip()[:-3]
    try:
        parsed = json.loads(text)
    except (TypeError, ValueError):
        try:
            from json_repair import repair_json

            parsed = json.loads(repair_json(text))
        except Exception:  # noqa: BLE001 — unparseable is a counted refusal
            return None
    if not isinstance(parsed, dict):
        return None

    task_type = str(parsed.get("task_type") or "").strip()[:_CAP_TASK_TYPE]
    if not task_type:
        return None
    outcome = str(parsed.get("outcome") or "").strip().lower()
    if outcome not in OUTCOMES:
        # An out-of-vocabulary outcome is not a reason to drop the facet — the
        # rest of it still clusters — but it must not enter the vocabulary,
        # because downstream sampling groups on it.
        outcome = "unclear"

    topics_raw = parsed.get("topics")
    topics = []
    if isinstance(topics_raw, list):
        for t in topics_raw[:_MAX_TOPICS]:
            tag = str(t).strip()[:_CAP_TOPIC]
            if tag:
                topics.append(tag)

    failure = parsed.get("failure_mode")
    failure_mode = (
        str(failure).strip()[:_CAP_FAILURE]
        if failure not in (None, "", "null", "none")
        else None
    )

    return {
        "task_type": task_type,
        "outcome": outcome,
        "summary": str(parsed.get("summary") or "").strip()[:_CAP_SUMMARY],
        "topics": topics,
        "failure_mode": failure_mode,
    }


def extract_facet(read, capability: str = "fast") -> FacetResult:
    """One model call for one session.

    ``capability='fast'`` routes to the cheapest tier the bridge has
    configured (haiku on claude). ``tool=True`` strips agent context and
    extended thinking — this is a mechanical classification, not reasoning.
    """
    from okuro.bridge.invoke import invoke

    result = FacetResult(session_id=read.session_id)

    # Checked BEFORE the prompt is built, so prompt_chars counts only prompts
    # that were actually sent. Counting an unsent prompt would inflate both the
    # pilot's cost measurement and the daemon's budget consumption with work
    # nobody paid for.
    if not read.turns:
        result.error = "empty_transcript"
        return result

    prompt = build_prompt(read)
    result.prompt_chars = len(prompt)

    reply = invoke(
        prompt=prompt,
        capability=capability,
        system_prompt=_SYSTEM_PROMPT,
        tool=True,
        isolated=True,
    )
    result.model = reply.get("model")
    result.duration = float(reply.get("duration") or 0.0)
    output = reply.get("output") or ""
    result.output_chars = len(output)

    if not reply.get("success"):
        result.error = f"invoke_failed: {str(reply.get('error'))[:200]}"
        return result

    facet = parse_facet(output)
    if facet is None:
        result.error = "unparseable_facet"
        return result
    result.facet = facet
    return result


def facet_summary_text(facet: dict, tools=()) -> str:
    """What gets embedded: the facet, plus the tools the session actually used.

    The tool list is included deliberately. It is the strongest cheap signal of
    what a session DID, it costs nothing (tier-0 already has it), and without
    it two sessions described as "bug fix / completed" cluster together whether
    one edited a migration and the other drove a browser.

    Takes a plain tool list rather than a ``SessionRead`` so
    ``embed/repair.py`` can rebuild the IDENTICAL text from the stored row.
    A backfill that embeds different text than the live writer is the drift
    that makes a rebuilt index disagree with the one it replaced.
    """
    parts = [
        facet.get("task_type") or "",
        facet.get("outcome") or "",
        facet.get("summary") or "",
        " ".join(facet.get("topics") or []),
        facet.get("failure_mode") or "",
        " ".join(list(tools)[:20]),
    ]
    return "\n".join(p for p in parts if p)[:_EMBED_CHAR_CAP]


def row_embed_text(row: dict) -> str | None:
    """Rebuild the embed input from a stored ``distill_facets`` row.

    The backfill path. Delegates to :func:`facet_summary_text` so there is one
    definition of what a session's vector is made of.
    """
    try:
        facet = json.loads(row.get("facet") or "")
    except (TypeError, ValueError):
        return None
    if not isinstance(facet, dict):
        return None
    try:
        tools = json.loads(row.get("distinct_tools") or "[]")
    except (TypeError, ValueError):
        tools = []
    text = facet_summary_text(facet, tools if isinstance(tools, list) else [])
    return text or None


def store_facet(db, read, result: FacetResult) -> bool:
    """Persist the facet onto the existing tier-0 row and advance its stage.

    UPDATE, never INSERT: tier-0 owns row creation and every deterministic
    column on it. A tier-1 INSERT would have to restate all of them and would
    drift from tier-0 the first time either changed.
    """
    if not result.ok:
        return False
    blob = json.dumps(result.facet, ensure_ascii=False)
    if len(blob) > _CAP_FACET_JSON:
        # Should be unreachable given the per-field caps; if a future field
        # makes it reachable, drop the longest field rather than let the CHECK
        # abort the batch.
        trimmed = dict(result.facet)
        trimmed["summary"] = (trimmed.get("summary") or "")[:200]
        blob = json.dumps(trimmed, ensure_ascii=False)[:_CAP_FACET_JSON]
    with db.write():
        db.execute(
            """
            UPDATE distill_facets
               SET stage              = 'tier1',
                   facet              = ?,
                   facet_model        = ?,
                   facet_chars        = ?,
                   extraction_version = ?,
                   updated_at         = datetime('now')
             WHERE session_id = ?
            """,
            (
                blob,
                result.model,
                # Characters. Measured, unlike tokens — see the module
                # docstring and the column's own comment in migration 137.
                result.prompt_chars + result.output_chars,
                EXTRACTION_VERSION,
                read.session_id,
            ),
        )
    return True


def embed_facet(db, read, facet: dict) -> bool:
    """Embed the facet summary into ``vec_distill_sessions``.

    Writes through :func:`okuro.sense.retrieval.vec_write`, the sanctioned
    chokepoint, rather than issuing the INSERT here. ``vec_distill_sessions``
    is UNPARTITIONED — ~10k session-level vectors fit one chunk-block, and a
    partition key on a session id would preallocate 4 MiB PER SESSION (the
    measurement in ``embed/repair.py``: vec_artifacts held 2320 NULL chunks
    for 3577 vectors, 9434 MB where 14 MB was needed).
    """
    from okuro.embed.client import embed_one, to_bytes
    from okuro.sense.retrieval import vec_write

    text = facet_summary_text(facet, read.distinct_tools)
    if not text.strip():
        return False
    try:
        blob = to_bytes(embed_one(text))
    except Exception as exc:  # noqa: BLE001 — embed service down is a counted
        # refusal, never a lost facet: the row is already stored.
        log.warning("embed failed for %s: %s", read.session_id, exc)
        return False
    with db.write():
        db.execute(f"DELETE FROM {VEC_TABLE} WHERE id = ?", (read.session_id,))
        vec_write(db, VEC_TABLE, read.session_id, blob)
    return True


# Refusal reasons that are NOT an attempt at this session. The distinction is
# load-bearing: `budget_exhausted` means the run stopped before reaching this
# session, so nothing was sent and nothing came back. Counting it would park
# the tail of the backlog for the crime of being at the back of the queue,
# which is precisely what the re-queue clause exists to prevent.
NON_ATTEMPT_REFUSALS = frozenset({"budget_exhausted"})


def record_facet_failure(db, session_id: str, error: str) -> int:
    """Count one failed extraction attempt against a session. Returns the total.

    Parking is not decided here — the row records how many times it has failed
    and why, and ``pipeline.select_sessions`` compares that against
    ``MAX_FACET_ATTEMPTS``. Keeping the ceiling out of this function is what
    lets it be raised later without a migration or a backfill: every row below
    the new ceiling becomes eligible again on the next pass.
    """
    with db.write():
        db.execute(
            """
            UPDATE distill_facets
               SET facet_attempts   = facet_attempts + 1,
                   facet_last_error = ?,
                   updated_at       = datetime('now')
             WHERE session_id = ?
            """,
            (str(error)[:200], session_id),
        )
    row = db.fetchone(
        "SELECT facet_attempts FROM distill_facets WHERE session_id = ?",
        (session_id,),
    )
    return int(row["facet_attempts"]) if row else 0


# Bounded worker pool over the facet LLM calls. 4 because the bottleneck is a
# provider round-trip (~5s measured on haiku at ~13.5k prompt chars), not local
# CPU, and because the pool multiplies the SPEND rate as well as the throughput
# — the daily budget still bounds the total, but a wide pool reaches it faster
# and in burstier shape. Raise it in config once a real run has shown the
# provider tolerates it.
_CONFIG_SECTION = "distill"
_CONFIG_KEY_CONCURRENCY = "facet_concurrency"
DEFAULT_FACET_CONCURRENCY = 4
MAX_FACET_CONCURRENCY = 16


def facet_concurrency() -> int:
    """``distill.facet_concurrency`` from config, clamped to a sane band.

    Clamped rather than trusted: 0 or a negative would deadlock the pool, and
    an accidental 500 would open 500 provider connections from a nightly job.
    """
    from okuro.db.engine import _load_config

    raw = (_load_config().get(_CONFIG_SECTION) or {}).get(_CONFIG_KEY_CONCURRENCY)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_FACET_CONCURRENCY
    return max(1, min(value, MAX_FACET_CONCURRENCY))


def _extract_one(read, capability: str):
    """The whole of what a worker thread does. **It holds no DB handle.**

    That is the concurrency design in one line: SQLite has one writer, okuro's
    connections are thread-local, and the two schema sentinels
    (``okuro_distill_gate_armed`` / ``okuro_trace_upsert_armed``) are
    thread-local flags read by SQL functions registered per connection. A
    worker that opened its own connection would get its own disarmed sentinels
    and its own writer contention, and a worker that shared the main thread's
    connection would race on a live cursor — the failure ``db/sentinels.py``
    documents, where ``create_function`` raises because a statement is still
    active.

    So workers do exactly one thing: call the bridge and hand back a value.
    Every read and every write stays on the calling thread.

    Returns ``(read, FacetResult)``. Never raises: a worker that dies takes its
    own session down and nothing else, which is the property
    ``test_one_workers_crash_does_not_lose_the_others`` pins.
    """
    try:
        return read, extract_facet(read, capability=capability)
    except Exception as exc:  # noqa: BLE001 — a crashed worker is one failed
        # session, not a failed run. The reason is counted like any other.
        return read, FacetResult(
            session_id=read.session_id,
            error=f"worker_crashed: {exc!r}"[:200],
        )


def run_tier1(reads, db=None, capability: str = "fast",
              char_budget: int | None = None,
              concurrency: int | None = None) -> Tier1Run:
    """Extract, store and embed facets for sessions tier-0 already read.

    ``reads`` is an iterable of :class:`~.facets.SessionRead` — tier-0 produced
    them, and passing them through avoids a second walk of ``agent_events`` for
    every session in the corpus.

    ``char_budget`` bounds prompt+output characters for the whole run and is
    how the daemon's token budget is actually enforced. Characters because that
    is what can be measured (see the module docstring); the caller converts.

    CONCURRENCY, and what stays on this thread
    ------------------------------------------
    The facet calls run in a bounded pool because they are provider
    round-trips — ~5s each, almost all of it waiting. Everything else stays
    exactly where it was:

    * **workers get no DB handle** (see :func:`_extract_one`). They call the
      bridge and return a value.
    * **every write happens here**, on the calling thread, through the same
      single-writer path as before — facet rows, attempt counters, embeddings.
    * **write ORDER is not preserved** and does not matter: each row is keyed
      by its own session_id and no write reads another's result.

    ONE loop serves every concurrency, including 1. A separate serial branch
    would be a second implementation of the same semantics, and this pipeline
    has already paid twice for exactly that kind of duplicate (the pilot's
    private copies of the `unusable` rule and of session selection). A pool of
    one is a serial run.

    BUDGET OVERSHOOT IS BOUNDED AND DELIBERATE. The check runs at SUBMIT time
    against the characters that have LANDED, so up to ``concurrency - 1``
    in-flight calls can be admitted just before the budget closes. Bounding it
    exactly would mean either serialising submission behind completion — which
    is the serial run — or refusing to submit until every in-flight call
    returns. The overshoot is at most one round of calls; the alternative is no
    concurrency at all.

    ``duration`` sums MODEL time across workers, so with a pool it exceeds wall
    time. That is not a bug to correct: the ratio between them is the clearest
    evidence the pool is actually running in parallel, and the pilot prints
    both.
    """
    if db is None:
        from okuro.db import get_db

        db = get_db()
    if concurrency is None:
        concurrency = facet_concurrency()
    concurrency = max(1, min(int(concurrency), MAX_FACET_CONCURRENCY))

    run = Tier1Run()

    def _apply(read, result) -> None:
        """Land one worker's result. Runs on the calling thread, always."""
        run.prompt_chars += result.prompt_chars
        run.output_chars += result.output_chars
        run.duration += result.duration
        if not result.ok:
            reason = result.error or "unknown"
            run.refuse(reason)
            # A real attempt was made and it failed. Count it, so a transcript
            # that fails the same way every night eventually stops costing a
            # model call every night. Exactly once per attempt: this runs on
            # one thread, for one result, from one worker.
            record_facet_failure(db, read.session_id, reason)
            return
        store_facet(db, read, result)
        run.extracted += 1
        if embed_facet(db, read, result.facet):
            run.embedded += 1
        else:
            run.refuse("embed_failed")

    # `invoke(tool=True)` does a read-modify-restore on the process-global
    # MAX_THINKING_TOKENS (bridge/invoke.py), which is not thread-safe: with N
    # workers the restore can leave the variable holding another thread's saved
    # value. Every worker here sets the SAME value, so the in-flight effect is
    # benign — but the leak would outlive the run and reach the next non-tool
    # invoke in this process, which for the daemon is another task entirely.
    # Snapshotting around the whole pool converts "possibly leaked" into
    # "restored exactly", without reaching into a bridge that has other callers.
    prev_think = os.environ.get("MAX_THINKING_TOKENS")

    pending: set = set()
    stream = iter(reads)
    exhausted = False
    try:
        with ThreadPoolExecutor(
            max_workers=concurrency, thread_name_prefix="distill-facet"
        ) as pool:
            while True:
                while not exhausted and len(pending) < concurrency:
                    read = next(stream, None)
                    if read is None:
                        exhausted = True
                        break
                    run.considered += 1
                    if (char_budget is not None
                            and run.prompt_chars + run.output_chars >= char_budget):
                        # NOT an attempt — see NON_ATTEMPT_REFUSALS. Nothing was
                        # sent, so this session must come back next pass rather
                        # than burn a life. Deliberately does NOT break: the
                        # remaining reads still need counting and refusing.
                        run.refuse("budget_exhausted")
                        continue
                    pending.add(pool.submit(_extract_one, read, capability))

                if not pending:
                    break

                done, pending = wait(pending, return_when=FIRST_COMPLETED)
                for future in done:
                    try:
                        read, result = future.result()
                    except Exception as exc:  # noqa: BLE001 — _extract_one
                        # already guards; this covers a failure of the future
                        # machinery itself so one bad future cannot discard the
                        # results sitting beside it in `done`.
                        log.warning("distill facet future failed: %s", exc)
                        run.refuse("future_failed")
                        continue
                    _apply(read, result)
    finally:
        if prev_think is None:
            os.environ.pop("MAX_THINKING_TOKENS", None)
        else:
            os.environ["MAX_THINKING_TOKENS"] = prev_think

    return run


def cluster_corpus(db=None, k: int | None = None, seed: int | None = None):
    """Cluster every embedded facet and write ``cluster_id`` back.

    Runs over the WHOLE vec table, not a window: a cluster id only means
    something relative to the population it was computed against, so a partial
    re-cluster would leave two incompatible labellings in one column.
    """
    from .cluster import DEFAULT_SEED, kmeans

    if db is None:
        from okuro.db import get_db

        db = get_db()

    rows = db.fetchall(f"SELECT id, embedding FROM {VEC_TABLE} ORDER BY id")
    if not rows:
        return kmeans([], k=0)

    import numpy as np

    ids = [r["id"] for r in rows]
    vectors = [np.frombuffer(r["embedding"], dtype=np.float32) for r in rows]
    result = kmeans(vectors, k=k, seed=DEFAULT_SEED if seed is None else seed)

    with db.write():
        for sid, label in zip(ids, result.labels):
            db.execute(
                "UPDATE distill_facets SET cluster_id = ? WHERE session_id = ?",
                (int(label), sid),
            )
    return result
