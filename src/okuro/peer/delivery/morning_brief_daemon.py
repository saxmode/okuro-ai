# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: peer.delivery.morning_brief_daemon — daemon handler that mines recent
#   okuro activity across MANY senses (progress, memories, knowledge graph,
#   signals, thoughts, todos, model discoveries) into a MorningBriefFindings,
#   runs it through an LLM SPEECH-REWRITE (clean, spoken prose — no code tokens,
#   ids, cron strings, paths, version numbers that make a voice model
#   unintelligible), composes the fixed script (morning_brief.compose_script),
#   and renders it to audio (morning_brief_audio.render_brief_audio). Called by
#   the 'morning-brief' daemon task (registry.py) on its cron tick — plain
#   function, no LLM agent, never raises into the scheduler.
# index: imports | readers (_user_name/_recent_*) | _open_todos
#   | _mark_todos_surfaced | _raw_digest
#   | _speech_rewrite | _sanitize | _fallback_findings | gather_findings
#   | generate_and_deliver
# AGENT_HEADER_END -->
"""Daemon handler: mine recent activity -> clean spoken brief -> audio.

The brief is what happened to okuro *for the user* — so it mines the full
sense surface (progress, memories, knowledge, signals, thoughts, todos), not
just progress. The raw rows are DB strings full of model names, cron
expressions, code tokens and ids; fed straight to TTS they are unintelligible
(and destabilise the voice model). So the mined material goes through a single
LLM rewrite (:func:`_speech_rewrite`, same ``bridge.invoke`` seam the summary
channel uses) that returns clean, spoken content for the fixed script's three
slots. The template still owns the shape (verbatim opener, sections, exactly 3
actions); the LLM only owns the words. If the LLM is unavailable the raw
strings are run through a deterministic :func:`_sanitize` so the brief is still
speakable — degraded, never broken, never invented.
"""

from __future__ import annotations

import json
import logging
import re

from okuro.peer.delivery.morning_brief import (
    BriefCompositionError,
    MorningBriefFindings,
    compose_script,
)

log = logging.getLogger(__name__)

# Keep the LLM prompt bounded: at most this many items per source, each body
# clipped to this many chars. The rewrite condenses anyway.
_MAX_PER_SOURCE = 6
_CLIP_CHARS = 240

# How long a todo stays out of the brief after being spoken. A week of silence
# is long enough that the brief stops feeling like a stuck record, short enough
# that something genuinely urgent comes back around. With 54 open priority-5
# todos and 3 slots a morning, the P5 tier alone rotates for ~18 days.
_BRIEF_COOLDOWN_DAYS = 7

# Self-maintenance / process narration the brief must NEVER surface. The brief
# reports okuro's OUTCOMES for the user, not its own housekeeping: a daily
# role-refresh sweep, a "no change" verification, a health check or hygiene
# tick is process, not a result. This matches the same "no-op" text the role
# maintenance writes into role_knowledge ("no significant changes in week of…"),
# so one pattern filters both the progress rows and the role deltas.
_PROCESS_NOISE = re.compile(
    r"role-refresh|role-researcher|maintenance sweep|maintenance window|"
    r"refresh the knowledge of (?:every|all|the|stale) role|schedule-bearing|"
    r"stale\s*[:=]\s*false|no significant changes|no change\b|nothing new|"
    r"already (?:current|fresh|up.?to.?date)|health check|hygiene|drift alarm|"
    r"outcome\s*=\s*empty",
    re.I,
)

# Knowledge-graph triples are mostly internal wiring: an entity UUID linked to a
# task/artifact by a provenance predicate. Those are plumbing, not a fact the
# user wants spoken — drop any triple whose predicate wires provenance or whose
# subject/object is a raw id.
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)
_KG_PLUMBING = frozenset({
    "produced_by", "for_task", "for_subtask", "references", "part_of",
    "derived_from", "belongs_to", "generated_by", "created_by", "tracked_by",
})


# ── readers ───────────────────────────────────────────────────────────────
def _user_name() -> str:
    try:
        from okuro.yu.profile import get_profile

        data = json.loads(get_profile(format="json", section="identity"))
        name = (data.get("identity") or {}).get("name")
        return name or "there"
    except Exception:  # noqa: BLE001
        return "there"


