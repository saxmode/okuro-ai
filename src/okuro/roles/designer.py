# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Role designer: storage API for agent-generated roles.
# index: imports | canonical structure | def validate_role_structure | def store_designed_role | def get_role_template
# AGENT_HEADER_END -->
"""Role designer: storage API for agent-generated roles.

The canonical role structure below is DERIVED from the shipped roles
(87 of them, measured 2026-08-25 while they were still YAML; migration 155
moved them into the `roles` table and deleted the files), not invented.
Reproduce the measurement with:

    .venv/bin/python -m okuro.roles.audit_structure

Grade-specific, because the grades genuinely differ across roles:
  full  — carries the persona block (traits/neurotype/cognitive profile): 74-80%
  lean  — does NOT carry the persona block (2-8%); CORE/PROTOCOL/COLLABORATION
  micro — flat YAML; `purpose` + `expertise` feed the description AND embedding
"""

import json
import re
from datetime import datetime, timezone

import yaml

from okuro.db import get_db
from okuro.roles.fences import search_unfenced
from okuro.roles.write import promote_role, upsert_role


# Maintenance schedule defaults by domain
DOMAIN_SCHEDULES = {
    "engineering": "weekly",
    "quality": "weekly",
    "system": "weekly",
    "design": "biweekly",
    "research": "biweekly",
    "marketing": "biweekly",
    "planning": "biweekly",
    "documentation": "monthly",
    "c-level": "monthly",
    "freaks": "monthly",
}


# ---------------------------------------------------------------------------
# Canonical role structure
# ---------------------------------------------------------------------------

# LANGUAGE — every curated role in the shipped catalog is written in ENGLISH.
# A role authored in another language still matches (the embedding is
# multilingual) but reads as a foreign body next to the other 85 and cannot be
# maintained by the English-language maintenance sweeps. Author roles in English
# regardless of the language the requesting conversation is held in.
ROLE_LANGUAGE = "en"

# The six trait axes are IDENTICAL in all 65 catalog roles that carry the
# table — this is canon, not a suggestion. Values are 0-100 with a one-line
# meaning per axis explaining what THIS value implies for behaviour.
TRAIT_AXES = (
    "Intuition vs Data",
    "Conservative vs Bold",
    "Detail vs Big Picture",
    "Independent vs Collaborative",
    "Reactive vs Proactive",
    "Specialist vs Generalist",
)

# Required sections of the FULL grade. (label, regex) — catalog frequency in
# the comment. Missing any of these means the role is structurally incomplete.
FULL_REQUIRED = (
    ("IDENTITY", r"^##\s+IDENTITY"),                              # 80%
    ("Personality (Archetype + Experience Level)", r"\*\*Archetype:\*\*"),  # 80%
    ("Traits (0-100) table", r"^###\s+Traits\s*\(0-100\)"),       # 76%
    ("Neurotype Balance (50/50 MANDATORY)", r"Neurotype Balance"),  # 74%
    ("COGNITIVE PROFILE", r"^##\s+COGNITIVE PROFILE"),            # 76%
    ("EXPERTISE", r"^##\s+EXPERTISE"),                            # 78%
    ("PROTOCOL", r"^##\s+PROTOCOL"),                              # 80%
    ("TOOLS", r"^##\s+TOOLS"),                                    # 77%
)

# Present in most catalog roles but not load-bearing for identity — reported as
# advisory only, never gating.
FULL_RECOMMENDED = (
    ("SKILLS", r"^##\s+SKILLS"),                                  # 77%
    ("SELF-MAINTENANCE", r"^##\s+SELF-MAINTENANCE"),              # 72%
    ("KNOWLEDGE PROTOCOL", r"^##\s+KNOWLEDGE PROTOCOL"),          # 71%
    ("SUCCESS CRITERIA", r"^##\s+SUCCESS CRITERIA"),              # 80%
    ("FAILURE MODES", r"^##\s+FAILURE MODES"),                    # 78%
    ("COLLABORATION", r"^##\s+COLLABORATION"),                    # 78%
)

# LEAN grade — the catalog's lean prompts deliberately DROP the persona block
# (traits appear in 4%, neurotype in 2%). Requiring it here would contradict
# every shipped role, so only the operating spine is required.
LEAN_REQUIRED = (
    ("CORE", r"^##\s+CORE"),                                      # 72%
    ("PROTOCOL", r"^##\s+PROTOCOL"),                              # 88%
)
LEAN_RECOMMENDED = (
    ("COLLABORATION", r"^##\s+COLLABORATION"),                    # 82%
    ("REFERENCES", r"^##\s+REFERENCES"),                          # 63%
)

