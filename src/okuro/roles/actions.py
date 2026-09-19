# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: The seven-state machine over `role_structure_actions` — the gates
#   that decide whether a structural change may move, written as data and
#   enforced in code rather than asked for in a prompt.
# index:
#   STATES | TRANSITIONS | ARTIFACT_FOR_STATE | AGENT_REACHABLE_STATES
#   class ActionRefused
#   def known_elements
#   def measurable_labels / resolve_measurable_label / role_carries_element
#   def resolve_element
#   def human_actors
#   def propose
#   def _re_anchor
#   def transition
#   def _fenced_blocks / _json_objects / report_findings
#   def lift_findings_from_report
#   def list_actions / count_actions
#   def get_action
#   def action_events
#   def claim_dispatch / release_dispatch / open_dispatch_claim
# AGENT_HEADER_END -->
"""What has to be TRUE before a structural change is allowed to move.

The workstream this closes has one recorded failure mode and it is not
laziness. okuro's agent-honesty controls ship as PROMPT TEXT — "fetch every
source in-run and quote the sentence" is an acceptance criterion a critic
judges by reading the agent's own report, which is exactly the artifact a
fabricating agent controls. Three role-refresh fabrications each passed their
own acceptance criteria.

Migration 157 answered that for the QUOTE: the body is stored, and
``verify_quote`` is a substring assert. This module answers it for the
WORKFLOW. Every gate below is a Python condition over a stored row, so the
question "may this move" has an answer that does not depend on anybody's
good faith:

======================  ==========================================================
Transition              What must already exist
======================  ==========================================================
→ researched            ``research_artifact_id``
→ planned               ``plan_artifact_id``
→ critiqued             ``critique_artifact_id``
→ approved              a HUMAN actor, and the source's hash unmoved since proposal
→ implemented           ``migration_id`` or a diff/evidence artifact id
→ verified              a fit snapshot under the SAME rubric, structure not lower
→ rejected / superseded a reason
======================  ==========================================================

Three KINDS of row, and each one's proposal gate is a different code
condition over stored data:

====================  ====================================================
kind                  What `propose` proves before it writes
====================  ====================================================
``finding``           the quoted sentence is IN the body that run fetched
``source_health``     the poller's own alarm; deduplicated per source
``internal``          okuro measuring itself: the evidence artifact exists
                      and every affected role is re-measured to still fail
                      the element the finding names
====================  ====================================================

``internal`` exists because the quote gate has no answer when the subject is
okuro. "The structure gate stopped counting markers inside a fenced code
block" is on no vendor page, so until it existed an audit okuro ran on its own
store had nowhere to go but prose.

Nothing here writes a `roles` row. This is the backlog, not the pipeline —
P5 owns the write path and reads these rows to know what it was told to do.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone

from okuro.roles._time import now_iso

logger = logging.getLogger("okuro.roles.actions")


# ── The machine, as data ─────────────────────────────────────────────

#: Every state a row may hold. Mirrors the CHECK constraint in migration 158;
#: a mismatch between the two is a bug in one of them, and the test asserts
#: they agree rather than trusting that they do.
STATES: tuple[str, ...] = (
    "proposed",
    "researched",
    "planned",
    "critiqued",
    "approved",
    "implemented",
    "verified",
    "restored",
    "rejected",
    "superseded",
)

#: States nothing moves out of.
#:
#: `restored` is here because a reversal ENDS the action. The alternative — a
#: restored row walking back to `approved` for another attempt — would reuse
#: the approval of a change that has since been taken out, which is the same
#: mistake the hash-drift re-anchor exists to prevent one state earlier. A new
#: attempt is a new action, and it costs what a new action costs.
TERMINAL_STATES: frozenset[str] = frozenset(
    {"verified", "restored", "rejected", "superseded"})

#: A source_health row stops deduplicating once it reaches one of these — the
#: same set as TERMINAL_STATES, and the same set the partial UNIQUE index in
#: migration 158 excludes. Named separately because the index cannot import
#: this and the test asserts the two agree.
CLOSED_STATES: frozenset[str] = TERMINAL_STATES

#: The allowed moves. Two of them look odd and both are load-bearing.
#:
#: `critiqued → researched` is what a refused approval does when the source
#: moved under the quote. A transition the machine performs but the table does
#: not list is a machine the table does not describe.
#:
#: `researched → researched` is the RE-ANCHOR, and it is the only way out of
#: the drift. See :func:`_re_anchor`.
TRANSITIONS: dict[str, tuple[str, ...]] = {
    "proposed": ("researched", "rejected", "superseded"),
    "researched": ("researched", "planned", "rejected", "superseded"),
    "planned": ("critiqued", "rejected", "superseded"),
    "critiqued": ("approved", "researched", "rejected", "superseded"),
    "approved": ("implemented", "rejected", "superseded"),
    # `→ restored` is the reversal, and it is reachable from BOTH written
    # states. A verified change can be wrong — verification says the structure
    # score did not drop, which is a floor and not a judgement — so the way
    # back may not stop being available the moment the measurement passes.
    "implemented": ("verified", "restored", "rejected", "superseded"),
    "verified": ("restored",),
    "restored": (),
    "rejected": (),
    "superseded": (),
}

#: Which column has to be non-empty before the move, per target state. The
#: value is BOTH the gate and the destination: the id the caller passes is
#: written into this column, so a state and its evidence arrive together or
#: neither does.
ARTIFACT_FOR_STATE: dict[str, str] = {
    "researched": "research_artifact_id",
    "planned": "plan_artifact_id",
    "critiqued": "critique_artifact_id",
}

#: The states an MCP-reachable agent may request. `approved` is absent and
#: that absence is the point — see the migration header on why the human gate
#: is fenced three times.
#: `restored` is absent alongside `approved`, for a related reason: reversing
#: a change the owner approved is not an agent's call either. The CLI command
#: reaches it, and the tier matrix classifies the verb that would destructive.
AGENT_REACHABLE_STATES: frozenset[str] = frozenset(
    {"researched", "planned", "critiqued", "implemented", "verified",
     "rejected", "superseded"}
)

#: Reaching one of these is a DECISION and stamps `decided_by`/`decided_at`.
#: The rest are progress, and progress has an event row rather than a
#: signature.
DECISION_STATES: frozenset[str] = frozenset({"approved", "rejected"})

#: Every kind a row may hold. Mirrors the CHECK constraint in migration 163,
#: and it is ONE tuple because it used to be four literals.
#:
#: THE CLASS THIS FIXES. `internal` was added to `propose`'s allowlist and the
#: readers kept their own copies: `list_actions` and `count_actions` each
#: refused `kind='internal'` as "not a kind", and the MCP list verb's enum did
#: not offer it. The row could be opened and then not listed, not counted and
#: not filtered — invisible in the panel the approval happens in. A kind that
#: only the writer knows about is worse than no kind.
KINDS: tuple[str, ...] = ("finding", "source_health", "internal")

#: The kinds an MCP-reachable agent may PROPOSE. `source_health` is absent for
#: the reason the MCP dispatch states: an agent that could mint one could
#: report a feed as broken without ever fetching it.
AGENT_PROPOSABLE_KINDS: tuple[str, ...] = ("finding", "internal")

#: The three fidelity grades, accepted as an `okuro_element` in their own
#: right — "the micro grade" is a real thing a finding can bear on.
GRADES: tuple[str, ...] = ("full", "lean", "micro")

#: The `designer` constants a finding may name. Taken from the module at call
#: time rather than hard-coded, so a constant that is renamed there stops
#: validating here instead of silently accepting a name that no longer exists.
DESIGNER_CONSTANTS: tuple[str, ...] = (
    "FULL_REQUIRED",
    "FULL_RECOMMENDED",
    "LEAN_REQUIRED",
    "LEAN_RECOMMENDED",
    "MICRO_REQUIRED_KEYS",
    "MICRO_RECOMMENDED_KEYS",
    "TRAIT_AXES",
)


class ActionRefused(ValueError):
    """A gate said no. Nothing was written.

    Deliberately one exception rather than a family: every caller — the MCP
    verb, the API route, the lifter — does the same thing with it, which is
    hand the message back verbatim. A refusal whose text does not say what
    failed is a refusal the agent will retry identically.
    """


# ── What counts as an okuro element ──────────────────────────────────


def known_elements(db) -> dict[str, str]:
    """Every name a finding is allowed to put in ``okuro_element``.

    Returns ``{normalised name: what kind of thing it is}``.

    Three populations, and all three are read from the live system rather than
    listed here:

    * the `designer` module's gating constants and the section LABELS they
      produce — a finding about "the Traits table" names a thing that either
      is or is not in ``FULL_REQUIRED`` today;
    * the three fidelity grades;
    * the `roles` table's own columns, via ``PRAGMA table_info``.

    The reason this is a lookup and not a regex: AC4 says a finding must name
    the okuro element it touches, and the recorded way that criterion fails is
    a plausible-sounding name for something that does not exist. A validator
    that accepts any dotted identifier accepts every one of those.
    """
    from okuro.roles import designer

    out: dict[str, str] = {}

    for const in DESIGNER_CONSTANTS:
        if hasattr(designer, const):
            out[const.lower()] = "designer constant"

    # The section labels themselves — "EXPERTISE", "COGNITIVE PROFILE",
    # "micro key 'purpose'". These are what validate_role_structure reports as
    # missing, so they are the vocabulary a repair would be written in.
    for grade, labels in designer.REQUIRED_LABELS.items():
        for label in labels:
            out[label.strip().lower()] = f"{grade} required section"
    for label, _pattern in designer.FULL_RECOMMENDED:
        out.setdefault(label.strip().lower(), "full recommended section")
    for label, _pattern in designer.LEAN_RECOMMENDED:
        out.setdefault(label.strip().lower(), "lean recommended section")
    for key in designer.MICRO_RECOMMENDED_KEYS:
        out.setdefault(designer.micro_label(key).lower(), "micro recommended key")

    for grade in GRADES:
        out.setdefault(grade, "fidelity grade")

    for row in db.fetchall("PRAGMA table_info(roles)"):
        name = str(row["name"]).strip().lower()
        out.setdefault(name, "roles column")
        out.setdefault(f"roles.{name}", "roles column")

    return out


#: Element kinds a ROLE can be measured against, one role at a time. An
#: internal finding is refused unless its element is one of these, and the
#: reason is the gate that comes after: every affected role is re-measured to
#: prove the finding reproduces. A `roles` column or a designer constant gives
#: that gate nothing to recompute — "FULL_REQUIRED is wrong" is a claim about
#: okuro, not a defect present in one role and absent in another.
MEASURABLE_ELEMENT_KINDS: frozenset[str] = frozenset({
    "full required section",
    "lean required section",
    "micro required key",
    "full recommended section",
    "lean recommended section",
    "micro recommended key",
})


def measurable_labels() -> dict[str, tuple[str, str]]:
    """``{normalised name: (exact label, register)}`` for every section label.

    ``register`` is ``"gating"`` when the label is in a grade's REQUIRED set
    and ``"advisory"`` when it is only RECOMMENDED. Both are measurable and
    both are legitimate subjects for a repair; they are not the same claim,
    so the verdict says which one it found.

    Read from the live ``designer`` constants for the reason
    :func:`known_elements` reads them: a label that is renamed there must stop
    validating here rather than keep matching a string nobody updated.
    """
    from okuro.roles import designer

    out: dict[str, tuple[str, str]] = {}
    for _grade, labels in designer.REQUIRED_LABELS.items():
        for label in labels:
            out[label.strip().lower()] = (label, "gating")
    for label, _pattern in designer.FULL_RECOMMENDED:
        out.setdefault(label.strip().lower(), (label, "advisory"))
    for label, _pattern in designer.LEAN_RECOMMENDED:
        out.setdefault(label.strip().lower(), (label, "advisory"))
    for key in designer.MICRO_RECOMMENDED_KEYS:
        label = designer.micro_label(key)
        out.setdefault(label.lower(), (label, "advisory"))
    return out


def resolve_measurable_label(value) -> tuple[str, str] | None:
    """The exact section label ``value`` names, and its register, or None.

    Accepts the two shapes :func:`resolve_element` accepts — a bare label and
    a dotted path whose last segment is the name — for the same reason: the
    question is whether this names a thing that exists, not whether it is
    spelled the way this module would spell it.
    """
    if not value or not str(value).strip():
        return None
    text = str(value).strip().lower()
    table = measurable_labels()
    if text in table:
        return table[text]
    return table.get(text.rsplit(".", 1)[-1].strip())


def role_carries_element(db, role_id: str, element) -> dict:
    """Re-measure ONE role against ONE section label. Reads, never trusts.

    Returns ``{"exists": bool, "carries": bool, "label": str,
    "register": str, "missing_in": [grade, ...]}``.

    ``carries`` is False when the label is reported missing (gating) or
    advisory-missing for at least one grade the role actually has — which is
    the same computation ``audit_structure`` prints and the fit scorer's
    structure segment reads, because it is the same function.
    """
    from okuro.roles.designer import validate_role_structure

    found = resolve_measurable_label(element)
    if not found:
        return {"exists": False, "carries": False, "label": None,
                "register": None, "missing_in": []}
    label, register = found

    row = db.fetchone(
        "SELECT prompt, lean_prompt, micro_prompt FROM roles WHERE role_id = ?",
        (role_id,),
    )
    if not row:
        return {"exists": False, "carries": False, "label": label,
                "register": register, "missing_in": []}

    content = {
        "full": row["prompt"] or "",
        "lean": row["lean_prompt"] or "",
        "micro": row["micro_prompt"] or "",
    }
    verdict = validate_role_structure(content)
    missing_in = [
        grade
        for grade in ("full", "lean", "micro")
        for bucket in (verdict["missing"], verdict["advisory"])
        if label in (bucket.get(grade) or [])
    ]
    return {
        "exists": True,
        "carries": not missing_in,
        "label": label,
        "register": register,
        "missing_in": sorted(set(missing_in)),
    }


def resolve_element(db, value) -> str | None:
    """What kind of okuro element this names, or None if it names nothing.

    Two shapes are accepted because the role body's FINDING SCHEMA emits the
    second one: a bare label (``EXPERTISE``, ``tier``) and a dotted path whose
    last segment is the name (``roles.designer.FULL_REQUIRED``). Matching on
    the last segment is deliberately generous — the question this gate answers
    is "does this name a thing that exists", not "is this spelled the way I
    would have spelled it".
    """
    if not value or not str(value).strip():
        return None
    text = str(value).strip().lower()
    table = known_elements(db)
    if text in table:
        return table[text]
    tail = text.rsplit(".", 1)[-1].strip()
    return table.get(tail)


# ── Who is the human ─────────────────────────────────────────────────


def human_actors() -> frozenset[str]:
    """The actor strings ``→ approved`` will accept: the profile handle, only.

    **This used to contain ``user`` and ``human`` unconditionally, and that was
    the human gate being no gate at all.** The reasoning was that the API route
    had already established the caller was on loopback behind the bearer, so it
    had no name to pass on and a generic one would do. What it actually meant
    is that reaching the route WAS the whole gate: the bearer is one shared
    token in the keyring, so anything on loopback that can read the keyring
    could post ``{"actor": "user"}`` and approve a change to okuro's canon. The
    two names that were meant to stand in for "a person did this" were the two
    names any process could type.

    So the allowlist is the profile's handle and nothing else, and the route
    DERIVES the actor from it rather than accepting one. There is no generic
    stand-in left to claim.

    It is an allowlist rather than a blocklist of agent-shaped names ("claude",
    "codex", "orchestrator", …) because such a list is wrong the first time a
    provider ships under a name nobody thought of, and wrong in that direction
    means an agent approved.

    **Fails CLOSED.** An unreadable profile, or one with no handle, returns the
    empty set and approval becomes impossible. A gate that opens when it cannot
    read its own allowlist is not a gate.
    """
    names: set[str] = set()
    try:
        from okuro.yu.profile import get_profile_raw

        identity = (get_profile_raw() or {}).get("identity") or {}
        handle = identity.get("handle")
        # Singular in every profile today; a list is accepted so a second
        # handle does not have to become a second code path.
        candidates = handle if isinstance(handle, (list, tuple)) else [handle]
        for value in candidates:
            if isinstance(value, str) and value.strip():
                names.add(value.strip().lower())
    except Exception as exc:  # noqa: BLE001 — an unreadable profile is not an approval
        logger.warning("could not read the profile for the human allowlist: %s", exc)
    return frozenset(names)


def _is_human(actor) -> bool:
    return bool(actor) and str(actor).strip().lower() in human_actors()


# ── Small helpers ────────────────────────────────────────────────────


#: One clock for the roles subsystem — see ``roles/_time.py``. ``write.py``
#: carried a byte-identical private copy; both write the same columns.
_now = now_iso


def _short(digest) -> str:
    """A hash, short enough to read in a sentence.

    A refusal message carrying two full sha256s is a refusal nobody reads to
    the end, and the point of the message is that somebody reads it.
    """
    if not digest:
        return "none"
    text = str(digest)
    return text[:12] + "…" if len(text) > 12 else text


def _role_ids_json(value) -> str:
    """Normalise ``affected_role_ids`` to a JSON list of strings.

    Refuses an integer outright. "34 roles affected" is the shape of claim the
    whole workstream exists to stop, and accepting it here — even helpfully,
    even by rendering it as a string — is how it gets back in.
    """
    if value is None:
        return "[]"
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        raise ActionRefused(
            "affected_role_ids must be an explicit list of role ids, never a "
            f"count — got {value!r}"
        )
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except ValueError:
            parsed = [value]
        value = parsed
    if not isinstance(value, (list, tuple, set)):
        raise ActionRefused(
            f"affected_role_ids must be a list of role ids, got {type(value).__name__}"
        )
    ids = [str(v).strip() for v in value if str(v).strip()]
    return json.dumps(ids)


def _row(db, action_id: str) -> dict:
    row = db.fetchone(
        "SELECT * FROM role_structure_actions WHERE id = ?", (action_id,)
    )
    if not row:
        raise ActionRefused(f"no structure action with id {action_id!r}")
    return dict(row)


def _append_event(db, action_id, from_state, to_state, actor, reason) -> None:
    db.execute(
        "INSERT INTO role_structure_action_events "
        "(id, action_id, from_state, to_state, actor, reason, at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            str(uuid.uuid4()),
            action_id,
            from_state,
            to_state,
            actor,
            reason,
            _now(),
        ),
    )


#: Columns that are documents, not fields. They are STRIPPED from every
#: response unless the caller asks for them by name.
#:
#: WHY. `plan_json` holds the operations and six bodies per affected role, and
#: `fit_baseline` a whole `compute_fit` result per role. A twelve-role action
#: carries a plan in the hundreds of kilobytes. `_row` is `SELECT *` and every
#: transition, every list and every MCP verb returned it — so a panel listing
#: forty actions shipped forty plans it never renders, and an agent calling
#: `roles_actions_list` spent its context on bodies it did not ask for.
#:
#: Stripped in `_hydrate` rather than by narrowing the SELECT, because the
#: gates read these columns: `verify` needs `fit_baseline` off the same row it
#: validates. One read, one shape, and the trimming happens where the row
#: becomes a response.
HEAVY_COLUMNS: frozenset[str] = frozenset({"plan_json", "fit_baseline"})


def _hydrate(row: dict, *, heavy: bool = False) -> dict:
    """Row → API/MCP shape. Parses the JSON list, never a count.

    ``heavy`` keeps :data:`HEAVY_COLUMNS`. Only the detail path passes it, and
    only when explicitly asked.
    """
    out = dict(row)
    if not heavy:
        for column in HEAVY_COLUMNS:
            out.pop(column, None)
    try:
        out["affected_role_ids"] = json.loads(out.get("affected_role_ids") or "[]")
    except (TypeError, ValueError):
        out["affected_role_ids"] = []
    out["quote_verified"] = bool(out.get("quote_verified"))
    return out


# ── propose ──────────────────────────────────────────────────────────


def propose(
    db,
    *,
    kind: str,
    title: str,
    created_by: str,
    source_id: str | None = None,
    run_id: str | None = None,
    evidence_url: str | None = None,
    quoted_sentence: str | None = None,
    okuro_element: str | None = None,
    affected_role_ids=None,
    research_artifact_id: str | None = None,
    evidence_artifact_id: str | None = None,
    reason: str | None = None,
) -> dict:
    """Open an action, or refuse and write nothing.

    **For ``kind='finding'`` the gate is CODE, not a criterion.** Three things
    must hold and all three are checked against stored rows:

    1. ``verify_quote`` returns True for (run_id, source_id, quoted_sentence)
       — the sentence appears in the body THAT RUN fetched. A quote shorter
       than ``source_poll.MIN_QUOTE_WORDS`` is refused before the comparison,
       with its own message, because a substring assert with no floor is a
       rubber stamp: the word "tools" appears in all nine registered bodies.
    2. ``okuro_element`` resolves against the live designer constants, the
       fidelity grades or the `roles` columns. AC4's recorded failure is a
       plausible name for something that does not exist.
    3. The source is in the registry, so ``source_hash_at_proposal`` has a
       value to hold.

    ``source_hash_at_proposal`` and ``rubric_version`` are COMPUTED here and
    are not parameters. A caller that supplies its own reference point has
    supplied the thing the reference point exists to fix.

    ``fetch_id`` likewise: it is resolved from the run's observation row. A
    caller choosing its own fetch_id chooses which body it is judged against.

    For ``kind='source_health'`` the poller is the author and there is no
    quote. It deduplicates: an open row for the same (source, title) is
    RETURNED rather than duplicated, so an alarm that persists for a year is
    one row and not fifty-two.

    **For ``kind='internal'`` there is no source and no quote, and the gate is
    still code.** The subject of an internal finding is okuro's own store, so
    the reference point is RECOMPUTED rather than quoted:

    1. ``evidence_artifact_id`` must name an artifact that exists — the
       measurement, so the reading can be reproduced. Stored in
       ``finding_artifact_id``: ``evidence_artifact_id`` the COLUMN belongs to
       ``implement`` (the pre-change bodies) and would overwrite this at the
       first write, which is the defect migration 160 moved one column over.
    2. ``okuro_element`` must name a section label or micro key — something a
       role can be MEASURED against. A designer constant or a `roles` column
       is refused, because gate 3 would have nothing to recompute.
    3. ``affected_role_ids`` must be explicit and non-empty, and EVERY named
       role is re-measured here. A role that currently CARRIES the element
       refuses the whole proposal: an internal finding that does not
       reproduce against the store does not exist.
    """
    if kind not in KINDS:
        raise ActionRefused(
            f"kind must be one of {', '.join(repr(k) for k in KINDS)}, "
            f"got {kind!r}"
        )
    if not title or not str(title).strip():
        raise ActionRefused("title must be non-empty")
    if not created_by or not str(created_by).strip():
        raise ActionRefused("created_by must name who is proposing this")

    title = str(title).strip()
    affected = _role_ids_json(affected_role_ids)

    source = None
    if source_id:
        source = db.fetchone(
            "SELECT id, url, last_hash FROM role_structure_sources WHERE id = ?",
            (source_id,),
        )

    if kind == "source_health":
        if not source:
            raise ActionRefused(
                f"source_health needs a registered source; {source_id!r} is not "
                f"in role_structure_sources"
            )
        placeholders = ", ".join("?" for _ in CLOSED_STATES)
        open_row = db.fetchone(
            f"SELECT * FROM role_structure_actions "
            f"WHERE kind = 'source_health' AND source_id = ? AND title = ? "
            f"AND state NOT IN ({placeholders}) LIMIT 1",
            (source_id, title, *sorted(CLOSED_STATES)),
        )
        if open_row:
            return _hydrate(dict(open_row))

    if kind == "finding":
        from okuro.roles.fit import rubric_version
        from okuro.roles.source_poll import MIN_QUOTE_WORDS, normalise, verify_quote

        if not run_id or not source_id:
            raise ActionRefused(
                "a finding must name the run and the source its quote came "
                "from — without both there is no stored body to check it "
                "against, and an unverifiable finding does not exist"
            )
        if not source:
            raise ActionRefused(
                f"source {source_id!r} is not in role_structure_sources"
            )
        if not quoted_sentence or not str(quoted_sentence).strip():
            raise ActionRefused(
                "a finding must quote the sentence that carries the change"
            )
        words = len(normalise(quoted_sentence).split())
        if words < MIN_QUOTE_WORDS:
            raise ActionRefused(
                f"the quote is {words} word(s); {MIN_QUOTE_WORDS} is the floor. "
                f"A substring assert with no floor certifies nothing — single "
                f"common words appear in every registered body."
            )
        if not verify_quote(db, run_id, source_id, quoted_sentence):
            raise ActionRefused(
                f"the quoted sentence does not appear in the body run "
                f"{run_id!r} fetched from {source_id!r}. Either it was "
                f"reworded, or that run has no body behind it. A finding that "
                f"cannot pass verify_quote does not exist."
            )
        element_kind = resolve_element(db, okuro_element)
        if not element_kind:
            raise ActionRefused(
                f"okuro_element {okuro_element!r} names nothing that exists. It "
                f"has to be a designer constant, a section label, a fidelity "
                f"grade or a `roles` column — a finding that names none of "
                f"those is knowledge, not structure."
            )

        fetch_row = db.fetchone(
            "SELECT fetch_id FROM source_fetch_runs "
            "WHERE run_id = ? AND source_id = ?",
            (run_id, source_id),
        )
        fetch_id = (fetch_row or {}).get("fetch_id")
        quote_verified = 1
        source_hash = source["last_hash"]
        rubric = rubric_version()
        evidence_url = evidence_url or source["url"]
    elif kind == "internal":
        from okuro.roles.fit import rubric_version

        if not evidence_artifact_id or not str(evidence_artifact_id).strip():
            raise ActionRefused(
                "an internal finding must name the artifact holding the "
                "measurement it rests on — there is no vendor page to quote, "
                "so the reproducible reading IS the evidence"
            )
        evidence_artifact_id = str(evidence_artifact_id).strip()
        artifact = db.fetchone(
            "SELECT id FROM artifacts WHERE id = ?", (evidence_artifact_id,)
        )
        if not artifact:
            raise ActionRefused(
                f"artifact {evidence_artifact_id!r} does not exist. An "
                f"internal finding whose evidence cannot be opened is a claim "
                f"with a citation to nothing."
            )

        element_kind = resolve_element(db, okuro_element)
        if not element_kind:
            raise ActionRefused(
                f"okuro_element {okuro_element!r} names nothing that exists. It "
                f"has to be a designer constant, a section label, a fidelity "
                f"grade or a `roles` column — a finding that names none of "
                f"those is knowledge, not structure."
            )
        if element_kind not in MEASURABLE_ELEMENT_KINDS:
            raise ActionRefused(
                f"okuro_element {okuro_element!r} is a {element_kind}, which no "
                f"single role can be measured against. An internal finding has "
                f"to name a section label or a micro key, because the next gate "
                f"re-measures every affected role and a {element_kind} gives it "
                f"nothing to recompute."
            )

        role_ids = json.loads(affected)
        if not role_ids:
            raise ActionRefused(
                "an internal finding must list the roles it affects. The "
                "finding is a measurement over named rows; without the names "
                "there is nothing to reproduce and nothing to repair."
            )

        unknown, carrying, misses = [], [], {}
        for role_id in role_ids:
            verdict = role_carries_element(db, role_id, okuro_element)
            if not verdict["exists"]:
                unknown.append(role_id)
            elif verdict["carries"]:
                carrying.append(role_id)
            else:
                misses[role_id] = verdict["missing_in"]
        if unknown:
            raise ActionRefused(
                f"no role in the store with id(s) {', '.join(sorted(unknown))} "
                f"— an affected-role list that names a role okuro does not have "
                f"cannot be reproduced or repaired"
            )
        if carrying:
            raise ActionRefused(
                f"role(s) {', '.join(sorted(carrying))} already carry "
                f"{okuro_element!r}, measured against the store just now. An "
                f"internal finding is refused unless EVERY affected role "
                f"currently fails the element it names — otherwise the repair "
                f"it opens would write a section that is already there."
            )
        logger.info(
            "internal finding reproduces: %s",
            "; ".join(f"{r} misses it in {', '.join(g)}"
                      for r, g in sorted(misses.items())),
        )

        fetch_id = None
        quote_verified = 0
        source_hash = None
        rubric = rubric_version()
    else:
        fetch_id = None
        quote_verified = 0
        source_hash = source["last_hash"] if source else None
        rubric = None
        evidence_url = evidence_url or (source["url"] if source else None)

    action_id = str(uuid.uuid4())
    now = _now()
    db.execute(
        "INSERT INTO role_structure_actions ("
        "  id, kind, state, title, source_id, run_id, fetch_id, evidence_url,"
        "  quoted_sentence, quote_verified, okuro_element, affected_role_ids,"
        "  rubric_version, source_hash_at_proposal, research_artifact_id,"
        "  finding_artifact_id, created_by, created_at, updated_at"
        ") VALUES (?, ?, 'proposed', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            action_id,
            kind,
            title,
            source_id,
            run_id,
            fetch_id,
            evidence_url,
            quoted_sentence,
            quote_verified,
            okuro_element,
            affected,
            rubric,
            source_hash,
            research_artifact_id,
            # `finding_artifact_id`, NOT `evidence_artifact_id` — that column
            # is `implement`'s, and writing the proposal's proof there would
            # have it overwritten by the pre-change bodies at the first write.
            evidence_artifact_id if kind == "internal" else None,
            str(created_by).strip(),
            now,
            now,
        ),
    )
    _append_event(db, action_id, None, "proposed", str(created_by).strip(), reason)
    return _hydrate(_row(db, action_id))


# ── transition ───────────────────────────────────────────────────────


def _structure_score(fit: dict):
    """The structure segment out of a ``compute_fit`` result, or None."""
    segments = (fit or {}).get("segments") or {}
    return ((segments.get("structure") or {})).get("score")


def _check_verified(db, row: dict, fit_before, fit_after) -> str:
    """The ``→ verified`` gate. Returns the audit line for the event row.

    Two conditions, and the first is amendment A5:

    * every snapshot on both sides carries the action's own
      ``rubric_version``. A score taken under a different rubric is not a
      worse or better score, it is a different measurement, and comparing the
      two would let a rubric edit verify a change that did nothing;
    * every affected role's ``structure`` segment is no lower after than
      before.

    The baseline is caller-supplied, and that is a real limit worth stating
    rather than hiding: nothing in this table stores the pre-change score, so
    the honesty of ``fit_before`` rests on the caller. What the gate does buy
    is that the comparison is MECHANICAL and its inputs end up in the event
    log, so a verification that was wrong is a row somebody can find rather
    than a claim in a report.
    """
    if not fit_after:
        raise ActionRefused(
            "→ verified needs a fit snapshot of the affected roles taken "
            "after the change; without one 'verified' is a word, not a "
            "measurement"
        )
    if not fit_before:
        raise ActionRefused(
            "→ verified needs the BEFORE snapshot too — 'structure score not "
            "lower' has no meaning against a single reading"
        )

    rubric = row.get("rubric_version")
    if not rubric:
        raise ActionRefused(
            "this action carries no rubric_version, so no fit comparison "
            "against it is valid"
        )

    before = {f.get("role_id"): f for f in fit_before if f.get("role_id")}
    after = {f.get("role_id"): f for f in fit_after if f.get("role_id")}

    for label, snapshots in (("before", fit_before), ("after", fit_after)):
        for fit in snapshots:
            got = fit.get("rubric_version")
            if got != rubric:
                raise ActionRefused(
                    f"the {label} snapshot for {fit.get('role_id')!r} was taken "
                    f"under rubric {got!r} and this action was proposed under "
                    f"{rubric!r}. Scores are only comparable within one rubric; "
                    f"a rubric change is its own action and verifies nothing."
                )

    affected = json.loads(row.get("affected_role_ids") or "[]")
    if not affected:
        raise ActionRefused(
            "this action names no affected roles, so there is nothing for a "
            "verification to measure"
        )

    lines = []
    for role_id in affected:
        if role_id not in before or role_id not in after:
            raise ActionRefused(
                f"role {role_id!r} is named as affected but is missing from the "
                f"{'before' if role_id not in before else 'after'} snapshot"
            )
        was = _structure_score(before[role_id])
        now = _structure_score(after[role_id])
        if was is None or now is None:
            raise ActionRefused(
                f"role {role_id!r} has no structure score on one side of the "
                f"comparison"
            )
        if now < was:
            raise ActionRefused(
                f"role {role_id!r} structure went {was} → {now}. A change that "
                f"lowers the score it was meant to raise is not verified."
            )
        lines.append(f"{role_id} {was}→{now}")

    return "structure " + ", ".join(lines)


def _re_anchor(db, row: dict, *, actor: str, reason: str | None,
               run_id: str | None, quoted_sentence: str | None) -> dict:
    """``researched → researched``: re-point a drifted action at the new body.

    **This exists because the drift refusal used to be a loop.** An approval
    refused on hash drift moves the row back to ``researched`` with its plan
    and critique artifact ids intact, so ``→ planned`` and ``→ critiqued``
    both pass on the ids already there and the row arrives at ``approved``
    again — against the same stale quote, and refused again, forever. The only
    other way out would have been for the refusal to rewrite the hash itself,
    which turns the gate into a formality: it would approve a change to okuro's
    canon on the strength of a sentence nobody re-read.

    So the way out is a transition that costs what re-work costs:

    * a run_id for a run that actually fetched the source again;
    * the quote re-verified against THAT run's body, by the same substring
      assert that let the action exist in the first place;
    * the plan and critique ids CLEARED, so the next walk to ``critiqued``
      needs new artifacts rather than the old ones.

    Only then does ``source_hash_at_proposal`` move.
    """
    source_id = row.get("source_id")
    if not source_id:
        raise ActionRefused(
            "this action names no source, so there is no hash to re-anchor to"
        )
    if not run_id:
        raise ActionRefused(
            "re-anchoring needs the run_id of a run that fetched this source "
            "again — the point is to check the quote against the body the "
            "source serves NOW, and a re-anchor with no new run checks nothing"
        )
    if not (reason and reason.strip()):
        raise ActionRefused(
            "re-anchoring must carry a reason: it moves the reference point "
            "every later gate compares against"
        )

    from okuro.roles.source_poll import MIN_QUOTE_WORDS, normalise, verify_quote

    quote = quoted_sentence or row.get("quoted_sentence")
    if not quote:
        raise ActionRefused("there is no quote to re-verify")
    words = len(normalise(quote).split())
    if words < MIN_QUOTE_WORDS:
        raise ActionRefused(
            f"the quote is {words} word(s); {MIN_QUOTE_WORDS} is the floor"
        )
    if not verify_quote(db, run_id, source_id, quote):
        raise ActionRefused(
            f"the quote does not appear in the body run {run_id!r} fetched "
            f"from {source_id!r}. The source moved and took the sentence with "
            f"it — this action is about a page that no longer exists, and the "
            f"honest move is to reject it and propose what the page says now."
        )

    current = db.fetchone(
        "SELECT url, last_hash FROM role_structure_sources WHERE id = ?",
        (source_id,),
    )
    fetch_row = db.fetchone(
        "SELECT fetch_id FROM source_fetch_runs WHERE run_id = ? AND source_id = ?",
        (run_id, source_id),
    )

    db.execute(
        "UPDATE role_structure_actions SET "
        "  run_id = ?, fetch_id = ?, quoted_sentence = ?, quote_verified = 1,"
        "  source_hash_at_proposal = ?, rubric_version = ?,"
        "  plan_artifact_id = NULL, critique_artifact_id = NULL,"
        "  last_refusal = NULL, updated_at = ? "
        "WHERE id = ?",
        (
            run_id,
            (fetch_row or {}).get("fetch_id"),
            quote,
            (current or {}).get("last_hash"),
            _rubric_version(),
            now_iso(),
            row["id"],
        ),
    )
    _append_event(
        db, row["id"], "researched", "researched", actor,
        f"re-anchored to run {run_id} "
        f"(hash {_short((current or {}).get('last_hash'))}); "
        f"plan and critique cleared — {reason.strip()}",
    )
    return _hydrate(_row(db, row["id"]))


def _rubric_version() -> str:
    from okuro.roles.fit import rubric_version

    return rubric_version()


def transition(
    db,
    action_id: str,
    to_state: str,
    *,
    actor: str,
    reason: str | None = None,
    artifact_id: str | None = None,
    migration_id: str | None = None,
    affected_role_ids=None,
    fit_before=None,
    fit_after=None,
    run_id: str | None = None,
    quoted_sentence: str | None = None,
) -> dict:
    """Move one action, enforcing the table above. Every move writes an event.

    Returns the hydrated row. Raises :class:`ActionRefused` and writes nothing
    when a gate says no — with ONE deliberate exception, which is the hash
    drift on ``→ approved``: that does not raise, it drops the row back to
    ``researched`` and writes the event explaining why. A refusal that leaves
    the row where it was would put an action that is no longer about anything
    real back in front of the same human tomorrow.
    """
    if to_state not in STATES:
        raise ActionRefused(
            f"{to_state!r} is not a state; the machine has {', '.join(STATES)}"
        )

    row = _row(db, action_id)
    from_state = row["state"]

    allowed = TRANSITIONS.get(from_state, ())
    if to_state not in allowed:
        if not allowed:
            raise ActionRefused(
                f"action {action_id} is {from_state!r}, which is terminal — "
                f"nothing moves out of it"
            )
        raise ActionRefused(
            f"{from_state!r} → {to_state!r} is not a move this machine makes. "
            f"From {from_state!r} the allowed targets are: {', '.join(allowed)}"
        )

    actor = str(actor or "").strip()
    if not actor:
        raise ActionRefused("every transition must name its actor")

    # `researched → researched` is not a no-op and not a state change; it is
    # the re-anchor, and it is the only move in the table whose source and
    # target are the same. Routed out here rather than folded into the gate
    # chain below, because none of those gates describe what it does.
    if from_state == "researched" and to_state == "researched":
        return _re_anchor(
            db, row, actor=actor, reason=reason,
            run_id=run_id, quoted_sentence=quoted_sentence,
        )

    updates: dict[str, object] = {}
    event_reason = reason

    if to_state in ("rejected", "superseded", "restored") \
            and not (reason and reason.strip()):
        raise ActionRefused(
            f"→ {to_state} is terminal and must carry a reason — a row that "
            f"ends with no stated reason is a decision nobody can revisit"
        )

    column = ARTIFACT_FOR_STATE.get(to_state)
    if column:
        existing = row.get(column)
        if not artifact_id and not existing:
            raise ActionRefused(
                f"→ {to_state} requires {column}: the state is a claim that "
                f"the work was done, and the artifact is the only thing that "
                f"makes the claim checkable"
            )
        if artifact_id:
            updates[column] = str(artifact_id).strip()

    if to_state == "implemented":
        have = migration_id or artifact_id or row.get("migration_id") \
            or row.get("evidence_artifact_id")
        if not have:
            raise ActionRefused(
                "→ implemented requires either migration_id (the shipped "
                "half of Q2's split — the roles a migration carries) or the "
                "artifact id holding the pre-change bodies (the in-DB half). "
                "Without one of them there is no way back."
            )
        if migration_id:
            updates["migration_id"] = str(migration_id).strip()
        # THE EVIDENCE GETS ITS OWN COLUMN AND THE DIFF IS LEFT ALONE.
        #
        # This used to write `diff_artifact_id`, which overwrote the dry-run
        # diff — the one document that says what the approval was ABOUT — with
        # a document produced after the decision. The row then pointed at
        # bodies-before-the-write while the panel and every later reader
        # believed they were looking at the reviewed change.
        if artifact_id:
            updates["evidence_artifact_id"] = str(artifact_id).strip()

    # THE SUPPLIED LIST IS APPLIED BEFORE IT IS VALIDATED AGAINST.
    #
    # This block used to sit BELOW the `verified` gate, which meant the gate
    # read the STORED affected_role_ids while the caller's list — passed in the
    # same call, and the only list that describes what was actually changed —
    # was written afterwards. A row whose stored list was empty passed the gate
    # on "this action names no affected roles" and then acquired a list nothing
    # had checked. Order is the whole fix.
    if affected_role_ids is not None:
        row["affected_role_ids"] = _role_ids_json(affected_role_ids)
        updates["affected_role_ids"] = row["affected_role_ids"]

    if to_state == "verified":
        event_reason = _check_verified(db, row, fit_before, fit_after)
        if reason:
            event_reason = f"{event_reason} — {reason}"

    # ── The human gate, and the drift check behind it ────────────────
    if to_state == "approved":
        if not _is_human(actor):
            allowed = sorted(human_actors())
            raise ActionRefused(
                f"actor {actor!r} is not the human. → approved is the one "
                f"transition an agent cannot make: it is the single decision "
                f"gate in this workflow and it is also what creates the todo. "
                + (
                    f"The profile handle is the only actor that qualifies "
                    f"({', '.join(allowed)}), and the API derives it from the "
                    f"authenticated identity rather than from a payload."
                    if allowed else
                    "The profile names no handle, so there is no actor that "
                    "qualifies — this gate fails closed rather than opening "
                    "when it cannot read its own allowlist."
                )
            )

        source_id = row.get("source_id")
        if source_id:
            current = db.fetchone(
                "SELECT last_hash FROM role_structure_sources WHERE id = ?",
                (source_id,),
            )
            now_hash = (current or {}).get("last_hash")
            if now_hash != row.get("source_hash_at_proposal"):
                drift = (
                    f"source {source_id} moved since this was proposed "
                    f"({_short(row.get('source_hash_at_proposal'))} → "
                    f"{_short(now_hash)}); back to researched rather than "
                    f"approving a change to a page that no longer says what it "
                    f"said. Re-anchor it (researched → researched with the new "
                    f"run_id) to make it approvable again."
                )
                # `source_hash_at_proposal` is NOT rewritten here, and that is
                # the fix for the loop this used to have: a drop-back that
                # re-anchored silently would leave the plan and critique ids in
                # place, so the row walked straight back to `critiqued` and
                # approved against evidence nobody re-read. Re-anchoring is its
                # own transition and it costs real work.
                db.execute(
                    "UPDATE role_structure_actions "
                    "SET state = 'researched', last_refusal = ?, updated_at = ? "
                    "WHERE id = ?",
                    (drift, now_iso(), action_id),
                )
                _append_event(db, action_id, from_state, "researched", actor, drift)
                return _hydrate(_row(db, action_id))

    # ── Apply ────────────────────────────────────────────────────────
    updates["state"] = to_state
    updates["updated_at"] = _now()
    if to_state in DECISION_STATES:
        updates["decided_by"] = actor
        updates["decided_at"] = updates["updated_at"]

    sets = ", ".join(f"{col} = ?" for col in updates)
    db.execute(
        f"UPDATE role_structure_actions SET {sets} WHERE id = ?",
        (*updates.values(), action_id),
    )
    _append_event(db, action_id, from_state, to_state, actor, event_reason)

    # ── Exactly one todo, and only on approve ────────────────────────
    if to_state == "approved" and not row.get("todo_id"):
        todo_id = _emit_todo(db, action_id)
        if todo_id:
            db.execute(
                "UPDATE role_structure_actions SET todo_id = ? WHERE id = ?",
                (todo_id, action_id),
            )

    return _hydrate(_row(db, action_id))


def _emit_todo(db, action_id: str) -> str | None:
    """The one line the owner's personal queue gains when he approves.

    Through ``sense.todos.todo_add`` rather than an INSERT written here. That
    helper carries the dedupe on ``source_event_id`` among ACTIVE rows, the
    project resolution and the priority validation; a second INSERT path would
    have none of them and would be the first thing to drift.

    ``source_event_id`` is the action id, so "exactly one todo per action" is
    the database's answer and not a flag this module has to remember.
    """
    row = _row(db, action_id)
    element = row.get("okuro_element") or "—"
    affected = json.loads(row.get("affected_role_ids") or "[]")
    detail_lines = [
        f"Structure action {action_id} — approved.",
        f"okuro element: {element}",
        f"affected roles ({len(affected)}): "
        + (", ".join(affected) if affected else "none named yet"),
    ]
    if row.get("evidence_url"):
        detail_lines.append(f"evidence: {row['evidence_url']}")
    if row.get("quoted_sentence"):
        detail_lines.append(f'quote: "{row["quoted_sentence"]}"')
    if row.get("critique_artifact_id"):
        detail_lines.append(f"critique: artifact {row['critique_artifact_id']}")

    try:
        from okuro.sense.todos import todo_add

        todo = todo_add(
            title=f"Role structure: {row['title']}",
            detail="\n".join(detail_lines),
            priority=3,
            project="okuro",
            source="system",
            source_event_id=f"role-structure-action:{action_id}",
            context={"role_structure_action_id": action_id},
        )
        return (todo or {}).get("id")
    except Exception as exc:  # noqa: BLE001
        # A failed todo must not un-approve the action: the approval is the
        # decision and it is already recorded with an event. Loud, not fatal.
        logger.error("approved action %s but the todo failed: %s", action_id, exc)
        return None


# ── Reads ────────────────────────────────────────────────────────────


def list_actions(db, state: str | None = None, kind: str | None = None,
                 limit: int = 200) -> list[dict]:
    """Actions, newest first, optionally filtered by state and kind."""
    clauses, params = [], []
    if state:
        if state not in STATES:
            raise ActionRefused(f"{state!r} is not a state")
        clauses.append("state = ?")
        params.append(state)
    if kind:
        if kind not in KINDS:
            raise ActionRefused(
                f"{kind!r} is not a kind; the kinds are {', '.join(KINDS)}")
        clauses.append("kind = ?")
        params.append(kind)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(int(limit))
    rows = db.fetchall(
        f"SELECT * FROM role_structure_actions {where} "
        f"ORDER BY created_at DESC, id DESC LIMIT ?",
        tuple(params),
    )
    return [_hydrate(dict(r)) for r in rows]


def _fenced_blocks(text: str) -> list[str]:
    """The body of every ``` fence in the document, whatever its info string.

    Any fence, not only ``json``: an agent that reaches for ``jsonc``, or for a
    bare block, has still put its rows where a reader would look, and a lift
    that silently ignored those would be the quiet-run failure wearing a
    different hat. What the fence buys is a BOUNDARY, and the boundary is the
    whole point — see :func:`_json_objects`.
    """
    out: list[str] = []
    current: list[str] | None = None
    for line in (text or "").splitlines():
        if line.lstrip().startswith("```"):
            if current is None:
                current = []
            else:
                out.append("\n".join(current))
                current = None
            continue
        if current is not None:
            current.append(line)
    # An unterminated fence still has content worth reading; the document
    # simply ended before the closing marker.
    if current:
        out.append("\n".join(current))
    return out


