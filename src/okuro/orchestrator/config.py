# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Okuro Orchestrator Configuration
# index:
#   imports
#   class CLIToolConfig
#   class ExecutionConfig
#   class SentinelConfig
#   class OrchestratorConfig
#   class SenseConfig
#   class Config
#   def load_config
#   def _default_cli_from_detected
#   def _render_default_config_yaml
#   def _generate_default_config
#   def validate_cli_binaries
#   def get_cli_tool_config
#   def get_cli_command_parts
#   def apply_cli_preference
#   def resolve_role
#   def load_role_index
# AGENT_HEADER_END -->
"""
Okuro Orchestrator Configuration

Loads config.yaml, resolves paths, reads role index from okuro.db.
"""

import logging
import shutil
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Dict, List, Tuple, Optional
from okuro.orchestrator.yamlfast import yload
from okuro.db.engine import okuro_home

_config_logger = logging.getLogger(__name__)

# Canonical, provider-agnostic tier vocabulary — the SINGLE source of truth.
# A role's tier MUST be one of these; each provider's tier_map maps them to a
# concrete model. `critical` is the top rung (fable on claude); it is canonical
# even where a provider's tier_map has not yet declared it. Anything outside
# this set is a bug — the resolver logs it loudly instead of silently running
# it as standard (the failure mode migration 106 cleaned up).
CANONICAL_TIERS: Tuple[str, ...] = ("fast", "standard", "strategic", "critical")

_TIER_RANK = {t: i for i, t in enumerate(CANONICAL_TIERS)}


def resolve_unit_tier(unit, task) -> str:
    """The tier for ONE dispatchable unit — a subtask or a DAG execution node.

    A unit's ``complexity`` IS its tier: the vocabularies are the same words
    (fast|standard|strategic, a subset of CANONICAL_TIERS). The field has always
    been carried through the plan and validated, but until now both dispatch
    paths ignored it and derived the tier from ``task.intelligence`` alone — so a
    planner (or a human drawing a workflow) could mark a subtask strategic and it
    would silently run as standard.

    ``task.intelligence == "max"`` still applies, but only ever UPGRADES: asking
    for maximum intelligence must not quietly make a strategic unit cheaper.

    Measured before making it live: across 200 recent task plans every subtask
    was ``standard``, so honouring the field changes nothing for existing work —
    it only gives drawn workflows the per-node control they declare.
    """
    tier = (getattr(unit, "complexity", "") or "standard").strip().lower()
    if tier not in _TIER_RANK:
        _config_logger.warning(
            "unknown complexity %r on %r — running as standard; expected one of %s",
            tier, getattr(unit, "id", "?"), CANONICAL_TIERS)
        tier = "standard"
    if getattr(task, "intelligence", "") == "max" and _TIER_RANK[tier] < _TIER_RANK["strategic"]:
        tier = "strategic"
    return tier

# Canonical, provider-agnostic effort (thinking-depth) vocabulary — the SINGLE
# source of truth. Claude's GA effort set; each provider's effort_map maps these
# to its own values, capping where the provider tops out (codex/agy -> high).
# Orthogonal to CANONICAL_TIERS: tier picks the model, effort picks how hard it
# thinks. Verified 2026-07-22 against `claude --effort` (all 5 accepted).
CANONICAL_EFFORTS: Tuple[str, ...] = ("low", "medium", "high", "xhigh", "max")

# Canonical retry cap — the SINGLE source of truth for "how many retry
# attempts a subtask gets before it escalates". Every fallback that
# previously hard-coded a literal (pipeline, deliberation, engine work
# loops) imports THIS constant, so a library/test caller with no config
# in scope behaves identically to a configured run. MUST equal the
# `execution.max_retries` default rendered in the bundled config.yaml
# (see _render_default_config_yaml) — verified by tests.
DEFAULT_MAX_RETRIES = 2

# Default config search path
_DEFAULT_CONFIG_PATHS = [
    okuro_home() / "orchestrator" / "config.yaml",
    Path("config.yaml"),
]

# Package-bundled prompts (fallback when no prompts_dir configured).
# Resolved via importlib.resources so the lookup works in zipped/wheel
# installs as well as normal pip-editable layouts. as_file() is the
# concrete-path bridge for callers that pass the result to pathlib APIs.
def _resolve_package_prompts_dir() -> Path:
    try:
        from importlib.resources import files
        traversable = files("okuro.orchestrator").joinpath("prompts")
        return Path(str(traversable))
    except Exception:
        # Fallback for older Python or unusual install shapes.
        return Path(__file__).parent / "prompts"