# MICRO grade — flat YAML. `purpose` and `expertise` are not cosmetic: they are
# the ONLY inputs to the role's DB description and its match embedding (see
# store_designed_role below). A micro that is prose rather than YAML, or that
# omits these keys, yields description == role name and a worthless embedding —
# the role then never wins a roles_match. 10 of 62 catalog micros are already
# unparseable this way.
MICRO_REQUIRED_KEYS = ("purpose", "expertise")
MICRO_RECOMMENDED_KEYS = ("id", "skills", "constraints", "protocol", "tools", "success")


def micro_label(key: str) -> str:
    """The exact label ``validate_role_structure`` emits for a missing micro key.

    Exported because a consumer has to be able to tell a real missing-key label
    apart from the sentinel messages below ("micro is not parseable YAML …"),
    and the only other way to do that is to hand-copy this f-string. A copy is a
    contract nothing enforces: change the wording here and the copy keeps
    matching nothing, silently, forever.
    """
    return f"micro key '{key}'"


MICRO_REQUIRED_LABELS = tuple(micro_label(k) for k in MICRO_REQUIRED_KEYS)
MICRO_RECOMMENDED_LABELS = tuple(micro_label(k) for k in MICRO_RECOMMENDED_KEYS)

#: Every gating label, by grade — the labels ``validate_role_structure`` puts
#: in ``missing``. One definition, so a scorer never reconstructs them.
REQUIRED_LABELS: dict[str, tuple[str, ...]] = {
    "full": tuple(label for label, _ in FULL_REQUIRED),
    "lean": tuple(label for label, _ in LEAN_REQUIRED),
    "micro": MICRO_REQUIRED_LABELS,
}


def validate_role_structure(role_content: dict) -> dict:
    """Check role_content against the canonical structure, per grade.

    Returns:
        {
          "ok": bool,               # no REQUIRED section missing in any grade
          "missing": {grade: [...]},   # required, gating
          "advisory": {grade: [...]},  # recommended, non-gating
        }

    FENCE-AWARE. A marker inside a fenced code block does NOT satisfy the
    gate. Ten role bodies carry an output template in a fence, and the
    markdown in such a template is what the role is told to PRODUCE — reading
    it as the role's own structure let ``role-researcher`` pass on an
    ``**Archetype:**`` and a ``Neurotype Balance`` it only ever tells its
    adopter to write, while carrying no persona block itself. The fence
    detection is :mod:`okuro.roles.fences`, the same one the repair engine
    has always used; this function used to have the second, blind answer.
    """
    missing: dict[str, list[str]] = {}
    advisory: dict[str, list[str]] = {}

    def _scan(grade: str, text: str, required, recommended) -> None:
        if not (text or "").strip():
            missing[grade] = [f"grade '{grade}' is empty"]
            return
        miss = [label for label, pat in required
                if not search_unfenced(pat, text, re.M | re.I)]
        adv = [label for label, pat in recommended
               if not search_unfenced(pat, text, re.M | re.I)]
        if miss:
            missing[grade] = miss
        if adv:
            advisory[grade] = adv

    _scan("full", role_content.get("full", ""), FULL_REQUIRED, FULL_RECOMMENDED)
    _scan("lean", role_content.get("lean", ""), LEAN_REQUIRED, LEAN_RECOMMENDED)

    # micro is YAML, not markdown — check keys, not headings
    micro = role_content.get("micro", "") or ""
    if not micro.strip():
        missing["micro"] = ["grade 'micro' is empty"]
    else:
        try:
            parsed = yaml.safe_load(micro)
        except yaml.YAMLError as exc:
            parsed = None
            missing["micro"] = [f"micro is not parseable YAML ({exc.__class__.__name__}) "
                                f"— description and embedding fall back to the role name"]
        if parsed is not None:
            if not isinstance(parsed, dict):
                missing["micro"] = ["micro parsed as prose, not a YAML mapping "
                                    "— description and embedding fall back to the role name"]
            else:
                miss = [micro_label(k) for k in MICRO_REQUIRED_KEYS if not parsed.get(k)]
                adv = [micro_label(k) for k in MICRO_RECOMMENDED_KEYS if not parsed.get(k)]
                if miss:
                    missing["micro"] = miss
                if adv:
                    advisory["micro"] = adv

    return {"ok": not missing, "missing": missing, "advisory": advisory}