def _json_objects(text: str) -> list[dict]:
    """Every balanced ``{…}`` in the text that parses as a JSON object.

    **Only ever called on the inside of a fence, and that restriction is the
    bug fix.** The scanner tracks string state so a brace inside a quoted
    sentence does not end an object — necessary, since these findings quote
    specification prose. But run over a WHOLE report it tracks string state
    through the PROSE as well, and prose is not JSON: one apostrophe-free
    ``"`` in a sentence before the findings puts the scanner inside a string
    it never leaves, every ``{`` after it is ignored, and the lift returns
    zero candidates for a report full of them. No exception is raised and
    nothing looks wrong. That is the quiet run this whole workstream exists
    to make impossible, produced by the tool meant to prevent it.

    Inside a fence the quotes are balanced because the content is JSON, so the
    scanner is doing the job it is correct for: recovering several objects
    from one block that ``json.loads`` cannot take whole.
    """
    out: list[dict] = []
    depth = 0
    start = -1
    in_string = False
    escaped = False
    for i, ch in enumerate(text or ""):
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
            continue
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            if depth:
                depth -= 1
                if depth == 0 and start >= 0:
                    try:
                        parsed = json.loads(text[start:i + 1])
                    except ValueError:
                        parsed = None
                    if isinstance(parsed, dict):
                        out.append(parsed)
                    start = -1
    return out