_PACKAGE_PROMPTS_DIR = _resolve_package_prompts_dir()


@dataclass
class CLIToolConfig:
    """Configuration for a CLI tool.

    Two orthogonal routing axes:
    - ``tier_map``   maps a role's tier (fast/standard/strategic/critical) to a
      concrete model name (or "" = let the CLI pick its own default).
    - ``effort_map`` maps a role's effort (low/medium/high/xhigh/max) to this
      provider's own effort value, capping where the provider tops out (codex
      and agy top out at ``high``). ``effort_flag`` is the CLI flag that carries
      it (``--effort`` for claude/agy, ``-c`` for codex). Empty ``effort_flag``
      means the provider exposes no effort knob — effort is silently skipped.
    """
    binary: str
    autonomous_flags: List[str]
    interactive_flags: List[str]
    model_flag: str
    tier_map: Dict[str, str]
    effort_flag: str = ""
    effort_map: Dict[str, str] = field(default_factory=dict)


@dataclass
class ExecutionConfig:
    """Execution settings."""
    subtask_timeout: int
    max_retries: int
    risk_enforcement: bool
    max_parallel: int = 3
    # F1 — when True, a phase whose reviewer FAILs after the retry budget is
    # exhausted does NOT park on `blocked_review` waiting for a human; the
    # engine classifies the failure, emits a `review_overridden` event, and
    # advances. Scoped to verdict==FAIL only — a NEEDS_USER verdict is a real
    # decision request and still parks. Default False (preserve blocking).
    continue_on_review_exhausted: bool = False


@dataclass
class SentinelConfig:
    """Sentinel settings."""
    enabled: bool
    check_interval: int
    # Wave-6 G19 — minutes a subtask may run before sentinel emits a
    # CRITICAL `sentinel_intervention_recommended` event to the activity
    # log. 0 disables (default — preserves pre-G19 soft-warning behavior).
    intervene_after_minutes: float = 0.0


@dataclass
class OrchestratorConfig:
    """Orchestrator LLM settings."""
    decompose_model: str
    review_model: str
    sentinel_model: str
    decompose_cli: str
    # Reviewer tier for CONCEPTUAL deliverables (plan/report — architecture,
    # strategy, audits). These carry no executable evidence and demand real
    # judgment; the strategic tier matches the producer's intelligence so the
    # Critic/Scorer don't misread disclosed conditions as defects. Code/evidence
    # deliverables keep ``review_model``.
    review_model_conceptual: str = "opus"
    # Global default for the deliberation-on-continuation toggle. When True,
    # a continuation on a deliberate-mode task runs a fresh council round
    # (panel positions -> discussion -> user proceed -> decompose) instead of
    # single-shot decompose_continuation. Overridable per task
    # (Task.deliberate_continuations) and per continuation
    # (Intervention.deliberate). Ships False to preserve the fast default.
    deliberate_continuations: bool = False
    # After a subtask passes review + finalizes, run the tailor pass
    # (orchestrator/tailor.py) to rewrite its user-facing report/plan artifacts
    # from the M3 reviewer-evidence shape (AC anchors, bash captures, exit
    # codes) into the user's own communication voice. The reviewer body is
    # preserved as a process-audience evidence copy (UI "Internal" tab). Ships
    # True; set False to revert to raw reviewer bodies in the user tab.
    tailor_user_artifacts: bool = True
    # F2 — autopilot. When True, every gate that would normally park on a human
    # decision (blocked_review, decision_gate, capability_gap, subtask_approval,
    # …) is instead answered from the user's profile: the resolver picks the
    # most-probable allowed option, logs an `autopilot_decision` event, and the
    # engine continues in-process WITHOUT parking. If the resolver abstains or
    # its confidence is below the floor, the gate parks as today (nothing is
    # silently mis-answered). Ships False. NOTE: autopilot answers EVERYTHING,
    # including high-risk approvals and capability-gap — there is no per-kind
    # exemption; the confidence/abstain fallback is the only safeguard.
    autopilot: bool = False
    # Whose profile the resolver answers as. Empty = the primary user. A named
    # profile lets the engine answer gates in a specific persona's voice.
    autopilot_profile: str = ""


