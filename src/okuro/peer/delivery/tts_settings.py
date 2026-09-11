# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: peer.delivery.tts_settings — the SINGLE user-configurable voice
#   store for the delivery audio pipeline (podcast / summary / morning brief).
#   One YAML (~/.okuro/tts-config.yaml), one read seam. The model is the same
#   for every engine: a standard FEMALE voice + a standard MALE voice, plus
#   okuro_gender saying WHICH of the two is okuro itself. Roles are DERIVED
#   (narrator/host_a = okuro's gender, host_b = the other), never configured.
#   Defaults: qwen (pro) sohee/aiden, kokoro (air+advanced) af_heart/am_michael,
#   okuro_gender=female, 1.0x pace (no robotic time-stretch).
# index: QWEN_VOICE_CATALOGUE | KOKORO_* | VOICE_FX | dataclass TTSSettings
#   | config_path | voice_pair | role_gender | role_speaker | is_okuro_role
#   | _fx | load | write | current | PRO_VOICE_CATALOGUE | source_wav
# AGENT_HEADER_END -->
"""User-configurable voice settings for the delivery audio pipeline.

Historically the podcast/summary voices were overridable ONLY through a
brand's ``voice_preset`` DB column — there was no user-facing setting. This
module is the missing layer: a single validated YAML store that all engines
read through, so a user can pick their narrator/host voices and pace once and
have it apply everywhere (podcast, summary, morning brief).

Precedence, unchanged elsewhere: **brand preset > these user settings >
hardcoded engine default**. Callers that already honour a brand preset keep
doing so; they fall through to :func:`current` only when no preset is set.

**The model (2026-07-15).** Every engine is configured the same way, with two
voices and one switch:

* a standard **female** voice and a standard **male** voice, from that engine's
  roster (``qwen_female``/``qwen_male``, ``kokoro_female``/``kokoro_male``);
* ``okuro_gender`` — which of the two okuro *itself* speaks with.

Roles are **derived** from those three, not configured (that is the point — a
per-role key is how the two hosts ended up as one voice). ``narrator`` and
``host_a`` are okuro, so they get okuro's gender voice; ``host_b`` is the
co-host and gets the OTHER one. A podcast therefore always uses both voices,
and "which one is okuro" is a single flip. Because the two voices come from
opposite halves of a gender-partitioned roster they can never collide — the
invariant ``role_voices_distinct`` guards is structural here, not a check bolted
on afterwards.

Pro tier (MOSS-TTSD) is voice-CLONE based: a "voice" is a short reference wav
(one of the auditioned ``candidate_NN`` clips in :data:`SOURCES_DIR`) that the
worker conditions every chunk on. Kokoro and Qwen are named-voice based. All are
expressed here as plain strings so one settings object serves every engine.
"""

from __future__ import annotations

import os
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

# Durable home for the MOSS reference source wavs (survives tmp cleanup). The
# auditioned candidate clips are copied here so a ref rebuild is reproducible
# and any candidate can be selected as a voice. Overridable for non-default
# installs (mirrors OKURO_MOSS_REFS in moss_worker).
def _sources_dir() -> Path:
    env = os.environ.get("OKURO_MOSS_REF_SOURCES")
    if env:
        return Path(env)
    from okuro.yu.conventions import get_convention

    return Path(
        get_convention("peer.moss_ref_sources", "~/.okuro/media/moss/refs/sources")
    ).expanduser()


SOURCES_DIR = _sources_dir()

# The neutral transcript every candidate clip was generated from — a MOSS
# voice-clone reference MUST carry the transcript matching its audio. All
# candidates share this text (see gen_candidates.py).
PRO_REF_TEXT = (
    "The deployment completed successfully. All three services restarted "
    "within expected limits, and memory usage remains stable."
)

