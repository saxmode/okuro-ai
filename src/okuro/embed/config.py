# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Persistent embedding tier + device config — single source of truth.
# index:
#   imports
#   class TierSpec
#   const TIERS
#   class EmbedConfig
#   def config_path
#   def load
#   def write
#   def load_or_init
# AGENT_HEADER_END -->
"""Persistent embedding tier + device config — single source of truth.

The user's tier + device choice lives in ``~/.okuro/embed-config.yaml``.
Every install + service-start reads this file. Onboarding writes it once.
The web settings page rewrites it when the user switches tiers. Nothing
else (install.py upgrades, daemon-reload, fresh ``okuro init``) is allowed
to overwrite an existing file — see ``load_or_init`` for the contract.

Why a YAML file (not a DB row): the file must be readable BEFORE the DB
exists, by install.py at first-install time, and must survive a wiped
SQLite without losing the user's hardware choice.
"""

from __future__ import annotations

import logging
import os
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Optional

logger = logging.getLogger("okuro.embed.config")

Tier = Literal["low", "high"]
ChosenBy = Literal["onboarding", "web", "cli", "auto"]


@dataclass(frozen=True)
class TierSpec:
    """Static description of one embedding tier."""

    tier: Tier
    model_id: str
    bundled_dirname: str
    dim: int
    description: str


# Tier registry. Adding a new tier is one entry here + (if needed) a
# vendored model directory under embed/models/.
TIERS: dict[Tier, TierSpec] = {
    "low": TierSpec(
        tier="low",
        model_id="Alibaba-NLP/gte-modernbert-base",
        bundled_dirname="gte-modernbert-base",
        dim=768,
        description="Laptop-safe — CPU/iGPU friendly, ~150MB.",
    ),
    "high": TierSpec(
        tier="high",
        model_id="Qwen/Qwen3-Embedding-0.6B",
        bundled_dirname="Qwen3-Embedding-0.6B",
        dim=1024,
        description="Best quality — needs >=8GB non-display GPU.",
    ),
}


@dataclass
class EmbedConfig:
    """Validated representation of ``~/.okuro/embed-config.yaml``."""

    # Default to the laptop-safe tier. "high" (Qwen3-0.6B, 1024d) needs a
    # >=8GB non-display GPU and a 1.2GB model download; defaulting to it left
    # GPU-less machines (e.g. a Mac) pulling Qwen3 from HF at runtime, which
    # failed and left okuro-embed inactive. recommended_tier() still upgrades
    # GPU boxes to "high"; this is only the fallback when nothing ran detection.
    tier: Tier = "low"
    # device strings: "cpu", "cuda:N", "mps", or "auto" (resolved by detect)
    device: str = "auto"
    chosen_at: str = ""  # ISO 8601 UTC, populated on write
    chosen_by: ChosenBy = "auto"
    last_detection: dict[str, Any] = field(default_factory=dict)

    @property
    def spec(self) -> TierSpec:
        return TIERS[self.tier]

    @property
    def model_id(self) -> str:
        return self.spec.model_id

    @property
    def bundled_dirname(self) -> str:
        return self.spec.bundled_dirname

    @property
    def dim(self) -> int:
        return self.spec.dim

    def cuda_visible_devices(self) -> Optional[str]:
        """Return the CUDA_VISIBLE_DEVICES value to inject, or None to leave unset.

        - cuda:N → "N"
        - cpu / mps / auto → None (unset; embed/server picks naturally)
        """
        if self.device.startswith("cuda:"):
            return self.device.split(":", 1)[1]
        return None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def config_path() -> Path:
    """Resolve ``~/.okuro/embed-config.yaml`` honouring ``$OKURO_HOME``."""
    home_env = os.environ.get("OKURO_HOME")
    base = Path(home_env) if home_env else Path.home() / ".okuro"
    return base / "embed-config.yaml"