@dataclass
class SenseConfig:
    """okuro.sense integration settings (all optional, degrade gracefully)."""
    enabled: bool = True
    bootstrap_budget: Dict[str, int] = None
    harvest: bool = True
    telemetry_tags: bool = True

    def __post_init__(self):
        if self.bootstrap_budget is None:
            self.bootstrap_budget = {"fast": 2000, "standard": 5000, "strategic": 5000}


@dataclass
class Config:
    """Main orchestrator configuration."""
    orchestrator_root: Path
    knowledge_dir: Path
    tasks_dir: Path
    prompts_dir: Path
    tools_registry_path: Path
    channels_config_path: Path
    cli_default: str
    cli_allowed: List[str]
    cli_tools: Dict[str, CLIToolConfig]
    execution: ExecutionConfig
    sentinel: SentinelConfig
    orchestrator: OrchestratorConfig
    sense: SenseConfig = None
    recurring_dir: Path = None

    def __post_init__(self):
        if self.sense is None:
            self.sense = SenseConfig()


def load_config(config_path: Optional[Path] = None) -> Config:
    """Load orchestrator configuration from config.yaml.

    Search order:
      1. Explicit config_path argument
      2. ~/.okuro/orchestrator/config.yaml
      3. ./config.yaml (CWD)
    """
    if config_path is not None:
        config_path = Path(config_path).resolve()
    else:
        for candidate in _DEFAULT_CONFIG_PATHS:
            resolved = candidate.resolve()
            if resolved.is_file():
                config_path = resolved
                break
        if config_path is None:
            # Generate default config
            config_path = _DEFAULT_CONFIG_PATHS[0]
            _generate_default_config(config_path)
            _config_logger.info("Generated default config at %s", config_path)

    orchestrator_root = config_path.parent

    with open(config_path, "r") as f:
        data = yload(f)

    # Resolve paths relative to config.yaml location
    knowledge_dir = (orchestrator_root / data.get("knowledge_dir", "./knowledge")).resolve()
    tasks_dir = (orchestrator_root / data.get("tasks_dir", "./tasks")).resolve()
    tools_registry_path = (orchestrator_root / data.get("tools_registry", "./tools/registry.yaml")).resolve()
    channels_config_path = (orchestrator_root / data.get("channels_config", "./channels.yaml")).resolve()
    recurring_dir = (orchestrator_root / data.get("recurring_dir", "./recurring")).resolve()

    # Prompts: config path → package bundled fallback
    prompts_rel = data.get("prompts_dir")
    if prompts_rel:
        prompts_dir = (orchestrator_root / prompts_rel).resolve()
    else:
        prompts_dir = _PACKAGE_PROMPTS_DIR

    # Parse CLI tools — migrate legacy hardcoded model names in-memory so
    # users with stale config.yaml don't have to manually edit. Disk file is
    # left untouched; the migration is per-load.
    cli_tools = {}
    for tool_name, tool_data in data["cli"]["tools"].items():
        cli_tools[tool_name] = CLIToolConfig(
            binary=tool_data["binary"],
            autonomous_flags=tool_data["autonomous_flags"],
            interactive_flags=tool_data["interactive_flags"],
            model_flag=tool_data["model_flag"],
            tier_map=_coerce_legacy_tier_map(
                tool_name, tool_data.get("tier_map", {})
            ),
            effort_flag=tool_data.get("effort_flag", ""),
            effort_map=tool_data.get("effort_map", {}),
        )

    cli_allowed = data["cli"].get("allowed")
    if cli_allowed is None:
        cli_allowed = list(cli_tools.keys())

    invalid_allowed = [name for name in cli_allowed if name not in cli_tools]
    if invalid_allowed:
        raise ValueError(
            "cli.allowed contains unknown CLI tool(s): "
            + ", ".join(sorted(invalid_allowed))
        )

    cli_default = data["cli"].get("default")
    if cli_default is None:
        # Self-heal: config.yaml had cli.default commented out — happens on a
        # fresh install where no CLI was on PATH at generate-time. Re-detect
        # now; claude/codex/antigravity may have been installed since.
        # We DO rewrite the file in this case (unlike legacy tier_map
        # coercion, which silently overrode user-set values). Here we are
        # only filling in a placeholder okuro itself generated, so there is
        # no user choice to preserve.
        detected = _default_cli_from_detected()
        if detected is None:
            raise ValueError(
                "cli.default is not set and no CLI binary "
                "(claude/codex/agy) was detected on PATH. Install one "
                f"and edit {config_path} to set cli.default."
            )
        if detected not in cli_allowed:
            raise ValueError(
                f"Detected CLI '{detected}' is not permitted by "
                f"cli.allowed: {cli_allowed}. Edit {config_path} to set "
                "cli.default."
            )
        cli_default = detected
        _persist_detected_cli_default(config_path, detected)
    elif cli_default not in cli_allowed:
        raise ValueError(
            f"cli.default '{cli_default}' is not permitted by cli.allowed: {cli_allowed}"
        )

    # Parse execution config
    exec_data = data["execution"]
    execution = ExecutionConfig(
        subtask_timeout=exec_data["subtask_timeout"],
        max_retries=exec_data["max_retries"],
        risk_enforcement=exec_data["risk_enforcement"],
        max_parallel=exec_data.get("max_parallel", 3),
        continue_on_review_exhausted=bool(
            exec_data.get("continue_on_review_exhausted", False)
        ),
    )

    # Parse sentinel config
    sentinel = SentinelConfig(
        enabled=data["sentinel"]["enabled"],
        check_interval=data["sentinel"]["check_interval"],
        intervene_after_minutes=float(
            data["sentinel"].get("intervene_after_minutes", 0) or 0
        ),
    )

    # Parse orchestrator LLM config
    orch_data = data["orchestrator"]
    decompose_cli = orch_data.get("decompose_cli", cli_default)
    if decompose_cli not in cli_allowed:
        raise ValueError(
            f"orchestrator.decompose_cli '{decompose_cli}' is not permitted by cli.allowed: "
            f"{cli_allowed}"
        )

    orchestrator = OrchestratorConfig(
        decompose_model=orch_data["decompose_model"],
        review_model=orch_data["review_model"],
        sentinel_model=orch_data["sentinel_model"],
        decompose_cli=decompose_cli,
        review_model_conceptual=orch_data.get("review_model_conceptual", "opus"),
        deliberate_continuations=bool(
            orch_data.get("deliberate_continuations", False)
        ),
        tailor_user_artifacts=bool(
            orch_data.get("tailor_user_artifacts", True)
        ),
        autopilot=bool(orch_data.get("autopilot", False)),
        autopilot_profile=str(orch_data.get("autopilot_profile", "") or ""),
    )

    # Parse sense config (optional — defaults if missing)
    sense_data = data.get("sense", data.get("launcher", {}))
    sense = SenseConfig(
        enabled=sense_data.get("enabled", True),
        bootstrap_budget=sense_data.get("bootstrap_budget", None),
        harvest=sense_data.get("harvest", True),
        telemetry_tags=sense_data.get("telemetry_tags", True),
    )

    return Config(
        orchestrator_root=orchestrator_root,
        knowledge_dir=knowledge_dir,
        tasks_dir=tasks_dir,
        prompts_dir=prompts_dir,
        tools_registry_path=tools_registry_path,
        channels_config_path=channels_config_path,
        cli_default=cli_default,
        cli_allowed=cli_allowed,
        cli_tools=cli_tools,
        execution=execution,
        sentinel=sentinel,
        orchestrator=orchestrator,
        sense=sense,
        recurring_dir=recurring_dir,
    )