# Auditioned pro-tier voices, labelled by ear (F0 from candidates_meta.json).
# label/gender are for the settings UI; the id is the wav basename in SOURCES_DIR.
PRO_VOICE_CATALOGUE: dict[str, dict[str, Any]] = {
    "candidate_01": {"gender": "male", "f0_hz": 137.9, "label": "Analyst · low male"},
    "candidate_02": {"gender": "male", "f0_hz": 118.2, "label": "Analyst · deep male"},
    "candidate_03": {"gender": "male", "f0_hz": 133.3, "label": "Analyst · even male"},
    "candidate_04": {"gender": "male", "f0_hz": 126.3, "label": "Analyst · warm male"},
    "candidate_05": {"gender": "male", "f0_hz": 122.4, "label": "Analyst · calm male"},
    "candidate_06": {"gender": "female", "f0_hz": 170.2, "label": "Analyst · even female"},
    "candidate_07": {"gender": "female", "f0_hz": 226.4, "label": "Analyst · bright female"},
    "candidate_08": {"gender": "male", "f0_hz": 125.7, "label": "Analyst · flat male"},
}

# Qwen3-TTS CustomVoice speakers — the pro-edition roster (2026-07-14). Gender is
# the SELECTION AXIS, not decoration: the settings model picks one female + one
# male, so the roster has to say which is which. qwen-tts ships no speaker
# metadata (checked in the isolated venv), hence this table.
QWEN_VOICE_CATALOGUE: dict[str, dict[str, Any]] = {
    "aiden":    {"gender": "male",   "label": "Aiden"},
    "dylan":    {"gender": "male",   "label": "Dylan"},
    "eric":     {"gender": "male",   "label": "Eric"},
    "ryan":     {"gender": "male",   "label": "Ryan"},
    "uncle_fu": {"gender": "male",   "label": "Uncle Fu"},
    "sohee":    {"gender": "female", "label": "Sohee"},
    "vivian":   {"gender": "female", "label": "Vivian"},
    "serena":   {"gender": "female", "label": "Serena"},
    "ono_anna": {"gender": "female", "label": "Ono Anna"},
}
QWEN_SPEAKERS = tuple(QWEN_VOICE_CATALOGUE)

GENDERS = ("female", "male")


def _other_gender(gender: str) -> str:
    return "male" if gender == "female" else "female"


def qwen_gender(voice_id: str) -> Optional[str]:
    """'female' | 'male' for a Qwen speaker id, or None if it isn't one."""
    meta = QWEN_VOICE_CATALOGUE.get(voice_id)
    return meta["gender"] if meta else None


def kokoro_gender(voice_id: str) -> Optional[str]:
    """'female' | 'male' for a Kokoro voice id, or None if unreadable.

    Kokoro encodes it in the id: ``<lang><f|m>_<name>`` (af_heart, am_michael,
    bf_alice, zm_yunjian…). Derived lexically ON PURPOSE — the alternative is
    ``tts.list_voices()``, which loads the ONNX engine, and settings validation
    runs on every synth. A lexical rule costs nothing and covers all 50+ ids
    including any the release adds later.
    """
    if len(voice_id) < 3 or voice_id[2] != "_":
        return None
    return {"f": "female", "m": "male"}.get(voice_id[1])
# Locked pro narrator instruct — structured acoustic-attribute block (the format
# Qwen3-TTS instruct control is trained on; see Qwen3-TTS release 2026-01-21).
QWEN_DEFAULT_INSTRUCT = (
    "pitch: low and warm, steady, minimal variation. "
    "speed: measured — unhurried but efficient, never rushed. "
    "volume: even and controlled. "
    "clarity: highly articulate, precise diction. "
    "fluency: seamless, no hesitation. "
    "emotion: calm and gently warm, low-key — no enthusiasm, no drama. "
    "tone: warm, composed, matter-of-fact, like a meditation guide reading a system readout. "
    "texture: warm, rounded, smooth, faintly synthetic. "
    "personality: calm, precise, reassuring."
)

