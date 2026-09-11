# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Public questionnaire consume endpoints — /q/{token} bypasses bearer auth so recipients can answer.
# index:
#   imports
#   router
#   def _fetch_questionnaire
#   def _apply_responses
#   def get_questionnaire
#   def submit_questionnaire
# AGENT_HEADER_END -->
"""Public consume endpoints for person questionnaires.

Minting is at ``POST /api/people/{id}/questionnaires`` (bearer-required,
localhost-only). The recipient-facing pair lives here:

- GET  /api/q/{token}     → returns {person_display_name, questions, status}
- POST /api/q/{token}     → accepts {responses}, transforms them into a
                            profile delta, applies via apply_profile_delta.

The paths live under ``/api/q/`` so the SPA route ``/q/:token`` can
serve the recipient-facing form HTML without conflicting with the JSON
endpoints. The bearer middleware is configured to let the
``/api/q/`` prefix through without a token — see ``_AUTH_EXEMPT_PREFIXES``
in orchestrator/api/main.py.

Security:
- Tokens are 32-byte URL-safe random strings (secrets.token_urlsafe(32)),
  so guessing is infeasible.
- Submissions are single-use — status flips to 'answered' → 'applied',
  and answered questionnaires return a shape that tells the UI to show
  "thanks, you can close this".
- Expired tokens return 410 Gone.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

logger = logging.getLogger("okuro.orchestrator.api.questionnaires")

router = APIRouter(tags=["questionnaires"])


# ── Helpers ──────────────────────────────────────────────────────────


def _fetch_questionnaire(token: str) -> dict[str, Any]:
    from okuro.db import get_db

    db = get_db()
    row = db.fetchone(
        "SELECT q.*, p.display_name AS person_display_name, p.role AS person_role "
        "FROM person_questionnaires q "
        "JOIN persons p ON p.id = q.person_id "
        "WHERE q.token = ?",
        (token,),
    )
    if not row:
        raise HTTPException(404, "Invalid link")
    q = dict(row)
    try:
        q["questions"] = json.loads(q.get("questions") or "[]")
    except (TypeError, json.JSONDecodeError):
        q["questions"] = []
    try:
        q["responses"] = (
            json.loads(q["responses"]) if q.get("responses") else None
        )
    except (TypeError, json.JSONDecodeError):
        q["responses"] = None

    if q.get("status") == "expired":
        raise HTTPException(410, "This link has expired")
    if q.get("expires_at"):
        try:
            exp = datetime.strptime(str(q["expires_at"]), "%Y-%m-%d %H:%M:%S")
            if exp < datetime.utcnow():
                # Lazy expiration — flip the row and return 410.
                db.execute(
                    "UPDATE person_questionnaires SET status = 'expired' WHERE token = ?",
                    (token,),
                )
                db.conn.commit()
                raise HTTPException(410, "This link has expired")
        except (ValueError, TypeError):
            pass
    return q


def _apply_responses(
    person_id: str, token: str, questions: list[dict], responses: dict[str, str]
) -> dict[str, Any]:
    """Transform {question_id: answer} into a profile delta and apply it.

    Uses the same bridge-based extractor as the file-drop path, but framed
    as "interview the respondent" so the LLM knows these are first-person
    self-report answers (confidence=0.95 on apply).
    """
    from okuro.peer.extract import extract_profile_delta_from_text
    from okuro.peer.sources import apply_profile_delta

    lookup = {q.get("id"): q for q in questions if isinstance(q, dict)}
    lines: list[str] = [
        "First-person self-report answers from the recipient (authoritative):"
    ]
    for qid, answer in responses.items():
        prompt = lookup.get(qid, {}).get("prompt") or qid
        lines.append(f"Q: {prompt}\nA: {answer}")
    corpus = "\n\n".join(lines)

    from okuro.db import get_db

    db = get_db()
    row = db.fetchone(
        "SELECT id, display_name, role, communication, cognitive FROM persons WHERE id = ?",
        (person_id,),
    )
    person = dict(row) if row else {"id": person_id}
    for col in ("communication", "cognitive"):
        raw = person.get(col)
        if isinstance(raw, str):
            try:
                person[col] = json.loads(raw)
            except (TypeError, json.JSONDecodeError):
                person[col] = {}

    delta = extract_profile_delta_from_text(corpus, person)
    applied: dict[str, Any] = {
        "sources_written": 0,
        "fields_changed": [],
        "apply": False,
    }
    if delta:
        evidence = delta.pop("_evidence", None)
        applied = apply_profile_delta(
            person_id,
            source_type="questionnaire",
            source_ref=token,
            delta=delta,
            confidence=0.95,
        )
        if evidence:
            applied["evidence"] = evidence
    return applied


def _stamp_started(token: str, q: dict[str, Any]) -> None:
    """Record the first time the recipient actually opened the form.

    Write-once. ``answered_at - started_at`` is the completion time P3.1's
    acceptance is stated in, and both of these would corrupt it:

    * a re-open mid-fill overwriting the stamp — the duration would then
      measure the last visit, not the whole engagement;
    * a stamp on an ALREADY answered form (the recipient revisits the
      thank-you screen) — the duration would come out negative.

    ``sent_at`` cannot stand in: it is when the link was minted, which for an
    emailed link can be days earlier. See migration 125.

    Never raises — a failed stamp must not cost the recipient their form.
    """
    if q.get("started_at") or q.get("status") in ("answered", "applied"):
        return
    try:
        from okuro.db import get_db

        db = get_db()
        db.execute(
            "UPDATE person_questionnaires SET started_at = datetime('now') "
            "WHERE token = ? AND started_at IS NULL",
            (token,),
        )
        db.conn.commit()
    except Exception:  # noqa: BLE001 — instrumentation never blocks the form
        logger.warning("could not stamp started_at for %s", token, exc_info=True)


# ── Structured (deterministic) survey path ───────────────────────────


def _sanitize_survey(raw: Any) -> dict[str, Any]:
    """Bound + type-check a public structured survey payload.

    ``score_survey`` already validates content deeply (ignores unknown pair
    ids, clamps depths, validates the archetype, bounds REI). This adds the
    size/shape hygiene a public endpoint needs so a hostile payload can't
    blow up the scorer (huge topic lists, non-dict buckets).
    """
    if not isinstance(raw, dict):
        return {}
    out: dict[str, Any] = {}

    ident = raw.get("identity")
    if isinstance(ident, dict):
        out["identity"] = {
            key: str(ident[key])[:120]
            for key in ("display_name", "profession", "function", "seniority")
            if isinstance(ident.get(key), str)
        }

    k = raw.get("knowledge")
    if isinstance(k, dict):
        areas = k.get("areas")
        weaknesses = k.get("weaknesses")
        kn: dict[str, Any] = {
            "areas": areas[:50] if isinstance(areas, list) else [],
            "weaknesses": weaknesses[:50] if isinstance(weaknesses, list) else [],
        }
        if isinstance(k.get("confidence"), (int, float)):
            kn["confidence"] = k["confidence"]
        out["knowledge"] = kn

    for bucket in ("cognitive", "angle"):
        b = raw.get(bucket)
        if isinstance(b, dict):
            out[bucket] = {
                str(key)[:40]: val
                for key, val in list(b.items())[:20]
                if isinstance(val, (int, float))
            }
    return out


def _apply_survey(person_id: str, token: str, survey: dict[str, Any]) -> dict[str, Any]:
    """Deterministic path: structured answers → profile delta, NO LLM.

    Reads the person's current cognitive shape (so unmeasured axes and prior
    topic_interests survive the merge), scores the survey, and applies the
    delta as a questionnaire source. Per-axis confidence (0.95 validated /
    0.70 preference / 0.60 over-claim) lives inside ``slider_provenance``;
    the ledger row stamps the overall 0.95 self-report confidence.
    """
    from okuro.db import get_db
    from okuro.peer.sources import apply_profile_delta
    from okuro.peer.survey import score_survey, survey_to_delta

    db = get_db()
    row = db.fetchone("SELECT cognitive FROM persons WHERE id = ?", (person_id,))
    cognitive: dict[str, Any] = {}
    if row and row["cognitive"]:
        try:
            loaded = json.loads(row["cognitive"])
            cognitive = loaded if isinstance(loaded, dict) else {}
        except (TypeError, json.JSONDecodeError):
            cognitive = {}

    scored = score_survey(survey)
    delta = survey_to_delta(survey, existing_cognitive=cognitive)
    applied = apply_profile_delta(
        person_id,
        source_type="questionnaire",
        source_ref=token,
        delta=delta,
        confidence=0.95,
    )
    applied["over_claim_flags"] = scored["over_claim_flags"]
    applied["measured_axes"] = sorted(scored["sliders"].keys())
    return applied


# ── Request model ────────────────────────────────────────────────────


class SubmitRequest(BaseModel):
    # Free-text path (legacy): {question_id: answer}. Parsed by the LLM bridge.
    responses: dict[str, str] = Field(default_factory=dict)
    # Structured path (preferred): forced-choice pairs + knowledge swipe + REI.
    # When present, scored deterministically — no LLM. See peer/survey.py.
    survey: dict[str, Any] | None = None


# ── Endpoints ────────────────────────────────────────────────────────


@router.get("/api/q/{token}")
def get_questionnaire(token: str) -> dict:
    """Public read — returns the question set and a status hint for the form.

    ``survey_form`` carries the structured-survey definition (knowledge
    swipe + forced-choice pairs + REI sliders) the SPA renders. It is the
    single source both the front-end and the scorer share, so the form can
    never drift from how answers are scored.
    """
    from okuro.peer.survey import survey_form

    q = _fetch_questionnaire(token)
    _stamp_started(token, q)
    questions = q.get("questions") or []
    return {
        "person_display_name": q.get("person_display_name"),
        "person_role": q.get("person_role"),
        "questions": questions,
        # The language the SENDER chose at mint time. NULL (a row minted
        # before migration 126) reads as English, which is what those
        # recipients actually saw.
        "survey_form": survey_form(q.get("lang")),
        # Discriminator: structured questionnaires are minted with empty
        # ``questions`` and render the survey_form; legacy free-text ones
        # carry their question list.
        "structured": not questions,
        "status": q.get("status") or "sent",
        "already_answered": q.get("status") in ("answered", "applied"),
        "expires_at": q.get("expires_at"),
    }


@router.post("/api/q/{token}")
async def submit_questionnaire(token: str, payload: SubmitRequest) -> dict:
    """Public write — persists responses, applies profile delta.

    Returns a small summary the form can render. No sensitive data is
    returned (no person_id, no translation_log) — the respondent only
    needs confirmation.
    """
    from okuro.db import get_db

    q = _fetch_questionnaire(token)
    if q.get("status") in ("answered", "applied"):
        raise HTTPException(409, "Already answered")

    person_id = q.get("person_id")
    db = get_db()

    # Structured (deterministic) path — no LLM. Preferred for the new survey.
    if payload.survey is not None:
        survey = _sanitize_survey(payload.survey)
        if not survey:
            raise HTTPException(400, "empty survey payload")
        db.execute(
            """UPDATE person_questionnaires
               SET responses = ?, status = 'answered', answered_at = datetime('now')
               WHERE token = ?""",
            (json.dumps({"survey": survey}), token),
        )
        db.conn.commit()
        applied = _apply_survey(person_id, token, survey)
        if applied.get("apply"):
            db.execute(
                """UPDATE person_questionnaires
                   SET status = 'applied', applied_at = datetime('now')
                   WHERE token = ?""",
                (token,),
            )
            db.conn.commit()
        return {
            "ok": True,
            "fields_updated": applied.get("fields_changed") or [],
            "measured_axes": applied.get("measured_axes") or [],
            "over_claim_flags": applied.get("over_claim_flags", 0),
        }

    # Free-text path (legacy) — answers parsed by the LLM bridge.
    clean: dict[str, str] = {}
    for k, v in (payload.responses or {}).items():
        if not isinstance(v, str):
            continue
        trimmed = v.strip()
        if not trimmed:
            continue
        clean[k] = trimmed[:2000]

    if not clean:
        raise HTTPException(400, "no non-empty answers")

    db.execute(
        """UPDATE person_questionnaires
           SET responses = ?, status = 'answered', answered_at = datetime('now')
           WHERE token = ?""",
        (json.dumps(clean), token),
    )
    db.conn.commit()

    applied = _apply_responses(
        person_id, token, q.get("questions") or [], clean
    )

    if applied.get("apply"):
        db.execute(
            """UPDATE person_questionnaires
               SET status = 'applied', applied_at = datetime('now')
               WHERE token = ?""",
            (token,),
        )
        db.conn.commit()

    return {
        "ok": True,
        "fields_updated": applied.get("fields_changed") or [],
        "evidence": applied.get("evidence"),
    }