def _recent_progress(hours: int) -> list[dict]:
    from okuro.db import get_db

    db = get_db()
    return db.fetchall(
        "SELECT project, summary, updated_at FROM progress "
        "WHERE updated_at >= datetime('now', ?) ORDER BY updated_at DESC",
        (f"-{int(hours)} hours",),
    )


def _recent_memories(hours: int) -> list[dict]:
    """Memories written in the window (agent_memory), newest first."""
    from okuro.db import get_db

    db = get_db()
    return db.fetchall(
        "SELECT topic, content, project, created_at FROM agent_memory "
        "WHERE created_at >= datetime('now', ?) ORDER BY created_at DESC LIMIT ?",
        (f"-{int(hours)} hours", _MAX_PER_SOURCE),
    )


def _recent_kg(hours: int) -> list[dict]:
    """Knowledge-graph facts asserted in the window (kg_triples)."""
    from okuro.db import get_db

    db = get_db()
    return db.fetchall(
        "SELECT subject, predicate, object, project, created_at FROM kg_triples "
        "WHERE created_at >= datetime('now', ?) AND invalidated_at IS NULL "
        "ORDER BY created_at DESC LIMIT ?",
        (f"-{int(hours)} hours", _MAX_PER_SOURCE),
    )


def _recent_signals(hours: int) -> list[dict]:
    """Open signals raised in the window (signals)."""
    from okuro.db import get_db

    db = get_db()
    return db.fetchall(
        "SELECT source, severity, summary, created_at FROM signals "
        "WHERE created_at >= datetime('now', ?) AND status = 'open' "
        "ORDER BY created_at DESC LIMIT ?",
        (f"-{int(hours)} hours", _MAX_PER_SOURCE),
    )


def _recent_thoughts(hours: int) -> list[dict]:
    """Captured thoughts in the window (thoughts)."""
    from okuro.db import get_db

    db = get_db()
    return db.fetchall(
        "SELECT content, project, created_at FROM thoughts "
        "WHERE created_at >= datetime('now', ?) AND status = 'open' "
        "ORDER BY created_at DESC LIMIT ?",
        (f"-{int(hours)} hours", _MAX_PER_SOURCE),
    )


def _recent_role_updates(hours: int) -> list[dict]:
    """Roles that learned something NEW in the window — deltas only.

    A daily maintenance sweep writes a ``role_knowledge`` row for every role it
    checks, most of them no-ops ("no significant changes in week of…"). Those
    are the process narration the user does not want. Here we keep only rows
    whose content is a real delta — a role now knows something it didn't — so
    the brief can say *which role* updated and *what it now knows*, and stay
    silent about roles entirely on days nothing changed.
    """
    from okuro.db import get_db

    db = get_db()
    rows = db.fetchall(
        "SELECT role_id, type, content, created_at FROM role_knowledge "
        "WHERE created_at >= datetime('now', ?) ORDER BY created_at DESC LIMIT ?",
        (f"-{int(hours)} hours", _MAX_PER_SOURCE),
    )
    return [
        r for r in rows
        if r.get("content") and not _PROCESS_NOISE.search(r["content"])
    ]


def _open_todos(limit: int = 3) -> list[dict]:
    """The action-item candidates — due-soon first, and never the same three.

    This used to be a bare ``todo_list(limit=3)``. Every other reader in this
    module is windowed (``created_at >= now - N hours``); that one was not, and
    ``todo_list`` orders by ``priority DESC, due_at, created_at DESC``. With a
    backlog nothing ever closes (1745 open / 35 done, 54 at priority 5, none
    with a due date) the tiebreak fell to ``created_at DESC`` on a set that
    stopped growing — so the top three froze, and the brief read out the SAME
    two commitments every morning for days (reported 2026-07-25).

    Two changes fix the class, not the instance:

    * a cooldown — a todo spoken in a brief is not eligible again for
      ``_BRIEF_COOLDOWN_DAYS``, so the brief walks the queue instead of
      standing on it. Stamped by :func:`_mark_todos_surfaced` only after the
      audio actually renders, so a failed tick does not burn candidates.
    * an order that means something for *today* — anything due within a week
      (overdue included) leads, soonest first; everything else falls back to
      priority, then ``updated_at`` (recently *touched* work is live work,
      whereas ``created_at`` just means "ingested last").
    """
    from okuro.db import get_db

    return get_db().fetchall(
        """
        SELECT id, title, detail, priority, project, due_at, status
          FROM todos
         WHERE status IN ('open', 'doing')
           AND id NOT LIKE 'stream-stub-%'
           AND (NOT json_valid(context)
                OR json_extract(context, '$.brief_surfaced_at') IS NULL
                OR json_extract(context, '$.brief_surfaced_at')
                     < datetime('now', ?))
         ORDER BY
              CASE WHEN due_at IS NOT NULL
                    AND due_at <= datetime('now', '+7 days') THEN 0 ELSE 1 END,
              -- Only the due-soon bucket sorts by date; the rest all collapse
              -- to NULL here (a tie) so priority decides them, and a due date
              -- six months out never outranks an urgent undated todo.
              CASE WHEN due_at IS NOT NULL
                    AND due_at <= datetime('now', '+7 days') THEN due_at END ASC,
              priority DESC,
              updated_at DESC
         LIMIT ?
        """,
        (f"-{_BRIEF_COOLDOWN_DAYS} days", int(limit)),
    )