#: Openings that mean the caller sent a POINTER instead of a role body. An
#: agent that has just written a long artifact reaches for "see artifact
#: <id>" as if the reader could follow it; the role table stores text and
#: nothing dereferences it, so the role ships as that one sentence.
ARTIFACT_REFERENCE_PREFIXES = ("see artifact", "see file", "see task", "refer to")

#: Below this length a grade that opens with one of the prefixes above is a
#: pointer, not prose that happens to start that way.
_POINTER_MAX_CHARS = 200


def flatten_missing(missing: dict) -> list[str]:
    """``{"full": ["IDENTITY"]}`` -> ``["full: IDENTITY"]``.

    One renderer, because every refusal message and every advisory payload in
    the system quotes this list and they must read identically.
    """
    return [
        f"{grade}: {', '.join(items)}" for grade, items in sorted(missing.items())
    ]


def artifact_reference_error(role_content: dict) -> str | None:
    """The pointer guard, or None when every grade carries real text."""
    for grade in ("full", "lean", "micro"):
        val = role_content.get(grade) or ""
        if (
            val
            and len(val) < _POINTER_MAX_CHARS
            and any(
                val.strip().lower().startswith(prefix)
                for prefix in ARTIFACT_REFERENCE_PREFIXES
            )
        ):
            return (
                f"role_content['{grade}'] contains an artifact reference "
                f"instead of actual content ({len(val)} chars). "
                f"Pass the full role text, not a pointer."
            )
    return None


def gate_role_body(role_content: dict) -> dict:
    """THE body gate: pointer guard + canonical structure, in one verdict.

    Every surface that accepts a role body has to apply both checks, and
    before this function existed each one carried its own copy — the pointer
    guard was duplicated verbatim in ``store_designed_role`` and
    ``draft_role``, and the MCP verb had the structure half and not the
    pointer half at all. Two gates that are supposed to agree and are written
    twice do not stay agreeing; they drift on the first edit to either.

    Returns ``{ok, error, missing, advisory, flat_missing}``. ``error`` is the
    pointer refusal (a sentence, because it names one grade and one fix);
    ``missing`` is the per-grade gating list and ``flat_missing`` its rendered
    form. ``ok`` is false when EITHER half objects, so a caller cannot pass by
    checking only the half it remembered.
    """
    pointer = artifact_reference_error(role_content)
    structure = validate_role_structure(role_content)
    return {
        "ok": pointer is None and structure["ok"],
        "error": pointer,
        "missing": structure["missing"],
        "advisory": structure["advisory"],
        "flat_missing": flatten_missing(structure["missing"]),
    }


