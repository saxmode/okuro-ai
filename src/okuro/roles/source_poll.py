# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Fetch the registered structure sources, store their bodies
#   content-addressed, and answer "does this quoted sentence really appear
#   in what that run fetched" without asking a model.
# index:
#   imports
#   ALARM_* constants
#   class HttpResponse
#   def urllib_fetch
#   def normalise
#   def sha256_of
#   def _held_body
#   def new_run_id
#   def _request
#   def _poll_host
#   def _apply
#   def fetch_source
#   def poll_all
#   def verify_quote
# AGENT_HEADER_END -->
"""The evidence half of structural role maintenance. No LLM lives here.

**Why this module has no model in it.** okuro's honesty controls have always
been prompt text — an acceptance criterion saying "fetch every source in-run
and quote the sentence", judged by a critic reading the agent's own report.
role-refresh produced three recorded fabrication incidents that each satisfied
their own acceptance criteria, because the report is the artifact the
fabricating agent writes. Asking a second model to check the first is the same
mistake one layer up.

So the check here is a substring assert against a stored body. It is boring,
it is cheap, and it cannot be talked out of its answer. ``verify_quote``
returns a bool and there is no branch in it that consults anything.

**The three moving parts.**

``fetch_source``   one source, one HTTP round trip, one row in the blob store
                   if the body is new and one row in the observation log
                   either way.
``poll_all``       every enabled source, plus the alarm verdicts.
``verify_quote``   the assert a finding has to pass to count as a finding.

**Conditional requests, and why all three check methods work with the two
validator columns the registry has.** The registry stores ``last_etag`` and
``last_hash``. There is no ``last_modified`` column, and one is not needed:

``etag``          ``If-None-Match: <last_etag>`` — a 304 means unchanged and
                  costs no body.
``last_modified`` ``If-Modified-Since: <last_checked_at>`` — the standard
                  conditional GET phrased as "has it changed since I last
                  looked", which is the question, and ``last_checked_at`` is
                  the answer to when that was.
``content_hash``  unconditional GET, compare sha256. The only method that
                  works against a vendor sending no validator at all, which
                  measured on 2026-09-17 is eight of the nine seeded sources.

A 304 is a real answer, not a failure: the body is unchanged, so the run's
observation points at the body already in the store and a quote verified
against it is verified against what that run saw. Two conditions make that
true and both are enforced rather than assumed. The stored body is resolved by
``last_hash`` and never by "the newest row", because a source that flip-flops
between two bodies makes those two answers disagree. And a conditional request
is only sent when a body is actually held: asking "has it changed since?"
while holding nothing turns the answer "no" into a permanently green source
with no evidence behind it.

**Alarms are reported, never raised.** Every one of them describes a source
that answered, so nothing here throws on a dead feed. A source that 404s, one
that answers 200 with a login wall, and one that has not moved in eight months
are three different problems and a human wants to see all three at once, not
the first one as a traceback.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
import uuid
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from urllib.parse import urlsplit

from okuro.roles._time import parse_ts as _parse_ts

logger = logging.getLogger("okuro.roles.source_poll")

#: Sent on every request. A poller that does not identify itself is the
#: first thing a vendor blocks, and the block arrives as a 403 that reads
#: exactly like a dead feed.
USER_AGENT = "okuro-source-poll/1.0 (+https://github.com/saxmode/okuro-ai)"

DEFAULT_TIMEOUT = 40.0

#: How many HOSTS are polled at once. The unit is a host and not a source,
#: because two sources on one host are a rate-limit waiting to happen — see
#: ``_poll_host``.
POLL_MAX_WORKERS = 6

#: Courtesy pause between two requests to the SAME host.
#:
#: THIS IS VENDOR GUIDANCE, NOT A MEASURED DEFECT, and the difference matters
#: because an earlier version of this comment claimed the measurement. arXiv
#: asks for roughly three seconds between API calls in its terms of use and is
#: the strictest of the registered vendors, so its number is the one used.
#: Tested 2026-09-17: two SIMULTANEOUS requests for the same arXiv query both
#: returned 200, twice — so concurrency alone does not trip anything today.
#: The pause is politeness against a limit that exists and is documented, not
#: a repair for a failure anybody has seen here.
#:
#: Only paid when a host carries more than one source: today that is
#: export.arxiv.org and modelcontextprotocol.io, so a whole run pays it twice.
SAME_HOST_DELAY = 3.0

#: The bound that actually matters. ``DEFAULT_TIMEOUT`` is per socket
#: operation, so a server dribbling one byte every thirty seconds never trips
#: it and holds the handler open indefinitely. This is the whole-run ceiling:
#: past it the run reports what it has and names the stragglers.
POLL_RUN_DEADLINE = 60.0

# ── Alarm identifiers (amendment A4) ────────────────────────────────────
# Strings rather than an enum because they are stored as JSON in
# source_fetch_runs.alarms and read back by an API, a UI and a role body.
# An enum would be three round trips to the same five words.

ALARM_HTTP_STATUS = "http_status"
ALARM_ANCHOR_MISSING = "anchor_missing"
ALARM_CROSS_HOST_REDIRECT = "cross_host_redirect"
ALARM_LENGTH_COLLAPSE = "length_collapse"
ALARM_STALE_UNCHANGED = "stale_unchanged"
ALARM_NO_STORED_BODY = "no_stored_body"
ALARM_POLL_FAILED = "poll_failed"
ALARM_POLL_TIMEOUT = "poll_timeout"

ALARM_REASONS: dict[str, str] = {
    ALARM_HTTP_STATUS:
        "the source did not answer 200 or 304",
    ALARM_ANCHOR_MISSING:
        "the registered anchor sentence is gone from the body — a 200 that "
        "no longer serves the page we registered",
    ALARM_CROSS_HOST_REDIRECT:
        "the request ended on a different host than the one registered",
    ALARM_LENGTH_COLLAPSE:
        "the body lost more than half its length against the last stored one",
    ALARM_STALE_UNCHANGED:
        "the content hash has not changed for longer than stale_after_months",
    ALARM_NO_STORED_BODY:
        "the run ended with no body behind it, so nothing quoted from this "
        "source can be verified — the source reported nothing, not no-change",
    ALARM_POLL_FAILED:
        "the poll itself raised before it could reach a verdict",
    ALARM_POLL_TIMEOUT:
        "the source did not answer inside the whole-run deadline, so it "
        "contributed nothing to this run",
}

#: Shortest quote ``verify_quote`` will consider, in words after normalisation.
#:
#: Without a floor the assert is trivially defeatable: "tools" appears in every
#: one of the nine registered bodies, so a one-word quote verifies True and the
#: control certifies nothing. Six words is short enough that no real sentence
#: from a specification is excluded and long enough that a passing string is a
#: quote rather than a token. The number is stated in the role body's AC2 so
#: the agent knows the rule rather than discovering it as a rejection.
MIN_QUOTE_WORDS = 6

#: Below this fraction of the previous stored length, the body has collapsed.
#: Half is not a tuned number — it is the point past which "they edited the
#: page" stops being the likely explanation.
LENGTH_COLLAPSE_RATIO = 0.5

#: Days per month for the staleness window. Calendar months would make the
#: alarm fire on a different day depending on when the source was registered,
#: for no gain: the question is "roughly half a year", not "since the 3rd".
DAYS_PER_MONTH = 30


# ── HTTP ────────────────────────────────────────────────────────────────


@dataclass
class HttpResponse:
    """What the poller needs from a response, and nothing else.

    A dataclass rather than the transport's own object so a test can build one
    in a line. The stub HTTP layer in ``tests/roles/test_source_poll.py``
    returns these; so does :func:`urllib_fetch`.
    """

    status: int
    body: str = ""
    final_url: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    error: str | None = None

    def header(self, name: str) -> str | None:
        """Case-insensitive header read — HTTP header names are not case
        sensitive and two of the four vendors here disagree about casing."""
        lowered = name.lower()
        for key, value in self.headers.items():
            if key.lower() == lowered:
                return value
        return None


def urllib_fetch(
    url: str,
    *,
    headers: dict[str, str] | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> HttpResponse:
    """The default transport: one GET, redirects followed, body decoded.

    ``urllib`` rather than ``httpx`` on purpose. This runs inside the API
    process and inside a migration-adjacent test suite, and the one thing it
    must not do is pull an async client into either. It also has to see the
    FINAL url after redirects, which ``urllib`` hands over directly.
    """
    import urllib.error
    import urllib.request

    request_headers = {"User-Agent": USER_AGENT}
    request_headers.update(headers or {})
    request = urllib.request.Request(url, headers=request_headers)

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
            return HttpResponse(
                status=int(response.status),
                body=raw.decode("utf-8", "replace"),
                final_url=response.geturl(),
                headers=dict(response.headers.items()),
            )
    except urllib.error.HTTPError as exc:
        # A 304 arrives here, not as a success — and it is the answer we
        # asked for, so it must not be reported as an error.
        body = ""
        try:
            body = exc.read().decode("utf-8", "replace")
        except Exception:  # noqa: BLE001 — a body-less error is normal
            pass
        return HttpResponse(
            status=int(exc.code),
            body=body,
            final_url=getattr(exc, "url", url) or url,
            headers=dict(exc.headers.items()) if exc.headers else {},
            error=None if exc.code == 304 else f"HTTP {exc.code}",
        )
    except Exception as exc:  # noqa: BLE001 — a dead host is a finding
        return HttpResponse(status=0, final_url=url, error=str(exc))


# ── Pure helpers ────────────────────────────────────────────────────────


def normalise(text: str) -> str:
    """Collapse every run of whitespace to one space.

    Both sides of every comparison go through this. A vendor rewrapping its
    markdown at 80 columns instead of 100 changes every byte of the file and
    none of the words, and an anchor check that reads that as "the page is
    gone" is an alarm nobody will trust twice.
    """
    return " ".join((text or "").split())


def sha256_of(text: str) -> str:
    """Hash the body as UTF-8 bytes. Not normalised — the hash answers 'did
    anything at all change', which is a different question from the anchor's
    'is this still the same page', and blurring them loses the cheap one."""
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def _host(url: str) -> str:
    return (urlsplit(url or "").hostname or "").lower()


def _conditional_headers(source: dict, held: dict | None) -> dict[str, str]:
    """The validator headers for this source's check method.

    ``held`` is the stored body a 304 would fall back to. **When there is
    none, this returns empty whatever the check method says**, and that is not
    an optimisation — it closes a silent-green hole.

    The sequence it closes: poll 1 gets a 403 and stores no body, but still
    stamps ``last_checked_at`` (correctly — we DID look). Poll 2 sends
    ``If-Modified-Since`` built from that stamp, the server answers 304, and a
    304 reads as "unchanged, healthy". From then on the source is green
    forever with no body behind it, and every sentence quoted from it is
    unverifiable. Asking "has it changed since?" when you are holding nothing
    to compare against is the bug; not asking is the fix.

    Returns empty for ``content_hash`` too, which is the method for a vendor
    that sends no validator — the extra header would only make the request
    look conditional in a log while behaving unconditionally.
    """
    if held is None:
        return {}
    method = (source.get("check_method") or "content_hash").strip()
    if method == "etag":
        etag = (source.get("last_etag") or "").strip()
        return {"If-None-Match": etag} if etag else {}
    if method == "last_modified":
        seen = _parse_ts(source.get("last_checked_at"))
        return {"If-Modified-Since": format_datetime(seen, usegmt=True)} if seen else {}
    return {}


# ── Store ───────────────────────────────────────────────────────────────


def _held_body(db, source: dict) -> dict | None:
    """The stored blob the registry currently BELIEVES this source serves.

    Resolved by ``last_hash``, not by "newest row". Those two answers diverge
    the moment a source flip-flops, and the divergence is silent:

        run 1  body A  -> blob A stored
        run 2  body B  -> blob B stored, last_hash = B
        run 3  body A  -> content-addressing returns blob A, last_hash = A
        run 4  304     -> "newest by fetched_at" is B. The observation points
                          at B, so an honest quote from A fails verify_quote
                          and a sentence that only exists in B passes it. The
                          staleness clock is read off B's timestamp too.

    Every consequence of that bug is a lie in the direction that matters most:
    it rejects the truthful agent and accepts the invented sentence.

    Falls back to the newest row only when there is no ``last_hash`` at all —
    a store written before this column was stamped. Returns None when the
    stamp names a body that is not here, which is the honest answer once the
    fetch history is pruned by age, and which the caller turns into an
    unconditional refetch rather than a green 304.
    """
    source_id = source["id"]
    digest = (source.get("last_hash") or "").strip()
    cols = "id, content_sha256, content_length, fetched_at"

    if digest:
        row = db.fetchone(
            f"SELECT {cols} FROM source_fetches "
            "WHERE source_id = ? AND content_sha256 = ?",
            (source_id, digest),
        )
        return dict(row) if row else None

    row = db.fetchone(
        f"SELECT {cols} FROM source_fetches WHERE source_id = ? "
        "ORDER BY fetched_at DESC, rowid DESC LIMIT 1",
        (source_id,),
    )
    return dict(row) if row else None


def _store_body(db, source_id: str, run_id: str, status: int, body: str) -> dict:
    """Insert the body if this source has never carried it, return the row.

    Content-addressed: the unique index on ``(source_id, content_sha256)``
    means polling an unchanged spec every day costs one row, not 365. The row
    that comes back is the one to point this run's observation at, whether it
    was written now or six weeks ago.
    """
    digest = sha256_of(body)
    existing = db.fetchone(
        "SELECT id, content_sha256, content_length, fetched_at "
        "FROM source_fetches WHERE source_id = ? AND content_sha256 = ?",
        (source_id, digest),
    )
    if existing:
        return dict(existing)

    fetch_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    db.execute(
        "INSERT INTO source_fetches "
        "(id, source_id, run_id, fetched_at, http_status, content_sha256, "
        " content_length, body) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (fetch_id, source_id, run_id, now, status, digest, len(body), body),
    )
    return {
        "id": fetch_id,
        "content_sha256": digest,
        "content_length": len(body),
        "fetched_at": now,
    }


def _record_observation(
    db,
    *,
    run_id: str,
    source_id: str,
    fetch_id: str | None,
    status: int,
    changed: bool,
    alarms: list[str],
) -> None:
    """One row per (run, source) — the thing ``verify_quote`` reads.

    Upsert rather than insert: re-running a poll under the same run_id is
    something a retry does, and it must correct the observation rather than
    trip a unique-index error that reads like a bug in the poller.
    """
    db.execute(
        "INSERT INTO source_fetch_runs "
        "(id, run_id, source_id, fetch_id, observed_at, http_status, "
        " changed, alarms) VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(run_id, source_id) DO UPDATE SET "
        "  fetch_id = excluded.fetch_id, "
        "  observed_at = excluded.observed_at, "
        "  http_status = excluded.http_status, "
        "  changed = excluded.changed, "
        "  alarms = excluded.alarms",
        (
            str(uuid.uuid4()),
            run_id,
            source_id,
            fetch_id,
            datetime.now(timezone.utc).isoformat(),
            status,
            1 if changed else 0,
            json.dumps(alarms),
        ),
    )


def _stamp_source(
    db,
    source_id: str,
    *,
    status: int,
    etag: str | None,
    digest: str | None,
) -> None:
    """Write back what the registry learned. ``last_hash`` keeps its previous
    value when there is no body (a 304 or a dead host), because overwriting it
    with NULL would report the next poll as 'changed' for no reason."""
    db.execute(
        "UPDATE role_structure_sources SET "
        "  last_status = ?, "
        "  last_etag = COALESCE(?, last_etag), "
        "  last_hash = COALESCE(?, last_hash), "
        "  last_checked_at = ?, "
        "  updated_at = ? "
        "WHERE id = ?",
        (
            status,
            etag,
            digest,
            datetime.now(timezone.utc).isoformat(),
            datetime.now(timezone.utc).isoformat(),
            source_id,
        ),
    )


# ── The three entry points ──────────────────────────────────────────────


def _request(source: dict, held: dict | None, http) -> HttpResponse:
    """The WIRE half: one conditional GET, plus the forced refetch. No database.

    Split out so ``poll_all`` can run nine of these in parallel while every
    write still happens on one thread. Nine sequential fetches at a 40-second
    socket timeout is a six-minute worst case inside a synchronous handler,
    and a slow-drip server never trips the per-operation timeout at all.
    """
    url = source["url"]
    response = http(
        url, headers=_conditional_headers(source, held), timeout=DEFAULT_TIMEOUT
    )

    # A 304 with nothing behind it is the silent-green case: the vendor says
    # "unchanged" and we are holding no body to have quotes checked against.
    # _conditional_headers should have prevented the conditional request in
    # the first place, so reaching here means the server answered 304 to an
    # unconditional one. One forced refetch, then an alarm if that fails too —
    # belt as well as braces, because the failure mode is invisible.
    if response.status == 304 and held is None:
        logger.warning(
            "%s answered 304 with no stored body — refetching unconditionally",
            source["id"],
        )
        response = http(url, headers={}, timeout=DEFAULT_TIMEOUT)
    return response


def _apply(db, source: dict, held: dict | None, response: HttpResponse,
           run_id: str) -> dict:
    """The STORE half: alarms, the blob, the observation, the stamp.

    Always called on the caller's thread, never inside the pool, so the write
    order is deterministic and nine workers never contend for the writer lock.

    Returns a dict describing the observation — the same shape ``poll_all``
    collects and the API ships:

        {source_id, name, url, status, changed, alarms, alarm_reasons,
         content_sha256, content_length, fetch_id, anchor_found, final_url,
         error}

    ``anchor_found`` is None, not False, when there was no body to look in.
    A 304 says "unchanged" on the vendor's own authority and carries no body;
    reporting that as "anchor missing" would invent a dead feed out of a
    working one.
    """
    source_id = source["id"]
    url = source["url"]
    previous_hash = (source.get("last_hash") or "") or (
        held.get("content_sha256") if held else ""
    )

    alarms: list[str] = []
    anchor_found: bool | None = None
    fetch_id: str | None = held.get("id") if held else None
    digest: str | None = None
    length: int | None = None
    changed = False

    if response.status not in (200, 304):
        alarms.append(ALARM_HTTP_STATUS)
    elif response.status == 304 and held is None:
        # Still nothing to point at. Say so rather than reporting unchanged:
        # this source contributed no verifiable evidence to this run.
        alarms.append(ALARM_NO_STORED_BODY)

    final_url = response.final_url or url
    if _host(final_url) and _host(url) and _host(final_url) != _host(url):
        alarms.append(ALARM_CROSS_HOST_REDIRECT)

    if response.status == 200:
        body = response.body or ""
        digest = sha256_of(body)
        length = len(body)
        changed = bool(previous_hash) and digest != previous_hash
        if not previous_hash:
            # A first sighting is not a change — there was nothing to change
            # from. Calling it one would hand the researcher nine "changed"
            # sources on the first ever run and no way to tell which moved.
            changed = False

        stored = _store_body(db, source_id, run_id, response.status, body)
        fetch_id = stored["id"]

        anchor = source.get("anchor_text")
        if anchor:
            anchor_found = normalise(anchor) in normalise(body)
            if not anchor_found:
                alarms.append(ALARM_ANCHOR_MISSING)

        # Baseline is the body the registry believed it was serving, not the
        # newest row — same reason as everything else in this function.
        if held and held.get("content_length"):
            prior_len = int(held["content_length"])
            if prior_len > 0 and length < prior_len * LENGTH_COLLAPSE_RATIO:
                alarms.append(ALARM_LENGTH_COLLAPSE)

    # Staleness reads how long the CURRENT body has been the body, which is
    # when it was first stored — not when it was last checked. A daily poll
    # would otherwise reset the clock every morning and the alarm would never
    # fire at all.
    unchanged_since = _parse_ts(
        (held or {}).get("fetched_at") if not changed else None
    )
    if unchanged_since is not None and response.status in (200, 304):
        months = int(source.get("stale_after_months") or 6)
        if datetime.now(timezone.utc) - unchanged_since > timedelta(
            days=months * DAYS_PER_MONTH
        ):
            alarms.append(ALARM_STALE_UNCHANGED)

    _record_observation(
        db,
        run_id=run_id,
        source_id=source_id,
        fetch_id=fetch_id,
        status=response.status,
        changed=changed,
        alarms=alarms,
    )
    _stamp_source(
        db,
        source_id,
        status=response.status,
        etag=response.header("ETag"),
        digest=digest,
    )

    return {
        "source_id": source_id,
        "name": source.get("name"),
        "url": url,
        "final_url": final_url,
        "kind": source.get("kind"),
        "vendor": source.get("vendor"),
        "status": response.status,
        "changed": changed,
        "alarms": alarms,
        "alarm_reasons": [ALARM_REASONS[a] for a in alarms],
        "content_sha256": digest,
        "content_length": length,
        "fetch_id": fetch_id,
        "anchor_found": anchor_found,
        "error": response.error,
    }


def new_run_id() -> str:
    """A run id that stays unique under a double click.

    The timestamp has one-second resolution, which two clicks comfortably fit
    inside. Two runs sharing an id share their observation rows — the second
    upserts over the first — and a quote verified against run one then reads
    against whatever run two fetched. The suffix costs six characters.
    """
    return (
        f"structrun-{datetime.now(timezone.utc):%Y%m%d-%H%M%S}"
        f"-{uuid.uuid4().hex[:6]}"
    )


def _failure_result(source: dict, alarm: str, error: str) -> dict:
    """The result shape for a source that produced no verdict of its own."""
    return {
        "source_id": source.get("id"),
        "name": source.get("name"),
        "url": source.get("url"),
        "final_url": source.get("url"),
        "kind": source.get("kind"),
        "vendor": source.get("vendor"),
        "status": 0,
        "changed": False,
        "alarms": [alarm],
        "alarm_reasons": [ALARM_REASONS[alarm]],
        "content_sha256": None,
        "content_length": None,
        "fetch_id": None,
        "anchor_found": None,
        "error": error,
    }


def _safe_failure_observation(db, run_id: str, source_id: str, alarm: str) -> None:
    """PERSIST a failure, do not merely return it.

    Without this row the registry panel keeps showing the PREVIOUS run's
    healthy observation while the task description for THIS run says the
    source alarmed — two surfaces telling different stories about the same
    source, and the reassuring one is the one on screen.

    Swallows its own error because the store may be exactly what broke, and a
    failure to record a failure must not replace it.
    """
    try:
        _record_observation(
            db,
            run_id=run_id,
            source_id=source_id,
            fetch_id=None,
            status=0,
            changed=False,
            alarms=[alarm],
        )
    except Exception:  # noqa: BLE001 — the store itself may be the fault
        logger.exception("could not record the failed observation for %s", source_id)


def _poll_host(
    group: list[dict], held_by_id: dict, http
) -> tuple[dict[str, HttpResponse], dict[str, str]]:
    """Every source on ONE host, in sequence, with a courtesy pause between.

    Grouping by host keeps this poller from ever opening N simultaneous
    connections to one vendor. arXiv publishes a ~3s inter-request guideline
    and is the strictest of the four registered vendors, so its number sets
    :data:`SAME_HOST_DELAY`. This is politeness against a documented limit, not
    a fix for a failure observed here — measured 2026-09-17, two simultaneous
    requests for the same arXiv query returned 200 both times.

    Returns what it managed, never raises for one bad source: a host carrying
    two sources must not lose the second because the first threw.
    """
    responses: dict[str, HttpResponse] = {}
    errors: dict[str, str] = {}
    for index, source in enumerate(group):
        if index:
            time.sleep(SAME_HOST_DELAY)
        try:
            responses[source["id"]] = _request(
                source, held_by_id[source["id"]], http
            )
        except Exception as exc:  # noqa: BLE001 — one bad source is not a host
            errors[source["id"]] = str(exc)
    return responses, errors


def fetch_source(db, source: dict, *, run_id: str, http=None) -> dict:
    """Poll ONE source end to end: resolve, request, store, stamp, report.

    The single-source entry point. ``poll_all`` does not call it — it runs the
    same three steps with the middle one in a thread pool — but both go
    through :func:`_request` and :func:`_apply`, so there is one definition of
    what a poll does.
    """
    held = _held_body(db, source)
    response = _request(source, held, http or urllib_fetch)
    return _apply(db, source, held, response, run_id)


def poll_all(db, *, run_id: str | None = None, http=None) -> dict:
    """Poll every enabled source and return the run summary.

    The summary is what the dispatch endpoint puts in the researcher's task
    description, so it names the sources that CHANGED explicitly. A researcher
    told "go look at the sources" reads all nine and reports on whichever it
    found something in; one told "these two changed" has a falsifiable scope.

    **Parallel per HOST, serial within one, serial at the store.** The fetches
    run in a thread pool with one worker per host under a whole-run deadline;
    two sources on the same host go one after the other with a courtesy pause,
    so no vendor ever sees N simultaneous connections from this poller. Every
    database write happens back here, on this thread, in source order. Sequentially, nine sources at a
    40-second socket timeout is a six-minute worst case inside a synchronous
    request handler — and a server that dribbles one byte at a time never
    trips a per-operation timeout at all, so the per-socket timeout is not the
    bound that matters. :data:`POLL_RUN_DEADLINE` is.

    A source still in flight when the deadline passes gets a
    ``poll_timeout`` observation and contributes nothing to the run. Its
    thread is not killable and will finish into a void; that is deliberate —
    applying a late result would mean a background thread writing after the
    caller has already read the summary, which is the race this shape exists
    to avoid.
    """
    run_id = run_id or new_run_id()
    http = http or urllib_fetch
    sources = [
        dict(r)
        for r in db.fetchall(
            "SELECT * FROM role_structure_sources "
            "WHERE enabled = 1 ORDER BY id"
        )
    ]

    # Resolve what each source falls back to BEFORE anything goes on the wire:
    # it is a database read, and database reads stay on this thread.
    held_by_id: dict[str, dict | None] = {
        s["id"]: _held_body(db, s) for s in sources
    }

    responses: dict[str, HttpResponse] = {}
    errors: dict[str, str] = {}
    timed_out: list[str] = []

    if sources:
        # ONE WORKER PER HOST, not per source, so a vendor never sees N
        # simultaneous connections from this poller. That is a politeness
        # bound taken from arXiv's published guidance, NOT a repair for an
        # observed failure: two simultaneous requests for the same arXiv query
        # returned 200 both times when it was tested. Nine sources sit on
        # seven hosts, so the run is still six-way parallel and only the two
        # doubled hosts pay anything.
        by_host: dict[str, list[dict]] = defaultdict(list)
        for source in sources:
            by_host[_host(source["url"])].append(source)

        executor = ThreadPoolExecutor(
            max_workers=min(POLL_MAX_WORKERS, len(by_host)),
            thread_name_prefix="okuro-source-poll",
        )
        try:
            futures = {
                executor.submit(_poll_host, group, held_by_id, http): host
                for host, group in by_host.items()
            }
            done, pending = wait(futures, timeout=POLL_RUN_DEADLINE)
            for future in done:
                host = futures[future]
                try:
                    ok_responses, host_errors = future.result()
                except Exception as exc:  # noqa: BLE001 — one bad host is not a run
                    for source in by_host[host]:
                        errors[source["id"]] = str(exc)
                else:
                    responses.update(ok_responses)
                    errors.update(host_errors)
            # A host that ran out of time takes its whole group with it: the
            # group is sequential, so nothing in it has a verdict to keep.
            timed_out = [
                source["id"]
                for future in pending
                for source in by_host[futures[future]]
                if source["id"] not in responses
            ]
        finally:
            executor.shutdown(wait=False, cancel_futures=True)

    results = []
    for source in sources:
        source_id = source["id"]

        if source_id in timed_out:
            logger.warning(
                "poll of %s did not finish inside the %ss run deadline",
                source_id, POLL_RUN_DEADLINE,
            )
            _safe_failure_observation(db, run_id, source_id, ALARM_POLL_TIMEOUT)
            results.append(
                _failure_result(source, ALARM_POLL_TIMEOUT,
                                f"no answer within {POLL_RUN_DEADLINE}s")
            )
            continue

        if source_id in errors:
            logger.warning("poll of %s failed: %s", source_id, errors[source_id])
            _safe_failure_observation(db, run_id, source_id, ALARM_POLL_FAILED)
            results.append(
                _failure_result(source, ALARM_POLL_FAILED, errors[source_id])
            )
            continue

        try:
            results.append(
                _apply(db, source, held_by_id[source_id],
                       responses[source_id], run_id)
            )
        except Exception as exc:  # noqa: BLE001 — one bad source is not a run
            logger.warning("storing the poll of %s failed: %s", source_id, exc)
            _safe_failure_observation(db, run_id, source_id, ALARM_POLL_FAILED)
            results.append(_failure_result(source, ALARM_POLL_FAILED, str(exc)))

    return {
        "run_id": run_id,
        "polled": len(results),
        "changed": [r["source_id"] for r in results if r.get("changed")],
        "alarmed": [r["source_id"] for r in results if r.get("alarms")],
        "health_actions": open_health_actions(db, results, run_id),
        "sources": results,
    }


def health_action_title(source_id: str, alarm: str) -> str:
    """The title of the source_health action for one (source, alarm).

    Deterministic, and keyed on the source ID rather than its NAME, because
    the dedupe is a UNIQUE index on (source_id, title): a display name that
    somebody edits would mint a second open row for an alarm that never
    stopped. The id is the primary key and does not move.
    """
    return f"source {source_id} — {alarm}"


def open_health_actions(db, results: list[dict], run_id: str) -> list[dict]:
    """Turn this run's alarms into `source_health` rows in the action container.

    **Amendment A4's last mile.** The poll already detects a dead feed; until
    now the detection lived in an observation row and on a panel, which means
    it was visible to anybody who went looking and to nobody who did not. An
    alarm that nothing tracks is indistinguishable from an alarm nobody raised
    after the week it fired.

    One row per (source, alarm kind), deduplicated while an open one exists —
    ``arxiv-persona-prompting`` alarms on every run and will keep doing so
    until arXiv serves uncached queries again, which is the alarm working, not
    a reason to mint fifty-two identical rows a year.

    Never fatal. A poll that fetched nine sources and then failed to file a
    row has still done the thing it was called for, and raising here would
    throw that away — the endpoint's caller would see "source poll failed"
    for a poll that succeeded.
    """
    from okuro.roles.actions import ActionRefused, propose

    opened: list[dict] = []
    for result in results:
        source_id = result.get("source_id")
        for alarm in result.get("alarms") or []:
            try:
                row = propose(
                    db,
                    kind="source_health",
                    title=health_action_title(source_id, alarm),
                    created_by="roles.source_poll",
                    source_id=source_id,
                    run_id=run_id,
                    reason=ALARM_REASONS.get(alarm, alarm),
                )
            except ActionRefused as exc:
                logger.warning(
                    "could not open a source_health action for %s/%s: %s",
                    source_id, alarm, exc,
                )
                continue
            except Exception as exc:  # noqa: BLE001 — filing is not the poll
                logger.warning(
                    "filing the %s alarm for %s raised: %s", alarm, source_id, exc
                )
                continue
            opened.append(
                {"action_id": row["id"], "source_id": source_id, "alarm": alarm}
            )
    return opened


def verify_quote(db, run_id: str, source_id: str, quoted_sentence: str) -> bool:
    """Does this sentence appear in the body THAT RUN fetched from THAT source?

    The whole phase exists for this one function. A structure finding that
    cannot pass it does not exist — not "is downgraded", not "is flagged":
    does not exist, and the role body says so in those words.

    Whitespace is normalised on both sides (see :func:`normalise`) and nothing
    else is. No case folding, no punctuation stripping, no fuzzy match: a
    quote that has been reworded is not a quote, and a matcher generous enough
    to accept a paraphrase is generous enough to accept an invention.

    **There is a length floor and it is load-bearing.** A substring assert
    with no minimum certifies nothing: the word "tools" appears in every one
    of the nine registered bodies, so a one-word quote passes and the control
    becomes a rubber stamp an agent can satisfy without reading anything.
    Anything under :data:`MIN_QUOTE_WORDS` words after normalisation is
    refused. The number is stated in the role body's AC2, so the rule is
    something the agent knows rather than something it discovers as a
    rejection.

    False when there is no observation for that run, when that observation has
    no body behind it (a failed fetch), when the sentence is empty, and when
    it is too short — the honest answer to "check this against nothing" is no.
    """
    normalised_quote = normalise(quoted_sentence)
    if len(normalised_quote.split()) < MIN_QUOTE_WORDS:
        return False

    row = db.fetchone(
        "SELECT f.body AS body FROM source_fetch_runs r "
        "JOIN source_fetches f ON f.id = r.fetch_id "
        "WHERE r.run_id = ? AND r.source_id = ?",
        (run_id, source_id),
    )
    if not row or row["body"] is None:
        return False

    return normalised_quote in normalise(row["body"])
