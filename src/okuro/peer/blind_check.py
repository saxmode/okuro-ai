# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: peer.blind_check — P3.4's review-is-not-validation pass. Pre-registered
#   A/B between a profile-predicted rewrite and its opposite pole, scored into
#   a T3 confirmation or an axis confidence floor.
# index: BlindCheckProtocol | axis_variant_profiles | present_pair |
#   build_pair | record_outcome
# AGENT_HEADER_END -->
"""Blind validation of a cognitive profile — the answer to overtrust.

THE PROBLEM. A profile that has been LOOKED AT reads as a profile that has
been TESTED. Every consumer downstream then treats a self-report as ground
truth, and the system's confidence in itself grows with use rather than with
evidence (Q1 schema research :92, :150 — the "overtrust engine").

THE INSTRUMENT. Take ONE real message. Rewrite it twice: once through the
profile as stored, once with a single axis flipped to its opposite pole.
Everything else — sender profile, prompt, model, the message itself — is held
identical, so a preference is attributable to that axis and nothing else.
Show both unlabelled, in randomised order. Record which one they picked.

TWO RULES THIS MODULE ENFORCES RATHER THAN DOCUMENTS.

*A threshold chosen after seeing results is not a test.* ``n`` and
``agreement_threshold`` have no defaults, and a protocol is refused unless it
carries the id of the artifact they were written into BEFORE the run. There
is no way to call this having decided what counts as success afterwards.

*Disagreement floors, never overwrites.* One preference between two rewrites
is evidence the stored value is wrong. It is NOT evidence of what the right
value is — a person who rejects "jargon=5" has not thereby said "jargon=1".
Writing the opposite pole would be the same overtrust with the sign reversed.
So agreement promotes to T3 (confidence 1.0, value untouched) and
disagreement lowers confidence (value untouched). The value only ever moves
when the person states it.

WHAT THIS MODULE DOES NOT DO. It does not send anything to anybody. Producing
the pair and recording an answer are separate calls with a human in between,
and the human-approval gate in PLAN v5 governs whether that human exists yet.
"""

from __future__ import annotations

import json
import random
from dataclasses import dataclass
from typing import Any

from okuro.peer.axis_registry import axis as axis_spec

# Disagreement floor. Below the 0.6 threshold person_lens uses to call an axis
# unconfirmed, so a rejected axis surfaces as "(prior)" everywhere a prior
# would — which is exactly what it has become.
DISAGREEMENT_CONFIDENCE = 0.4

_NEUTRAL = 3


@dataclass(frozen=True)
class BlindCheckProtocol:
    """What counts as success, fixed before any result is visible.

    Every field is required. A default here would be a threshold nobody
    chose, arriving with the authority of one that was.
    """

    n: int
    agreement_threshold: float
    axes: tuple[str, ...]
    registered_artifact_id: str

    def __post_init__(self) -> None:
        if not (self.registered_artifact_id or "").strip():
            raise ValueError(
                "registered_artifact_id is required: write n, the agreement "
                "threshold and the axes into an artifact and pre-register it "
                "BEFORE the run. A threshold chosen after seeing results is "
                "not a test."
            )
        if self.n < 1:
            raise ValueError(f"n must be at least 1, got {self.n}")
        if not 0.0 < self.agreement_threshold <= 1.0:
            raise ValueError(
                f"agreement_threshold must be in (0, 1], got "
                f"{self.agreement_threshold}"
            )
        if len(self.axes) < 3:
            raise ValueError(
                f"P3.4 requires at least 3 consumer-backed axes, got "
                f"{len(self.axes)}: {list(self.axes)}"
            )
        for a in self.axes:
            spec = axis_spec(a)  # raises on an unknown id
            if spec.usage == "prohibited":
                raise ValueError(
                    f"axis {a!r} is usage='prohibited' — it has no consumer, so "
                    "validating it would confirm something nothing reads. "
                    f"Use {spec.deprecated_by!r} instead."
                )