def store_designed_role(
    name: str,
    domain: str,
    role_content: dict,
    research_sources: list[str] | None = None,
    strict: bool = False,
    tier: str = "standard",
    model: str = "sonnet",
) -> dict:
    """Store a fully designed role.

    Args:
        name: Role identifier
        domain: Domain classification
        role_content: dict with "full", "lean", "micro" keys
        research_sources: URLs/references used during research.
            NOTE: not persisted as a column — put the sources in the `full`
            text under a SOURCES heading or they are lost.
        strict: refuse the write outright when the canonical structure is
            incomplete. Default False — see the structure gate below.
        tier: canonical dispatch tier, checked against CANONICAL_TIERS by the
            write helper. Used to be the literal 'standard' in this function's
            INSERT, so a role designed for the top model came out running on
            the default one and only a follow-up `update_role_info` could fix
            it. The defaults preserve the old behaviour for callers that do
            not pass them.
        model: provider model name. Was the literal 'sonnet' for the same
            reason.

    Structure gate (soft by design):
        A role missing a REQUIRED canonical section is still stored, but is
        named in `warnings` and does NOT get maturity='active':
          - new role   -> maturity='draft' (invisible to roles_match until
                          promoted, so an incomplete role cannot silently win
                          a match)
          - known role -> its existing maturity is left UNTOUCHED, so
                          re-upserting an incomplete body can never demote a
                          live role out of matching.
        Hard refusal is opt-in via strict=True. Rationale: 60 of the 100 live
        roles fail this check (measured 2026-08-25 with
        `python -m okuro.roles.audit_structure`; the earlier "23 of 85" figure
        predates the lean/micro gates) — the pipeline-machinery cohort
        (compressor, critic, scorer, phase-summarizer, prism-*) has no persona
        block on purpose. A hard default would break every re-upsert of those.
    """
    if "micro" not in role_content:
        return {"error": "role_content must include 'micro' key"}

    # Pointer guard + structure gate, through the one shared verdict.
    gate = gate_role_body(role_content)
    if gate["error"]:
        return {"error": gate["error"]}

    structure = {
        "ok": gate["ok"],
        "missing": gate["missing"],
        "advisory": gate["advisory"],
    }
    warnings: list[str] = []
    if not structure["ok"]:
        flat = "; ".join(gate["flat_missing"])
        if strict:
            return {
                "error": "role_content is structurally incomplete — "
                f"missing required sections [{flat}]. "
                "Call get_role_template(level) for the canonical skeleton.",
                "missing": structure["missing"],
            }
        warnings.append(f"missing required canonical sections — {flat}")
    for grade, items in structure["advisory"].items():
        warnings.append(f"{grade}: recommended but absent — {', '.join(items)}")
    target_maturity = "active" if structure["ok"] else "draft"

    # Parse description from micro
    try:
        micro_parsed = yaml.safe_load(role_content["micro"])
        description = micro_parsed.get("purpose", name) if isinstance(micro_parsed, dict) else name
        expertise = micro_parsed.get("expertise", []) if isinstance(micro_parsed, dict) else []
        if isinstance(expertise, list):
            expertise = ", ".join(expertise)
        description = f"{description}. Expertise: {expertise}" if expertise else description
    except yaml.YAMLError:
        description = name

    schedule = DOMAIN_SCHEDULES.get(domain, "monthly")
    now = datetime.now(timezone.utc).isoformat()

    # The row, the tags, the vector and updated_at all go through the one
    # write path. This used to be a hand-written INSERT with its own
    # DELETE/INSERT on vec_roles, which wrote the embedding WITHOUT setting
    # `description_embedded` — so the staleness backfill could not tell this
    # role's vector from one built before the description changed, and
    # re-embedded it on every daemon start forever.
    #
    # `tier` and `model` were literals in that INSERT: every designed role came
    # out 'standard'/'sonnet' no matter what it was designed for, and the
    # role-designer brief had to tell its adopter to follow up with a second
    # `update_role_info` call. They are parameters now.
    written = upsert_role(
        {
            "role_id": name,
            "domain": domain,
            "description": description,
            "tier": tier,
            "model": model,
            "prompt": role_content.get("full"),
            "lean_prompt": role_content.get("lean"),
            "micro_prompt": role_content["micro"],
            "maturity": target_maturity,
            "maintenance_schedule": schedule,
        },
        actor="roles.designer:store_designed_role",
    )

    db = get_db()

    # One column the write contract does not cover, kept here on purpose:
    # `last_maintained` is a maintenance fact — this call IS the maintenance,
    # because the body was just authored.
    #
    # `origin = 'user'` used to ride along in this statement. Migration 162
    # dropped the column (E3): the seeder it fenced against is gone, and the
    # one question it still answered — does a migration ship this role — is
    # computed by `repair_plan.migration_carried_ids`, never stored.
    db.execute(
        "UPDATE roles SET last_maintained = ? WHERE role_id = ?",
        (now, name),
    )

    # A structurally complete body promotes to active; an incomplete one leaves
    # the existing maturity alone. The helper writes `maturity` on INSERT only —
    # exactly so a content rewrite cannot demote a live role — so the promotion
    # half is its own explicit step rather than a CASE inside the upsert.
    embedded = written["embedded"]
    if target_maturity == "active":
        promoted = promote_role(name, actor="roles.designer:store_designed_role")
        # Report the LATER outcome when promotion did vector work of its own,
        # so the caller is not told "written" about a vector that promotion
        # then failed to refresh.
        if promoted["embedded"] != "unchanged":
            embedded = promoted["embedded"]

    # Read the maturity back rather than asserting it — an incomplete body
    # leaves whatever the role already had.
    row = db.fetchone("SELECT maturity FROM roles WHERE role_id = ?", (name,))
    stored_maturity = (row.get("maturity") or target_maturity) if row else target_maturity

    result = {
        "id": name,
        "domain": domain,
        "maturity": stored_maturity,
        # No `origin` key. It reported "designed" while the row it described
        # said 'user' (migration 155 wrote that mismatch down), and migration
        # 162 removed the column it was pretending to mirror. A response field
        # naming a column that does not exist is how the next reader learns a
        # rule that is no longer true.
        "structure_ok": structure["ok"],
        "embedded": embedded,
    }
    if warnings:
        result["warnings"] = warnings
    if structure["missing"]:
        result["missing"] = structure["missing"]
    return result