#: The keys the role body's FINDING SCHEMA emits. Named here so the lifter's
#: contract and the role's contract can be compared in one test rather than
#: discovered to disagree by a run that lifts nothing.
FINDING_KEYS: tuple[str, ...] = (
    "source_id", "source_url", "run_id", "quoted_sentence", "quote_verified",
    "okuro_element", "change", "implication", "confidence",
)

#: The subset without which an object is not a finding at all.
FINDING_REQUIRED_KEYS: tuple[str, ...] = (
    "source_id", "quoted_sentence", "okuro_element",
)

#: Signals that a report MEANT to carry findings. When one of these is present
#: and the parse produced nothing, the lift says `parse_failed` instead of
#: returning a clean zero — "no findings" and "I could not read your findings"
#: are opposite results and they must never render the same.
_FINDING_INTENT_MARKERS: tuple[str, ...] = (
    "finding schema",
    '"quoted_sentence"',
    "'quoted_sentence'",
)


def _findings_in_block(block: str) -> list[dict]:
    """Finding-shaped objects in one fenced block.

    ``json.loads`` on the whole block first, because that is what a well-formed
    block is and it accepts a bare object or a list of them. The brace scanner
    is the fallback for a block holding several objects back to back, or one
    with a trailing comment — shapes ``json.loads`` refuses whole but that are
    still unambiguous.
    """
    text = (block or "").strip()
    if not text:
        return []
    try:
        parsed = json.loads(text)
    except ValueError:
        parsed = None
    if isinstance(parsed, dict):
        return [parsed]
    if isinstance(parsed, list):
        return [item for item in parsed if isinstance(item, dict)]
    return _json_objects(text)