def _stored_sliders(person_id: str) -> dict[str, int]:
    """The sliders as STORED — not the projection.

    ``cognitive_profile_for_llm`` neutral-fills every unmeasured axis, so it
    cannot answer "did the profile actually predict this?". A blind check run
    against a manufactured neutral would be testing the default, not the
    person.
    """
    from okuro.db import get_db

    row = get_db().fetchone(
        "SELECT cognitive FROM persons WHERE id = ?", (person_id,)
    )
    if not row or not row["cognitive"]:
        raise LookupError(f"Person not found or has no profile: {person_id}")
    try:
        cog = json.loads(row["cognitive"])
    except (TypeError, json.JSONDecodeError):
        cog = {}
    return dict((cog or {}).get("sliders") or {})


def axis_variant_profiles(
    person_id: str, axis: str
) -> tuple[dict[str, int], dict[str, int]]:
    """``(predicted, opposite)`` slider vectors differing on ``axis`` alone.

    Holding every other axis identical is what makes a preference
    attributable. Refused in two cases, both of which would produce a test
    that cannot fail honestly:

    * the axis is UNMEASURED — there is no prediction to validate, and the
      neutral fill would be testing the default;
    * the stored value is NEUTRAL (3) — its opposite pole is also 3, so the
      two variants would be identical and the answer a coin toss.
    """
    axis_spec(axis)
    sliders = _stored_sliders(person_id)
    if axis not in sliders:
        raise ValueError(
            f"axis {axis!r} is not measured for {person_id} — there is no "
            "prediction to validate. Acquire it first (P3.1/P3.3)."
        )
    value = int(sliders[axis])
    if value == _NEUTRAL:
        raise ValueError(
            f"axis {axis!r} is stored at the neutral {_NEUTRAL}; its opposite "
            "pole is also 3, so the two variants carry no contrast and the "
            "answer would measure nothing."
        )
    predicted = dict(sliders)
    opposite = dict(sliders)
    opposite[axis] = 6 - value
    return predicted, opposite


def present_pair(
    predicted_text: str, opposite_text: str, seed: int | None = None
) -> dict[str, Any]:
    """Randomise which variant is shown first and return the key separately.

    ``options`` alone carries no hint of which is which — that is the point of
    "blind". ``predicted_key`` comes back so the CALLER can score the answer
    afterwards, which is a different moment from showing it. A fixed order
    would measure position rather than content.
    """
    rng = random.Random(seed)
    predicted_first = rng.random() < 0.5
    if predicted_first:
        options = {"A": predicted_text, "B": opposite_text}
        predicted_key = "A"
    else:
        options = {"A": opposite_text, "B": predicted_text}
        predicted_key = "B"
    return {"options": options, "predicted_key": predicted_key}


def build_pair(
    person_id: str,
    message: str,
    axis: str,
    protocol: BlindCheckProtocol,
    seed: int | None = None,
    provider: str | None = None,
) -> dict[str, Any]:
    """Rewrite ``message`` twice through translate's own prompt builder.

    Reuses ``translate._compose_prompt`` rather than composing a second
    prompt, so the only difference between the variants is the one axis. A
    bespoke prompt here would silently make this a test of the prompt.

    Never raises on a bridge failure — returns ``success=False`` so a failed
    generation is visibly a failed generation and not a null preference.
    """
    if axis not in protocol.axes:
        raise ValueError(
            f"axis {axis!r} is not in the pre-registered protocol "
            f"{list(protocol.axes)}"
        )
    from okuro.bridge.invoke import invoke
    from okuro.peer.cognitive_profile import cognitive_profile_for_llm
    from okuro.peer.translate import _compose_prompt
    from okuro.yu.profile import get_profile_raw

    predicted, opposite = axis_variant_profiles(person_id, axis)
    projection = cognitive_profile_for_llm(person_id)
    user_profile = get_profile_raw() or {}

    texts: list[str] = []
    for sliders in (predicted, opposite):
        variant = dict(projection)
        variant["sliders"] = sliders
        prompt = _compose_prompt(user_profile, variant, message, None)
        result = invoke(prompt, capability="translate", provider=provider)
        if not result.get("success"):
            return {
                "success": False,
                "axis": axis,
                "error": result.get("error") or "bridge returned no output",
            }
        texts.append((result.get("output") or "").strip())

    pair = present_pair(texts[0], texts[1], seed=seed)
    return {
        "success": True,
        "person_id": person_id,
        "axis": axis,
        "protocol_artifact": protocol.registered_artifact_id,
        **pair,
    }