_DEFAULT_CONFIG_YAML = """\
# Okuro Orchestrator Configuration (auto-generated)
knowledge_dir: ./knowledge
tasks_dir: ./tasks
tools_registry: ./tools/registry.yaml
channels_config: ./channels.yaml
recurring_dir: ./recurring

cli:
  __DEFAULT_CLI_LINE__
  allowed: [claude, antigravity, codex]
  tools:
    claude:
      binary: claude
      autonomous_flags: ["-p", "--output-format", "stream-json", "--verbose", "--permission-mode", "bypassPermissions", "--disallowedTools", "Agent,Glob,Grep"]
      interactive_flags: ["-p"]
      model_flag: "--model"
      tier_map:
        fast: sonnet
        standard: sonnet
        strategic: opus
        critical: fable   # verified 2026-07-22: `claude --model fable` is a valid alias
      # Effort (thinking depth) — orthogonal to tier. `claude -p --effort`
      # accepts all 5 canonical levels (verified 2026-07-22).
      effort_flag: "--effort"
      effort_map:
        low: low
        medium: medium
        high: high
        xhigh: xhigh
        max: max
    antigravity:
      binary: agy
      # Replaced the `gemini` tool on 2026-07-18 — the Gemini CLI stopped
      # serving individual accounts on 2026-06-18 and agy is Google's
      # sanctioned successor, reaching the same models.
      autonomous_flags: ["--dangerously-skip-permissions"]
      interactive_flags: []
      model_flag: "--model"
      # Empty tier_map values mean "use the CLI's own default model" — see
      # _coerce_legacy_tier_map() in this file. Pinning specific model names
      # drifted before: 2026-04-28 Mac install report — every Gemini call
      # returned HTTP 404 because the pinned name had been deprecated.
      # Letting the CLI pick its own default is robust to model name churn
      # and works on every account tier.
      tier_map:
        fast: ""
        standard: ""
        strategic: ""
      # agy --effort tops out at high; xhigh/max cap to high.
      effort_flag: "--effort"
      effort_map:
        low: low
        medium: medium
        high: high
        xhigh: high
        max: high
    codex:
      binary: codex
      autonomous_flags: ["exec", "--skip-git-repo-check", "--dangerously-bypass-approvals-and-sandbox"]
      interactive_flags: ["exec", "--skip-git-repo-check"]
      model_flag: "-m"
      # Codex with a ChatGPT-account login REJECTS API-only model names like
      # `o3` / `o4-mini` ("not supported when using Codex with a ChatGPT
      # account" — observed 2026-04-28). Empty values let codex pick the
      # account-appropriate default (`gpt-5-codex` for Plus/Pro accounts,
      # whatever ChatGPT serves for ChatGPT-only). Override at runtime via
      # subtask.model_override or `okuro init --port` CLI flag if needed.
      tier_map:
        fast: ""
        standard: ""
        strategic: ""
      # codex reasoning_effort tops out at high; xhigh/max cap to high.
      # Orthogonal to tier: -m/empty-tier = account default model; -c carries effort.
      effort_flag: "-c"
      effort_map:
        low: "model_reasoning_effort=low"
        medium: "model_reasoning_effort=medium"
        high: "model_reasoning_effort=high"
        xhigh: "model_reasoning_effort=high"
        max: "model_reasoning_effort=high"

execution:
  subtask_timeout: 1200
  max_retries: 2
  risk_enforcement: true
  # F1 — continue past an exhausted review loop instead of parking on
  # blocked_review. The failure is classified + logged (review_overridden),
  # not silently dropped. Only a FAIL verdict is overridden; NEEDS_USER still
  # asks. Leave false to keep the blocking behavior.
  continue_on_review_exhausted: false
  max_parallel: 3

sentinel:
  enabled: true
  check_interval: 300

sense:
  enabled: true
  bootstrap_budget:
    fast: 2000
    standard: 5000
    strategic: 5000
  harvest: true
  telemetry_tags: true

orchestrator:
  decompose_model: sonnet
  review_model: sonnet
  review_model_conceptual: opus
  sentinel_model: haiku
  # F2 — autopilot: auto-answer EVERY human gate from the profile instead of
  # parking. Resolver picks the most-probable allowed option, logs an
  # `autopilot_decision`, and the engine continues in-process. Abstain / low
  # confidence falls back to parking. Answers high-risk gates too — leave false
  # unless you want fully-unattended runs.
  autopilot: false
  # Whose profile to answer as (empty = the primary user).
  autopilot_profile: ""
  __DECOMPOSE_CLI_LINE__
"""