def report_findings(body: str) -> list[dict]:
    """Every finding-shaped object in a report, fence-scoped and deduplicated."""
    seen: set[str] = set()
    out: list[dict] = []
    for block in _fenced_blocks(body):
        for obj in _findings_in_block(block):
            if not all(k in obj for k in FINDING_REQUIRED_KEYS):
                continue
            fingerprint = json.dumps(obj, sort_keys=True, default=str)
            if fingerprint in seen:
                continue
            seen.add(fingerprint)
            out.append(obj)
    return out


def _finding_title(finding: dict) -> str:
    """One line naming what changed, for the list and for the todo."""
    change = str(finding.get("change") or "").strip()
    element = str(finding.get("okuro_element") or "").strip() or "—"
    if not change:
        return f"{element}: structural finding from {finding.get('source_id')}"
    first = change.split(". ")[0].strip().rstrip(".")
    if len(first) > 160:
        first = first[:157].rstrip() + "…"
    return f"{element}: {first}"


def lift_findings_from_report(db, run_id: str, artifact_id: str) -> dict:
    """Read the researcher's report and open one action per finding.

    **The agent's `quote_verified` is ignored.** It is a key in the schema
    because the report is also read by people, but every quote is re-checked
    here by :func:`propose` against the body okuro stored — trusting the
    field would put the verification back inside the artifact the fabricating
    agent controls, which is the exact shape this workstream exists to remove.

    A finding that names a DIFFERENT ``run_id`` than the one being lifted is
    refused rather than silently re-scoped. That is an agent producing
    evidence from a run this report is not about, and the only two ways it
    happens are a copy-paste from an old report or an invention.

    Returns a summary with the rows opened and, for everything refused, the
    reason in full. A lifter that swallowed its refusals would turn a report
    of five findings and four fabrications into "one action created", which
    reads like a quiet run.
    """
    from okuro.sense.artifacts import artifact_get

    artifact = artifact_get(artifact_id, include_body=True)
    if not artifact:
        raise ActionRefused(f"no artifact with id {artifact_id!r}")

    body = artifact.get("body") or ""
    candidates = report_findings(body)

    # "No findings" and "I could not read your findings" are opposite results.
    # A lift that returns a clean zero for a report full of rows it failed to
    # parse reads as the expected quiet week, which is precisely the reading
    # AC3 makes legitimate — so a zero against a report that plainly meant to
    # carry findings has to say so in its own field.
    lowered = body.lower()
    parse_failed = not candidates and any(
        marker in lowered for marker in _FINDING_INTENT_MARKERS
    )

    opened: list[dict] = []
    refused: list[dict] = []
    seen: set[tuple] = set()

    for finding in candidates:
        key = (
            str(finding.get("source_id")),
            str(finding.get("quoted_sentence") or "").strip(),
        )
        if key in seen:
            continue
        seen.add(key)

        claimed_run = str(finding.get("run_id") or "").strip()
        if claimed_run and claimed_run != run_id:
            refused.append({
                "finding": finding,
                "reason": (
                    f"the finding names run {claimed_run!r} and this report is "
                    f"about run {run_id!r}. Evidence from another run is not "
                    f"evidence for this one."
                ),
            })
            continue

        try:
            row = propose(
                db,
                kind="finding",
                title=_finding_title(finding),
                created_by=artifact.get("created_by") or "role-architecture-researcher",
                source_id=str(finding.get("source_id") or "").strip() or None,
                run_id=run_id,
                evidence_url=finding.get("source_url"),
                quoted_sentence=finding.get("quoted_sentence"),
                okuro_element=finding.get("okuro_element"),
                research_artifact_id=artifact_id,
                reason=str(finding.get("implication") or "").strip() or None,
            )
        except ActionRefused as exc:
            refused.append({"finding": finding, "reason": str(exc)})
            continue
        opened.append(row)

    return {
        "run_id": run_id,
        "artifact_id": artifact_id,
        "candidates": len(candidates),
        "opened": opened,
        "refused": refused,
        "parse_failed": parse_failed,
        "parse_failed_reason": (
            "the report carries the finding vocabulary but no fenced block "
            "yielded a finding-shaped object. Findings go in a fenced block; "
            "loose prose is not parsed, deliberately."
            if parse_failed else None
        ),
    }