def record_outcome(
    person_id: str,
    axis: str,
    *,
    agreed: bool,
    protocol: BlindCheckProtocol,
) -> dict[str, Any]:
    """Promote to T3 on agreement, floor confidence on disagreement.

    THE VALUE IS NEVER WRITTEN by either branch. Agreement means the stored
    value survived a test it could have failed, which is a statement about
    confidence, not about the number. Disagreement means the number is
    suspect, and a single A/B does not identify the replacement.

    Both branches write a ledger row, so "why is this axis at 1.0" and "why
    did this axis lose confidence" are answerable from person_sources rather
    than from memory.
    """
    if axis not in protocol.axes:
        raise ValueError(
            f"axis {axis!r} is not in the pre-registered protocol "
            f"{list(protocol.axes)} — scoring an unlisted axis after the fact "
            "is p-hacking, not validation."
        )
    from okuro.db import get_db
    from okuro.peer.sources import record_applied_value

    db = get_db()
    row = db.fetchone("SELECT cognitive FROM persons WHERE id = ?", (person_id,))
    if not row:
        raise LookupError(f"Person not found: {person_id}")
    try:
        cog = json.loads(row["cognitive"] or "{}") or {}
    except (TypeError, json.JSONDecodeError):
        cog = {}
    sliders = dict(cog.get("sliders") or {})
    if axis not in sliders:
        raise ValueError(f"axis {axis!r} is not measured for {person_id}")
    value = int(sliders[axis])

    prov = dict(cog.get("slider_provenance") or {})
    entry = dict(prov.get(axis) or {}) if isinstance(prov.get(axis), dict) else {}
    current = float(entry.get("confidence", 0.8))

    if agreed:
        confidence = 1.0
        tier = "T3"
    else:
        # Monotone downward: a second disagreement must not RAISE confidence
        # back to the floor from something already lower.
        confidence = min(current, DISAGREEMENT_CONFIDENCE)
        tier = "T0"

    entry.update({
        "value": value,
        "confidence": confidence,
        "source": "blind_check",
        "tier": tier,
        "protocol": protocol.registered_artifact_id,
    })
    prov[axis] = entry
    cog["slider_provenance"] = prov
    # sliders untouched — deliberately, in both branches.
    db.execute(
        "UPDATE persons SET cognitive = ?, updated_at = datetime('now') WHERE id = ?",
        (json.dumps(cog), person_id),
    )
    record_applied_value(
        db,
        person_id,
        "cognitive.sliders",
        value,
        # T3 — the person answered a question they could have answered the
        # other way. That is a different kind of evidence from "someone typed
        # it" (manual, T0), and the ledger says so.
        source_type="negotiated",
        source_ref=f"blind_check:{protocol.registered_artifact_id}",
        confidence=confidence,
        axis=axis,
    )
    db.conn.commit()

    return {
        "person_id": person_id,
        "axis": axis,
        "agreed": agreed,
        "tier": tier,
        "value": value,
        "confidence": confidence,
    }


__all__ = [
    "BlindCheckProtocol",
    "DISAGREEMENT_CONFIDENCE",
    "axis_variant_profiles",
    "build_pair",
    "present_pair",
    "record_outcome",
]