def _mark_todos_surfaced(todo_ids: list[str]) -> None:
    """Stamp the cooldown on todos the brief actually spoke.

    Writes ``context.brief_surfaced_at`` and deliberately leaves ``updated_at``
    alone — being read out is not activity on the todo, and bumping it would
    push the item back up the very ordering the cooldown just removed it from.
    """
    if not todo_ids:
        return
    from okuro.db import get_db

    db = get_db()
    for todo_id in todo_ids:
        db.execute(
            """UPDATE todos
                  SET context = json_set(
                          CASE WHEN json_valid(context) THEN context ELSE '{}' END,
                          '$.brief_surfaced_at', datetime('now'))
                WHERE id = ?""",
            (todo_id,),
        )


def _new_discoveries(limit: int = 3) -> list[dict]:
    """Unacknowledged model discoveries — the weekly scan's fresh fits."""
    try:
        from okuro.ai_models import list_discoveries

        return list_discoveries(status="new", limit=limit)
    except Exception:  # noqa: BLE001
        return []


def _safe(fn, *args):
    """Call a reader, returning [] on any failure (daemon must not raise)."""
    try:
        return fn(*args) or []
    except Exception as exc:  # noqa: BLE001
        log.debug("morning-brief: source %s failed: %s", getattr(fn, "__name__", fn), exc)
        return []


def _clip(text: str) -> str:
    text = " ".join(str(text or "").split())
    return text[:_CLIP_CHARS]


# ── raw material -> LLM digest ─────────────────────────────────────────────
def _raw_digest(hours: int, todos: list[dict]) -> tuple[str, dict]:
    """Assemble labelled raw material from every sense + a counts summary.

    Returns ``(digest_text, counts)``. ``digest_text`` is the LLM input;
    ``counts`` drives the deterministic fallback's honesty when a source empty.
    ``todos`` is passed in rather than pulled here so the LLM path, the
    fallback path and the cooldown stamp all act on ONE selection.
    """
    progress = _safe(_recent_progress, hours)
    memories = _safe(_recent_memories, hours)
    kg = _safe(_recent_kg, hours)
    signals = _safe(_recent_signals, hours)
    thoughts = _safe(_recent_thoughts, hours)
    role_updates = _safe(_recent_role_updates, hours)
    discoveries = _new_discoveries(limit=3)

    lines: list[str] = []

    def _section(title: str, items: list[str]) -> None:
        if items:
            lines.append(f"{title}:")
            lines.extend(f"- {it}" for it in items[:_MAX_PER_SOURCE])
            lines.append("")

    # Progress rows are the user's outcomes — but drop the daemon's own
    # housekeeping narration (role sweeps, "no change" ticks) so the brief
    # never reads back its own process.
    _section("PROGRESS (outcomes — what okuro produced or resolved)", [
        _clip(f"{r.get('project','')}: {r.get('summary','')}") for r in progress
        if r.get("summary") and not _PROCESS_NOISE.search(r["summary"])
    ])
    _section("ROLE UPDATES (a role now knows something it didn't before)", [
        _clip(f"{r.get('role_id','a role')} now knows: {r.get('content','')}")
        for r in role_updates if r.get("content")
    ])
    _section("MEMORIES (things okuro learned/decided)", [
        _clip(f"{r.get('topic','note')}: {r.get('content','')}") for r in memories
        if r.get("content") and not _PROCESS_NOISE.search(r["content"])
    ])
    _section("KNOWLEDGE (facts recorded)", [
        _clip(f"{r.get('subject','')} {r.get('predicate','')} {r.get('object','')}") for r in kg
        if r.get("subject")
        and (r.get("predicate") or "").lower() not in _KG_PLUMBING
        and not _UUID.search(f"{r.get('subject','')} {r.get('object','')}")
        and not _PROCESS_NOISE.search(
            f"{r.get('subject','')} {r.get('predicate','')} {r.get('object','')}")
    ])
    _section("SIGNALS (things flagged)", [
        _clip(f"[{r.get('severity','')}] {r.get('summary','')}") for r in signals if r.get("summary")
    ])
    _section("THOUGHTS (ideas captured)", [
        _clip(r.get("content", "")) for r in thoughts if r.get("content")
    ])
    if discoveries:
        names = ", ".join(d.get("display_name", "") for d in discoveries if d.get("display_name"))
        _section("NEW MODELS (scan found fits)", [f"{len(discoveries)} new: {names}"])
    _section("OPEN TODOS (candidate actions for today)", [
        _clip(t.get("title", "")) for t in todos if t.get("title")
    ])

    counts = {
        "progress": len(progress), "memories": len(memories), "kg": len(kg),
        "signals": len(signals), "thoughts": len(thoughts),
        "role_updates": len(role_updates),
        "todos": len(todos), "discoveries": len(discoveries),
    }
    return "\n".join(lines).strip(), counts


