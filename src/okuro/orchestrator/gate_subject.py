# SPDX-License-Identifier: Apache-2.0
# purpose: The subject of a decision gate — the artifact a decision is made
# AGAINST — and the hash that says whether that basis is still the one the
# user read. One place computes it, one place re-computes it, so "approve the
# diff" and "refuse a decision whose basis moved" are the same arithmetic.
#
# index: imports | SubjectError + subclasses | def content_sha256 |
#   def _fetch_parts | def gate_subject_for | dataclass SubjectCheck |
#   def verify_gate_subject | def subject_title
#
# Why this exists: a DecisionGate recorded the choice but not the world the
# choice was made against. A user approves a diff artifact, a later agent
# supersedes that artifact, and the ADR still reads as an approval of text
# nobody approved. Hashing the content at gate time turns that silent drift
# into a refusal at resolve time. Kept out of state.py because it is the one
# place the orchestrator reaches into okuro.sense.artifacts, and state.py
# should not grow that dependency for every caller that merely loads a plan.

from __future__ import annotations

import hashlib
from dataclasses import dataclass


def _lead(gate_id: str) -> str:
    return (
        f"Gate {gate_id} cannot be resolved: its"
        if gate_id else "The decision's"
    )


class SubjectError(ValueError):
    """The basis a gate was posed against is not the one on record.

    THE MESSAGE IS BUILT HERE, not at the call site. It is read in a 409
    toast and in a terminal — two places where the caller has no room to
    compose a sentence — so every caller can do the same one thing:
    ``except SubjectError as exc: raise ValueError(str(exc))``.

    A ValueError subclass so the existing ValueError → 409 mapping in the
    resolve endpoint keeps working untouched.
    """

    def __init__(self, message: str, *, artifact_id: str = "", gate_id: str = ""):
        super().__init__(message)
        self.artifact_id = artifact_id
        self.gate_id = gate_id


class SubjectMissing(SubjectError):
    """The artifact a gate names as its subject is not in the store."""

    def __init__(self, artifact_id: str, *, gate_id: str = ""):
        super().__init__(
            f"{_lead(gate_id)} subject artifact {artifact_id} no longer "
            f"exists, so the basis for this decision moved. Pose the gate "
            f"again against a current artifact, or skip it.",
            artifact_id=artifact_id, gate_id=gate_id,
        )


class SubjectSuperseded(SubjectError):
    """The subject artifact has been replaced by a newer one."""

    def __init__(self, artifact_id: str, successor_id: str, *, gate_id: str = ""):
        self.successor_id = successor_id
        super().__init__(
            f"{_lead(gate_id)} subject artifact {artifact_id} was superseded "
            f"by {successor_id}, so the basis for this decision moved. Read "
            f"the newer version and pose the gate again, or skip it.",
            artifact_id=artifact_id, gate_id=gate_id,
        )


class SubjectChanged(SubjectError):
    """The subject artifact's content no longer hashes to what was recorded."""

    def __init__(self, artifact_id: str, recorded: str, current: str,
                 *, gate_id: str = ""):
        self.recorded = recorded
        self.current = current
        super().__init__(
            f"{_lead(gate_id)} subject artifact {artifact_id} changed since "
            f"the gate was posed, so the basis for this decision moved "
            f"(recorded {recorded[:12]}, now {current[:12]}). Read the "
            f"current version and pose the gate again, or skip it.",
            artifact_id=artifact_id, gate_id=gate_id,
        )


class SubjectUnverifiable(SubjectError):
    """The store could not be read, so the basis could not be checked.

    FAIL CLOSED. A store that will not answer is not evidence that
    nothing moved, and the difference between "unchanged" and "unknown"
    is the whole value of the check. The gate stays pending and the user
    can try again; the alternative — a raw sqlite error — is a 500 at the
    API and a dead engine mid-prompt at the TTY.
    """

    def __init__(self, artifact_id: str, reason: str, *, gate_id: str = ""):
        self.reason = reason
        super().__init__(
            f"{_lead(gate_id)} basis could not be verified: {reason}. The "
            f"gate stays pending — try again once the store is reachable.",
            artifact_id=artifact_id, gate_id=gate_id,
        )


def content_sha256(body: str | None, blob: bytes | None = None) -> str:
    """Hash an artifact's whole content — text body AND binary blob.

    Hashes the two sub-digests rather than the two payloads, so the input
    to the outer hash is always 64 fixed-length bytes and no pair of
    (body, blob) values can be rearranged into another pair's input. A
    plain concatenation could: body "ab" with no blob would hash
    identically to body "a" with blob b"b".

    ``None`` and empty hash identically and deliberately: a gate posed
    against an empty artifact has nothing to drift, and distinguishing
    the two would make a NULL column read as tampering.
    """
    outer = hashlib.sha256()
    outer.update(hashlib.sha256((body or "").encode("utf-8")).digest())
    outer.update(hashlib.sha256(blob or b"").digest())
    return outer.hexdigest()


# Kept as the documented name for "hash this artifact's content".
body_sha256 = content_sha256


