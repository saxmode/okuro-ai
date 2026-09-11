# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Forward local reviews to the maintainer's Supabase drop box —
#   opt-in, local-first, with an explicit readiness check for older installs.
# index:
#   SENT_FIELDS | def sync_config | def readiness | def payload_for
#   def sync_pending | def _post_batch
# AGENT_HEADER_END -->
"""Review sync — getting a user's feedback to the maintainer.

An okuro install is somebody else's machine. Reviews captured there are useful
to the maintainer, and are also the user's business data, so three properties
are non-negotiable:

1. OPT-IN. Sync is off until the operator turns it on. Off means purely local
   — exactly how reviews behaved before this module existed.

2. WRITE-ONLY AT THE FAR END. The endpoint is a Supabase table whose RLS grants
   `anon` INSERT and defines NO select policy, so the shipped key can submit and
   can never read. Supabase's anon key is public by design (it ships in every
   client), so the design assumes it is known: the worst a leaked key buys is
   the ability to write junk into the maintainer's table, never to read anyone's
   reviews.

3. NOTHING LEAVES UNANNOUNCED. :data:`SENT_FIELDS` is the complete outbound
   shape and :func:`payload_for` is the only builder, so the settings UI can
   render the exact bytes that would be transmitted. Entity ids
   (``target_id``) are deliberately NOT sent — they are the highest-leak-risk
   field and a complaint is actionable without them.

LOCAL-FIRST, ALWAYS. The review is committed to the local table before any of
this runs. Sync is a separate drain over `synced_at IS NULL`, so an install
that is offline, misconfigured, or updated into this feature without an
org_label loses nothing — it queues and drains later.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from okuro.db.engine import okuro_home

logger = logging.getLogger("okuro.sense.review_sync")

CONFIG_PATH = okuro_home() / "config.yaml"

# Shipped defaults, so a fresh install only has to set an org label and say yes.
#
# The key is a Supabase PUBLISHABLE key and is meant to be distributed — it is
# the same class of secret as the one in any browser app. Its safety does not
# come from being hidden; it comes from the far end's RLS, which grants this
# role INSERT and no SELECT. Verified against the live project: INSERT 201,
# SELECT 401 permission denied. A service_role key must NEVER appear here — it
# carries bypassrls and would hand every install full read/write.
DEFAULT_ENDPOINT = (
    "https://rysvmrwyqxfoefnpcuwu.supabase.co/rest/v1/okuro_reviews"
)
DEFAULT_ANON_KEY = "sb_publishable_G3lYlLGyXwaiJKpevzuTUw_fs-XCUwG"

# THE COMPLETE OUTBOUND SHAPE. Anything not listed here never leaves the
# machine. Keep this list and payload_for in step — the settings preview
# renders from them, and a field that bypasses this is a field the user never
# consented to.
SENT_FIELDS = (
    "id",            # local review id — dedups re-sends at the far end
    "install_id",
    "org_label",
    "surface_id",
    "severity",
    "comment",       # the signal, and the sensitive part; shown in the preview
    "rating",
    "route",
    "viewport",
    "app_version",
    "client_created_at",
)

# A 4xx is the endpoint saying "this will never work" — schema mismatch, bad
# key, rejected by a policy. Retrying burns attempts and hides the real error,
# so those are recorded and not retried. 5xx / network faults are transient and
# stay queued.
_MAX_ATTEMPTS = 5


def _under_test() -> bool:
    """True while pytest is running.

    A HARD interlock, not a convenience. `review_add` forwards on every write,
    and the tests use an isolated DB but the REAL ~/.okuro/config.yaml — so on a
    configured machine the suite transmitted its own fixtures to the
    maintainer's production table. Observed: 24 rows (`page.alpha`,
    `busy.surface`, `x.y` …) delivered by one `pytest tests/sense` run.

    Fixed here rather than in a fixture so it holds for EVERY caller: any test,
    in any file, on any developer's configured machine. A test that wants to
    exercise the transport stubs `_post_batch` and never reaches this.
    """
    import os
    return bool(os.environ.get("PYTEST_CURRENT_TEST"))


def sync_config() -> dict:
    """Read the ``feedback`` block. Absent keys degrade to safe defaults."""
    if _under_test():
        # Disabled, so sync_pending() short-circuits before any network call.
        return {"enabled": False, "org_label": "", "install_id": "",
                "endpoint": "", "anon_key": ""}
    try:
        import yaml
        raw = yaml.safe_load(CONFIG_PATH.read_text()) or {}
    except Exception:
        raw = {}
    fb = (raw.get("feedback") or {}) if isinstance(raw, dict) else {}
    return {
        "enabled": bool(fb.get("sync_enabled", False)),
        "org_label": (fb.get("org_label") or "").strip(),
        "install_id": (fb.get("install_id") or "").strip(),
        "endpoint": (fb.get("endpoint") or DEFAULT_ENDPOINT).strip(),
        "anon_key": (fb.get("anon_key") or DEFAULT_ANON_KEY).strip(),
    }


def ensure_install_id() -> str:
    """A stable per-install id, minted once and persisted.

    Deliberately random and meaningless: it groups one install's reviews
    without identifying a person. The org label is what makes it human, and the
    operator sets that themselves.
    """
    cfg = sync_config()
    if cfg["install_id"]:
        return cfg["install_id"]

    new_id = str(uuid.uuid4())
    try:
        import yaml
        raw = yaml.safe_load(CONFIG_PATH.read_text()) or {}
        if not isinstance(raw, dict):
            raw = {}
        raw.setdefault("feedback", {})["install_id"] = new_id
        CONFIG_PATH.write_text(yaml.dump(raw, default_flow_style=False))
    except Exception as exc:  # noqa: BLE001
        logger.warning("could not persist install_id (%r)", exc)
    return new_id


def set_config(*, sync_enabled: Optional[bool] = None,
               org_label: Optional[str] = None) -> dict:
    """Persist the two fields a user actually owns. Returns fresh readiness.

    Endpoint and key are shipped defaults and deliberately not edited here —
    they are the maintainer's, not the operator's.
    """
    import yaml
    try:
        raw = yaml.safe_load(CONFIG_PATH.read_text()) or {}
    except Exception:
        raw = {}
    if not isinstance(raw, dict):
        raw = {}
    fb = raw.setdefault("feedback", {})
    if sync_enabled is not None:
        fb["sync_enabled"] = bool(sync_enabled)
    if org_label is not None:
        fb["org_label"] = org_label.strip()
    fb.setdefault("install_id", str(uuid.uuid4()))
    CONFIG_PATH.write_text(yaml.dump(raw, default_flow_style=False))

    # Turning it on (or naming the org) is exactly when the backlog should go.
    try:
        retry_failed()
        sync_pending()
    except Exception as exc:  # noqa: BLE001
        logger.warning("post-config drain failed (%r)", exc)
    return readiness()


def readiness() -> dict:
    """Can this install sync, and if not, exactly what is missing?

    This is what closes the upgrade gap. An install that predates the feature
    has no ``org_label``, and its user will try to give feedback long before
    they read a changelog. Rather than failing at submit time, the UI asks this
    and points at the specific settings field — while the review itself is
    already saved locally regardless.
    """
    cfg = sync_config()
    missing: list[str] = []
    if not cfg["org_label"]:
        missing.append("org_label")
    if not cfg["endpoint"]:
        missing.append("endpoint")
    if not cfg["anon_key"]:
        missing.append("anon_key")

    return {
        "enabled": cfg["enabled"],
        "ready": cfg["enabled"] and not missing,
        "missing": missing,
        "org_label": cfg["org_label"],
        "install_id": cfg["install_id"],
        # Reviews sit here until the gap is closed; nothing is discarded.
        "pending": pending_count(),
        # Gave up after repeated rejection. Separate, because a number that
        # never moves is worse than no number — and these are recoverable via
        # retry_failed() once the cause is fixed.
        "failed": failed_count(),
    }


def pending_count() -> int:
    """Reviews still eligible to send.

    Deliberately EXCLUDES rows whose attempt budget is spent. Counting those as
    "pending" told the user "1 queued" forever about something that would never
    move — the number has to mean "will send", or it is worse than no number.
    Those are reported separately by :func:`failed_count`.
    """
    from okuro.db import get_db
    row = get_db().fetchone(
        "SELECT COUNT(*) AS n FROM reviews "
        "WHERE synced_at IS NULL AND sync_attempts < ?",
        (_MAX_ATTEMPTS,),
    )
    return int(dict(row).get("n", 0)) if row else 0


def failed_count() -> int:
    """Reviews that gave up. Still on disk and still readable — never dropped."""
    from okuro.db import get_db
    row = get_db().fetchone(
        "SELECT COUNT(*) AS n FROM reviews "
        "WHERE synced_at IS NULL AND sync_attempts >= ?",
        (_MAX_ATTEMPTS,),
    )
    return int(dict(row).get("n", 0)) if row else 0


def retry_failed() -> int:
    """Clear the attempt budget so exhausted rows are eligible again.

    The operator's escape hatch: an endpoint or config fault that has since
    been fixed should not require the user to re-type anything. Returns how
    many rows were released.
    """
    from okuro.db import get_db
    cur = get_db().execute(
        "UPDATE reviews SET sync_attempts = 0, sync_error = NULL "
        "WHERE synced_at IS NULL AND sync_attempts >= ?",
        (_MAX_ATTEMPTS,),
    )
    return getattr(cur, "rowcount", 0) or 0


def payload_for(review: dict, cfg: Optional[dict] = None) -> dict:
    """The exact object that would be transmitted for one review.

    The single builder, so the settings preview cannot drift from the wire.
    """
    cfg = cfg or sync_config()
    return {
        "id": review.get("id"),
        "install_id": cfg.get("install_id") or "",
        "org_label": cfg.get("org_label") or "",
        "surface_id": review.get("surface_id"),
        "severity": review.get("severity"),
        "comment": review.get("comment"),
        "rating": review.get("rating"),
        "route": review.get("route"),
        "viewport": review.get("viewport"),
        "app_version": review.get("app_version"),
        "client_created_at": review.get("created_at"),
        # target_id / dom_hint / route_params are intentionally absent — see
        # the module docstring. Adding one here is a consent decision.
    }


def _post_batch(rows: list[dict], cfg: dict, *, timeout: int = 15) -> tuple[bool, str]:
    """POST a batch to the Supabase REST endpoint. Returns (ok, error)."""
    url = cfg["endpoint"].rstrip("/")
    body = json.dumps([payload_for(r, cfg) for r in rows]).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "apikey": cfg["anon_key"],
            "Authorization": f"Bearer {cfg['anon_key']}",
            # Name the schema explicitly. PostgREST resolves an unqualified
            # table against the project's FIRST exposed schema, which on this
            # project is `graphql_public` — so without this the POST 404s with
            # `relation "graphql_public.okuro_reviews" does not exist` even
            # though the table is right there in `public`. Verified live.
            "Content-Profile": "public",
            "Accept-Profile": "public",
            # Re-sending an already-accepted id must not 409 the whole batch;
            # the far end dedups on the primary key.
            # Plain INSERT, NOT an upsert. `resolution=merge-duplicates` turns
            # this into an UPSERT, which needs UPDATE privilege — and the far
            # end deliberately grants the publishable role INSERT only, so an
            # upsert is rejected with "permission denied for table". Verified
            # live against the real endpoint: identical payload, 401 with
            # merge-duplicates, 201 without. Re-sends are handled by treating a
            # duplicate-key conflict as already-delivered, below.
            "Prefer": "return=minimal",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if 200 <= resp.status < 300:
                return True, ""
            return False, f"HTTP {resp.status}"
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")[:300]
        except Exception:
            pass
        # 409 on the primary key means this batch ALREADY landed — a retry
        # after a response we never saw (timeout, dropped connection). The far
        # end has the data, so this is a success, not a failure to retry
        # forever. 23505 is Postgres' unique_violation.
        if exc.code == 409 or "23505" in detail:
            return True, ""
        return False, f"HTTP {exc.code}: {detail}"
    except Exception as exc:  # noqa: BLE001 — network, DNS, TLS, timeouts
        return False, repr(exc)


def sync_pending(limit: int = 50) -> dict:
    """Drain the queue. Safe to call on a timer, on boot, or by hand.

    Never raises: a sync failure must leave the local review untouched and
    still queued.
    """
    cfg = sync_config()
    if not cfg["enabled"]:
        return {"sent": 0, "skipped": "disabled"}
    if not (cfg["org_label"] and cfg["endpoint"] and cfg["anon_key"]):
        # The upgrade gap. Queue, do not discard, and let readiness() explain.
        return {"sent": 0, "skipped": "not_configured",
                "missing": readiness()["missing"]}

    from okuro.db import get_db
    db = get_db()
    rows = db.fetchall(
        "SELECT * FROM reviews WHERE synced_at IS NULL AND sync_attempts < ? "
        "ORDER BY created_at LIMIT ?",
        (_MAX_ATTEMPTS, int(limit)),
    )
    rows = [dict(r) for r in rows or []]
    if not rows:
        return {"sent": 0, "skipped": "nothing_pending"}

    ok, err = _post_batch(rows, cfg)
    now = datetime.now(timezone.utc).isoformat()

    if ok:
        for r in rows:
            db.execute(
                "UPDATE reviews SET synced_at = ?, sync_error = NULL "
                "WHERE id = ?",
                (now, r["id"]),
            )
        return {"sent": len(rows), "skipped": None}

    # A 4xx will not fix itself; burn the budget so it surfaces instead of
    # retrying forever. Transient faults keep their attempts for the next run.
    permanent = err.startswith("HTTP 4")
    for r in rows:
        db.execute(
            "UPDATE reviews SET sync_attempts = ?, sync_error = ? WHERE id = ?",
            (_MAX_ATTEMPTS if permanent else int(r.get("sync_attempts", 0)) + 1,
             err[:500], r["id"]),
        )
    logger.warning("review sync failed (%s) — %d row(s) still queued",
                   err, len(rows))
    return {"sent": 0, "error": err, "queued": len(rows)}