def _validate(raw: dict[str, Any]) -> EmbedConfig:
    tier_in = str(raw.get("tier", "low")).lower()
    if tier_in not in TIERS:
        raise ValueError(
            f"invalid tier {tier_in!r} — must be one of {list(TIERS)}"
        )
    device = str(raw.get("device", "auto"))
    if device != "auto" and device != "cpu" and device != "mps":
        if not device.startswith("cuda:"):
            raise ValueError(
                f"invalid device {device!r} — expected auto|cpu|mps|cuda:N"
            )
        suffix = device.split(":", 1)[1]
        if not suffix.isdigit():
            raise ValueError(
                f"invalid device {device!r} — cuda index must be integer"
            )
    chosen_by = str(raw.get("chosen_by", "auto")).lower()
    if chosen_by not in {"onboarding", "web", "cli", "auto"}:
        raise ValueError(
            f"invalid chosen_by {chosen_by!r} — onboarding|web|cli|auto"
        )
    last_detection = raw.get("last_detection") or {}
    if not isinstance(last_detection, dict):
        raise ValueError("last_detection must be a mapping")
    return EmbedConfig(
        tier=tier_in,  # type: ignore[arg-type]
        device=device,
        chosen_at=str(raw.get("chosen_at", "")),
        chosen_by=chosen_by,  # type: ignore[arg-type]
        last_detection=last_detection,
    )


def load(path: Optional[Path] = None) -> Optional[EmbedConfig]:
    """Load + validate the config file. Returns ``None`` if it doesn't exist.

    A malformed file raises ``ValueError`` rather than silently returning
    None — callers (install.py, server) decide whether to fall back to
    detection or surface the error to the user.
    """
    p = path or config_path()
    if not p.exists():
        return None
    import yaml

    raw = yaml.safe_load(p.read_text()) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"{p} root must be a YAML mapping, got {type(raw)}")
    return _validate(raw)


def write(
    config: EmbedConfig,
    *,
    path: Optional[Path] = None,
) -> Path:
    """Atomically write the config file with mode 0600.

    Stamps ``chosen_at`` to now (UTC) so callers don't have to.
    """
    p = path or config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = config.to_dict()
    payload["chosen_at"] = datetime.now(timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    import yaml

    body = yaml.safe_dump(payload, sort_keys=False, default_flow_style=False)
    # Atomic write: tmp file in same dir + rename. Mode 0600 set on the tmp
    # file so the file is never world-readable in flight.
    fd, tmp_path = tempfile.mkstemp(prefix=".embed-config.", dir=str(p.parent))
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


def load_or_init(
    *,
    interactive: bool = False,
    detector=None,
    path: Optional[Path] = None,
) -> EmbedConfig:
    """Return the persisted config, or write+return a recommended one.

    Hard rules (the meta-requirement of v2):
    - If the file already exists and validates: return it untouched.
    - If absent: run hardware detection, derive the recommendation,
      write it as ``chosen_by="auto"``, return it.
    - If present-but-malformed: log a warning, treat as missing.

    Interactive callers (CLI ``okuro init``) should bypass this helper
    and prompt the user, then call ``write()`` directly with
    ``chosen_by="cli"``. ``interactive`` is exposed for symmetry but
    this helper itself never prompts.
    """
    p = path or config_path()
    if p.exists():
        try:
            existing = load(p)
            if existing is not None:
                return existing
        except (ValueError, OSError) as exc:
            logger.warning(
                "embed-config.yaml at %s is malformed (%s) — "
                "regenerating from detection",
                p, exc,
            )

    if detector is None:
        from okuro.embed import detect as detect_mod
        detector = detect_mod.capabilities

    detection = detector()
    from okuro.embed import detect as detect_mod
    tier = detect_mod.recommended_tier(detection)
    device = detect_mod.recommended_device(detection)
    cfg = EmbedConfig(
        tier=tier,
        device=device,
        chosen_by="auto",
        last_detection=detection,
    )
    write(cfg, path=p)
    return cfg