def _fetch_parts(artifact_id: str, gate_id: str = "") -> tuple[str | None, bytes | None]:
    """Read the subject's content, or say why it could not be read.

    Every failure to READ becomes :class:`SubjectUnverifiable`, never a
    pass. The bare ``except`` is the point: a locked store, a missing
    table and a corrupt page are all "we do not know", and none of them
    may be allowed to look like "unchanged".
    """
    from okuro.sense.artifacts import artifact_body_parts

    try:
        parts = artifact_body_parts(artifact_id)
    except Exception as exc:  # noqa: BLE001 — see docstring; fails closed
        raise SubjectUnverifiable(
            artifact_id, f"{type(exc).__name__}: {exc}", gate_id=gate_id,
        ) from exc
    if parts is None:
        raise SubjectMissing(artifact_id, gate_id=gate_id)
    return parts


def successor_of(artifact_id: str, gate_id: str = "") -> str:
    """The id of the newest artifact that replaced this one, or ``""``."""
    from okuro.sense.artifacts import artifact_successor

    try:
        return artifact_successor(artifact_id)
    except Exception as exc:  # noqa: BLE001 — fails closed, as above
        raise SubjectUnverifiable(
            artifact_id, f"{type(exc).__name__}: {exc}", gate_id=gate_id,
        ) from exc


def gate_subject_for(artifact_id: str) -> tuple[str, str]:
    """Return ``(artifact_id, sha256)`` for the artifact a gate is posed
    against — the pair to write into ``DecisionGate.subject_artifact_id``
    and ``DecisionGate.subject_sha256``.

    Raises :class:`SubjectMissing` when the artifact does not exist, so a
    gate can never be posed against a basis that is already gone.
    """
    aid = (artifact_id or "").strip()
    if not aid:
        raise SubjectMissing("")
    body, blob = _fetch_parts(aid)
    return aid, content_sha256(body, blob)


@dataclass(frozen=True)
class SubjectCheck:
    """The outcome of verifying a gate's basis.

    ``recorded_now`` is True when the gate named a subject but carried no
    hash, and this call pinned one. See :func:`verify_gate_subject`.
    """
    sha256: str
    recorded_now: bool


def verify_gate_subject(
    artifact_id: str,
    recorded_sha256: str,
    *,
    gate_id: str = "",
) -> SubjectCheck:
    """Confirm the subject is still the basis the gate was posed against.

    THE HASH IS NOT THE WHOLE QUESTION. The class of failure this guards
    is "the basis is no longer the version the user read", and it has
    four instances, only one of which a hash can see:

      - the content was edited in place → the hash moves
      - the artifact was deleted        → there is nothing to hash
      - the artifact was SUPERSEDED     → the old row is untouched and its
        hash still matches perfectly, because supersede writes a NEW row
        and only drops the old one's confidence
      - the store would not answer      → unknown, which is not unchanged

    The third is how okuro actually versions a document, so checking the
    hash alone would pass a gate whose diff has since been rewritten —
    exactly the approval this feature exists to refuse. All four raise a
    :class:`SubjectError`.

    AN EMPTY ``recorded_sha256`` MEANS "NOT PINNED YET", NOT "MISMATCH".
    A gate can name its subject before anything has been hashed; refusing
    that forever against a blank recorded value would make the state
    unreachable rather than safe. The current hash is returned with
    ``recorded_now=True`` and the caller pins it — the user is approving
    what is in front of them right now, which is precisely what the first
    resolve attempt means.

    Makes no judgement about skips: a skip is not an approval and never
    reaches here.
    """
    body, blob = _fetch_parts(artifact_id, gate_id)

    successor = successor_of(artifact_id, gate_id)
    if successor:
        raise SubjectSuperseded(artifact_id, successor, gate_id=gate_id)

    current = content_sha256(body, blob)
    recorded = (recorded_sha256 or "").strip()
    if not recorded:
        return SubjectCheck(sha256=current, recorded_now=True)
    if current != recorded:
        raise SubjectChanged(artifact_id, recorded, current, gate_id=gate_id)
    return SubjectCheck(sha256=current, recorded_now=False)


def subject_title(artifact_id: str) -> str:
    """The subject artifact's title, or ``""`` when it cannot be read.

    Display-only — a missing title must never be the thing that stops a
    gate from rendering, so every failure here is swallowed.
    """
    try:
        from okuro.sense.artifacts import artifact_get

        row = artifact_get(artifact_id, include_body=False)
    except Exception:  # noqa: BLE001 — display path, never load-bearing
        return ""
    return str((row or {}).get("title") or "")


def is_basis_refusal(exc: BaseException | None) -> bool:
    """True when this error is a gate refusing a moved basis.

    ``resolve_decision_gate`` converts a :class:`SubjectError` into a
    plain ValueError to keep its contract, but chains the original as
    ``__cause__``. Callers that must react differently — the autopilot
    parks instead of shrugging — ask here rather than matching on
    message text or re-querying the store and racing themselves.
    """
    while exc is not None:
        if isinstance(exc, SubjectError):
            return True
        exc = exc.__cause__
    return False


def basis_moved(artifact_id: str, recorded_sha256: str) -> bool:
    """Would resolving this gate be refused right now?

    For SURFACES that want to warn before the user picks an option. Never
    a substitute for the check in ``resolve_decision_gate``: this answer
    is stale the moment it is rendered, and the only verdict that decides
    anything is the one taken under the resolver's file lock.
    """
    if not artifact_id:
        return False
    try:
        verify_gate_subject(artifact_id, recorded_sha256)
    except SubjectError:
        return True
    return False