# ── LLM speech-rewrite ─────────────────────────────────────────────────────
_SPEECH_SYSTEM = (
    "You write a short spoken morning audio brief for one person. You will be "
    "given raw developer notes (progress logs, memories, knowledge facts, role "
    "updates, signals, thoughts, todos). Rewrite them into clean, natural "
    "SPOKEN English that a text-to-speech voice can read aloud smoothly.\n"
    "\n"
    "WHAT THE LISTENER WANTS — read this twice. He wants OUTCOMES and "
    "POTENTIAL, never process:\n"
    "- Report the RESULT of what happened, not the activity. State what is now "
    "true, what was produced, resolved, decided, or learned — not that work "
    "'was done', a task 'was run', a check 'was performed', or a sweep "
    "'happened'. 'X now works' or 'X is decided', not 'okuro worked on X'.\n"
    "- If a note describes only routine activity with no result — a maintenance "
    "sweep, a health check, a 'checked' / 'verified' / 'no change' tick — DROP "
    "it entirely. Never narrate housekeeping. Never say okuro 'checked', "
    "'reviewed', 'maintained', or 'looked at' something with nothing to show.\n"
    "- For a ROLE UPDATE, say WHICH role now knows something it did not before, "
    "and WHAT that new knowledge is. Speak the role in plain words (e.g. "
    "'your MCP engineer', 'the security auditor'). One sentence per role.\n"
    "- Every action item must carry its POTENTIAL: state the action AND what it "
    "unlocks, de-risks, or makes possible if he takes it on today. Never a bare "
    "task title — always 'do X, which would Y'.\n"
    "\n"
    "TTS-SAFETY rules (hard):\n"
    "- Absolutely NO code tokens, file paths, function names, ids, hashes, cron "
    "expressions, version strings, or symbols like / _ * -> [] () {} #. If a "
    "raw item contains them, describe it in plain words instead of reading it.\n"
    "- Say technical/model names naturally or omit them; never spell out "
    "strings like 'Qwen3.6-27B-NVFP4'.\n"
    "- Preserve the real facts; invent nothing. If a source was empty, don't "
    "mention it. Do not manufacture an outcome or a potential that the notes do "
    "not support — if an item has no real result, drop it rather than inflate "
    "it.\n"
    "- Warm, concise, factual. No headings, no bullet markers, no stage "
    "directions.\n"
    "- Do NOT greet, do NOT address the listener by name, and do NOT open with "
    "'yesterday'/'today'/'over the last few days' — the surrounding template "
    "already supplies the greeting and the time framing. Start each sentence "
    "with the substance.\n"
    "\n"
    "Return ONLY a JSON object with exactly these keys:\n"
    '  "assets": array of 2-4 short spoken sentences, each stating an OUTCOME '
    "(a result or a role's new knowledge) — not an activity,\n"
    '  "combinations": array of 1-2 short spoken sentences on connections or '
    "potential worth noticing across the work,\n"
    '  "actions": array of EXACTLY 3 short spoken action items, each pairing the '
    "action with the potential it unlocks if he takes it over today.\n"
    "No prose outside the JSON."
)


