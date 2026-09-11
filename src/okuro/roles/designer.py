# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Role designer: storage API for agent-generated roles.
# index: imports | canonical structure | def validate_role_structure | def store_designed_role | def get_role_template
# AGENT_HEADER_END -->
"""Role designer: storage API for agent-generated roles.

The canonical role structure below is DERIVED from the shipped catalog
(src/okuro/roles/catalog/*.yaml, 85 roles measured 2026-08-25), not invented.
Reproduce the measurement with:

    .venv/bin/python -m okuro.roles.audit_structure

Grade-specific, because the grades genuinely differ in the catalog:
  full  — carries the persona block (traits/neurotype/cognitive profile): 74-80%
  lean  — does NOT carry the persona block (2-8%); CORE/PROTOCOL/COLLABORATION
  micro — flat YAML; `purpose` + `expertise` feed the description AND embedding
"""

import json
import re
from datetime import datetime, timezone

import yaml

from okuro.db import get_db
from okuro.embed import embed_one
from okuro.embed.client import to_bytes


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


def validate_role_structure(role_content: dict) -> dict:
    """Check role_content against the canonical structure, per grade.

    Returns:
        {
          "ok": bool,               # no REQUIRED section missing in any grade
          "missing": {grade: [...]},   # required, gating
          "advisory": {grade: [...]},  # recommended, non-gating
        }
    """
    missing: dict[str, list[str]] = {}
    advisory: dict[str, list[str]] = {}

    def _scan(grade: str, text: str, required, recommended) -> None:
        if not (text or "").strip():
            missing[grade] = [f"grade '{grade}' is empty"]
            return
        miss = [label for label, pat in required
                if not re.search(pat, text, re.M | re.I)]
        adv = [label for label, pat in recommended
               if not re.search(pat, text, re.M | re.I)]
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
                miss = [f"micro key '{k}'" for k in MICRO_REQUIRED_KEYS if not parsed.get(k)]
                adv = [f"micro key '{k}'" for k in MICRO_RECOMMENDED_KEYS if not parsed.get(k)]
                if miss:
                    missing["micro"] = miss
                if adv:
                    advisory["micro"] = adv

    return {"ok": not missing, "missing": missing, "advisory": advisory}


def store_designed_role(
    name: str,
    domain: str,
    role_content: dict,
    research_sources: list[str] | None = None,
    strict: bool = False,
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

    # Guard: reject artifact references
    for grade in ("full", "lean", "micro"):
        val = role_content.get(grade, "")
        if val and len(val) < 200 and any(
            val.strip().lower().startswith(prefix)
            for prefix in ("see artifact", "see file", "see task", "refer to")
        ):
            return {
                "error": f"role_content['{grade}'] contains an artifact reference "
                f"instead of actual content ({len(val)} chars). "
                f"Pass the full role text, not a pointer."
            }

    # Structure gate — canonical sections, per grade
    structure = validate_role_structure(role_content)
    warnings: list[str] = []
    if not structure["ok"]:
        flat = "; ".join(
            f"{grade}: {', '.join(items)}" for grade, items in structure["missing"].items()
        )
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

    embedding = embed_one(description)
    emb_bytes = to_bytes(embedding)
    db = get_db()

    with db.transaction():
        db.execute(
            "INSERT INTO roles (role_id, domain, description, maturity, "
            "maintenance_schedule, last_maintained, "
            "prompt, lean_prompt, micro_prompt, tier, model, origin) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'standard', 'sonnet', 'user') "
            "ON CONFLICT(role_id) DO UPDATE SET "
            "domain = excluded.domain, "
            "description = excluded.description, "
            # A structurally complete body promotes to active; an incomplete one
            # leaves the existing maturity alone — never demotes a live role.
            "maturity = CASE WHEN excluded.maturity = 'active' "
            "THEN 'active' ELSE roles.maturity END, "
            "last_maintained = excluded.last_maintained, "
            "prompt = excluded.prompt, "
            "lean_prompt = excluded.lean_prompt, "
            "micro_prompt = excluded.micro_prompt",
            (
                name,
                domain,
                description,
                target_maturity,
                schedule,
                now,
                role_content.get("full"),
                role_content.get("lean"),
                role_content["micro"],
            ),
        )

        db.execute("DELETE FROM vec_roles WHERE id = ?", (name,))
        db.execute(
            "INSERT INTO vec_roles (id, embedding) VALUES (?, ?)",
            (name, emb_bytes),
        )

    # Read the maturity back rather than asserting it — the upsert's CASE may
    # have kept the pre-existing value.
    row = db.fetchone("SELECT maturity FROM roles WHERE role_id = ?", (name,))
    stored_maturity = (row.get("maturity") or target_maturity) if row else target_maturity

    result = {
        "id": name,
        "domain": domain,
        "maturity": stored_maturity,
        "origin": "designed",
        "structure_ok": structure["ok"],
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