def count_actions(db, state: str | None = None, kind: str | None = None) -> dict:
    """Rows per state under the SAME filters :func:`list_actions` applies.

    Separate from the listing because the listing has a ``limit`` and a count
    must not. Sharing the WHERE clause with it is the point: a count computed
    over the whole table while the rows beside it are filtered is a header that
    contradicts the list under it, and the reader believes the header.

    Every state is present, including the ones at zero. A missing key and a
    zero are different claims, and a panel that groups by state renders the
    first as an absent group rather than as "none".
    """
    clauses, params = [], []
    if state:
        if state not in STATES:
            raise ActionRefused(f"{state!r} is not a state")
        clauses.append("state = ?")
        params.append(state)
    if kind:
        if kind not in KINDS:
            raise ActionRefused(
                f"{kind!r} is not a kind; the kinds are {', '.join(KINDS)}")
        clauses.append("kind = ?")
        params.append(kind)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""

    counts = {s: 0 for s in STATES}
    for row in db.fetchall(
        f"SELECT state, COUNT(*) AS n FROM role_structure_actions {where} "
        f"GROUP BY state",
        tuple(params),
    ):
        counts[row["state"]] = row["n"]
    return counts


def get_action(db, action_id: str, *, include_plan: bool = False) -> dict | None:
    """One action with its full event history, or None.

    ``include_plan`` is the ONLY way to get :data:`HEAVY_COLUMNS` out of this
    module. Off by default because the plan is a document and the caller that
    wants to read it knows that it does.
    """
    row = db.fetchone(
        "SELECT * FROM role_structure_actions WHERE id = ?", (action_id,)
    )
    if not row:
        return None
    out = _hydrate(dict(row), heavy=include_plan)
    out["events"] = action_events(db, action_id)
    return out