# okuro voice post-fx chain — the HOUSE sound. Applied at the tts synth seam via
# ``voice_fx.house``, so EVERY engine's voices wear it (qwen, kokoro, orpheus);
# it lived inside qwen_worker until 2026-07-15, which made the house sound an
# accident of which engine your hardware got. An exact port of the browser
# fx-tuner DSP (numpy+scipy only, no new dep, platform-independent).
# Founder-tuned default (2026-07-14); fully adjustable via
# ~/.okuro/tts-config.yaml `voice_fx`. `enabled` is the single global switch —
# false ships every voice dry, on every engine (surfaced in /settings/tts).
VOICE_FX = {
    "enabled": True,
    "highpass_hz": 141.0,
    "eq": {"low_hz": 40.0, "low_db": -8.5, "mid_hz": 2600.0, "mid_db": -14.0,
           "mid_q": 1.7, "high_hz": 4900.0, "high_db": -0.5},
    "chorus": {"rate_hz": 0.6, "depth_ms": 3.8, "mix": 0.59},
    "delay": {"seconds": 0.18, "feedback": 0.12, "mix": 0.07},
    "reverb": {"decay_s": 0.7, "mix": 0.07},
    "compressor": {"threshold_db": -21.0, "ratio": 20.0},
    "lowpass_hz": 6000.0,
}

# okuro brand chord-drone bed under the brief — a low open quartal pad (D–G–C–F),
# pre-roll intro + fade-out tail. Applied in render_brief_audio via voice_drone.
# Founder-tuned (2026-07-14); adjustable via ~/.okuro/tts-config.yaml `voice_drone`.
VOICE_DRONE = {
    "enabled": True,
    "notes": [73.42, 98.00, 130.81, 174.61],   # D2–G2–C3–F3 (quartal-hi)
    "level": 0.13,
    "cutoff_hz": 4800.0,
    # Clip length at which the bed reaches full size. Shorter clips get a
    # proportionally smaller bed, so branding never outweighs content — the
    # times below are tuned for a ~2min brief, and 5s of pad wrapped around a
    # 15s prism audio-brief would be the bed talking, not okuro. <=0 disables
    # scaling (always full). See voice_drone.apply.
    "full_at_s": 30.0,
    "pre_roll_s": 2.5,
    "tail_s": 2.5,
    "fade_in_s": 2.0,
    "fade_out_s": 2.5,
    "detune_cents": 4.0,
    "breath_hz": 0.07,
    "breath": 0.15,
}

# Kokoro pace/speed clamp (shared with pro). Below 0.5 / above 2.0 the stretch
# artefacts dominate regardless of algorithm.
SPEED_MIN = 0.5
SPEED_MAX = 2.0


@dataclass
class TTSSettings:
    """Validated representation of ``~/.okuro/tts-config.yaml``.

    Field defaults ARE the shipped defaults — a fresh install with no YAML gets
    exactly these via :func:`current`.
    """

    # WHICH of the two standard voices is okuro itself. One switch, engine-wide:
    # okuro's identity doesn't change because the hardware tier did, and the user
    # only ever sees one tier's roster. narrator/host_a follow it; host_b is
    # always the other gender, so a podcast is always two people.
    okuro_gender: str = "female"

    # Pro tier = Qwen3-TTS CustomVoice (2026-07-14; replaced MOSS). The founder-
    # approved pair (2026-07-15) — sohee is okuro, aiden co-hosts.
    qwen_female: str = "sohee"             # QWEN_VOICE_CATALOGUE, gender=female
    qwen_male: str = "aiden"               # QWEN_VOICE_CATALOGUE, gender=male
    qwen_language: str = "english"
    # okuro's delivery direction. Only okuro's own voice wears it — the co-host
    # renders without it, so it talks like a co-host, not a second okuro.
    qwen_instruct: str = QWEN_DEFAULT_INSTRUCT
    qwen_speed: float = 1.0                # 1.0 = NO time-stretch
    voice_fx: dict = field(default_factory=lambda: dict(VOICE_FX))      # post-synth fx chain
    voice_drone: dict = field(default_factory=lambda: dict(VOICE_DRONE))  # chord-drone bed

    # Legacy pro tier (MOSS voice-clone refs) — retired from auto-selection, kept
    # for the explicit OKURO_TTS_ENGINE=moss pin. id = wav basename in SOURCES_DIR.
    pro_female_ref: str = "candidate_06"   # -> [S1] narrator / host_a (female)
    pro_male_ref: str = "candidate_02"     # -> [S2] host_b (male)
    pro_speed: float = 1.0                 # 1.0 = NO time-stretch (robotic-free)

    # air + advanced tiers (Kokoro named voices). Same two-voice model.
    kokoro_female: str = "af_heart"
    kokoro_male: str = "am_michael"
    kokoro_speed: float = 1.0

    updated_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def config_path() -> Path:
    """Resolve ``~/.okuro/tts-config.yaml`` honouring ``$OKURO_HOME``."""
    home_env = os.environ.get("OKURO_HOME")
    base = Path(home_env) if home_env else Path.home() / ".okuro"
    return base / "tts-config.yaml"


