# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Shared helpers for profile-compliance hooks across providers.
# index: imports | helpers | def extract_profile_compliance_rules | def build_profile_turn_context | writers
# AGENT_HEADER_END -->
"""Shared compliance-rules cache for provider hook scripts.

Multiple provider adapters (claude, gemini, future) install Stop/AfterAgent
hooks that enforce the user profile. They all read the same JSON cache at
``~/.okuro/profile-hook-rules.json`` so the rules live in one place even
though the hook scripts and their stdin/stdout shapes differ per provider.

This module owns the caches: extracting the profile subset and writing it
atomically to disk. Adapters call ``write_profile_hook_rules()`` and
``write_profile_turn_context()`` from their ``install_hooks()`` methods.
"""

from __future__ import annotations

import calendar
import json
import logging
import os
import time
from collections import Counter
from typing import Any
from okuro.db.engine import okuro_home

log = logging.getLogger(__name__)


def _rules_path() -> str:
    return str(okuro_home() / "profile-hook-rules.json")


def _turn_context_path() -> str:
    return str(okuro_home() / "profile-turn-context.txt")


def atomic_write_text(path: str, content: str) -> str:
    """Atomically write ``content`` to ``path`` (temp file + os.replace).

    Shared by every provider adapter that persists a config or hook-script
    file so the "write tmp, fsync, rename" pattern lives in exactly one
    place. ``os.replace`` is atomic on POSIX and on same-volume Windows, so
    a concurrent reader sees either the whole old file or the whole new one
    — never a truncated mid-write.
    """
    target_dir = os.path.dirname(path) or "."
    os.makedirs(target_dir, exist_ok=True)
    ts = time.strftime("%Y%m%dT%H%M%S")
    tmp_path = os.path.join(
        target_dir,
        f"{os.path.basename(path)}.tmp.{os.getpid()}.{ts}",
    )
    try:
        with open(tmp_path, "w") as f:
            f.write(content)
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                pass  # fsync optional — atomicity comes from rename
        os.replace(tmp_path, path)
    except Exception:
        try:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
        except OSError:
            pass
        raise
    return path


def atomic_write_json(path: str, obj: Any) -> str:
    """Atomically write ``obj`` as indented JSON. See ``atomic_write_text``."""
    return atomic_write_text(path, json.dumps(obj, indent=2) + "\n")


def _violations_log_path() -> str:
    return str(okuro_home() / "profile-violations.jsonl")


# Map detector violation strings to a stable bucket key (the prefix the
# detectors themselves use) plus a short corrective directive. The directive
# is what the agent sees — a literal "don't do X" derived from the detector
# that just caught the agent doing X. Reusing detector vocabulary keeps the
# feedback loop tight: the agent sees the same name in instructions, in the
# per-turn context, and (eventually) in the block-mode reason text.
_BUCKET_DIRECTIVES: dict[str, str] = {
    "show_ids": "no commit/memory IDs in backticks; no UUIDs in body",
    "long_paragraph": "no 5+ sentence prose; use tables/bullets",
    "items_per_level": "cap any single list block at 7 items",
    "section_numbering": "don't number headings (## 1. / ### 2))",
    "avoid_preamble": "don't open with 'Sure'/'Of course'/'Let me'",
    "avoid_question_restatement": "don't open by restating the request",
    "codebase_intel_bypass": "call cortex_scope before claiming code is absent; an empty scoped result is not proof it's missing",
    "reply_volume": "cut prose length; move detail to a table or an artifact",
}


