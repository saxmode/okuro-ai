# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Onboarding API — dependency-gated wizard with 9 steps.
# index:
#   imports
#   class OnboardingStep
#   class OnboardingState
#   class DetectionResult
#   class ProfilePatch
#   class DesignOption
#   class ScrapeRequest
#   class QuestionnaireItem
#   class NeurodivergenceInfo
#   class QuestionnaireAnswers
#   class KeyringInitRequest
#   class _StepDef
#   STEP_DEFS
#   def _resolve_steps
#   def _load_profile
#   def _save_profile
#   def _merge_profile_patch
#   def _map_answers
#   def _check_logged_in
#   def _cli_status
#   def _keyring_status
#   def _check_cli_setup .. _check_design (step check functions)
#   def get_state
#   def detect_system
#   def get_profile_endpoint
#   def patch_profile
#   def init_keyring
#   def canon_skip
#   def complete_onboarding
#   def list_design_options
#   def scrape_design_from_url
#   def get_questionnaire
#   def submit_questionnaire
# AGENT_HEADER_END -->
"""Onboarding API — backend endpoints for the 5-screen wizard.

Covers:
- State introspection (what steps are done, what's missing)
- System detection passthrough (hardware, CLIs, timezone)
- User profile read/patch/save
- Completion marker

The frontend wizard (React) lives in ``src/okuro/web/`` and is served
from the same FastAPI process thanks to the A1 merge. Design decisions
for the wizard itself are in ``docs/okuro-packaging-incl-web.md §7``
and ``docs/onboarding-communication-style-research.md``.

Contract notes:
- Okuro has **zero API keys of its own** (decision O3). The onboarding
  "tools" screen is a CLI status board, not a key-entry form.
- The questionnaire is 10 IPIP-based forced-choice items (decision O4
  + research spike). This endpoint returns the inferred profile fields
  — the frontend is responsible for rendering the questions and
  submitting the answer mapping.
- The design step ships with 1 neutral baseline + a light URL scraper
  (decision O2). The scraper is NOT implemented here yet — that's a
  Phase 4 follow-up. For now ``/api/onboarding/design/options`` just
  returns the baseline.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from okuro.orchestrator.yamlfast import yload

logger = logging.getLogger("okuro.orchestrator.api.onboarding")

router = APIRouter(prefix="/api/onboarding", tags=["onboarding"])


# Hook coverage, as told to the UI by BOTH /canon/targets and /canon/deploy.
# One constant because the previous copy lived in two places and drifted in
# both: it claimed hooks were "Claude-only" long after the (since retired)
# gemini adapter grew
# BeforeAgent/AfterAgent hooks, so the API told users something this repo's own
# template.py contradicted.
#
# It states what OKURO DOES, not what the ecosystem supports. Cursor ships a
# real hook system too (it even exposes beforeMCPExecution); okuro just hasn't
# implemented a per-turn hook there yet — that is a policy gap, not a
# capability gap.
#
# Keep in sync with sense/providers/template.py's per-provider extras and the
# adapters' install_hooks(); tests/orchestrator/api/test_onboarding_canon_deploy.py
# asserts this note matches which adapters actually install hooks.
_HOOK_ASYMMETRY_PREFIX = (
    "Note: okuro installs a per-turn communication hook on every provider it "
    "detects. Enforcement — a gate that can REFUSE — is not uniform, and the "
    "rule-by-rule truth is measured from what each adapter actually emits: "
)

_HOOK_ASYMMETRY_SUFFIX = (
    " Everything outside those rules is advisory on every provider. The Cursor "
    "gate is compiled from the same tool_routing contract as the others but "
    "has not yet been observed firing on a live Cursor install."
)


def _hook_asymmetry_note() -> str:
    """The coverage note, DERIVED — never a hand-written claim.

    This string has been factually wrong twice. It called enforcement
    "Claude-only" after a second adapter gained hooks, and it told users Cursor
    was hooks-less while Cursor shipped a GA hook system. Both times the prose
    was the last thing anyone updated, because a string is not reachable by any
    coverage-based selection.

    The test guarding it derives WHICH ADAPTERS install hooks. That is a
    boolean per adapter, and it stays green while a provider installs a
    per-turn injection hook and no gate at all — which is exactly what codex
    and antigravity do. So the note was true by accident, at the wrong grain.

    The middle clause now comes from the same measurement the gate parity test
    uses, so porting a rule to a second provider rewrites this sentence with no
    prose to remember.
    """
    from okuro.sense.providers import get_provider
    from okuro.sense.providers._gate_contract import describe_gate_coverage

    adapters = []
    for name in ("claude", "codex", "cursor", "antigravity"):
        try:
            adapters.append(get_provider(name))
        except Exception:  # an adapter that cannot load enforces nothing
            continue
    return _HOOK_ASYMMETRY_PREFIX + describe_gate_coverage(adapters) + "." + _HOOK_ASYMMETRY_SUFFIX


# ── Models ────────────────────────────────────────────────────────────


class OnboardingStep(BaseModel):
    key: str
    label: str
    done: bool
    detail: Optional[str] = None
    locked: bool = False               # hard prerequisite unmet
    skippable: bool = True             # gates=true → not skippable
    enhanced_by: list[str] = Field(default_factory=list)  # completed enhancer steps
    # Sprint-2F: when true, skipping this step requires the frontend to
    # POST ``consent_to_skip=true`` to the relevant skip endpoint.
    requires_explicit_skip_consent: bool = False


class OnboardingState(BaseModel):
    steps: list[OnboardingStep]
    completed: bool
    completed_at: Optional[str] = None
    current_step: Optional[str] = None  # first not-done, not-locked step
    # /complete side-effects — populated only by the /complete handler so the
    # wizard frontend can surface install errors instead of pretending Done.
    # ``services_install`` is the dict returned by install_all_services
    # (e.g. ``{"okuro-orchestrator": "started", "okuro-daemon": "error: ..."}``);
    # ``services_install_error`` is set if install_all_services itself raised
    # before producing per-service results.
    services_install: Optional[dict[str, str]] = None
    services_install_error: Optional[str] = None
    # Path to the comprehensive install-report JSON written at /complete time.
    # Stable path: ~/.okuro/install-report.json. The wizard's Done page should
    # surface this so the user can paste the file when reporting issues.
    install_report_path: Optional[str] = None
    install_report_error: Optional[str] = None


class DetectionResult(BaseModel):
    os: dict
    shell: str
    timezone: str
    python: dict
    gpus: list[dict]
    providers: dict[str, Any]  # {cli: {installed, path, logged_in}}
    tools: dict[str, bool]


class ProfilePatch(BaseModel):
    """Partial profile update. Each top-level key replaces that section.

    Frontends typically submit one screen's worth of fields at a time
    (e.g. ``{"identity": {...}}`` after the Identity screen).

    ``extra="allow"`` lets UI-state keys (``welcome``, ``onboarding``,
    future panel-dismiss flags) flow through without needing a model
    edit every time a new one is added — without it, pydantic silently
    drops unknown keys and the PATCH returns 200 with no visible effect.
    """

    model_config = ConfigDict(extra="allow")

    identity: Optional[dict] = None
    cognitive_style: Optional[dict] = None
    communication: Optional[dict] = None
    expertise: Optional[dict] = None
    work_style: Optional[dict] = None
    decision_style: Optional[dict] = None
    boundaries: Optional[dict] = None
    principles: Optional[dict] = None


class DesignOption(BaseModel):
    id: str
    name: str
    description: str
    source: str = Field(..., description="'baseline' or 'scraped'")
    colors: Optional[dict] = None


class ScrapeRequest(BaseModel):
    url: str
    name: Optional[str] = None


class ExtractRequest(BaseModel):
    urls: list[str] = Field(..., min_length=1, max_length=6)
    name: Optional[str] = None


class QuestionnaireItem(BaseModel):
    id: int
    question: str
    option_a: str
    option_b: str


class NeurodivergenceInfo(BaseModel):
    disclosed: bool = False
    details: Optional[str] = None


class QuestionnaireAnswers(BaseModel):
    """Answers to the 10-item forced-choice questionnaire.

    Each answer is 'a' or 'b'. Keys are question IDs (1-10).
    """
    answers: dict[str, str]
    neurodivergence: Optional[NeurodivergenceInfo] = None


class KeyringInitRequest(BaseModel):
    """Master password for keyring initialization.

    Either ``password`` (user-chosen) or ``generate=True`` (system-picked,
    stored in the OS keychain so the user never has to remember it).
    Enforced in the endpoint: one of the two must be set.
    """
    password: Optional[str] = Field(
        default=None,
        min_length=8,
        description="Master password (min 8 chars). Omit if generate=true.",
    )
    generate: bool = Field(
        default=False,
        description="Let okuro pick a strong random password and store it in the OS keychain.",
    )


class CognitiveTraitAnswers(BaseModel):
    """Answers to cognitive trait preference questions."""
    answers: dict[str, str] = Field(..., description="trait_id → 'a' or 'b'")
    neurodivergent: bool = Field(default=False, description="Optional self-disclosure")


# ── Step Dependency Engine ───────────────────────────────────────────
# Each step declares its dependencies (requires/enhances/gates).
# get_state() topologically sorts and computes locked/enhanced status.
# Adding a new step = one entry here + one check function + one UI component.

class _StepDef:
    """Internal step definition with dependency metadata."""
    __slots__ = (
        "key",
        "label",
        "requires",
        "enhances",
        "gates",
        "writes",
        "check",
        "requires_explicit_skip_consent",
    )

    def __init__(
        self,
        key: str,
        label: str,
        check: str,                    # name of _check_* function
        requires: tuple[str, ...] = (),
        enhances: tuple[str, ...] = (),
        gates: bool = False,
        writes: tuple[str, ...] = (),
        requires_explicit_skip_consent: bool = False,
    ):
        self.key = key
        self.label = label
        self.check = check
        self.requires = requires
        self.enhances = enhances
        self.gates = gates
        self.writes = writes
        # Sprint-2F audit gap #30: even though a step is technically
        # ``gates=False`` (skippable to keep the wizard moving), some
        # steps have hidden costs when skipped — agents lose all okuro
        # context. The frontend wizard MUST set ``consent_to_skip=true``
        # on the skip endpoint when this flag is true; the backend
        # enforces the contract.
        self.requires_explicit_skip_consent = requires_explicit_skip_consent


# Step registry — order here is the default presentation order.
# Topological sort respects `requires` edges; within the same
# dependency tier, this order is preserved.
STEP_DEFS: list[_StepDef] = [
    _StepDef(
        key="cli_setup",
        label="AI Tools",
        check="_check_cli_setup",
        gates=True,
    ),
    _StepDef(
        # User chose an embedding tier + device. The file
        # ~/.okuro/embed-config.yaml is the persistent record; the step
        # is "done" once chosen_by is "onboarding" or "cli" (i.e. the
        # user actively made the choice, not just the auto fallback).
        # gates=False — onboarding never blocks here; install.py falls
        # back to the system recommendation if the user skipped.
        key="embed_setup",
        label="Embedding model",
        check="_check_embed_setup",
        requires=("cli_setup",),
        gates=False,
    ),
    _StepDef(
        # H6: explicit consent before okuro writes to the user's home dir.
        # Runs after cli_setup so the user has at least one CLI to register
        # the MCP server with. `gates=False` → the user may skip; the /complete
        # safety net has been removed, so skipping means *nothing gets written*
        # (and agents in that CLI won't see the behavioral contract until the
        # user re-runs the deploy from settings). That's the explicit trade-off.
        key="canon_deploy",
        label="Connect AI tools",
        check="_check_canon_deploy",
        requires=("cli_setup",),
        # Audit #30: skipping silently leaves agents with no okuro
        # context. The wizard may still proceed (gates=False) so users
        # aren't trapped, but the skip path now requires
        # ``consent_to_skip=true`` on POST /api/onboarding/canon/skip.
        requires_explicit_skip_consent=True,
    ),
    _StepDef(
        key="identity",
        label="Who are you?",
        check="_check_identity",
        writes=("identity",),
    ),
    _StepDef(
        key="profile_sources",
        label="Import your profile",
        check="_check_profile_sources",
        requires=("cli_setup",),
        enhances=("communication", "cognitive_style", "design"),
        writes=("expertise", "communication", "work_style", "identity"),
    ),
    _StepDef(
        key="communication",
        label="How should agents work with you?",
        check="_check_communication",
        writes=("communication", "work_style"),
    ),
    _StepDef(
        key="cognitive_style",
        label="How your mind works",
        check="_check_cognitive_style",
        writes=("cognitive_style",),
    ),
    _StepDef(
        key="principles",
        label="What matters to you",
        check="_check_principles",
        writes=("principles",),
    ),
    _StepDef(
        key="boundaries",
        label="Agent permissions",
        check="_check_boundaries",
        writes=("boundaries",),
    ),
    _StepDef(
        key="keyring",
        label="Secrets vault",
        check="_check_keyring_step",
        # gates=True — keyring MUST be initialized before onboarding can
        # complete. The downstream bridge/providers flow assumes secrets
        # can be stored; a silently-skipped vault means "Run: okuro init"
        # appears in the doctor and the user hits a wall later.
        gates=True,
    ),
    _StepDef(
        key="design",
        label="Visual identity",
        check="_check_design",
        writes=("design",),
    ),
]

_STEP_BY_KEY: dict[str, _StepDef] = {s.key: s for s in STEP_DEFS}


def _resolve_steps(done_keys: set[str]) -> list[OnboardingStep]:
    """Topologically sort steps and compute locked/enhanced/skippable state."""
    result: list[OnboardingStep] = []

    for sdef in STEP_DEFS:
        # Locked if any hard prerequisite is not done
        locked = any(req not in done_keys for req in sdef.requires)

        # Which enhancer steps are completed
        enhanced_by = [e for e in STEP_DEFS
                       if sdef.key in e.enhances and e.key in done_keys]

        result.append(OnboardingStep(
            key=sdef.key,
            label=sdef.label,
            done=sdef.key in done_keys,
            locked=locked,
            skippable=not sdef.gates,
            enhanced_by=[e.key for e in enhanced_by],
            requires_explicit_skip_consent=sdef.requires_explicit_skip_consent,
        ))

    return result


# ── Helpers ───────────────────────────────────────────────────────────


def _load_profile() -> dict:
    """Read the current profile dict. Returns {} if the row is missing."""
    try:
        from okuro.yu.profile import get_profile_raw

        return get_profile_raw()
    except Exception as exc:
        logger.warning("failed to load profile: %s", exc)
        return {}


def _save_profile(profile: dict) -> None:
    from okuro.db import get_db

    db = get_db()
    row = db.fetchone("SELECT id FROM user_profile WHERE id = 1")
    payload = json.dumps(profile)
    if row:
        db.execute(
            "UPDATE user_profile SET profile = ?, updated_at = datetime('now') WHERE id = 1",
            (payload,),
        )
    else:
        db.execute(
            "INSERT INTO user_profile (id, profile) VALUES (1, ?)",
            (payload,),
        )
    db.conn.commit()

    # Fire-and-forget: if the write introduced any new bare-string rules,
    # enrich them in the background so the next bootstrap serves the full
    # triple. Safe no-op when nothing needs enrichment.
    try:
        from okuro.sense.rules import auto_enrich_async
        auto_enrich_async()
    except Exception as exc:
        logger.debug("auto_enrich_async skipped: %s", exc)

    # Regenerate the provider instruction files, hook caches, AND the Claude
    # Code system-tier output style so an edit to the communication profile
    # takes effect without waiting for a daemon tick. Debounced (a settings
    # screen saves as a burst of PATCH calls) via the same timer that
    # yu.profile.update_profile() uses — patch_profile() previously wrote the
    # DB only and relied on the enrichment daemon's eventual refresh, so a
    # comm-profile edit did NOT regenerate CLAUDE.md / the output style / the
    # hook payload until the next tick. This closes that gap for every profile
    # write through the onboarding + settings API.
    try:
        from okuro.yu.profile import _schedule_regen
        _schedule_regen()
    except Exception as exc:
        logger.debug("provider-instruction regen scheduling skipped: %s", exc)


def _merge_profile_patch(base: dict, patch: ProfilePatch) -> dict:
    """Deep-merge a patch onto the base profile.

    A screen-level patch like ``{"communication": {"response_length":
    "concise"}}`` should update that one field, not replace the whole
    ``communication`` section. Dict-to-dict merges recurse so nested
    writes (``format_preferences.preferred``) compose with earlier
    writes from the questionnaire step. Non-dict values (scalars, lists)
    still replace.
    """
    patch_dict = patch.model_dump(exclude_none=True)
    merged = json.loads(json.dumps(base))  # deep clone so caller's base is untouched
    _deep_merge(merged, patch_dict)
    return merged


QUESTIONNAIRE: list[dict] = [
    {"id": 1, "question": "When someone gives you information, you prefer they...",
     "option_a": "lead with the conclusion, then explain",
     "option_b": "walk you through the reasoning first"},
    {"id": 2, "question": "Your ideal message is...",
     "option_a": "short, even if a little ambiguous",
     "option_b": "precise, even if a little longer"},
    {"id": 3, "question": "You'd rather a teammate be...",
     "option_a": "warm and careful with phrasing",
     "option_b": "blunt and efficient"},
    {"id": 4, "question": "When reading a report, you prefer...",
     "option_a": "bullet points and tables",
     "option_b": "flowing paragraphs"},
    {"id": 5, "question": "When making a decision, you want to see...",
     "option_a": "one clear recommendation",
     "option_b": "several options with trade-offs"},
    {"id": 6, "question": "You enjoy working through...",
     "option_a": "complex, abstract problems",
     "option_b": "concrete, well-defined tasks"},
    {"id": 7, "question": "You'd rather finish something...",
     "option_a": "quickly, then iterate",
     "option_b": "slowly, and get it right the first time"},
    {"id": 8, "question": "When plans change mid-task, you prefer to be...",
     "option_a": "told immediately, even if it interrupts",
     "option_b": "told at a natural pause"},
    {"id": 9, "question": "When something goes wrong, you'd rather...",
     "option_a": "stop and be alerted right away",
     "option_b": "let the system continue and summarise at the end"},
    {"id": 10, "question": "In a long conversation, you prefer...",
     "option_a": "consistent structure and format every time",
     "option_b": "variety in format, even if less predictable"},
]


def _deep_merge(base: dict, incoming: dict) -> dict:
    """Recursively merge ``incoming`` into ``base``. Dict values merge key
    by key; everything else (scalars, lists) is replaced. A ``None`` value
    in ``incoming`` removes the corresponding key from ``base`` — needed
    so the design picker's reset can actually delete an override instead
    of leaving a stale entry.

    Needed because the questionnaire now writes nested structures like
    ``communication.format_preferences.preferred`` — a shallow
    ``base.update(patch)`` would stomp sibling keys (``avoid``).
    """
    for key, value in incoming.items():
        if value is None:
            base.pop(key, None)
        elif (
            isinstance(value, dict)
            and isinstance(base.get(key), dict)
        ):
            _deep_merge(base[key], value)
        else:
            base[key] = value
    return base


def _map_answers(answers: dict[str, str]) -> dict[str, Any]:
    """Map questionnaire answers to profile fields.

    Writes the same shape the bootstrap behavioral section reads:
      * ``communication.patterns``          — list of rule strings
      * ``communication.response_length``   — minimal | balanced | thorough
      * ``communication.directness``        — diplomatic | blunt
      * ``communication.format_preferences.preferred`` — nested list
      * ``communication.pacing``            — fast | reflective (reference)
      * ``decision_style.framing``          — recommendation | options-with-pros-cons
      * ``decision_style.speed_vs_quality`` — speed | quality (was dropped
        into work_style by the previous mapper, which the behavioral
        section never reads)
      * ``cognitive_style.abstraction``     — abstract | concrete
      * ``error_handling``                  — top-level scalar, used by
        reminders/strategy

    See docs/onboarding-communication-style-research.md § 4.
    """
    patterns: list[str] = []
    comm: dict[str, Any] = {}
    decision_style: dict[str, str] = {}
    cognitive_style: dict[str, str] = {}

    a = answers.get

    # Q1: lead with answer vs walk through reasoning
    if a("1") == "a":
        patterns.append("lead with answer")
    else:
        patterns.append("walk through reasoning")

    # Q2: short vs precise → response_length (what build_profile actually reads)
    if a("2") == "a":
        comm["response_length"] = "concise"
    else:
        comm["response_length"] = "detailed"

    # Q3: diplomatic vs blunt
    if a("3") == "a":
        comm["directness"] = "diplomatic"
    else:
        comm["directness"] = "blunt"

    # Q4: bullets vs prose → format_preferences as NESTED dict so the
    # behavioral section's load_section("preferred") resolves, and the
    # existing .avoid defaults survive the deep merge.
    if a("4") == "a":
        comm["format_preferences"] = {"preferred": ["bullet lists", "tables"]}
        comm["structure"] = "highly-structured"
    else:
        comm["format_preferences"] = {"preferred": ["long paragraphs"]}
        comm["structure"] = "freeform"

    # Q5: recommendation vs options → decision_style.framing (nested so the
    # existing decision_style dict — speed_vs_quality, risk_tolerance,
    # build_vs_buy — survives the merge).
    if a("5") == "a":
        decision_style["framing"] = "recommendation"
    else:
        decision_style["framing"] = "options-with-pros-cons"

    # Q6: abstract vs concrete
    if a("6") == "a":
        cognitive_style["abstraction"] = "abstract"
        if comm.get("response_length") == "concise":
            comm["response_length"] = "balanced"
    else:
        cognitive_style["abstraction"] = "concrete"

    # Q7: speed vs quality → decision_style.speed_vs_quality (this is what
    # build_behavioral_section's **Approach:** line actually reads).
    if a("7") == "a":
        comm["pacing"] = "fast"
        decision_style["speed_vs_quality"] = "speed"
    else:
        comm["pacing"] = "reflective"
        decision_style["speed_vs_quality"] = "quality"

    # Q8+Q9: error handling (Q9 authoritative, Q8 tiebreaker)
    q8 = a("8")
    q9 = a("9")
    if q9 == "a":
        error_handling = "interrupt-on-error"
    elif q9 == "b":
        error_handling = "summarize-at-end"
    elif q8 == "a":
        error_handling = "interrupt-on-error"
    else:
        error_handling = "log-and-continue"

    # Q10: consistent vs varied
    if a("10") == "a":
        if comm.get("structure") != "freeform":
            comm["structure"] = "highly-structured"
        patterns.append("consistent format")
    else:
        comm["structure"] = "semi-structured"
        patterns.append("vary format")

    comm["patterns"] = patterns

    return {
        "communication": comm,
        "decision_style": decision_style,
        "error_handling": error_handling,
        "cognitive_style": cognitive_style,
    }


def _check_logged_in(name: str, home: Path) -> Optional[bool]:
    """Compatibility shim — delegates to ``cli_probe``.

    The legacy implementation was a filesystem proxy: file/keychain
    existence ≠ valid session. ``cli_probe`` is the canonical detector
    used by the wizard, dashboard, bridge, and doctor; routing all
    callers through it eliminates the surface-disagreement that produced
    "wizard green, dashboard red" on the same machine. ``home`` is
    accepted for API compat but ignored — cli_probe reads the live home
    via ``Path.home()``.
    """
    try:
        from okuro.system.cli_probe import detect as _detect
        state = _detect(name)
    except (ValueError, Exception):
        return None
    if state.auth.state == "ok":
        return True
    # "ineligible" (valid creds, unentitled account) is a definite negative —
    # see cli_installer._cli_logged_in. Falling through to None would show the
    # user "unknown" for something we proved.
    if state.auth.state in ("missing", "expired", "ineligible"):
        return False
    return None


def _cli_status() -> dict[str, dict]:
    """Return a CLI-status-board dict for the Tools & Subscriptions screen.

    Goes through ``cli_probe`` so the wizard's status board, the
    dashboard's status lights, the bridge's routing decisions, and
    ``okuro doctor`` all read from the same source. The legacy
    implementation called ``shutil.which`` (no enhanced PATH) and a
    file-existence auth proxy — that's exactly the disagreement this
    module exists to kill.

    Shape: ``{cli_name: {"installed": bool, "path": str | None, "logged_in": bool | None}}``.
    """
    from okuro.system.cli_probe import detect_all

    states = detect_all()
    out: dict[str, dict] = {}
    for name, st in states.items():
        if st.auth.state == "ok":
            logged_in: Optional[bool] = True
        elif st.auth.state in ("missing", "expired"):
            logged_in = False
        else:
            logged_in = None
        out[name] = {
            "installed": st.on_path,
            "path": st.path,
            "logged_in": logged_in,
        }
    return out


def _keyring_status() -> dict:
    """Check if the keyring vault is initialized."""
    try:
        from okuro.keyring import KeyringStorage
        store = KeyringStorage()
        return {"initialized": store.is_initialized}
    except Exception:
        return {"initialized": False}


# ── Step check functions ─────────────────────────────────────────────
# Each returns (done: bool, detail: str | None).
# Named to match _StepDef.check strings.

def _check_cli_setup() -> tuple[bool, Optional[str]]:
    """At least one INFERENCE CLI installed AND authenticated.

    Canon-driven: reads tools/clis/*.yaml via cli_installer. Cursor / VSCode
    are NOT inference CLIs — they consume okuro's MCP server and belong to
    a later step, not this gate.
    """
    from okuro.system.cli_installer import list_inference_clis

    clis = list_inference_clis()
    ready = [c["name"] for c in clis if c["installed"] and c["authenticated"]]
    if ready:
        return True, ", ".join(ready)
    installed = [c["name"] for c in clis if c["installed"]]
    if installed:
        return False, f"installed but not authenticated: {', '.join(installed)}"
    return False, None


def _check_identity() -> tuple[bool, Optional[str]]:
    profile = _load_profile()
    identity = profile.get("identity", {})
    name = identity.get("name") or identity.get("handle")
    return bool(name), name


def _check_profile_sources() -> tuple[bool, Optional[str]]:
    """Check if user has imported profile from external sources."""
    profile = _load_profile()
    sources = profile.get("profile_sources", {})
    if sources.get("imported"):
        return True, ", ".join(sources.get("sources", []))
    return False, None


def _check_communication() -> tuple[bool, Optional[str]]:
    profile = _load_profile()
    comm = profile.get("communication", {})
    return bool(comm.get("patterns") or comm.get("format_preferences")), None


def _check_cognitive_style() -> tuple[bool, Optional[str]]:
    profile = _load_profile()
    cog = profile.get("cognitive_style", {})
    return bool(cog), None


def _check_principles() -> tuple[bool, Optional[str]]:
    """Check if user has selected and prioritized principles."""
    profile = _load_profile()
    princ = profile.get("principles", {})
    selected = princ.get("selected", [])
    if selected:
        return True, f"{len(selected)} selected"
    return False, None


def _check_boundaries() -> tuple[bool, Optional[str]]:
    profile = _load_profile()
    bounds = profile.get("boundaries", {})
    return bool(bounds.get("ok_autonomous") or bounds.get("never_without_asking")), None


def _check_keyring_step() -> tuple[bool, Optional[str]]:
    kr = _keyring_status()
    return kr["initialized"], "initialized" if kr["initialized"] else None


def _check_design() -> tuple[bool, Optional[str]]:
    profile = _load_profile()
    design_profile = profile.get("design", {}).get("profile")
    return bool(design_profile), design_profile


def _check_canon_deploy() -> tuple[bool, Optional[str]]:
    """Done = at least one provider instruction file or TOOL-PROTOCOL.md exists.

    The canon-deploy step writes per-provider instruction files
    (``~/.claude/CLAUDE.md``, ``~/.gemini/AGENTS.md`` (antigravity),
    ``~/.codex/AGENTS.md``) plus the shared
    ``~/.okuro/TOOL-PROTOCOL.md``. The user may have deployed to a subset
    of detected providers, so we only require one file to consider the
    step done. Cursor currently ships an MCP config only (no adapter for
    instruction generation — H7 backlog), so its presence is not
    counted here.
    """
    from pathlib import Path

    home = Path.home()
    candidates = [
        home / ".claude" / "CLAUDE.md",
        # antigravity reuses ~/.gemini; its instruction file is AGENTS.md.
        # (The retired gemini CLI's GEMINI.md was dropped 2026-07-18.)
        home / ".gemini" / "AGENTS.md",
        home / ".codex" / "AGENTS.md",
        home / ".okuro" / "TOOL-PROTOCOL.md",
    ]
    for path in candidates:
        try:
            if path.is_file():
                return True, path.name
        except OSError:
            continue
    return False, None


def _check_embed_setup() -> tuple[bool, Optional[str]]:
    """Done iff embed-config.yaml exists and was written by the user.

    chosen_by="auto" is the system fallback (load_or_init ran without
    onboarding interaction); the step stays "pending" so the wizard
    surfaces the picker. chosen_by="onboarding"/"web"/"cli" all count
    as the user having made an explicit choice.
    """
    from okuro.embed import config as embed_cfg

    try:
        cfg = embed_cfg.load()
    except (ValueError, OSError):
        return False, None
    if cfg is None:
        return False, None
    if cfg.chosen_by == "auto":
        return False, f"auto-selected: {cfg.tier} / {cfg.device}"
    return True, f"{cfg.tier} / {cfg.device}"


# Map check function names to callables
_CHECK_FNS: dict[str, callable] = {
    "_check_cli_setup": _check_cli_setup,
    "_check_embed_setup": _check_embed_setup,
    "_check_canon_deploy": _check_canon_deploy,
    "_check_identity": _check_identity,
    "_check_profile_sources": _check_profile_sources,
    "_check_communication": _check_communication,
    "_check_cognitive_style": _check_cognitive_style,
    "_check_principles": _check_principles,
    "_check_boundaries": _check_boundaries,
    "_check_keyring_step": _check_keyring_step,
    "_check_design": _check_design,
}


# ── Endpoints ────────────────────────────────────────────────────────


@router.get("/state", response_model=OnboardingState)
def get_state() -> OnboardingState:
    """Report which onboarding steps are complete.

    Steps are defined in STEP_DEFS with dependency metadata. The engine
    runs each step's check function, computes locked/enhanced state via
    _resolve_steps(), and returns them in topological order.

    Adding a new step = one _StepDef entry + one _check_* function.
    """
    # Run all check functions to determine which steps are done
    done_keys: set[str] = set()
    details: dict[str, Optional[str]] = {}

    for sdef in STEP_DEFS:
        check_fn = _CHECK_FNS.get(sdef.check)
        if check_fn:
            try:
                done, detail = check_fn()
                if done:
                    done_keys.add(sdef.key)
                details[sdef.key] = detail
            except Exception as exc:
                logger.warning("step check %s failed: %s", sdef.key, exc)
                details[sdef.key] = None

    # Resolve dependencies and build step list
    steps = _resolve_steps(done_keys)

    # Attach details from check functions
    for step in steps:
        step.detail = details.get(step.key)

    # Completion status from profile
    profile = _load_profile()
    onboarding = profile.get("onboarding", {}) if isinstance(profile.get("onboarding"), dict) else {}
    completed_at = onboarding.get("completed_at")

    # Current step: first not-done AND not-locked step
    current = next((s.key for s in steps if not s.done and not s.locked), None)

    return OnboardingState(
        steps=steps,
        completed=bool(completed_at),
        completed_at=completed_at,
        current_step=current,
    )


@router.get("/clis")
def list_clis() -> list[dict]:
    """Return inference CLIs (claude-code / codex / antigravity) with install +
    auth state, backed by canon. Used by step 1 of the wizard to render the
    install/authenticate grid.
    """
    from okuro.system.cli_installer import list_inference_clis
    return list_inference_clis()


@router.get("/clis/{tool_id}/state")
def get_cli_state(tool_id: str) -> dict:
    """Single-tool state probe for polling after install/auth terminals open."""
    from okuro.system.cli_installer import detect_state
    return detect_state(tool_id)


@router.post("/clis/{tool_id}/install")
def install_cli_endpoint(tool_id: str) -> dict:
    """Spawn a terminal running the canon install command. Non-blocking —
    the frontend polls ``/clis/{tool_id}/state`` for completion.
    """
    from okuro.system.cli_installer import install_cli
    result = install_cli(tool_id)
    if result.get("status") in ("unknown_tool", "unsupported_os", "no_command_for_os"):
        raise HTTPException(status_code=400, detail=result)
    return result


@router.post("/clis/{tool_id}/authenticate")
def authenticate_cli_endpoint(tool_id: str) -> dict:
    """Spawn a terminal running the canon auth command. Non-blocking."""
    from okuro.system.cli_installer import authenticate_cli
    result = authenticate_cli(tool_id)
    if result.get("status") in ("unknown_tool", "no_auth_command"):
        raise HTTPException(status_code=400, detail=result)
    return result


@router.get("/detect", response_model=DetectionResult)
def detect_system() -> DetectionResult:
    """Run hardware/CLI/timezone detection for the wizard.

    Wraps ``okuro.yu.detection.detect_system()`` and replaces the
    providers dict with the richer CLI-status shape expected by the
    Tools & Subscriptions screen.
    """
    from okuro.yu.detection import detect_system as _detect

    raw = _detect()
    return DetectionResult(
        os=raw["os"],
        shell=raw["shell"],
        timezone=raw["timezone"],
        python=raw["python"],
        gpus=raw["gpus"],
        providers=_cli_status(),
        tools=raw["tools"],
    )


@router.get("/profile")
def get_profile_endpoint() -> dict:
    """Return the current profile as a plain dict.

    Returns an empty object if no profile row exists yet. Frontends can
    use ``GET /api/onboarding/state`` to know whether to prompt for a
    first-time setup or pre-fill a return visit.
    """
    return _load_profile()


@router.patch("/profile")
def patch_profile(patch: ProfilePatch) -> dict:
    """Shallow-merge a profile patch onto the stored profile.

    Pass only the sections you want to update (typically one screen's
    worth). Other sections remain unchanged. Returns the merged profile.
    """
    base = _load_profile()
    merged = _merge_profile_patch(base, patch)
    try:
        _save_profile(merged)
    except Exception as exc:
        logger.exception("profile save failed")
        raise HTTPException(status_code=500, detail=f"profile save failed: {exc}")
    return merged


@router.post("/keyring")
def init_keyring(body: KeyringInitRequest, request: Request):
    """Initialize the keyring vault with a master password.

    Localhost-only. Creates ``~/.okuro/keyring/`` with encrypted vault,
    salt, and marker file. Stores the master password in the OS credential
    store (macOS Keychain / GNOME Keyring / Windows Credential Locker) with
    a config.json fallback for headless environments.

    When ``generate=true``, okuro picks a 32-byte URL-safe random master
    and relies on the OS keychain to retrieve it on every app start — the
    user never sees or types the password. This is the recommended path on
    machines that have a real OS keychain (macOS/GNOME); if the keychain
    write fails, the password is written to ``~/.okuro/keyring/config.json``
    (0600) and the response flags ``storage='file'`` so the UI can warn.

    Idempotent — returns 200 if already initialized.
    """
    from okuro.orchestrator.api.main import _require_loopback

    _require_loopback(request)

    kr = _keyring_status()
    if kr["initialized"]:
        return {"status": "already_initialized"}

    password = body.password
    if body.generate and not password:
        import secrets as _secrets
        # 32 bytes URL-safe ≈ 43 chars. PBKDF2 key derivation doesn't cap
        # password length usefully; entropy matters, not ergonomics, since
        # the user never types this.
        password = _secrets.token_urlsafe(32)
    elif not password:
        raise HTTPException(
            status_code=400,
            detail="Provide password (min 8 chars) or set generate=true.",
        )

    try:
        from okuro.keyring import KeyringStorage

        store = KeyringStorage()
        store.initialize(password)

        # Report which backend actually stored the master so the UI can
        # tell the user 'it's in your Keychain' vs 'we wrote it to a file
        # because your OS keyring is unavailable'. Checking the config.json
        # absence is the strongest signal — initialize() only leaves it in
        # place when _set_in_os_keyring() returned False.
        config_json = store.keys_file.parent / "config.json"
        storage_backend = "file" if config_json.exists() else "os-keychain"

        return {
            "status": "initialized",
            "storage": storage_backend,
            "generated": bool(body.generate),
        }
    except Exception as exc:
        logger.exception("keyring init failed")
        raise HTTPException(status_code=500, detail=f"Keyring init failed: {exc}")


@router.get("/canon/targets")
def canon_targets() -> dict:
    """Preview what canon deployment would write for each detected provider.

    Returns ``{"targets": [{"id", "name", "detected", "paths": [str, ...]}, ...]}``.
    Paths are absolute locations the deployment will create or update (the
    provider's instruction files + the MCP server config entries). The UI
    renders this so the user knows exactly what okuro is about to touch in
    their home directory before they approve.
    """
    from okuro.sense.providers import list_providers
    from okuro.cli.mcp_config import SERVERS

    targets = []
    for adapter in list_providers():
        detected = False
        try:
            detected = bool(adapter.detect())
        except Exception:
            detected = False
        targets.append({
            "id": adapter.name,
            "name": adapter.name,
            "detected": detected,
        })

    return {
        "targets": targets,
        "mcp_servers": list(SERVERS.keys()),
        "notes": [_hook_asymmetry_note()],
    }


class CanonDeployRequest(BaseModel):
    """Optional subset of provider ids to deploy to. Omit to deploy to all detected."""
    targets: Optional[list[str]] = None


@router.post("/canon/deploy")
def canon_deploy(body: Optional[CanonDeployRequest] = None) -> dict:
    """Write MCP configs + instruction files (CLAUDE.md, AGENTS.md, ...) for
    every detected provider, plus the shared TOOL-PROTOCOL.md. Edits the
    user's home directory — callable only after onboarding confirms consent.

    Returns per-target results so the UI can show exactly what was written
    and which targets (if any) failed.

    NOTE (hook asymmetry). Corrected 2026-07-17 — the previous wording said
    hooks were "Claude-only". That was false at the time — the gemini adapter
    installed BeforeAgent/AfterAgent hooks, and template.py documented them.
    That adapter was removed with the provider on 2026-07-18, so today okuro
    installs hooks for claude only; codex, cursor
    and antigravity get empty lists because okuro has not implemented them
    there — not because those tools lack hook systems. As of 2026 all three
    ship real ones, so this is a policy gap, not a capability gap. The
    ``notes`` key surfaces it for the UI.
    """
    from okuro.canon.deploy import deploy_surface

    results = deploy_surface(targets=body.targets if body else None)

    # Surface the hook asymmetry to the UI so users understand why
    # codex/cursor/antigravity show empty hooks lists.
    results["notes"] = [_hook_asymmetry_note()]

    return results


class ConsumerStatus(BaseModel):
    """One MCP consumer's merged install / auth / registration state.

    Merges three existing sources — canon.consumers (the full consumer set),
    mcp_config.registration_status (per-provider MCP registration), and
    cli_probe (live install + auth). Keyed on the consumer's short name, which
    is the same key all three already use, so the merge is a plain join.
    """

    id: str                                # short_name (claude, codex, …)
    tool_id: str                           # canon id (claude-code, …)
    name: str                              # display label
    kind: str                              # cli | desktop | ide | extension
    provider_id: str
    installed: Optional[bool] = None       # cli: on PATH; desktop: config present
    logged_in: Optional[bool] = None
    # Raw cli_probe state so the UI can distinguish ``ineligible`` (valid creds,
    # unentitled — e.g. retired gemini tier) from ``unknown``.
    auth_state: Optional[str] = None
    mcp_registered: bool = False
    transport: Optional[str] = None        # transport the current entry points at
    preferred_transport: Optional[str] = None  # what canon writes for THIS consumer
    transports: list[str] = Field(default_factory=list)
    stale: bool = False                    # registered but NOT matching expected
    legacy_found: list[str] = Field(default_factory=list)
    config_exists: bool = False
    config_path: Optional[str] = None
    supported: bool = False                # canon has a writer for this consumer
    # True when detected (installed) but not registered, or registered stale —
    # the "register me" case (e.g. codex installed AFTER okuro).
    actionable: bool = False


class ConsumersResponse(BaseModel):
    consumers: list[ConsumerStatus]
    # okuro entries in files canon does NOT write: the ~/.mcp.json project-scope
    # shadow (starts disconnected) and per-project disabledMcpServers (tools
    # won't load). Surfaced verbatim from registration_status.
    unmanaged: list[dict] = Field(default_factory=list)
    transport_expected: str = ""
    notes: list[str] = Field(default_factory=list)


def _build_consumers(base_home: Optional[Path] = None) -> ConsumersResponse:
    """Join canon.consumers + registration_status + cli_probe by short name.

    Read-only. Never raises on a missing config — a consumer with no config
    file, no writer, or no probe simply reports the corresponding fields as
    False/None. ``base_home`` is threaded through so tests can point at a
    scratch home; production passes None (real home).
    """
    from okuro.canon.consumers import list_consumers
    from okuro.cli.mcp_config import real_user_home, registration_status

    reg = registration_status(base_home=base_home)
    reg_providers = reg.get("providers", {})

    try:
        from okuro.system.cli_probe import detect_all

        cli_states = detect_all()
    except Exception as exc:  # noqa: BLE001
        logger.warning("cli probe failed during consumer status: %s", exc)
        cli_states = {}

    home = base_home or real_user_home()
    out: list[ConsumerStatus] = []
    for c in list_consumers():
        sid = c.short_name
        rp = reg_providers.get(sid)
        st = cli_states.get(sid)

        installed: Optional[bool]
        logged_in: Optional[bool]
        auth_state: Optional[str]
        if st is not None:
            installed = st.on_path
            auth_state = st.auth.state
            if auth_state == "ok":
                logged_in = True
            elif auth_state in ("missing", "expired", "ineligible"):
                # ineligible is a definite negative, not "unknown" — see
                # cli_probe: valid creds the CLI still refuses.
                logged_in = False
            else:
                logged_in = None
        elif rp is not None:
            # Desktop apps aren't CLI-probeable; the app having written its MCP
            # config is the strongest install signal we have.
            installed = rp.get("config_exists")
            logged_in = None
            auth_state = None
        else:
            installed = None
            logged_in = None
            auth_state = None

        mcp_registered = bool(rp.get("registered")) if rp else False
        transport = rp.get("transport") if rp else None
        stale = bool(rp.get("stale")) if rp else False
        legacy_found = list(rp.get("legacy_found") or []) if rp else []
        config_exists = bool(rp.get("config_exists")) if rp else False
        supported = bool(rp.get("supported")) if rp else False
        if rp and rp.get("path"):
            config_path = rp.get("path")
        else:
            cpath = c.mcp_path(home)
            config_path = str(cpath) if cpath else None

        detected = bool(installed)
        actionable = supported and detected and (not mcp_registered or stale)

        out.append(ConsumerStatus(
            id=sid,
            tool_id=c.tool_id,
            name=c.tool_id,
            kind=c.kind,
            provider_id=c.provider_id,
            installed=installed,
            logged_in=logged_in,
            auth_state=auth_state,
            mcp_registered=mcp_registered,
            transport=transport,
            preferred_transport=c.mcp.get("preferred_transport"),
            transports=list(c.mcp.get("transports") or []),
            stale=stale,
            legacy_found=legacy_found,
            config_exists=config_exists,
            config_path=config_path,
            supported=supported,
            actionable=actionable,
        ))

    return ConsumersResponse(
        consumers=out,
        unmanaged=reg.get("unmanaged", []),
        transport_expected=reg.get("transport_expected", ""),
        notes=[_hook_asymmetry_note()],
    )


@router.get("/consumers", response_model=ConsumersResponse)
def list_consumer_status() -> ConsumersResponse:
    """Per-consumer install / auth / MCP-registration board for Settings.

    The read side of the "register okuro's surface into a consumer installed
    AFTER okuro" flow. The register action itself is the existing
    ``POST /api/onboarding/canon/deploy`` (all, or ``{targets:[...]}``) — this
    endpoint adds no second deploy path.
    """
    return _build_consumers()


class CanonSkipRequest(BaseModel):
    """Explicit consent to skip canon-deploy.

    Sprint-2F audit gap #30. The wizard cannot mark canon_deploy as
    "intentionally not done" without flipping this flag — silent skips
    leave agents with zero okuro context and the doctor flags it.
    """

    consent_to_skip: bool = False


@router.post("/canon/skip")
def canon_skip(body: CanonSkipRequest) -> dict:
    """Record explicit user consent to skip canon-deploy.

    Three outcomes:
      * canon already deployed → 200, no consent needed (the user is
        effectively un-skipping by deploying after the wizard).
      * ``consent_to_skip`` true and canon not deployed → records the
        flag in ``profile.onboarding.canon_deploy_skipped`` and emits a
        progress-event log line so the dashboard can show it.
      * ``consent_to_skip`` false and canon not deployed → 400. The
        frontend MUST collect explicit consent before posting here. The
        error body explains the cost of the decision so the frontend can
        surface it to the user.
    """
    deployed, _ = _check_canon_deploy()
    if deployed:
        # Whether they sent consent or not is moot — the work happened.
        return {"status": "already_deployed", "consent_recorded": False}

    if not body.consent_to_skip:
        raise HTTPException(
            status_code=400,
            detail=(
                "Set consent_to_skip=true to skip this step explicitly — "
                "note that agents will have no okuro context until you "
                "deploy or run okuro init."
            ),
        )

    profile = _load_profile()
    onboarding = profile.setdefault("onboarding", {})
    if not isinstance(onboarding, dict):
        onboarding = {}
        profile["onboarding"] = onboarding
    onboarding["canon_deploy_skipped"] = True
    onboarding["canon_deploy_skipped_at"] = datetime.now(timezone.utc).isoformat()
    try:
        _save_profile(profile)
    except Exception as exc:
        logger.exception("canon-skip persist failed")
        raise HTTPException(status_code=500, detail=f"canon-skip persist failed: {exc}")

    # Emit a progress event so the dashboard can surface the skip.
    # No dedicated event table exists yet; structured logger.info is the
    # contract the dashboard ingest already understands.
    logger.info(
        "progress_event onboarding.canon_deploy_skipped consent=true at=%s",
        onboarding["canon_deploy_skipped_at"],
    )

    return {
        "status": "skipped",
        "consent_recorded": True,
        "skipped_at": onboarding["canon_deploy_skipped_at"],
    }


@router.post("/complete")
def complete_onboarding(request: Request) -> OnboardingState:
    """Mark onboarding as complete, install background services, return state.

    Two side-effects:
      1. Stamps ``profile.onboarding.completed_at`` with the current UTC
         timestamp. Idempotent.
      2. Installs + enables the orchestrator service, starting it only
         when the wizard is not already bound to the service port. Non-fatal
         on failure (unsupported platform, no systemd/launchd, etc.) — the
         wizard still completes and the user can install services later via
         ``okuro service install <name>``.
    """
    profile = _load_profile()
    completion_ts = datetime.now(timezone.utc).isoformat()
    profile.setdefault("onboarding", {})["completed_at"] = completion_ts
    try:
        _save_profile(profile)
    except Exception as exc:
        logger.exception("complete failed")
        raise HTTPException(status_code=500, detail=f"complete failed: {exc}")

    # Seed the role catalog on first completion. Without this, fresh
    # installs have zero rows in the `roles` table and every roles_match
    # call returns empty forever — the wizard steps don't own role data.
    # Idempotent: no-op when roles already exist (returning user,
    # tm-Supabase migration, custom roles added).
    #
    # We only seed when the DB file already exists — _run_web_init always
    # creates it via migrate() before /complete can fire. Skipping when
    # absent means isolated endpoint tests don't create ~/.okuro/ just by
    # completing onboarding.
    try:
        from okuro.cli.db_helpers import default_db_path, get_db
        from okuro.roles.seed import seed_if_empty

        if default_db_path().exists():
            db = get_db()
            inserted = seed_if_empty(db)
            db.close()
            if inserted:
                logger.info("post-onboarding: seeded %d roles from catalog", inserted)
    except Exception as exc:  # noqa: BLE001
        logger.warning("post-onboarding role seed skipped: %s", exc)

    # Install the orchestrator so subsequent `okuro` launches reuse it.
    # We only auto-install orchestrator + daemon; embed is opt-in because
    # first-start downloads a sentence-transformers model (~90 MB).
    #
    # Audit 2026-04-27: previously this was wrapped in `except Exception` that
    # swallowed everything to logger.warning, leaving the wizard to stamp
    # "Done" while services silently failed to install (observed on macOS
    # fresh installs where install_all_services returned per-service errors
    # like "address in use" — wizard's own uvicorn was still bound to 13333).
    # We now surface the results structurally so the frontend can show them
    # AND continue stamping completed_at (the wizard finished its own work
    # even if the post-onboarding install needs a manual retry).
    services_install_results: Optional[dict[str, str]] = None
    services_install_error: Optional[str] = None
    try:
        from okuro.system.install import install_all_services
        from okuro.system.port_registry import (
            orchestrator_port as _orch_port,
            embed_port as _emb_port,
        )

        # Defer the start of any service whose plist port matches the wizard's
        # current port — without this, address-in-use kills the plist and the
        # wizard finishes "OK" while the persistent service silently dies.
        # 2026-04-28 install report: wizard squatted on 13334 (embed port) and
        # the embed plist could only bind after the wizard exited.
        request_port = request.url.port
        wizard_owns_orchestrator = request_port == _orch_port()
        wizard_owns_embed = request_port == _emb_port()
        start_services = not wizard_owns_orchestrator
        # Install orchestrator + daemon in one call. Both are independent of
        # the embed port, so the wizard's port-conflict guard only needs to
        # gate the orchestrator start path here.
        services_install_results = install_all_services(
            names=["okuro-orchestrator", "okuro-daemon"],
            start=start_services,
        )
        # Embed lives on its own port (13334 by default) and was historically
        # OMITTED from the wizard install — leaving the user with cortex
        # semantic search degraded and a manual `okuro service install
        # okuro-embed` follow-up. Install it explicitly here, deferring the
        # start only when the wizard actually squats on the embed port (rare,
        # only triggered by a user-supplied --port that collides with 13334).
        embed_start = start_services and not wizard_owns_embed
        embed_results = install_all_services(
            names=["okuro-embed"],
            start=embed_start,
        )
        services_install_results = dict(services_install_results or {})
        services_install_results.update(embed_results or {})
        if not start_services:
            services_install_results["_start_deferred"] = (
                f"wizard is running on orchestrator port {_orch_port()}; "
                "services were installed but not started"
            )
        if wizard_owns_embed:
            services_install_results["_embed_start_deferred"] = (
                f"wizard is running on embed port {_emb_port()}; "
                "embed plist installed but not started — launchd will load "
                "it on next login (or call `okuro service start okuro-embed` "
                "manually)"
            )
        logger.info("post-onboarding service install: %s", services_install_results)
        # Promote per-service errors to WARN so they land in journalctl/Console.app
        # operator views without grep gymnastics.
        for svc, status in (services_install_results or {}).items():
            if str(status).startswith("error:") or "start failed" in str(status):
                logger.warning("post-onboarding service install: %s = %s", svc, status)
    except Exception as exc:  # noqa: BLE001
        services_install_error = f"{exc.__class__.__name__}: {exc}"
        logger.exception("post-onboarding service install raised before producing results")

    # H6: the pre-H6 silent safety net here unconditionally wrote to the user's
    # home directory (~/.claude/, ~/.codex/, ~/.gemini/, ~/.cursor/,
    # ~/.okuro/TOOL-PROTOCOL.md) on every /complete call — even when the user
    # skipped the canon-deploy step. That was a trust gap the audit flagged
    # as CRITICAL-3 (01-promises.md). Canon deployment is now first-class in
    # STEP_DEFS with explicit consent UX (CanonDeployStep). Skipping means
    # nothing is written — the user can re-deploy from settings later.
    #
    # Memory d83d5c1b tracks the missing settings-tab for re-deploy; that's
    # a separate task, out of scope for Day 4.
    canon_deployed = False
    try:
        canon_deployed, _ = _check_canon_deploy()
        if not canon_deployed:
            logger.info(
                "canon not deployed during onboarding; user can re-deploy from settings later"
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning("canon-deploy status check skipped at /complete: %s", exc)

    # Force a synchronous regen of the static instruction files
    # (~/.claude/CLAUDE.md, ~/.codex/AGENTS.md, ~/.gemini/AGENTS.md) so
    # they reflect the FINAL profile state when the user clicks Done.
    # update_profile() schedules a debounced regen via threading.Timer (2 s);
    # if the user changed any communication / cognitive_style / boundary
    # field on the very last wizard step, the timer hasn't fired yet at
    # /complete and the instruction files are stale (observed on the Mac
    # install-report 2026-04-28: rendered profile said "concise/blunt/speed"
    # but the static files still carried the previous values).
    if canon_deployed:
        try:
            from okuro.sense.providers import regen_all_provider_instructions
            regen_paths = regen_all_provider_instructions()
            logger.info("post-/complete instruction regen: %d files", len(regen_paths))
        except Exception as exc:  # noqa: BLE001
            logger.warning("post-/complete instruction regen failed (non-fatal): %s", exc)

    state = get_state()
    state.services_install = services_install_results
    state.services_install_error = services_install_error

    # Single comprehensive install report — written every time /complete fires
    # so the user has ONE file to paste back if anything's wrong. Stable path:
    # ~/.okuro/install-report.json. Per-run archive: ~/.okuro/install/<run>/install-report.json.
    # live=False so we don't spawn `claude mcp list` from inside the wizard's
    # uvicorn process (would block the response). The standalone probe at
    # ./scripts/contract-probe.sh --live runs probe 6 separately.
    try:
        from okuro.diagnostics import write_report

        report_path = write_report(
            live=False,
            extra={
                "wizard_services_install": services_install_results,
                "wizard_services_install_error": services_install_error,
                "wizard_completed_at": completion_ts,
            },
        )
        state.install_report_path = str(report_path)
        logger.info("install-report written: %s", report_path)
    except Exception as exc:  # noqa: BLE001
        logger.exception("install-report write failed at /complete (non-fatal)")
        state.install_report_error = f"{exc.__class__.__name__}: {exc}"

    return state


@router.get("/design/options", response_model=list[DesignOption])
def list_design_options() -> list[DesignOption]:
    """The design systems a new user can start from.

    LISTS KITS, not v0 profiles. The scrape endpoints below now fork okuro-ds
    rather than author a flat profile, so what a user picks here and what a
    scrape produces are the same kind of thing -- which is the point of having
    one design-system module.
    """
    options: list[DesignOption] = []
    try:
        from okuro.design_engine import store

        for row in store.list_kits():
            kit_id = row["id"]
            shipped = row.get("origin") == "package"
            options.append(
                DesignOption(
                    id=kit_id,
                    name=kit_id,
                    description="okuro's own design system"
                    if shipped
                    else "your design system",
                    source="baseline" if shipped else "scraped",
                )
            )
    except Exception as exc:
        logger.warning("design kit listing failed: %s", exc)
    return options


@router.post("/design/extract", response_model=DesignOption)
def extract_design_from_urls(req: ExtractRequest) -> DesignOption:
    """Scrape 1-6 URLs in parallel, hand the aggregated data to bridge_invoke,
    save the LLM-distilled profile, return a DesignOption.

    Degrades to the single-URL heuristic builder if Playwright or bridge
    are unavailable or the LLM output fails validation.
    """
    # Playwright is an optional dep (extra `browser`). The scraper module
    # imports `playwright.async_api` inside its functions, so `import
    # scrape_urls_parallel` succeeds even when the package isn't installed —
    # the failure surfaces only at call time as a generic Exception with
    # "no module named 'playwright'" in its string, which the outer
    # `except Exception` then wraps as "Scrape failed: …". Pre-check here
    # so the user sees the actionable install guidance instead.
    import importlib.util

    if importlib.util.find_spec("playwright") is None:
        raise HTTPException(
            status_code=501,
            detail=(
                "URL scraping requires Playwright + a browser binary, which "
                "aren't shipped by default (≈200 MB). Install in your okuro "
                "venv:\n"
                "  pip install 'okuro[browser]'\n"
                "  python -m playwright install chromium\n"
                "Or skip the scrape step — pick any baseline design profile "
                "and customise it later from Settings → Design."
            ),
        )

    from okuro.design_engine.scan import scan_urls

    try:
        scrapes = scan_urls(req.urls)
    except Exception as exc:
        # Most common runtime miss: Playwright package present but the
        # browser binary was never downloaded (`playwright install chromium`
        # step skipped). Detect that one specifically — its message is
        # distinctive — so the user doesn't bounce between "install
        # playwright" and "download browser" tickets.
        msg = str(exc)
        if "Executable doesn't exist" in msg or "playwright install" in msg:
            logger.warning("playwright browser binary missing: %s", exc)
            raise HTTPException(
                status_code=501,
                detail=(
                    "Playwright is installed but its browser binary is "
                    "missing. In your okuro venv:\n"
                    "  python -m playwright install chromium"
                ),
            )
        logger.exception("multi-scrape failed")
        raise HTTPException(status_code=502, detail=f"Scrape failed: {exc}")

    valid = [s for s in scrapes if s.element_count]
    if not valid:
        raise HTTPException(
            status_code=502,
            detail=f"Nothing readable at any of {len(req.urls)} URL(s)",
        )

    # SAME RULE AS THE SINGLE-URL PATH: the pages supply what a kit can hold,
    # everything else is okuro-ds. The LLM extraction that used to run here
    # produced a v0 profile — ~40 authored values in a store nothing renders
    # from — and its extra precision bought nothing a fork keeps.
    #
    # THE FIRST READABLE PAGE WINS per field, rather than a vote. Sites are not
    # uniform: a marketing page and a docs page legitimately differ, and
    # averaging two brand colours produces a third that belongs to neither.
    # Order is the caller's, so "put your main site first" is the whole rule.
    merged = valid[0]
    for later in valid[1:]:
        for slot in ("accent", "background", "foreground", "font_family",
                     "radius_px", "border_px"):
            if getattr(merged, slot) is None:
                setattr(merged, slot, getattr(later, slot))

    kit_id = _kit_id_from(req.name, req.urls[0])
    brand = _fork_from_scan(kit_id, merged, f"{len(valid)} page(s)")
    return DesignOption(
        id=brand.id,
        name=brand.id,
        description=f"Forked from okuro-ds with what {len(valid)} page(s) actually show",
        source="scraped",
        colors={**merged.as_dict(), "urls": req.urls},
    )


def _fork_from_scan(kit_id: str, found, source: str):
    """A scan result -> a saved kit. The one place the two scan paths meet.

    The scanner reports what a page shows; `fork_kit` decides what a brand is.
    Keeping those apart is why a missing field here is not an error: a page
    with no visible border radius simply leaves the base's, which is exactly
    the owner's "rest same as base".

    The FONT is only applied when the family is installed on this machine. The
    engine describes a typeface by its measured faces, so naming a family it
    cannot measure would author weight slots that resolve to nothing.
    """
    from okuro.design_engine import store
    from okuro.design_engine.api import installed_families
    from okuro.design_engine.fork import fork_kit

    measured = None
    if found.font_family:
        measured = next(
            (f for f in installed_families() if f["family"] == found.font_family),
            None,
        )
        if measured is None:
            logger.info(
                "scan of %s found font %r, which is not installed here; "
                "keeping the base typeface",
                source, found.font_family,
            )

    try:
        brand = fork_kit(
            kit_id,
            colour=found.accent,
            font_family=found.font_family if measured else None,
            font_faces=measured["faces"] if measured else None,
            font_quad=measured["quad"] if measured else None,
            radius_base=found.radius_px,
            border_base_px=found.border_px,
        )
        path = store.save(brand, create=False)
    except Exception as exc:
        logger.exception("could not fork a kit from the scan of %s", source)
        raise HTTPException(status_code=502, detail=f"Fork failed: {exc}")

    logger.info("scanned %s into kit %s at %s", source, kit_id, path)
    return brand


def _kit_id_from(name: str | None, url: str) -> str:
    """A kit id is a FILENAME, so it has to be a slug before it is a path.

    Mirrors `design_engine.store.KIT_ID` -- lowercase alphanumerics with single
    hyphens between them, 64 chars. Prefers the name the user typed; falls back
    to the URL's host, which is what they would have typed anyway.
    """
    import re
    from urllib.parse import urlparse

    raw = (name or "").strip()
    if not raw:
        host = urlparse(url if "//" in url else f"//{url}").netloc or url
        raw = host.removeprefix("www.")
    slug = re.sub(r"[^a-z0-9]+", "-", raw.lower()).strip("-")[:64]
    return slug or "scraped-brand"


@router.post("/design/scrape", response_model=DesignOption)
def scrape_design_from_url(req: ScrapeRequest) -> DesignOption:
    """Scrape colors and fonts from a URL to create a design option.

    Requires the ``browser`` extra (``pip install okuro[browser]``).
    Returns a DesignOption with ``source='scraped'`` and extracted colors.
    """
    try:
        from okuro.design_engine.scan import scan_url
    except ImportError:
        raise HTTPException(
            status_code=501,
            detail="Playwright not installed. Run: pip install okuro[browser]",
        )

    try:
        found = scan_url(req.url)
    except Exception as exc:
        logger.exception("scan failed for %s", req.url)
        raise HTTPException(status_code=502, detail=f"Scan failed: {exc}")
    if not found.element_count:
        raise HTTPException(
            status_code=502, detail=f"Nothing readable at {req.url}"
        )

    # A SCRAPE FEEDS A FORK OF okuro-ds. It used to build a v0 design profile,
    # a flat palette of ~40 authored values in a store nothing renders from.
    # The owner: "This should be the first okuro design system, that is registered
    # based on okuro-ds. color, type … keep it simple."
    #
    # So the page supplies TWO values -- its dominant colour and its dominant
    # font -- and everything else is the base's. Radius is NOT among them: the
    # scraper returns colours, fonts, type scale, weights, line heights, letter
    # spacing and transform, and no border-radius (measured against
    # `summarize_samples`), so "rest same as base" covers it.
    kit_id = _kit_id_from(req.name, req.url)
    brand = _fork_from_scan(kit_id, found, req.url)
    return DesignOption(
        id=brand.id,
        name=brand.id,
        description=f"Forked from okuro-ds with what {req.url} actually shows",
        source="scraped",
        colors=found.as_dict(),
    )


# ── Questionnaire ───────────────────────────────────────────────────


@router.get("/questionnaire", response_model=list[QuestionnaireItem])
def get_questionnaire() -> list[QuestionnaireItem]:
    """Return the 10-item forced-choice questionnaire."""
    return [QuestionnaireItem(**q) for q in QUESTIONNAIRE]


@router.post("/questionnaire")
def submit_questionnaire(submission: QuestionnaireAnswers) -> dict:
    """Process questionnaire answers and patch the user profile.

    Maps 10 forced-choice answers to communication, decision_style,
    error_handling, work_style, and cognitive_style profile fields.
    Optionally stores neurodivergence disclosure.

    Returns the merged profile.
    """
    mapped = _map_answers(submission.answers)

    if submission.neurodivergence and submission.neurodivergence.disclosed:
        nd = {"disclosed": True}
        if submission.neurodivergence.details:
            nd["details"] = submission.neurodivergence.details
        mapped["cognitive_style"]["neurodivergence"] = nd

    base = _load_profile()
    for section, values in mapped.items():
        if isinstance(values, dict):
            existing = base.get(section)
            if isinstance(existing, dict):
                # Deep-merge so nested writes (e.g. format_preferences.preferred,
                # decision_style.framing) don't stomp sibling keys.
                _deep_merge(existing, values)
                base[section] = existing
            else:
                base[section] = values
        else:
            base[section] = values

    try:
        _save_profile(base)
    except Exception as exc:
        logger.exception("questionnaire profile save failed")
        raise HTTPException(status_code=500, detail=f"Save failed: {exc}")

    return base


# ── Cognitive Traits ─────────────────────────────────────────────────


def _load_cognitive_traits() -> dict:
    """Load cognitive trait questions + mapping from YAML."""

    yaml_path = Path(__file__).resolve().parent.parent.parent / "sense" / "data" / "cognitive_traits.yaml"
    if not yaml_path.exists():
        return {}
    try:
        with open(yaml_path) as f:
            return yload(f) or {}
    except Exception:
        return {}


@router.get("/cognitive-traits")
def get_cognitive_traits():
    """Return cognitive trait questions for the onboarding UI."""
    data = _load_cognitive_traits()
    return data.get("questions", [])


@router.post("/cognitive-traits")
def save_cognitive_traits(body: CognitiveTraitAnswers):
    """Map cognitive trait answers to strategy parameters and implications.

    Each trait question maps to behavioral_overrides values that
    reminders/strategy.py uses for adaptive behavior. No clinical labels —
    just preferences that shape agent behavior.
    """
    data = _load_cognitive_traits()
    mapping = data.get("mapping", {})
    impl_lookup = data.get("implications", {})

    # Build behavioral overrides from answers
    overrides: dict = {}
    implications: list[str] = []

    for trait_id, answer in body.answers.items():
        trait_map = mapping.get(trait_id, {})
        answer_map = trait_map.get(answer, {})
        if isinstance(answer_map, dict):
            overrides.update(answer_map)

        # Collect implications
        trait_impl = impl_lookup.get(trait_id, {})
        impl_text = trait_impl.get(answer)
        if impl_text:
            implications.append(impl_text)

    # Save to profile
    profile = _load_profile()
    cog = profile.get("cognitive_style", {})
    if not isinstance(cog, dict):
        cog = {}

    cog["traits"] = body.answers
    cog["implications"] = implications

    if body.neurodivergent:
        cog.setdefault("neurodivergence", {})["disclosed"] = True

    profile["cognitive_style"] = cog

    # Save behavioral overrides
    if overrides:
        profile["behavioral_overrides"] = overrides

    try:
        _save_profile(profile)
    except Exception as exc:
        logger.exception("cognitive traits save failed")
        raise HTTPException(status_code=500, detail=f"Save failed: {exc}")

    return {"traits": body.answers, "implications": implications, "overrides": overrides}


# ── Principles ──────────────────────────────────────────────────────


class PrinciplesSelection(BaseModel):
    """User's selected and ordered principles."""
    selected: list[str] = Field(..., description="Principle IDs in priority order")
    custom: list[dict] = Field(default_factory=list, description="Custom principles [{id, name, description}]")


@router.get("/principles")
def get_available_principles():
    """Return all available principles for the selection UI."""
    from okuro.sense.principles import get_principles, seed_principles
    from okuro.db import get_db

    db = get_db()
    rows = db.fetchall(
        "SELECT id, title, description FROM principles WHERE active = 1 ORDER BY priority ASC"
    )
    if not rows:
        seed_principles()
        rows = db.fetchall(
            "SELECT id, title, description FROM principles WHERE active = 1 ORDER BY priority ASC"
        )

    # Also load user's current selection
    profile = _load_profile()
    user_princ = profile.get("principles", {})

    return {
        "available": [{"id": r["id"], "name": r["title"], "description": r["description"]} for r in rows],
        "selected": user_princ.get("selected", []),
        "custom": user_princ.get("custom", []),
    }


@router.post("/principles")
def save_principles(body: PrinciplesSelection):
    """Save user's principle selection and priority order."""
    profile = _load_profile()
    profile["principles"] = {
        "selected": body.selected,
        "custom": body.custom,
    }

    try:
        _save_profile(profile)
    except Exception as exc:
        logger.exception("principles save failed")
        raise HTTPException(status_code=500, detail=f"Save failed: {exc}")

    return {"selected": body.selected, "custom": body.custom}


class FormulateRequest(BaseModel):
    """Raw user need to be formulated into a principle by LLM."""
    need: str = Field(..., min_length=3, description="What the user wants, in their own words")


@router.post("/principles/formulate")
def formulate_principle(body: FormulateRequest):
    """Use bridge_invoke to formulate a user's raw need into a principle.

    Takes informal input like "I hate when agents waste money" and
    returns a structured principle with ID, name, and description.
    """
    from okuro.bridge.invoke import invoke

    prompt = f"""Formulate this user need into a concise design principle for AI agent behavior.

User need: "{body.need}"

Return ONLY a JSON object (no markdown, no explanation):
{{
  "name": "SHORT-CAPS-NAME",
  "description": "One sentence describing the principle and why it matters."
}}

Examples:
- Need: "don't waste money" → {{"name": "COST-CONSCIOUS", "description": "Prefer free and open-source solutions. Challenge every cost before spending."}}
- Need: "always explain what you did" → {{"name": "TRANSPARENT-ACTIONS", "description": "Log and explain every action taken. No silent changes."}}
"""

    result = invoke(prompt=prompt, capability="analysis", timeout=60)

    if not result.get("success"):
        raise HTTPException(503, f"LLM formulation failed: {result.get('error', 'unknown')}")

    import json as json_mod

    output = result.get("output", "").strip()
    # Extract JSON from potential markdown wrapping
    if "```" in output:
        for part in output.split("```")[1::2]:
            part = part.strip()
            if part.startswith("json"):
                part = part[4:].strip()
            try:
                parsed = json_mod.loads(part)
                parsed["id"] = f"CUSTOM-{hash(body.need) % 10000:04d}"
                return parsed
            except json_mod.JSONDecodeError:
                continue

    try:
        parsed = json_mod.loads(output)
        parsed["id"] = f"CUSTOM-{hash(body.need) % 10000:04d}"
        return parsed
    except json_mod.JSONDecodeError:
        raise HTTPException(422, "LLM returned non-JSON output")