# CLI binary preference order for fresh installs. Claude is first because
# the user is most likely to have it (okuro's authoring agent), but the
# generator falls through to codex/antigravity if claude isn't installed — so
# a CI box or a user who only has codex/agy doesn't hit the
# "cli.default 'claude' is not permitted by cli.allowed" boot crash.
_DEFAULT_CLI_PREFERENCE: tuple[str, ...] = ("claude", "codex", "antigravity")


# Hardcoded model names that USED to live in the default config template
# but have since drifted (deprecated by the upstream CLI / not supported by
# common account tiers). When we find one in a user's existing config.yaml,
# silently treat it as empty in-memory so the engine omits the -m flag and
# the CLI picks its own account-appropriate default. Add rows here when a
# new model name goes stale; never remove rows.
_LEGACY_TIER_MODEL_VALUES: dict[str, set[str]] = {
    # Codex with a ChatGPT account: o3 / o4-mini are API-only and rejected
    # at runtime ("not supported when using Codex with a ChatGPT account",
    # observed 2026-04-28).
    "codex": {"o3", "o4-mini"},
    # Gemini CLI: gemini-2.0-flash returns HTTP 404 (deprecated upstream).
    # gemini-2.5-pro still works but pinning is fragile across CLI updates.
    "gemini": {"gemini-2.0-flash"},
}