# DETECTOR VALIDITY — one registry, read by EVERY consumer.
#
# WHY IT EXISTS. `avoid_preamble` was excluded from block mode on 2026-07-29
# with a written rationale (below), and the exclusion reached exactly one
# consumer: the block-detector default. The ADVISORY failure-modes block —
# which is injected into every turn and into every provider's instruction file
# — kept promoting it, so the detector the system had just judged invalid was
# the loudest thing an agent read each turn. Measured: `avoid_preamble ×13` at
# the top of a live turn-context block.
#
# CLASS: a judgement recorded at one consumer instead of at the fact. Same
# shape as the tool_routing rule that reached 2 of 4 surfaces (WP1). The fix is
# that validity is now a property of the DETECTOR, and both consumers derive
# from it, so retiring the next detector cannot reach half the system.
#
#   block  — safe to REFUSE a turn on. True only where the violation is a fact
#            about the text, not a judgement about its content, and where the
#            fix cannot be gamed by rewording around the check.
#   advise — worth showing an agent as a recurring failure mode.
_DETECTOR_VALIDITY: dict[str, dict] = {
    "show_ids":            {"block": True,  "advise": True},
    "long_paragraph":      {"block": True,  "advise": True},
    "items_per_level":     {"block": False, "advise": True},
    # BLOCKING since 2026-09-09, and the precision fix came FIRST.
    #
    # It is the right SHAPE for a block: one regex over headings, a fact about
    # the text, satisfiable by rewriting. Rate 0.18% over 30d (7 of 3,828) and
    # 0.28% over 90d (15 of 5,381) — stable, and an order of magnitude rarer
    # than reply_volume's 1.7%.
    #
    # BUT IT SCANNED RAW TEXT. `^#{1,6}\s*\d+[.)]\s` matches a Python comment
    # inside a fence — `# 1. claim the task` — exactly as it matches `## 1. Foo`.
    # Blocking it un-fixed would have refused a turn over a code sample: the
    # same "penalise the desired output" failure this registry already records
    # for the retired bare-hex show_ids arm and for long_paragraph counting
    # ordered-list markers as sentences. The detector now reads
    # `_strip_fences(last_text)`, sharing one fence definition with
    # long_paragraph rather than re-deriving its own.
    #
    # KILL CRITERIA, same three as reply_volume: block rate > 5% over a rolling
    # week · redrafts do not remove the numbering · a blocked reply was correct
    # and the redraft lost structure.
    "section_numbering":   {"block": True,  "advise": True},
    # INVALIDATED AS A SIGNAL, not merely unsafe to block. The regex matches an
    # OPENING WORD, so it fires on proof-announcing openers ("Measured:", "Let
    # me check whether...") at a 0.8% base rate, and it fired ZERO times on all
    # seven replies the user actually identified as failures. A detector that
    # cannot see the failure it is named for must not be the headline an agent
    # reads every turn — it trains avoidance of a word while the real defect
    # (reply volume) goes unmeasured.
    "avoid_preamble":      {"block": False, "advise": False,
                            "why": "matches an opening word, not the failure; "
                                   "0 hits on the 7 labelled bad replies"},
    "avoid_question_restatement": {"block": False, "advise": True},
    "codebase_intel_bypass":      {"block": False, "advise": True},
    # BLOCKING since 2026-09-09. Log-only from 2026-07-29 (WP5/I12) under a
    # stated precondition: "block mode needs kill criteria and a week of logs
    # before it can be trusted to refuse a turn." Both are now satisfied, and
    # the evidence is measured rather than argued:
    #
    #   * SIX WEEKS of logs, not one. 3,825 evaluated turns in the last 30 days
    #     alone.
    #   * 66 hits in that window = 1.7%. A refusal rate that low is a
    #     correction, not an obstacle course.
    #   * THE THRESHOLD IS CALIBRATED TO THE USER'S OWN LABELS. It was derived
    #     from seven replies the owner named as failures, whose prose ran
    #     2,032-3,560 chars. Live violations run 2,004-3,202 (p50 2,273) —
    #     entirely inside the band he labelled. The detector fires on the thing
    #     it was built to catch, at the size he called too long.
    #   * IT CANNOT PUNISH THE FORMAT HE ASKED FOR. `reply_volume` counts PROSE
    #     chars, excluding code fences and table rows, so a table-heavy answer
    #     is never penalised. That is the profile's own preference, enforced.
    #
    # KILL CRITERIA — the part that was missing, stated so a future session can
    # act on it without re-deriving the argument. Revert to {"block": False} if
    # ANY holds over a rolling week:
    #   1. block rate > 5% of evaluated turns (currently 1.7%) — the threshold
    #      has drifted from what the user considers long;
    #   2. redrafts are not shorter than the reply they replaced — the block is
    #      producing churn, not correction;
    #   3. a blocked reply was correct as written and the redraft lost content
    #      — precision failure, fix the threshold before re-enabling.
    #
    # Cost is bounded by the `stop_hook_active` loop guard: one redraft per
    # turn, never two.
    "reply_volume":        {"block": True, "advise": True},
}


def detector_validity(bucket: str) -> dict:
    """Validity record for a detector bucket. Unknown buckets advise, never block."""
    return _DETECTOR_VALIDITY.get(bucket, {"block": False, "advise": True})


def blockable_detectors() -> list[str]:
    """Detectors safe to refuse a turn on — derived, never hand-listed."""
    return [k for k, v in _DETECTOR_VALIDITY.items() if v.get("block")]


def _bucket_for_violation(violation: str) -> str:
    """Return the stable bucket key for a violation string, or '' if unknown.

    Detector strings have the shape ``"<key>=<bool> but <reason>"`` or
    ``"<key>: <detail>"``. The bucket key is the first identifier — that's
    also the only stable surface across detector wording changes.
    """
    if not isinstance(violation, str):
        return ""
    head = violation.strip().split(None, 1)[0] if violation.strip() else ""
    head = head.rstrip(":").split("=", 1)[0]
    return head if head in _BUCKET_DIRECTIVES else ""


