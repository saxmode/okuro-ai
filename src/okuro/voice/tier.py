# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: STT tier authority — the single seam that maps an okuro subscription
#   tier (air / plus / pro) to a concrete Whisper backend (model size, device,
#   compute type) for both streaming and batch dictation. Owns the human-facing
#   tier catalogue served to Settings, the entitlement hook where okuro-pro
#   billing will plug in, and the hardware clamp so we never promise a GPU model
#   on a CPU-only box.
# index:
#   Tier / STTBackend / TierSpec types
#   TIERS catalogue (labels + characteristics + best_for)
#   entitled_tier / hardware_tier / _detection
#   selected_tier / resolve_tier / resolve_stt
#   config load / write (~/.okuro/stt-config.yaml)
# AGENT_HEADER_END -->
"""STT tier resolution — one authority for "which Whisper, on what device".

Three subscription tiers, ascending quality/cost:

    air   — CPU-only, small models. Runs anywhere, private, fast, modest accuracy.
    plus  — bigger models, opportunistic GPU. Better multilingual accuracy.
    pro   — large-v3 on a dedicated GPU. Best possible accuracy, needs a GPU.

The *effective* tier is resolved by precedence (highest first):

    1. OKURO_STT_TIER env          — raw testing override, bypasses the clamp so
                                     you can exercise air/plus/pro on one box.
                                     Forcing above your hardware may be slow/fail.
    2. ~/.okuro/stt-config.yaml     — the user's choice from Settings (clamped to
                                     what the subscription entitles).
    3. min(entitled, hardware)      — automatic default.

GPU selection ALWAYS goes through ``capability.recommended_device`` — the shared
hardware authority that already refuses to auto-pick the *display* GPU (the rule
that ended the Chrome GPU-process freezes). This module never scans GPUs itself.

Explicit per-engine model pins (``OKURO_STT_STREAM_MODEL`` for streaming,
``OKURO_WHISPER_MODEL`` for batch) still win over the tier's model size — a power
user who pinned a model keeps it; only the device/compute come from the tier.
"""

from __future__ import annotations

import logging
import os
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Optional

logger = logging.getLogger("okuro.voice.tier")

Tier = Literal["air", "plus", "pro"]
ChosenBy = Literal["onboarding", "web", "cli", "auto"]

# ascending order — index is the comparison key for clamping.
TIER_ORDER: tuple[Tier, ...] = ("air", "plus", "pro")


def _rank(t: Tier) -> int:
    return TIER_ORDER.index(t)


def _min_tier(a: Tier, b: Tier) -> Tier:
    return a if _rank(a) <= _rank(b) else b


# ── tier catalogue ──────────────────────────────────────────────────────
@dataclass(frozen=True)
class TierSpec:
    """Static description of one STT tier — the single source of truth shared by
    the inference path and the Settings UI (served verbatim via the API)."""

    tier: Tier
    label: str
    # streaming (real-time dictation) + batch (upload) model sizes.
    stream_model: str
    batch_model: str
    # "cpu" forces CPU; "auto" means capability.recommended_device (GPU if a
    # non-display >=8GB GPU exists, else CPU).
    device_policy: Literal["cpu", "auto"]
    needs_gpu: bool
    characteristics: str
    best_for: str


# Adding a tier is one entry here — the API, Settings UI and resolver all read
# from this map, so descriptions stay in exactly one place.
TIERS: dict[Tier, TierSpec] = {
    "air": TierSpec(
        tier="air",
        label="Air",
        stream_model="base",
        batch_model="small",
        device_policy="cpu",
        needs_gpu=False,
        characteristics=(
            "CPU-only, small Whisper models (~150–250 MB). Real-time on any "
            "modern laptop, fully private, no GPU required. Solid English, "
            "usable German; accuracy drops on noisy audio or rare words."
        ),
        best_for="Laptops and GPU-less servers. Anyone who wants dictation that just works everywhere.",
    ),
    "plus": TierSpec(
        tier="plus",
        label="Plus",
        stream_model="small",
        batch_model="medium",
        device_policy="auto",
        needs_gpu=False,
        characteristics=(
            "Larger models with opportunistic GPU acceleration (falls back to "
            "CPU if none). Noticeably better multilingual and punctuation "
            "accuracy than Air, still comfortably local."
        ),
        best_for="Mixed-language users and mid-range boxes. The everyday sweet spot when you have some GPU headroom.",
    ),
    "pro": TierSpec(
        tier="pro",
        label="Pro",
        stream_model="large-v3",
        batch_model="large-v3",
        device_policy="auto",
        needs_gpu=True,
        characteristics=(
            "Whisper large-v3 on a dedicated non-display GPU (float16). Best "
            "accuracy available — strongest on accents, code-switching and "
            "domain terms. Requires a ≥8 GB GPU; on CPU it is too slow for "
            "real-time and will be refused unless forced."
        ),
        best_for="Workstations with a spare GPU. Users who want the highest-quality transcription okuro can produce.",
    ),
}

DEFAULT_TIER: Tier = "air"


# ── resolved backend ────────────────────────────────────────────────────
@dataclass(frozen=True)
class STTBackend:
    """Concrete Whisper launch parameters for one request."""

    tier: Tier
    model_size: str
    device: str  # "cpu" | "cuda:N" | "mps"
    compute_type: str  # "int8" | "int8_float16" | "float16"
    use_broker: bool  # pro/GPU → reserve VRAM via the inference broker (hook)

    def describe(self) -> str:
        return f"{self.tier}:{self.model_size} on {self.device} ({self.compute_type})"