def _coerce_legacy_tier_map(tool_name: str, tier_map: Dict[str, str]) -> Dict[str, str]:
    """Coerce known-broken pinned model names to empty (== use CLI default).

    Why this exists: the original default config template hardcoded
    Codex/Gemini model names that didn't survive upstream updates and account
    tier differences. Existing users with config.yaml on disk would otherwise
    keep hitting the same 400 / 404 errors until they manually edited the
    file. This in-memory coercion makes upgrade hands-off.

    The user's file is NOT modified — okuro doesn't rewrite config.yaml
    unless the user explicitly initialises it.
    """
    legacy = _LEGACY_TIER_MODEL_VALUES.get(tool_name)
    if not legacy:
        return tier_map
    coerced: Dict[str, str] = {}
    swapped = False
    for tier, model in tier_map.items():
        if model in legacy:
            coerced[tier] = ""
            swapped = True
        else:
            coerced[tier] = model
    if swapped:
        _config_logger.info(
            "%s: tier_map contained deprecated model name(s) %s — using CLI "
            "default instead. Edit ~/.okuro/orchestrator/config.yaml to set "
            "your preferred model permanently.",
            tool_name,
            sorted(set(tier_map.values()) & legacy),
        )
    return coerced


def _default_cli_from_detected(detected: Optional[List[str]] = None) -> Optional[str]:
    """Pick the first installed CLI from _DEFAULT_CLI_PREFERENCE.

    Args:
        detected: Optional pre-computed list of CLI short names that were
            found on PATH. If None, probes PATH directly via shutil.which.

    Returns:
        The first preference name that's installed, or None if none of
        claude/codex/antigravity are on PATH (fresh CI box scenario).
    """
    if detected is None:
        detected = [name for name in _DEFAULT_CLI_PREFERENCE if shutil.which(name)]
    detected_set = set(detected)
    for name in _DEFAULT_CLI_PREFERENCE:
        if name in detected_set:
            return name
    return None


def _render_default_config_yaml(default_cli: Optional[str]) -> str:
    """Render the default config YAML with a detected default CLI.

    If no default is supplied the cli.default and orchestrator.decompose_cli
    keys are emitted as commented placeholders so the user (or a later boot)
    can fill them in once a CLI is installed. The rest of the config — the
    full cli.tools table, allowed list, etc. — stays intact so the user can
    edit one line and have a working config.
    """
    if default_cli:
        default_line = f"default: {default_cli}"
        decompose_line = f"decompose_cli: {default_cli}"
    else:
        default_line = (
            "# default: <set after installing one of claude/codex/agy>"
        )
        decompose_line = (
            "# decompose_cli: <set after installing one of claude/codex/agy>"
        )
    return (
        _DEFAULT_CONFIG_YAML
        .replace("__DEFAULT_CLI_LINE__", default_line)
        .replace("__DECOMPOSE_CLI_LINE__", decompose_line)
    )


def _generate_default_config(config_path: Path) -> None:
    """Generate a default orchestrator config.yaml.

    Picks cli.default by detecting which CLI binary is installed on PATH
    (preference order: claude → codex → antigravity). If none are detected the
    field is emitted as a commented placeholder rather than crashing the
    user on first boot — they can install a CLI and edit one line.
    """
    config_path.parent.mkdir(parents=True, exist_ok=True)
    detected_default = _default_cli_from_detected()
    if detected_default is None:
        _config_logger.warning(
            "No CLI binary detected on PATH (claude/codex/agy). "
            "Generated config has cli.default commented out — install one "
            "and edit %s before running tasks.",
            config_path,
        )
    config_path.write_text(_render_default_config_yaml(detected_default))
    # Also create the required subdirs
    for subdir in ["knowledge", "tasks", "tools", "recurring"]:
        (config_path.parent / subdir).mkdir(parents=True, exist_ok=True)