def _recent_violation_counts(
    log_path: str | None = None,
    *,
    window_seconds: int = 7 * 24 * 3600,
    now: float | None = None,
) -> Counter[str]:
    """Tally violations by bucket within the trailing ``window_seconds``.

    Best-effort read: missing file, malformed lines, or unknown buckets are
    silently dropped. The renderer treats an empty counter as "no failure
    modes worth surfacing" and emits no block at all.

    The special key ``"__turns__"`` carries the number of evaluated turns in
    the window — the DENOMINATOR. Without it a count is unreadable: "13" is a
    crisis at 30 turns and noise at 3,000, and the block was printing bare
    counts.
    """
    path = log_path or _violations_log_path()
    counts: Counter[str] = Counter()
    if not os.path.isfile(path):
        return counts

    cutoff = (now if now is not None else time.time()) - window_seconds

    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                ts = rec.get("ts")
                if not isinstance(ts, str):
                    continue
                try:
                    rec_epoch = calendar.timegm(
                        time.strptime(ts, "%Y-%m-%dT%H:%M:%SZ")
                    )
                except Exception:
                    continue
                if rec_epoch < cutoff:
                    continue
                # Every in-window record is one evaluated turn, whether or not
                # it carried a violation.
                counts["__turns__"] += 1
                vlist = rec.get("violations")
                if not isinstance(vlist, list):
                    continue
                seen_per_record: set[str] = set()
                for v in vlist:
                    bucket = _bucket_for_violation(v)
                    if not bucket:
                        continue
                    seen_per_record.add(bucket)
                # One record may contain multiple violations of the same
                # bucket (e.g. two different show_ids strings). We only
                # count each bucket once per record so the count reflects
                # turns-affected, not detector firings — closer to the
                # signal the agent should care about.
                for bucket in seen_per_record:
                    counts[bucket] += 1
    except Exception:
        return counts
    return counts