def source_wav(voice_id: str) -> Path:
    """Absolute path to a pro-tier candidate reference wav in SOURCES_DIR."""
    return SOURCES_DIR / f"{voice_id}.wav"


# Per-engine binding of the ONE model: which fields hold the pair, and how to
# read a voice id's gender. Adding an engine is a row here, not a new code path —
# every resolver below drives off this table.
_ENGINE_PAIR: dict[str, tuple[str, str, Any]] = {
    "qwen": ("qwen_female", "qwen_male", qwen_gender),
    "kokoro": ("kokoro_female", "kokoro_male", kokoro_gender),
}


def voice_pair(engine: str, settings: Optional["TTSSettings"] = None) -> Optional[tuple[str, str]]:
    """``(female_voice, male_voice)`` for an engine, or None if it has no pair.

    MOSS is the None case: it tags turns ``[S1]``/``[S2]`` inline rather than
    selecting voice ids, so it has no pair to resolve.
    """
    fields = _ENGINE_PAIR.get(engine)
    if not fields:
        return None
    s = settings or current()
    female_field, male_field, _ = fields
    return getattr(s, female_field), getattr(s, male_field)


def is_okuro_role(role: str) -> bool:
    """True when this role IS okuro speaking (everything except the co-host)."""
    return role != "host_b"


def role_gender(role: str, okuro_gender: str) -> str:
    """Gender for a semantic role — okuro's for okuro, the other for the co-host."""
    return okuro_gender if is_okuro_role(role) else _other_gender(okuro_gender)


def role_speaker(role: str, engine: str,
                 settings: Optional["TTSSettings"] = None) -> Optional[str]:
    """The engine voice id a semantic role speaks with, or None if no pair.

    This is the whole derivation: narrator/host_a get okuro's gender voice,
    host_b gets the other. Nothing else needs to know how roles map to voices.
    """
    s = settings or current()
    pair = voice_pair(engine, s)
    if pair is None:
        return None
    female, male = pair
    return female if role_gender(role, s.okuro_gender) == "female" else male


def _clamp_speed(val: Any, default: float) -> float:
    try:
        s = float(val)
    except (TypeError, ValueError):
        return default
    return max(SPEED_MIN, min(SPEED_MAX, s))


def _pick(raw: dict[str, Any], key: str, gender: str, gender_of: Any,
          default: str, legacy_keys: tuple[str, ...] = ()) -> str:
    """Resolve one half of an engine's pair — never raises.

    A voice must exist AND be of the slot's gender, else the shipped default
    wins. Falling back rather than raising is the store's contract: a bad
    setting must never break the daily brief, and the UI only ever offers
    same-gender ids anyway, so this fires for hand-edited/legacy YAML only.

    ``legacy_keys`` are pre-2026-07-15 per-role keys (qwen_speaker, kokoro_host_a
    …) read ONLY when the new key is absent: an existing config keeps the voices
    it had instead of silently snapping back to defaults on upgrade.
    """
    val = raw.get(key)
    if val is None:
        for legacy in legacy_keys:
            cand = raw.get(legacy)
            if cand is not None and gender_of(str(cand)) == gender:
                val = cand
                break
    if val is None:
        return default
    val = str(val)
    return val if gender_of(val) == gender else default


def _fx(raw: dict[str, Any]) -> dict[str, Any]:
    """The ``voice_fx`` block with ``enabled`` normalized to a real bool.

    ``enabled`` is a user-facing switch (settings UI), so it has to survive a
    round trip through hand-edited YAML. A MISSING key means "shipped default"
    (on) rather than off: ``voice_fx.apply`` reads it falsily, so an fx block
    that merely omits the key would silently ship every voice dry — the switch is
    the only thing allowed to do that.
    """
    got = raw.get("voice_fx")
    if not isinstance(got, dict):
        return dict(VOICE_FX)
    fx = dict(got)
    fx["enabled"] = bool(fx.get("enabled", VOICE_FX["enabled"]))
    return fx