_COMMENTED_DEFAULT_PREFIX = "# default:"
_COMMENTED_DECOMPOSE_PREFIX = "# decompose_cli:"


def _persist_detected_cli_default(config_path: Path, detected: str) -> None:
    """Replace the commented cli.default / decompose_cli placeholders on disk.

    Writes via tmp + rename so a crash mid-write can't leave a half-file.
    Only touches lines that still match the placeholder shape — if the user
    has edited them in any way we leave the file alone and stay in-memory.
    Failures are logged and swallowed; the in-memory load is authoritative.
    """
    try:
        original = config_path.read_text()
        lines = original.splitlines(keepends=True)
        replaced_default = False
        replaced_decompose = False
        for idx, line in enumerate(lines):
            stripped = line.lstrip()
            indent = line[: len(line) - len(stripped)]
            newline = "\n" if line.endswith("\n") else ""
            if not replaced_default and stripped.startswith(_COMMENTED_DEFAULT_PREFIX):
                lines[idx] = f"{indent}default: {detected}{newline}"
                replaced_default = True
            elif not replaced_decompose and stripped.startswith(
                _COMMENTED_DECOMPOSE_PREFIX
            ):
                lines[idx] = f"{indent}decompose_cli: {detected}{newline}"
                replaced_decompose = True
        if not replaced_default:
            _config_logger.info(
                "cli.default placeholder not found in %s — leaving file as-is "
                "and using detected CLI '%s' for this run only.",
                config_path,
                detected,
            )
            return
        new_text = "".join(lines)
        tmp_path = config_path.with_suffix(config_path.suffix + ".tmp")
        tmp_path.write_text(new_text)
        tmp_path.replace(config_path)
        _config_logger.info(
            "cli.default was unset in %s; persisted detected CLI '%s' to disk.",
            config_path,
            detected,
        )
    except OSError as exc:
        _config_logger.warning(
            "Could not persist detected cli.default '%s' to %s (%s); "
            "continuing in-memory only. Edit the file manually to make it "
            "permanent.",
            detected,
            config_path,
            exc,
        )


def validate_cli_binaries(config: Config) -> List[str]:
    """Check that configured CLI binaries exist in PATH.

    Returns list of warnings for missing binaries.
    """
    warnings = []
    for name, tool in config.cli_tools.items():
        if not shutil.which(tool.binary):
            warnings.append(f"CLI binary '{tool.binary}' ({name}) not found in PATH")
    return warnings


def get_cli_tool_config(cli_name: str, config: Config, context: str = "CLI") -> CLIToolConfig:
    """Resolve a CLI tool and enforce the configured allowlist."""
    if cli_name not in config.cli_tools:
        raise ValueError(f"{context} '{cli_name}' is not defined in cli.tools")

    if cli_name not in config.cli_allowed:
        raise ValueError(
            f"{context} '{cli_name}' is blocked by cli.allowed: {config.cli_allowed}"
        )

    return config.cli_tools[cli_name]


def get_cli_command_parts(cli_name: str, tier: str, config: Config) -> Tuple[str, List[str], str]:
    """Get command parts for building CLI commands.

    Returns (binary, flags_list, model_string).
    """
    tool = get_cli_tool_config(cli_name, config, "dispatcher CLI")
    if tier not in tool.tier_map:
        _config_logger.warning(
            "Unresolvable tier %r for cli=%s (valid tiers: %s) — falling back "
            "to 'standard'. The caller passed a tier this provider cannot map; "
            "fix the role's tier rather than relying on this fallback.",
            tier, cli_name, sorted(tool.tier_map),
        )
    model = tool.tier_map.get(tier, tool.tier_map["standard"])
    return (tool.binary, tool.autonomous_flags, model)