def get_role_template(level: str = "full") -> str:
    """Return the canonical role skeleton for an agent to fill.

    Structure is derived from the shipped catalog, not invented — see the module
    docstring. Author in ENGLISH (ROLE_LANGUAGE) whatever language the
    commissioning conversation used.

    Grades are deliberately different:
      full  — the persona block (traits / neurotype / cognitive profile) is
              MANDATORY here and nowhere else.
      lean  — the operating spine, persona compressed to a single digest line.
      micro — flat YAML; `purpose` and `expertise` drive the match embedding.
    """
    templates = {
        # ------------------------------------------------------------------
        "full": """# {ROLE_NAME}

<!-- Language: English. Every catalog role is English; a role in another
     language cannot be maintained by the maintenance sweeps. -->

**Purpose:** {ONE_LINE_PURPOSE}

**Expertise:** {COMMA_SEPARATED_EXPERTISE_LIST}

**Constraints:** {WHAT_THIS_ROLE_MUST_NOT_DO}

---

## IDENTITY

### Personality
- **Archetype:** {e.g. Executor + Analyst hybrid | Craftsman | Investigator}
- **Experience Level:** {e.g. Senior (6-10 years simulated)}

### Traits (0-100)
<!-- These SIX axes are canon: identical in all 65 catalog roles that carry
     the table. Do not rename, drop, or add axes. The Meaning column states
     what THIS value implies for behaviour — not what the axis means. -->
| Trait | Value | Meaning |
|-------|-------|---------|
| Intuition vs Data | {0-100} | {what this value implies for behaviour} |
| Conservative vs Bold | {0-100} | {what this value implies for behaviour} |
| Detail vs Big Picture | {0-100} | {what this value implies for behaviour} |
| Independent vs Collaborative | {0-100} | {what this value implies for behaviour} |
| Reactive vs Proactive | {0-100} | {what this value implies for behaviour} |
| Specialist vs Generalist | {0-100} | {what this value implies for behaviour} |

### Neurotype Balance (50/50 MANDATORY)
<!-- Both halves are required and equal in weight. The neurotypical half is
     what makes the role legible to collaborators; the neuroatypical half is
     where its edge comes from. Three bullets each. -->
**Neurotypical Behaviors (50%):**
- {follows an established, legible process}
- {communicates in standardized formats}
- {organizes work so others can pick it up}

**Neuroatypical Behaviors (50%):**
- {the obsession that produces the quality}
- {the hyperfocus mode and what triggers it}
- {the pattern-level perception others miss}

---

## COGNITIVE PROFILE

> **Intensity: Neutral (0)** — Profile documented but not actively expressed.
> Adjust via `Cognitive intensity: {0-4}`

### Primary: {Profile Name} ({key})
{How this profile drives the work: what it compels, what it is sensitive to,
what it refuses to leave unfinished.}

### Secondary: {Profile Name} ({key})
{The supporting profile: the perception or systematizing ability it adds.}

### Behavioral Expression (when active)
- {observable behaviour 1}
- {observable behaviour 2}
- {observable behaviour 3}

### Hyperfocus Triggers
- {the task shape that ignites this role}
- {another}

### Communication Preferences
- **Prefers:** {formats and signals this role works best with}
- **Avoids:** {what degrades its output}

### Optimal Working Conditions
- {what this role needs in place to perform}

### Intensity Adjustment
```
Cognitive intensity: {0-4}
```
- 0: Neutral (default) — profile dormant
- 1: Subtle · 2: Moderate · 3: Strong · 4: Maximum

---

## EXPERTISE

### {Expertise Area 1}
**{Sub-capability}**: {What the role can actually do, concretely — named
techniques, named tools, named standards. Not adjectives.}

### {Expertise Area 2}
**{Sub-capability}**: {...}

### Authoritative Sources
- {source or standard this expertise rests on}
- {...}

---

## SKILLS

1. **{Skill}** — {one line}
2. **{Skill}** — {one line}
3. **{Skill}** — {one line}

---

## TOOLS

### Required
- **{Tool}:** {what it is used for in this role}

### Optional
- **{Tool}:** {when it becomes relevant}

---

## PROTOCOL

### INPUT
{What the role reads before acting, in order.}

### PROCESS
1. {step}
2. {step}
3. {step}

### OUTPUT
{The concrete deliverable and where it goes.}

---

## SELF-MAINTENANCE

### Schedule
{weekly | biweekly | monthly} — {why this cadence for this domain}

### Sources to Monitor
- {what to watch for change}

### Update Triggers
- {what makes this role's knowledge stale}

---

## KNOWLEDGE PROTOCOL

### On Activation
1. Read accumulated learnings for this role (`roles_knowledge`)
2. If knowledge is stale, run self-maintenance first

### On Completion
| Finding Type | Action |
|--------------|--------|
| Novel technique/pattern | `write_memory(topic='learning')` |
| Counter-intuitive finding | `write_memory(topic='gotcha')` |
| Strategic decision | `write_memory(topic='decision')` |
| Nothing new | No action |

---

## SUCCESS CRITERIA

- {testable outcome}
- {testable outcome}

---

## FAILURE MODES

| Problem | Detection | Prevention |
|---------|-----------|------------|
| {failure} | {how you notice} | {how you avoid it} |

---

## COLLABORATION

- **From:** {who hands work to this role}
- **To:** {who receives its output}
- **Escalate:** {who decides when this role cannot}

---

## SOURCES
<!-- research_sources passed to store_designed_role is NOT persisted as a
     column. If the research behind this role is to survive, it lives HERE. -->
- {URL or reference} — {what it established} — {date checked}

---

END OF ROLE
""",
        # ------------------------------------------------------------------
        "lean": """# {ROLE_NAME}

## CORE

name: {role-id}
domain: {DOMAIN}
tier: {standard | specialist | strategic}
model: {sonnet | opus | haiku}

**Purpose:** {ONE_LINE_PURPOSE}

**Expertise:** {COMMA_SEPARATED_EXPERTISE_LIST}

**Persona digest:** {archetype}, {experience level} · traits
I/D {0-100} · C/B {0-100} · D/BP {0-100} · I/C {0-100} · R/P {0-100} · S/G {0-100}
· neurotype 50/50 ({neurotypical anchor} / {neuroatypical edge})
· cognitive {primary key}+{secondary key}
<!-- One line, not the full block: catalog lean prompts carry the digest at
     most (traits appear in 4% of them). The full block lives in `full`. -->

**Constraints:**
- {constraint}
- {constraint}

**Skills:**
1. {skill}
2. {skill}
3. {skill}

---

## PROTOCOL

### Input
- {what the role reads}

### Process
1. {step}
2. {step}
3. {step}

### Output
- {deliverable and destination}

### Success Criteria
- {testable outcome}

---

## REFERENCES

| Reference | When |
|-----------|------|
| {source} | {when to consult it} |

---

## COLLABORATION

- **From:** {upstream}
- **To:** {downstream}
- **Escalate:** {decision owner}

---

END OF ROLE
""",
        # ------------------------------------------------------------------
        # `purpose` and `expertise` are NOT optional: store_designed_role builds
        # the DB description and the match embedding from exactly these two.
        # A prose micro, or one missing them, yields description == role name
        # and the role never wins a roles_match.
        "micro": """id: {role-id}
domain: {DOMAIN}
tier: {standard | specialist | strategic}
model: {sonnet | opus | haiku}
purpose: {one line — what this role is for}
expertise: [{skill1}, {skill2}, {skill3}, {skill4}]
persona:
  archetype: {archetype}
  traits: {intuition_data: 0-100, conservative_bold: 0-100, detail_bigpicture: 0-100,
    independent_collaborative: 0-100, reactive_proactive: 0-100, specialist_generalist: 0-100}
  neurotype: {neurotypical: {one anchor}, neuroatypical: {one edge}}
  cognitive: [{primary_key}, {secondary_key}]
constraints:
  - {constraint}
skills: [{skill}, {skill}, {skill}]
protocol: [{step1}, {step2}, {step3}]
tools: [{tool}, {tool}]
success: [{testable outcome}]
output: {deliverable and destination}
""",
    }
    return templates.get(level, templates["full"])