def action_events(db, action_id: str) -> list[dict]:
    """The append-only log for one action, oldest first."""
    return [
        dict(r)
        for r in db.fetchall(
            "SELECT id, action_id, from_state, to_state, actor, reason, at "
            "FROM role_structure_action_events WHERE action_id = ? "
            "ORDER BY at ASC, id ASC",
            (action_id,),
        )
    ]


# ── The durable dispatch claim ───────────────────────────────────────
#
# P3 guarded its dispatch with a Python `set` plus a scan of task directories,
# and its own report named the hole rather than hiding it: the set is per
# PROCESS, so two uvicorn workers receiving simultaneous clicks both pass it,
# and the directory scan cannot see a run that has not spawned yet — the gap
# between "request arrives" and "task dir exists" is the entire poll.
#
# The fix is not a better scan. It is that the uniqueness belongs to the
# database: an INSERT that conflicts is atomic across every process on the
# host, and a SELECT-then-INSERT is precisely the race it was supposed to
# close.

#: How long a claim may sit unreleased before another dispatch may take it.
#:
#: A worker killed mid-poll leaves its claim behind, and without an expiry the
#: repair for one crash is a human deleting a row they have no reason to know
#: exists. The window it has to cover is the DISPATCH — the poll under its own
#: whole-run deadline, plus spawning a process — and nothing longer, because
#: the claim is released as soon as the task is spawned and a run that is
#: already going is caught by the task-status scan instead.
#:
#: It was a quarter of an hour, which is the same repair with a longer wait:
#: one crash and the button is dead for fifteen minutes with nothing on screen
#: explaining why. Derived from the poll deadline rather than typed, so the two
#: cannot drift apart.
CLAIM_SPAWN_ALLOWANCE_SECONDS = 120