# ── entitlement + hardware ──────────────────────────────────────────────
def entitled_tier() -> Tier:
    """Highest tier the user's *subscription* allows.

    STUB / integration seam: okuro-pro billing plugs in HERE. Until then the
    self-hosted install is unlimited (``pro``), overridable with
    ``OKURO_ENTITLEMENT`` for testing the entitlement clamp itself.
    """
    env = os.environ.get("OKURO_ENTITLEMENT", "").strip().lower()
    if env in TIERS:
        return env  # type: ignore[return-value]
    return "pro"


def _detection() -> dict[str, Any]:
    from okuro.capability import capabilities

    return capabilities()


def hardware_tier(detection: Optional[dict[str, Any]] = None) -> Tier:
    """Highest tier this *hardware* can actually run.

    A non-display GPU with ≥8 GB VRAM unlocks ``pro``; otherwise the CPU ceiling
    is ``plus`` (``medium`` batch is slow but works; ``air`` stays real-time).
    """
    from okuro.capability import recommended_device

    det = detection if detection is not None else _detection()
    device = recommended_device(det)
    if device.startswith("cuda:") or device == "mps":
        return "pro"
    return "plus"


def _resolve_device(policy: str, detection: dict[str, Any]) -> str:
    if policy == "cpu":
        return "cpu"
    from okuro.capability import recommended_device

    dev = recommended_device(detection)  # never the display GPU
    return dev if dev != "auto" else "cpu"


def _compute_for(device: str) -> str:
    if device.startswith("cuda:"):
        return "float16"
    if device == "mps":
        return "int8_float16"
    return "int8"


# ── tier selection ──────────────────────────────────────────────────────
def selected_tier() -> Optional[Tier]:
    """The user's saved choice from Settings, if any."""
    cfg = load()
    return cfg.tier if cfg else None


def resolve_tier(detection: Optional[dict[str, Any]] = None) -> tuple[Tier, str]:
    """Return (effective_tier, reason). See module docstring for precedence."""
    det = detection if detection is not None else _detection()
    hw = hardware_tier(det)
    ent = entitled_tier()

    env = os.environ.get("OKURO_STT_TIER", "").strip().lower()
    if env in TIERS:
        if _rank(env) > _rank(hw):  # type: ignore[arg-type]
            logger.warning(
                "OKURO_STT_TIER=%s forces a tier above this hardware (%s) — "
                "may be slow or fail; honouring it for testing.",
                env,
                hw,
            )
        return env, "env-override"  # type: ignore[return-value]

    chosen = selected_tier()
    if chosen is not None:
        return _min_tier(chosen, ent), "settings"

    return _min_tier(ent, hw), "auto"


def resolve_stt(mode: Literal["stream", "batch"], language: str | None = None) -> STTBackend:
    """Resolve the concrete Whisper backend for a request.

    ``mode`` picks stream vs batch model size. Explicit model-pin envs still win
    over the tier's model size (device/compute come from the tier regardless).
    """
    det = _detection()
    tier, _reason = resolve_tier(det)
    spec = TIERS[tier]

    device = _resolve_device(spec.device_policy, det)
    compute = _compute_for(device)
    use_broker = spec.needs_gpu and device.startswith("cuda:")

    if mode == "stream":
        model = os.environ.get("OKURO_STT_STREAM_MODEL") or spec.stream_model
    else:
        model = os.environ.get("OKURO_WHISPER_MODEL") or spec.batch_model

    return STTBackend(
        tier=tier,
        model_size=model,
        device=device,
        compute_type=compute,
        use_broker=use_broker,
    )


# ── persistence: ~/.okuro/stt-config.yaml ───────────────────────────────
@dataclass
class STTConfigFile:
    """Validated representation of ``~/.okuro/stt-config.yaml``."""

    tier: Tier = DEFAULT_TIER
    chosen_at: str = ""
    chosen_by: ChosenBy = "auto"
    last_detection: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def config_path() -> Path:
    """Resolve ``~/.okuro/stt-config.yaml`` honouring ``$OKURO_HOME``."""
    home_env = os.environ.get("OKURO_HOME")
    base = Path(home_env) if home_env else Path.home() / ".okuro"
    return base / "stt-config.yaml"


def _validate(raw: dict[str, Any]) -> STTConfigFile:
    tier_in = str(raw.get("tier", DEFAULT_TIER)).lower()
    if tier_in not in TIERS:
        raise ValueError(f"invalid tier {tier_in!r} — must be one of {list(TIERS)}")
    chosen_by = str(raw.get("chosen_by", "auto")).lower()
    if chosen_by not in {"onboarding", "web", "cli", "auto"}:
        raise ValueError(f"invalid chosen_by {chosen_by!r} — onboarding|web|cli|auto")
    last_detection = raw.get("last_detection") or {}
    if not isinstance(last_detection, dict):
        raise ValueError("last_detection must be a mapping")
    return STTConfigFile(
        tier=tier_in,  # type: ignore[arg-type]
        chosen_at=str(raw.get("chosen_at", "")),
        chosen_by=chosen_by,  # type: ignore[arg-type]
        last_detection=last_detection,
    )


def load(path: Optional[Path] = None) -> Optional[STTConfigFile]:
    """Load + validate the config file. Returns ``None`` if it doesn't exist."""
    p = path or config_path()
    if not p.exists():
        return None
    import yaml

    raw = yaml.safe_load(p.read_text()) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"{p} root must be a YAML mapping, got {type(raw)}")
    return _validate(raw)


def write(config: STTConfigFile, *, path: Optional[Path] = None) -> Path:
    """Atomically write the config file with mode 0600; stamps chosen_at=now."""
    p = path or config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = config.to_dict()
    payload["chosen_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    import yaml

    body = yaml.safe_dump(payload, sort_keys=False, default_flow_style=False)
    fd, tmp_path = tempfile.mkstemp(prefix=".stt-config.", dir=str(p.parent))
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(body)
        os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, p)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
    return p