def build_recent_failure_modes_block(
    *,
    top_n: int = 4,
    log_path: str | None = None,
    window_seconds: int = 7 * 24 * 3600,
    now: float | None = None,
    audience: str = "agent",
) -> str:
    """Render a compact block listing the top-N recurring failure modes.

    Returns ``""`` when nothing recent triggered a detector — the consumer
    decides whether to omit the section or render an empty placeholder.

    ``audience`` decides whether the counts are printed, and it is the whole
    point of this parameter:

    - ``"agent"`` (default) — buckets and their corrective directives, ordered
      most-repeated first, WITHOUT counts, denominators or percentages. A rate
      three words from "do not repeat" argues that the violation is rare and
      therefore tolerable; calibrating an imperative is precisely what an agent
      must not do. Frequency survives as ORDER.
    - ``"maintainer"`` — the same rows carrying ``×count/turns (rate%)``. This
      is the owner's before/after instrument and the ONLY place these detector
      rates are rendered: ``compliance_scorecard`` covers protocol adherence
      (bootstrap / report / cortex / memory / progress), never the profile
      detectors. Deleting the numbers outright would have destroyed the
      measurement, which is why they moved channel instead.

    The denominator itself is NOT optional in either mode's computation — clean
    turns still count toward ``__turns__``. A log of only violating turns makes
    every rate 100% by construction, which is the defect the denominator was
    added to fix; that fix is untouched, it simply stopped being agent-facing.
    """
    counts = _recent_violation_counts(
        log_path=log_path,
        window_seconds=window_seconds,
        now=now,
    )
    turns = counts.pop("__turns__", 0)
    # Detectors judged invalid are not advised. The judgement lives in
    # _DETECTOR_VALIDITY, so a retirement reaches this consumer and the
    # block-mode default together — the whole point of the registry.
    counts = Counter({
        b: n for b, n in counts.items() if detector_validity(b).get("advise")
    })
    if not counts:
        return ""

    top = counts.most_common(top_n)
    days = max(1, window_seconds // (24 * 3600))

    if audience == "maintainer":
        header = f"Frequent failure modes (last {days}d) — DO NOT repeat:"
        if turns:
            header = (
                f"Frequent failure modes (last {days}d, {turns} turns "
                f"evaluated) — DO NOT repeat:"
            )
        lines = [header]
        for bucket, count in top:
            directive = _BUCKET_DIRECTIVES.get(bucket, "")
            suffix = f" — {directive}" if directive else ""
            # Rate, not a bare count. "13" is a crisis at 30 turns and noise at
            # 3,000, and this block once printed only the numerator.
            rate = f" ×{count}"
            if turns:
                rate = f" ×{count}/{turns} ({100.0 * count / turns:.1f}%)"
            lines.append(f"- {bucket}{rate}{suffix}")
        return "\n".join(lines)

    # audience == "agent": order carries the frequency, nothing else does.
    lines = [
        f"Your own violations from the last {days}d, most repeated first. "
        f"Do not repeat them:"
    ]
    for bucket, _count in top:
        directive = _BUCKET_DIRECTIVES.get(bucket, "")
        suffix = f" — {directive}" if directive else ""
        lines.append(f"- {bucket}{suffix}")
    return "\n".join(lines)


def _rule_text(item: Any) -> str:
    if isinstance(item, str):
        return item
    if isinstance(item, dict):
        for key in ("rule", "label", "title", "name", "description"):
            value = item.get(key)
            if isinstance(value, str) and value.strip():
                return value
    return ""


def _flatten_rule_items(items: Any) -> list[str]:
    if isinstance(items, (list, tuple)):
        out: list[str] = []
        for item in items:
            out.extend(_flatten_rule_items(item))
        return out
    if isinstance(items, dict):
        text = _rule_text(items)
        if text:
            return [text]
        out = []
        for value in items.values():
            out.extend(_flatten_rule_items(value))
        return out
    if isinstance(items, str) and items.strip():
        return [items.strip()]
    return []


def _contains_any(text: str, needles: tuple[str, ...]) -> bool:
    lower = text.lower()
    return any(needle in lower for needle in needles)


# The hard answer-first line every communication directive closes on. This is
# structural contract text (the single most load-bearing rule), not a profile
# rule — it renders identically regardless of profile so the model always sees
# the same final instruction. Everything ABOVE it is dynamic from the profile.
COMM_DIRECTIVE_CLOSER = "First token = the answer."


def _cap_first(text: str) -> str:
    text = text.strip()
    return text[:1].upper() + text[1:] if text else text


# ---------------------------------------------------------------------------
# Response length, as a NUMBER the agent can actually follow
# ---------------------------------------------------------------------------
#
# `communication.response_length` was a bare word — "concise" — and the gate
# that enforced it counted characters against a hardcoded 2000 that no layer
# ever told the agent. So the instruction was qualitative and the refusal was
# quantitative: told to be shorter, never told shorter THAN WHAT, and the only
# way to learn the number was to cross it and be blocked into a re-emit.
#
# The owner, 2026-09-09: "the setting in okuro about answer length should have a
# character value that can be followed. Answering then stoping then rewriting
# the answer via the stop-hook is stupid."
#
# Same class this session has been closing all along: a rule whose precise form
# lives in the enforcement layer and whose vague form lives in the instruction
# layer. One definition now, and the budget is STATED wherever the rule is.
#
# Counts PROSE only — code fences and table rows are excluded by the detector,
# so a long table never eats the budget. That is deliberate: tables are the
# format this user asked for, and a budget that punished them would push the
# reply back toward the prose it exists to prevent.
RESPONSE_LENGTH_BUDGETS: dict[str, int] = {
    "terse": 800,
    "brief": 1200,
    "concise": 2000,
    "balanced": 3000,
    "moderate": 3000,
    "detailed": 4500,
    "thorough": 4500,
    "comprehensive": 6000,
}

#: Used when `response_length` is set to something not in the map. Matches the
#: previous hardcoded gate threshold, so an unknown word never tightens the
#: budget silently — it lands exactly where the gate already was.
DEFAULT_RESPONSE_BUDGET = 2000


def response_budget(profile: dict | None = None) -> int:
    """The prose-character budget this profile's `response_length` means."""
    if profile is None:
        # Same loader every other reader here uses. An earlier draft called a
        # `_load_profile()` that does not exist -- it raised only on the
        # no-argument path, and the one test covering that path SKIPS when
        # $OKURO_HOME points at a test sandbox, so the suite stayed green over
        # a NameError. Caught by calling it for real.
        try:
            from okuro.yu.profile import get_profile_raw
            profile = get_profile_raw()
        except Exception:
            return DEFAULT_RESPONSE_BUDGET
    comm = (profile or {}).get("communication") or {}
    rl = comm.get("response_length")
    if isinstance(rl, str):
        return RESPONSE_LENGTH_BUDGETS.get(rl.strip().lower(), DEFAULT_RESPONSE_BUDGET)
    return DEFAULT_RESPONSE_BUDGET


def build_comm_directive(profile: dict | None = None) -> str:
    """Render the communication protocol as ONE positive imperative checklist.

    Single source of truth for BOTH the Claude Code system-tier output-style
    body AND the per-turn hook payload (``build_profile_turn_context``). One
    builder means the two channels can never drift.

    Contract:
      * Positive / imperative phrasing only. Bullets are derived from the
        profile's POSITIVE fields (``communication.patterns``,
        ``format_preferences.preferred``, ``response_length``) — never the
        avoid / pet-peeve / hate lists. Negations ("no preamble") get
        under-weighted; the positive form ("lead with the answer") does not.
      * At most 7 bullets.
      * Always closes with the hard answer-first line so recency lands on the
        single most load-bearing rule.

    Dynamic from the profile — no baked rule prose in the bullets. Returns
    ``""`` only when there is no profile at all (caller omits the block).
    """
    if profile is None:
        try:
            from okuro.yu.profile import get_profile_raw
            profile = get_profile_raw()
        except Exception:
            profile = None
    if not isinstance(profile, dict) or not profile:
        return ""

    comm = profile.get("communication") if isinstance(profile.get("communication"), dict) else {}
    fmt = comm.get("format_preferences") if isinstance(comm.get("format_preferences"), dict) else {}

    bullets: list[str] = []
    seen: set[str] = set()

    def _add(text: str) -> None:
        text = " ".join(text.split()).rstrip(".")
        key = text.lower()
        if not text or key in seen or len(bullets) >= 7:
            return
        seen.add(key)
        bullets.append(_cap_first(text))

    # 1. Positive behavioural rules (patterns) — "lead with answer" leads.
    for rule in _flatten_rule_items(comm.get("patterns")):
        _add(rule)

    # 2. Preferred formats, collapsed into one imperative bullet.
    preferred = _flatten_rule_items(fmt.get("preferred", fmt.get("prefer")))
    if preferred:
        _add("Use " + ", ".join(preferred) + " over prose")

    # 3. Response length.
    rl = comm.get("response_length")
    if isinstance(rl, str) and rl.strip():
        budget = RESPONSE_LENGTH_BUDGETS.get(rl.strip().lower())
        if budget:
            # POSITIVE phrasing, pinned by test_comm_directive: this list is
            # the directive layer and a negation here reads as a prohibition
            # the agent then has to invert before acting on it.
            _add(
                "Keep replies " + rl.strip() + " — up to "
                + str(budget) + " characters of PROSE "
                "(code blocks and table rows are free)"
            )
        else:
            _add("Keep replies " + rl.strip())

    lines = ["Communication protocol — apply to EVERY reply:"]
    lines.extend(f"- {b}" for b in bullets)
    lines.append(COMM_DIRECTIVE_CLOSER)
    return "\n".join(lines)


def _profile_enforces_rewrite(comm: dict | None) -> bool:
    """Does the profile ask for a violation to REFUSE the answer?

    Absent key means no. `communication.enforce_rewrite: true` is the only way
    to arm it, so the setting is visible in the profile the user owns rather
    than living in a shell variable only an agent can reach.
    """
    if not isinstance(comm, dict):
        return False
    return bool(comm.get("enforce_rewrite"))


def extract_profile_compliance_rules() -> dict[str, Any]:
    """Pull the profile subset used by the compliance detectors.

    Reads the active profile via ``okuro.yu.profile.get_profile_raw()`` and
    projects out the keys the hook understands. Any read failure (no DB,
    empty profile, schema drift) returns ``{"enabled": False}`` so the
    hook becomes a no-op rather than misfiring on malformed inputs.
    """
    try:
        from okuro.yu.profile import get_profile_raw
        profile = get_profile_raw()
    except Exception:
        return {"enabled": False}

    if not isinstance(profile, dict) or not profile:
        return {"enabled": False}

    ui = profile.get("ui_adaptations") or {}
    info = ui.get("information") if isinstance(ui, dict) else {}
    layout = ui.get("layout") if isinstance(ui, dict) else {}
    comm = profile.get("communication") or {}
    fmt = comm.get("format_preferences") if isinstance(comm, dict) else {}

    info = info if isinstance(info, dict) else {}
    layout = layout if isinstance(layout, dict) else {}
    fmt = fmt if isinstance(fmt, dict) else {}

    avoid_list = fmt.get("avoid") if isinstance(fmt.get("avoid"), list) else []
    raw_preferred = fmt.get("preferred", fmt.get("prefer"))
    preferred_list = raw_preferred if isinstance(raw_preferred, list) else []
    pattern_list = comm.get("patterns") if isinstance(comm.get("patterns"), list) else []
    pet_peeves = comm.get("pet_peeves") if isinstance(comm.get("pet_peeves"), list) else []

    profile_text = " ".join(
        _flatten_rule_items(avoid_list)
        + _flatten_rule_items(preferred_list)
        + _flatten_rule_items(pattern_list)
        + _flatten_rule_items(pet_peeves)
    )

    avoid_long = any("long paragraph" in item.lower() for item in _flatten_rule_items(avoid_list))
    avoid_preamble = _contains_any(
        profile_text,
        (
            "lead with answer",
            "lead with the answer",
            "no preamble",
            "avoid preamble",
            "filler",
        ),
    )
    avoid_restatement = _contains_any(
        profile_text,
        (
            "restating the question",
            "restate the question",
            "restating",
            "repeat the question",
        ),
    )

    # mode: "log" (default — append violations and exit clean) or "block"
    # (emit decision:block / decision:deny so the provider re-invokes the
    # agent with the violation reason as feedback). Opt-in via env var to
    # avoid a profile-schema bump; the loop guard (stop_hook_active) caps
    # redraft cost at one per turn.
    raw_mode = (os.environ.get("OKURO_PROFILE_HOOK_MODE") or "log").strip().lower()
    mode = raw_mode if raw_mode in ("log", "block") else "log"

    # block_detectors: per-detector enforcement, independent of the global
    # `mode`. A named detector blocks even while everything else only logs.
    #
    # WHY PER-DETECTOR AND NOT A GLOBAL FLIP (adversarial review, 2026-07-29).
    # Blocking is only honest where the detector is MECHANICAL — where the
    # violation is a fact about the text, not a judgement about its content.
    # `show_ids` and `long_paragraph` qualify: a UUID is present or it is not,
    # a paragraph is 5 sentences or it is not, and neither can be satisfied by
    # rewording around the check.
    #
    # `avoid_preamble` is deliberately NOT a default: its regex matches an
    # opening word, so blocking it teaches "open with a different word"
    # rather than "lead with the answer" — enforcement on a proxy.
    #
    # The same reasoning killed a proposed `require_systemic_frame` detector
    # (block a recommendation that lacks class-level language). The block
    # reason is fed back to the model, which leaks the phrase list, so the
    # cheapest way to pass is to sprinkle the phrases in. That converts a
    # working measurement into a 100% pass rate and no behaviour change.
    # Do not add content-judgement detectors to this list.
    #
    # DERIVED, not hand-listed, since 2026-07-29: the membership decision lives
    # in _DETECTOR_VALIDITY so it reaches the advisory failure-modes block too.
    # It reached only this consumer before, which is how `avoid_preamble` was
    # excluded here and simultaneously promoted as the agent's top failure mode
    # in every turn context.
    # THE FORCED REWRITE IS OFF UNLESS THE PROFILE ASKS FOR IT.
    #
    # What this switch controls, precisely: whether a detected violation makes
    # the Stop hook REFUSE the finished answer and demand a re-emit. The check
    # and the violations log are unaffected either way -- only the refusal.
    #
    # The owner, 2026-09-09: "Answering then stoping then rewriting the answer
    # via the stop-hook is stupid." and "Force rewrite needs to stay off."
    #
    # WHY IT IS A PROFILE FIELD AND NOT AN ENV VAR. It WAS an env var, and that
    # made it unholdable: the variable exists only for the process that sets
    # it, this function then writes the rules cache without it, and the next
    # `install_hooks()` -- which the auto-deploy runs on every commit touching
    # src/ -- regenerates the cache with blocking back ON. Measured the same
    # day: the switch was set, a commit deployed, and the cache came back armed
    # with no error and no log line. A switch a deploy silently reverses is the
    # same silent-absence class as a gate rule that reaches one provider.
    #
    # The profile is the only store that survives a regeneration, because the
    # regeneration reads FROM it.
    #
    # DEFAULT OFF is the deliberate choice, not an accident of an absent key.
    # A refusal spends the user's turn and hands them a second answer they did
    # not ask for; that cost needs an explicit yes. The budget is now STATED in
    # the directive and in the per-turn block, so the rule is visible before
    # drafting rather than enforced after it.
    enforce = _profile_enforces_rewrite(comm)
    raw_block = os.environ.get("OKURO_PROFILE_HOOK_BLOCK_DETECTORS")
    if raw_block is not None:
        block_detectors = [
            part.strip() for part in raw_block.split(",") if part.strip()
        ]
    elif enforce:
        block_detectors = list(blockable_detectors())
    else:
        block_detectors = []

    return {
        "enabled": True,
        "mode": mode,
        "block_detectors": block_detectors,
        "section_numbering": info.get("section_numbering"),
        "show_ids": info.get("show_ids"),
        "items_per_level": layout.get("items_per_level"),
        "avoid_long_paragraphs": avoid_long,
        "avoid_preamble": avoid_preamble,
        "avoid_question_restatement": avoid_restatement,
        "max_sentences_per_paragraph": 4,
        "long_paragraph_min_chars": 400,
        # Provisional, log-only. Derived from the one labelled session where
        # the user named seven replies as failures: their prose length ran
        # 2,032-3,560 chars and NO existing detector fired on any of them.
        # Raising this to a block threshold needs kill criteria and a week of
        # logs, and reply_volume is absent from blockable_detectors() so it
        # cannot become a refusal by an env flip.
        # DERIVED from response_length, not hardcoded. The number the gate
        # counts against is now the same number the agent is told, so crossing
        # it is a mistake the agent can see coming instead of one it can only
        # discover by being refused.
        "reply_volume_min_chars": response_budget(profile),
        "violations_log_path": _violations_log_path(),
    }


#: The anchor the packet stamps on the full contract and the per-turn block
#: cites. One literal, so the pointer cannot name a section that moved.
COMM_ANCHOR = "§COMM"

#: Hard cap on the compact block. The per-turn channel fires before EVERY
#: draft, so its cost is paid once per turn for the life of the session — the
#: opposite of the packet, which is paid once. Anything that does not change
#: what the next reply looks like does not belong here.
_COMPACT_MAX_RULES = 5
_COMPACT_MAX_CHARS = 620


def build_comm_compact(profile: dict | None = None) -> str:
    """The COGNITIVE contract, compressed for the per-turn channel.

    WHY THIS EXISTS, measured 2026-09-09 by null test: ``build_comm_directive``
    reads ONE of thirteen profile sections and never touches
    ``cognitive_style``. Feeding it the real profile, a profile with
    cognitive_style deleted, and a profile with the neurotype INVERTED
    ("prefers long prose, enjoys repetition, wants open questions") produced
    byte-identical output. So the two channels that fire on every turn — the
    system-prompt output style and this pre-draft hook — carried a generic
    style guide, while everything that actually describes the user reached the
    bootstrap packet ONCE and was never repeated.

    That is the wrong way round. The packet is read once and competes with
    ~23k chars; the per-turn block is the only channel that can shape a reply
    before it is drafted. The deepest part of the profile was in the weakest
    place.

    WHAT COMPRESSES. The packet's Behavioral Contract renders each rule with
    its RATIONALE ("monotropic attention locks onto one thread; overflow breaks
    focus"). The rationale is what makes a rule persuasive on first read and
    what makes it expensive on the fiftieth. This block keeps the rule and
    drops the rationale, then points at the anchor where the full form lives —
    so the model can re-read the reasoning without it being re-sent every turn.

    GENERIC BY CONSTRUCTION. Every line derives from the profile:
    ``cognitive_style.neurotype``, ``cognitive_style.implications``,
    ``communication.response_length`` and ``communication.anger_protocol``.
    A user with no ``cognitive_style`` gets the formatting rules alone; a user
    with a different neurotype gets different rules. Nothing here is written
    for one person, which is the test ``test_comm_compact_is_generic`` pins.
    """
    if profile is None:
        try:
            from okuro.yu.profile import get_profile_raw
            profile = get_profile_raw()
        except Exception:
            profile = None
    if not isinstance(profile, dict) or not profile:
        return ""

    cog = profile.get("cognitive_style") if isinstance(profile.get("cognitive_style"), dict) else {}
    comm = profile.get("communication") if isinstance(profile.get("communication"), dict) else {}

    # --- headline: who you are talking to, in one line -------------------
    bits: list[str] = []
    neuro = _flatten_rule_items(cog.get("neurotype"))
    if neuro:
        bits.append("/".join(neuro[:2]))
    rl = comm.get("response_length")
    if isinstance(rl, str) and rl.strip():
        # The BUDGET travels with the word. The per-turn block is the only
        # profile text present before the agent drafts, so if the number is
        # not here the agent cannot aim at it -- it can only be told, after
        # the fact, that it missed.
        budget = RESPONSE_LENGTH_BUDGETS.get(rl.strip().lower())
        bits.append(f"{rl.strip()} (max {budget} prose chars)" if budget else rl.strip())
    abstraction = cog.get("abstraction")
    if isinstance(abstraction, str) and abstraction.strip():
        bits.append(abstraction.strip())

    # --- the rules that change the SHAPE of a reply ----------------------
    # `implications` is the profile's own list of what the neurotype means in
    # practice. Rules are taken in declared order: the profile author put the
    # load-bearing ones first, and re-ranking here would silently override an
    # editorial decision made in the profile.
    rules: list[str] = []
    seen: set[str] = set()
    for text in _flatten_rule_items(cog.get("implications")):
        # Keep the RULE, drop a trailing rationale clause. The packet carries
        # the full sentence; this channel carries the instruction.
        rule = text.split(" — ")[0].split(" - ")[0].strip().rstrip(".")
        key = rule.lower()
        if not rule or key in seen:
            continue
        seen.add(key)
        rules.append(rule)
        if len(rules) >= _COMPACT_MAX_RULES:
            break

    # --- the conditional protocol, if the profile declares one -----------
    anger = comm.get("anger_protocol") if isinstance(comm.get("anger_protocol"), dict) else {}
    anger_line = ""
    trigger = anger.get("trigger")
    do_items = _flatten_rule_items(anger.get("do"))
    if isinstance(trigger, str) and trigger.strip() and do_items:
        anger_line = f"If frustrated ({trigger.strip()}): " + ", ".join(do_items[:3]) + "."

    # THE BLOCK MUST EARN ITS PLACE, because emitting it SUPPRESSES the full
    # directive in the per-turn channel. A profile with no cognitive_style but
    # a `response_length` produced a two-line stub — "Reader: concise." plus a
    # pointer — which replaced "lead with answer / consistent format / use
    # bullets and tables". That user got LESS than before the change.
    #
    # So the bar is COGNITIVE content: a neurotype or at least one implication.
    # `response_length` and `abstraction` decorate the headline; they cannot
    # justify the block on their own. Below the bar this returns "" and the
    # caller falls back to the directive, which is the right answer for a user
    # who never filled the section in.
    if not rules and not neuro:
        return ""

    lines = [f"{COMM_ANCHOR} — how this user needs to be answered"]
    if bits:
        lines.append("Reader: " + " · ".join(bits) + ".")
    for rule in rules:
        lines.append(f"- {rule}")
    if anger_line:
        lines.append(anger_line)
    lines.append(
        f"Full contract with reasoning: {COMM_ANCHOR} in your bootstrap packet."
    )

    out = "\n".join(lines)
    if len(out) > _COMPACT_MAX_CHARS:
        out = out[: _COMPACT_MAX_CHARS - 3].rstrip() + "..."
    return out


def build_profile_turn_context() -> str:
    """Render concise per-turn profile guidance for prompt-injection hooks.

    Splits the user's profile into two explicit blocks so anti-patterns
    (pet_peeves, avoid-rules, hates) cannot be misread as positive directives
    when injected into a model's pre-turn context. Layout::

        Okuro user profile for this turn:
        Do:
        - lead with answer
        - tables
        Avoid:
        - long paragraphs
        - preamble
    """
    try:
        from okuro.yu.profile import get_profile_raw
        profile = get_profile_raw()
    except Exception:
        return ""

    if not isinstance(profile, dict) or not profile:
        return ""

    comm = profile.get("communication") if isinstance(profile.get("communication"), dict) else {}
    fmt = comm.get("format_preferences") if isinstance(comm.get("format_preferences"), dict) else {}
    work = profile.get("work_style") if isinstance(profile.get("work_style"), dict) else {}

    do_candidates: list[str] = []
    do_candidates.extend(_flatten_rule_items(comm.get("patterns")))
    do_candidates.extend(_flatten_rule_items(fmt.get("preferred", fmt.get("prefer"))))

    # Order: structural avoid-rules first (most actionable per-turn — "no
    # long paragraphs", "no preamble"), then pet_peeves and hates which
    # are broader behavioral signals. The cap then keeps the structural
    # signals visible even when pet_peeves/hates lists are long.
    avoid_candidates: list[str] = []
    avoid_candidates.extend(_flatten_rule_items(fmt.get("avoid")))
    avoid_candidates.extend(_flatten_rule_items(comm.get("pet_peeves")))
    avoid_candidates.extend(_flatten_rule_items(work.get("hates")))

    def _dedupe(items: list[str], cap: int) -> list[str]:
        out: list[str] = []
        seen: set[str] = set()
        for item in items:
            text = " ".join(item.split()).rstrip(".")
            key = text.lower()
            if not text or key in seen:
                continue
            seen.add(key)
            out.append(text)
            if len(out) >= cap:
                break
        return out

    do_bullets = _dedupe(do_candidates, cap=4)
    # NOT cap=4. The profile carried five avoid items and the fifth --
    # "complicated language" -- was dropped before it ever reached a turn.
    # A cap that silently discards the newest entry makes a profile edit look
    # like it did not take. The whole list is short by construction; the 1400
    # char clamp below is the real budget and it reports nothing silently
    # either, because `Avoid:` is the last thing added before it.
    avoid_bullets = _dedupe(avoid_candidates, cap=8)

    # Communication directive: same source as the Claude Code output-style
    # body, appended LAST so recency lands on the answer-first contract.
    directive = build_comm_directive(profile)

    # The COGNITIVE half — who the reader is, not just how to format for them.
    compact = build_comm_compact(profile)

    # THE `Do:` BLOCK IS DROPPED WHEN THE DIRECTIVE IS PRESENT, because they
    # are the same content twice. Both read `communication.patterns` and
    # `format_preferences.preferred`: `Do:` rendered them as bare bullets and
    # `build_comm_directive` rendered them as imperatives, so this channel
    # opened and closed with "lead with answer / consistent format / bullets /
    # tables". Measured on the live profile: ~35 tokens of pure repetition, in
    # the one channel that pays its cost on EVERY turn — and repetition is the
    # user's own first-listed pet peeve. The freed budget is what pays for the
    # cognitive block; net cost is roughly flat.
    #
    # `Avoid:` stays. It is the only place the anti-patterns appear, and the
    # split exists so a pet peeve is never misread as a positive directive.
    if not do_bullets and not avoid_bullets and not directive and not compact:
        return ""

    lines = ["Okuro user profile for this turn:"]
    if compact:
        lines.append(compact)
        lines.append("")
    elif do_bullets:
        lines.append("Do:")
        lines.extend(f"- {b}" for b in do_bullets)
    if avoid_bullets:
        lines.append("Avoid:")
        lines.extend(f"- {b}" for b in avoid_bullets)

    failure_modes = build_recent_failure_modes_block()
    if failure_modes:
        lines.append("")
        lines.append(failure_modes)

    context = "\n".join(lines)
    if len(context) > 1400:
        context = context[:1397].rstrip() + "..."

    # Append AFTER truncation so the closing instruction is never clipped — it
    # must be intact and last, the final thing the model reads before drafting.
    #
    # WHEN THE COMPACT BLOCK IS PRESENT, ONLY THE CLOSER FOLLOWS. The full
    # directive re-states `patterns` + `preferred` + `response_length`, which
    # the compact block already carries as "structured output over prose" and
    # "needs short, focused communication blocks". Shipping both put the same
    # instruction in this channel three times (Do: bullets, compact rules,
    # directive bullets) — measured at 1185 chars, LARGER than the 1063 it
    # replaced, which is the opposite of the point.
    #
    # The closer survives alone because its value is positional, not
    # informational: it is the last line before drafting, and recency is what
    # makes "first token = the answer" land. The output style keeps the full
    # directive — that channel has no compact block to duplicate.
    if compact:
        context = context + "\n\n" + COMM_DIRECTIVE_CLOSER
    elif directive:
        context = context + "\n\n" + directive if context else directive
    return context


def write_profile_hook_rules() -> str:
    """Atomically write the compliance-rules cache to ``~/.okuro/profile-hook-rules.json``.

    Hooks no-op when the cache is missing, so a write failure here
    degrades gracefully (advisory absent, no false blocks).
    """
    rules = extract_profile_compliance_rules()
    return atomic_write_json(_rules_path(), rules)


def write_profile_turn_context() -> str:
    """Atomically write concise profile context for per-turn prompt hooks."""
    context = build_profile_turn_context()
    return atomic_write_text(
        _turn_context_path(),
        (context + "\n") if context else "",
    )


__all__ = [
    "COMM_DIRECTIVE_CLOSER",
    "atomic_write_json",
    "atomic_write_text",
    "build_comm_directive",
    "build_profile_turn_context",
    "build_recent_failure_modes_block",
    "extract_profile_compliance_rules",
    "write_profile_hook_rules",
    "write_profile_turn_context",
]