def _claim_ceiling() -> int:
    from okuro.roles.source_poll import POLL_RUN_DEADLINE

    return int(POLL_RUN_DEADLINE) + CLAIM_SPAWN_ALLOWANCE_SECONDS


CLAIM_STALE_SECONDS = _claim_ceiling()


def open_dispatch_claim(db, registry_state: str) -> dict | None:
    """The live claim on this registry state, or None.

    Expired claims are not live. They are left in the table rather than
    deleted, because the row is also the record that a dispatch was attempted
    and died, and that is worth more than a clean table.
    """
    from okuro.roles._time import parse_ts

    row = db.fetchone(
        "SELECT * FROM role_structure_dispatch_claims "
        "WHERE registry_state = ? AND released_at IS NULL",
        (registry_state,),
    )
    if not row:
        return None
    claimed = parse_ts(row["claimed_at"])
    if claimed:
        age = (datetime.now(timezone.utc) - claimed).total_seconds()
        if age > CLAIM_STALE_SECONDS:
            return None
    return dict(row)


def claim_dispatch(db, registry_state: str, *, run_id: str | None = None) -> bool:
    """Take the claim for this registry state. True when we got it.

    One statement, so two workers cannot both win. The ``DO UPDATE`` branch
    fires when a previous claim exists and is either released or stale, and it
    is guarded by that same condition in SQL rather than in Python — a check
    done here and an update done there is the race again, one layer up.
    """
    now = _now()
    cutoff = datetime.now(timezone.utc).timestamp() - CLAIM_STALE_SECONDS
    stale_before = datetime.fromtimestamp(cutoff, tz=timezone.utc).isoformat()

    db.execute(
        "INSERT INTO role_structure_dispatch_claims "
        "(registry_state, run_id, task_id, claimed_at, released_at) "
        "VALUES (?, ?, NULL, ?, NULL) "
        "ON CONFLICT(registry_state) DO UPDATE SET "
        "  run_id = excluded.run_id, task_id = NULL, "
        "  claimed_at = excluded.claimed_at, released_at = NULL "
        "WHERE role_structure_dispatch_claims.released_at IS NOT NULL "
        "   OR role_structure_dispatch_claims.claimed_at < ?",
        (registry_state, run_id, now, stale_before),
    )
    held = db.fetchone(
        "SELECT run_id, claimed_at FROM role_structure_dispatch_claims "
        "WHERE registry_state = ? AND released_at IS NULL",
        (registry_state,),
    )
    return bool(held) and held["claimed_at"] == now


def note_dispatch_task(db, registry_state: str, task_id: str | None) -> None:
    """Record which task the claim spawned, so a 409 can name it."""
    if not task_id:
        return
    db.execute(
        "UPDATE role_structure_dispatch_claims SET task_id = ? "
        "WHERE registry_state = ? AND released_at IS NULL",
        (task_id, registry_state),
    )


def release_dispatch(db, registry_state: str) -> None:
    """Give the claim back. Never raises — a failed release expires anyway."""
    try:
        db.execute(
            "UPDATE role_structure_dispatch_claims SET released_at = ? "
            "WHERE registry_state = ? AND released_at IS NULL",
            (_now(), registry_state),
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("could not release the dispatch claim: %s", exc)