def _extract_json(text: str) -> dict | None:
    """Best-effort parse of a JSON object from an LLM output string."""
    text = (text or "").strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except Exception:  # noqa: BLE001
        pass
    m = re.search(r"\{.*\}", text, re.DOTALL)  # first ... last brace
    if m:
        try:
            return json.loads(m.group(0))
        except Exception:  # noqa: BLE001
            return None
    return None


def _speech_rewrite(digest: str, name: str, hours: int) -> MorningBriefFindings | None:
    """Rewrite the raw digest into clean spoken findings via the LLM bridge.

    Returns a ``MorningBriefFindings`` on success, or ``None`` if the bridge is
    unavailable / fails / returns unusable output (caller falls back to the
    deterministic sanitiser). Mirrors summary._narration's guard pattern.
    """
    if not digest.strip():
        return None
    span = "the last 24 hours" if hours <= 24 else f"the last {round(hours / 24)} days"
    prompt = (
        f"Listener's name: {name}. Time span covered: {span}.\n\n"
        f"Raw notes to turn into the spoken brief:\n\n{digest}\n\n"
        "Output ONLY the JSON object described in the system message."
    )
    try:
        from okuro.bridge.invoke import invoke

        result = invoke(
            prompt, capability="translate", system_prompt=_SPEECH_SYSTEM,
            timeout=120, tool=True, response_format="json",
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("morning-brief: bridge invoke raised (%s) — sanitiser fallback", exc)
        return None
    if not (isinstance(result, dict) and result.get("success")):
        log.warning("morning-brief: bridge invoke unsuccessful — sanitiser fallback")
        return None

    data = _extract_json(str(result.get("output") or ""))
    if not isinstance(data, dict):
        return None
    assets = [str(x).strip() for x in (data.get("assets") or []) if str(x).strip()]
    combinations = [str(x).strip() for x in (data.get("combinations") or []) if str(x).strip()]
    actions = [str(x).strip() for x in (data.get("actions") or []) if str(x).strip()]
    if not assets or not combinations or not actions:
        return None
    # Enforce the composer's exactly-3-actions contract.
    while len(actions) < 3:
        actions.append("Review yesterday's activity for anything unresolved")
    return MorningBriefFindings(
        user_name=name, assets=assets, combinations=combinations, action_items=actions[:3]
    )


# ── deterministic fallback (no LLM) ────────────────────────────────────────
_HOSTILE = re.compile(r"[/_*#\[\]{}<>|`]|->|::|0o\d+|\b[\w.-]*\d[\w.-]*[A-Za-z][\w.-]*\b|\S*/\S*")


def _sanitize(text: str) -> str:
    """Strip speech-hostile tokens so raw strings are at least speakable.

    Not as good as the LLM rewrite (can't summarise), but removes the model
    names / paths / code tokens / cron symbols that make TTS unintelligible.
    """
    text = str(text or "")
    text = text.replace("&", " and ")
    # Drop clearly code-like tokens (has a slash, underscore, or digit-letter mix).
    kept = [w for w in text.split()
            if not re.search(r"[/_*#\[\]{}<>|`@]|->|::|0o\d|\d[A-Za-z]|[A-Za-z]\d", w)]
    out = " ".join(kept)
    out = re.sub(r"\s+([.,;:])", r"\1", out)
    out = re.sub(r"\s{2,}", " ", out).strip(" ,;:-")
    return out


def _fallback_findings(hours: int, name: str, counts: dict,
                       todos: list[dict]) -> MorningBriefFindings:
    """Build findings from raw sources with only deterministic cleaning.

    Used when the LLM bridge is unavailable. Mines the same broad surface but
    sanitises each clause instead of rewriting it. ``todos`` is the same
    selection the LLM path saw — see :func:`_raw_digest`.
    """
    progress = _safe(_recent_progress, hours)
    memories = _safe(_recent_memories, hours)
    role_updates = _safe(_recent_role_updates, hours)
    signals = _safe(_recent_signals, hours)

    # Drop the daemon's own housekeeping narration here too — the deterministic
    # path must be as quiet about process as the LLM path.
    progress = [r for r in progress
                if r.get("summary") and not _PROCESS_NOISE.search(r["summary"])]

    assets: list[str] = []
    for r in progress[:4]:
        c = _sanitize(f"{r.get('project','')}: {r.get('summary','')}")
        if c:
            assets.append(c)
    for r in memories[:2]:
        c = _sanitize(r.get("content", ""))
        if c:
            assets.append("okuro noted that " + c)
    for r in role_updates[:2]:
        c = _sanitize(r.get("content", ""))
        if c:
            assets.append(f"The {_sanitize(r.get('role_id',''))} role now knows that {c}")
    if not assets:
        assets = ["No new activity was logged in this period"]

    combinations: list[str] = []
    projects = sorted({r.get("project") for r in progress if r.get("project")})
    if len(projects) >= 2:
        combinations.append(
            "You touched several projects — " + _sanitize(", ".join(projects))
            + " — worth checking whether they connect")
    if signals:
        combinations.append(f"There are {len(signals)} open signals worth a look")
    if not combinations:
        combinations = ["Nothing crossed project lines in this period"]

    action_items = [_sanitize(t.get("title", "")) for t in todos if t.get("title")]
    action_items = [a for a in action_items if a][:3]
    while len(action_items) < 3:
        action_items.append("Review recent activity for anything unresolved")

    return MorningBriefFindings(
        user_name=name, assets=assets[:5], combinations=combinations,
        action_items=action_items[:3],
    )


# ── public entry ───────────────────────────────────────────────────────────
def gather_findings(hours: int = 24,
                    todos: list[dict] | None = None) -> MorningBriefFindings:
    """Mine recent activity (last ``hours``) into clean spoken ``MorningBriefFindings``.

    Broad sense surface -> single LLM speech-rewrite -> findings. Falls back to
    a deterministic sanitiser if the LLM bridge is unavailable. ``hours`` widens
    the window (daemon default 24h; a manual dispatch can ask for e.g. 72h).
    Never raises — every source and the rewrite are guarded.

    ``todos`` lets the caller supply the action-item candidates it intends to
    stamp with the brief cooldown (see :func:`generate_and_deliver`); omitted,
    they are selected here, which is the right behaviour for a preview that
    must not consume candidates.
    """
    name = _user_name()
    if todos is None:
        todos = _safe(_open_todos, 3)
    digest, counts = _raw_digest(hours, todos)

    rewritten = _speech_rewrite(digest, name, hours)
    if rewritten is not None:
        return rewritten
    log.info("morning-brief: using deterministic sanitiser (no LLM rewrite)")
    return _fallback_findings(hours, name, counts, todos)


def generate_and_deliver(hours: int = 24) -> dict:
    """Daemon handler — compose + render the morning brief for the last ``hours``.

    Never raises (mirrors ``recurring_media.tick``'s contract) so a bad morning
    never kills the scheduler tick.
    """
    # Selected once here, not inside gather_findings, so the cooldown stamp
    # below lands on exactly the todos that made it into the script.
    todos = _safe(_open_todos, 3)
    try:
        findings = gather_findings(hours=hours, todos=todos)
        script = compose_script(findings)
    except BriefCompositionError as exc:
        log.warning("morning-brief: composition failed: %s", exc)
        return {"success": False, "error": str(exc)}
    except Exception as exc:  # noqa: BLE001
        log.exception("morning-brief: findings gathering failed")
        return {"success": False, "error": str(exc)}

    from okuro.peer.delivery.morning_brief_audio import render_brief_audio

    result = render_brief_audio(script)
    if result.get("error"):
        log.warning("morning-brief: audio render failed: %s", result["error"])
        return {"success": False, "error": result["error"]}

    # Only now — a brief that never rendered was never heard, so its candidates
    # must stay eligible tomorrow.
    try:
        _mark_todos_surfaced([t["id"] for t in todos if t.get("id")])
    except Exception as exc:  # noqa: BLE001
        # A missed stamp costs one repeated item, not the brief.
        log.warning("morning-brief: cooldown stamp failed: %s", exc)

    log.info(
        "morning-brief: generated %s (%.1fs audio)",
        result.get("path"),
        result.get("audio_seconds", 0),
    )
    return {"success": True, "script": script, **result}