def get_cli_effort_parts(cli_name: str, effort: str, config: Config) -> List[str]:
    """Return the argv fragment that sets reasoning effort for ``cli_name``.

    Provider-agnostic: the role carries a canonical effort (low..max); each
    provider's ``effort_map`` maps it to that provider's own value, capping
    where the provider tops out. Returns ``[]`` when the provider has no effort
    knob (empty ``effort_flag``) or the effort is unknown/unmapped — effort is
    advisory, so an unmapped value degrades to the CLI's own default rather than
    erroring. A ``-c key=value`` flag (codex) is emitted as two argv items; a
    bare flag (``--effort`` for claude/agy) as ``[flag, value]``.
    """
    tool = get_cli_tool_config(cli_name, config, "dispatcher CLI")
    if not tool.effort_flag or not tool.effort_map:
        return []
    if effort not in CANONICAL_EFFORTS:
        _config_logger.warning(
            "Unknown effort %r for cli=%s (valid: %s) — using the CLI default.",
            effort, cli_name, list(CANONICAL_EFFORTS),
        )
        return []
    mapped = tool.effort_map.get(effort)
    if not mapped:
        return []
    # Two argv items in every case: `["-c", "model_reasoning_effort=high"]` for
    # codex, `["--effort", "high"]` for claude/agy. The mapped value already
    # carries the codex `key=value` payload, so no per-provider special-casing.
    return [tool.effort_flag, mapped]


def resolve_tier_model(config: Config, tier: str, cli_name: Optional[str] = None) -> str:
    """Resolve a tier (fast|standard|strategic) to the configured model name for
    the given CLI — provider-agnostic. Defaults to ``config.cli_default``.

    Returns ``""`` when the provider uses its own default model (empty tier
    value) or the CLI/tier is unknown. NEVER returns a hardcoded model literal:
    orchestration is provider-agnostic, so model names come from each provider's
    ``tier_map`` in config, not from claude-specific constants.
    """
    cli = cli_name or getattr(config, "cli_default", "") or ""
    tool = (getattr(config, "cli_tools", {}) or {}).get(cli)
    tm = getattr(tool, "tier_map", None) if tool else None
    if not tm:
        return ""
    # Present-but-empty = the provider uses its own default model → return "".
    # Absent tier → fall back to the standard tier.
    if tier in tm:
        return (tm[tier] or "").strip()
    _config_logger.warning(
        "Unresolvable tier %r for cli=%s (valid tiers: %s) — falling back to "
        "'standard'. Fix the role's tier; this fallback hides misrouting.",
        tier, cli, sorted(tm),
    )
    return (tm.get("standard") or "").strip()


def apply_cli_preference(config: Config, cli_name: Optional[str]) -> Config:
    """Apply a task-level CLI preference across dispatch and orchestration."""
    if not cli_name:
        return config

    tool = get_cli_tool_config(cli_name, config, "task preferred_cli")
    standard_model = tool.tier_map.get("standard", config.orchestrator.decompose_model)
    fast_model = tool.tier_map.get("fast", standard_model)

    return replace(
        config,
        cli_default=cli_name,
        orchestrator=replace(
            config.orchestrator,
            decompose_cli=cli_name,
            decompose_model=standard_model,
            review_model=standard_model,
            review_model_conceptual=tool.tier_map.get("strategic", standard_model),
            sentinel_model=fast_model,
        ),
    )


def resolve_role(role_name: str, config: Config = None) -> str:
    """Resolve role prompt content from okuro.roles DB.

    Prefers lean_prompt, falls back to full prompt.

    Raises ValueError if role not found or has no prompt content.
    """
    from okuro.roles.registry import get_role

    role = get_role(role_name, level="lean")
    if role and role.get("content"):
        return role["content"]

    # Try full prompt as fallback
    role = get_role(role_name, level="full")
    if role and role.get("content"):
        return role["content"]

    raise ValueError(f"Role '{role_name}' not found or has no prompt content")


def load_role_index() -> Dict[str, Dict]:
    """Load role index from okuro.roles DB.

    Returns: {role_id: {domain, tier, model, description}}
    Falls back to empty dict on failure.
    """
    try:
        from okuro.roles.registry import list_roles

        roles = {}
        for r in list_roles():
            if r.get("maturity") == "draft":
                continue
            roles[r["id"]] = {
                "domain": r["domain"],
                "tier": r.get("tier", "standard"),
                "model": r.get("model", "sonnet"),
                "description": r.get("description", ""),
            }
        return roles
    except Exception as e:
        _config_logger.warning(f"Failed to load role index: {e}")
        return {}