def _validate(raw: dict[str, Any]) -> TTSSettings:
    d = TTSSettings()  # defaults
    female = str(raw.get("pro_female_ref", d.pro_female_ref))
    male = str(raw.get("pro_male_ref", d.pro_male_ref))
    if female not in PRO_VOICE_CATALOGUE:
        raise ValueError(
            f"invalid pro_female_ref {female!r} — one of {sorted(PRO_VOICE_CATALOGUE)}")
    if male not in PRO_VOICE_CATALOGUE:
        raise ValueError(
            f"invalid pro_male_ref {male!r} — one of {sorted(PRO_VOICE_CATALOGUE)}")

    okuro_gender = str(raw.get("okuro_gender", d.okuro_gender)).strip().lower()
    if okuro_gender not in GENDERS:
        okuro_gender = d.okuro_gender

    # The two voices are drawn from opposite halves of a gender-partitioned
    # roster, so female != male holds by construction — there is no way to
    # express the "both hosts are one voice" bug in this model.
    qwen_female = _pick(raw, "qwen_female", "female", qwen_gender, d.qwen_female,
                        legacy_keys=("qwen_speaker", "qwen_host_b"))
    qwen_male = _pick(raw, "qwen_male", "male", qwen_gender, d.qwen_male,
                      legacy_keys=("qwen_host_b", "qwen_speaker"))
    kokoro_female = _pick(raw, "kokoro_female", "female", kokoro_gender, d.kokoro_female,
                          legacy_keys=("kokoro_host_a", "kokoro_narrator", "kokoro_host_b"))
    kokoro_male = _pick(raw, "kokoro_male", "male", kokoro_gender, d.kokoro_male,
                        legacy_keys=("kokoro_host_b", "kokoro_narrator", "kokoro_host_a"))

    return TTSSettings(
        okuro_gender=okuro_gender,
        qwen_female=qwen_female,
        qwen_male=qwen_male,
        qwen_language=str(raw.get("qwen_language", d.qwen_language)),
        qwen_instruct=str(raw.get("qwen_instruct", d.qwen_instruct)),
        qwen_speed=_clamp_speed(raw.get("qwen_speed"), d.qwen_speed),
        voice_fx=_fx(raw),
        voice_drone=raw.get("voice_drone") if isinstance(raw.get("voice_drone"), dict) else dict(VOICE_DRONE),
        pro_female_ref=female,
        pro_male_ref=male,
        pro_speed=_clamp_speed(raw.get("pro_speed"), d.pro_speed),
        kokoro_female=kokoro_female,
        kokoro_male=kokoro_male,
        kokoro_speed=_clamp_speed(raw.get("kokoro_speed"), d.kokoro_speed),
        updated_at=str(raw.get("updated_at", "")),
    )


def load(path: Optional[Path] = None) -> Optional[TTSSettings]:
    """Load + validate the YAML. Returns ``None`` if the file doesn't exist."""
    p = path or config_path()
    if not p.exists():
        return None
    import yaml

    raw = yaml.safe_load(p.read_text()) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"{p} root must be a YAML mapping, got {type(raw)}")
    return _validate(raw)


def current(path: Optional[Path] = None) -> TTSSettings:
    """The effective settings — the YAML if present, else shipped defaults.

    Never raises for a missing file (a fresh install has none): callers always
    get usable values. A malformed file DOES raise so it isn't silently ignored.
    """
    got = load(path)
    return got if got is not None else TTSSettings()


def write(settings: TTSSettings, *, path: Optional[Path] = None) -> Path:
    """Atomically write the YAML (mode 0600); stamps ``updated_at=now``."""
    p = path or config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = settings.to_dict()
    payload["updated_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    import yaml

    body = yaml.safe_dump(payload, sort_keys=False, default_flow_style=False)
    fd, tmp_path = tempfile.mkstemp(prefix=".tts-config.", dir=str(p.parent))
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
