# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: One clock. Every timestamp okuro writes or compares comes from here.
# index: imports | now | sql | parse
# AGENT_HEADER_END -->
"""The single source of time for okuro.

WHY THIS EXISTS. Timezone drift is this codebase's most-repeated defect — five
recorded incidents, none of them caught by a test, each found by a human
noticing a number that looked wrong:

  2026-05-07  engine, dispatcher, state, recurring, watchdog, checkpoint and
              signals/db wrote LOCAL naive while api/main.py wrote UTC naive.
              The frontend assumes naive means UTC, so engine timestamps
              rendered two hours in the future. 25 replacements.
  2026-05-08  recurring.py wrote last_run_at with utcnow() and compared it
              with now(). For the tz-offset window after each cron tick the
              next run looked overdue, so the daemon re-fired it every five
              minutes — 24 spurious runs a day for four days.
  (undated)   sentinel.py and proactive/scanners.py compared utcnow()-written
              columns against now(), inflating every elapsed measure by the
              offset and raising false "running 120min / may be stuck" alarms.
  (undated)   trace windows compare an ISO-8601 'T' column against SQLite's
              space-separated datetime('now'). ' ' < 'T', so the window silently
              extends up to 24h. Fixed in trace/stats.py, still live in
              trace/mcp_tools.py.
  2026-07-28  an agent compared `git log` output (LOCAL, +0200) against
              transcript timestamps (UTC) and concluded that an unattended
              session had committed on its own. It had not; the user had typed
              the instruction 22 seconds earlier.

Every one of those is the same shape: two clocks that LOOK alike. A naive
local datetime and a naive UTC datetime are the same type, print the same way,
compare without error, and differ by the offset. Nothing in Python objects.

So the fix is not another convention note. It is one module, plus a test that
fails when anything else is used — see tests/system/test_clock_discipline.py.

WHICH FUNCTION TO USE
    utc_now()        aware UTC. Default for new code, and the only form that
                     cannot be silently mixed with a local one.
    utc_now_naive()  naive UTC, matching what SQLite's datetime('now') stores.
                     Use ONLY to compare against an existing naive column.
    utc_iso()        ISO-8601 with a Z suffix, for JSON and for the ISO
                     columns in the trace store.
    SQL_UTC_ISO      the SQLite expression that produces the same shape, for
                     comparing against those ISO columns without the ' ' < 'T'
                     trap.

READING SOMEONE ELSE'S CLOCK. Anything crossing a process boundary — git,
transcripts, log files — is a foreign clock until proven otherwise. Prefer an
epoch integer (`git log --format=%ct`, `stat -c %Y`): it has no timezone to
get wrong. Where a string is unavoidable, parse it with `parse_to_utc` rather
than `fromisoformat`, which happily returns a naive local-looking value.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

__all__ = [
    "utc_now",
    "utc_now_naive",
    "utc_iso",
    "parse_to_utc",
    "age_seconds",
    "SQL_UTC_ISO",
    "SQL_UTC_NAIVE",
    "local_tz",
    "to_local",
    "fmt_local",
    "local_now",
]

# ---------------------------------------------------------------------------
# STORE UTC, SHOW LOCAL — the half that is easy to forget
# ---------------------------------------------------------------------------
# UTC is for storage, ordering and arithmetic; it is NOT what a human should
# ever be shown. The operator's profile declares a timezone and active hours
# in local wall-clock, and a reminder for "9am" means 9am where they are.
# Printing a bare UTC timestamp at them is a defect, not a neutral choice — the
# same "two clocks that look alike" failure, moved to the reading end.
#
# WHY STORAGE STILL STAYS UTC, since the obvious question is "why not just
# store local": DST makes local time non-injective and non-total.
#   * 2026-10-25 02:30 CEST->CET happens TWICE in Zurich. Two distinct instants
#     share one local string, so stored local rows cannot be ordered.
#   * 2026-03-29 02:30 does not exist at all. A local timestamp can be invalid.
#   * Any subtraction spanning a transition silently gains or loses an hour.
# UTC has none of those properties. So: one instant in the database, one
# rendering at the edge.
#
# The timezone is RESOLVED, never hardcoded. sense/mcp_tools.py pins
# ZoneInfo("Europe/Zurich") inline for reminder parsing, which is correct today
# and wrong on any other machine — the same single-source defect this module
# exists to end. Order: the user's declared profile timezone, then the detected
# system zone, then UTC.


def utc_now() -> datetime:
    """Timezone-AWARE current time in UTC. The default for new code."""
    return datetime.now(timezone.utc)


def utc_now_naive() -> datetime:
    """Naive UTC — the shape SQLite's ``datetime('now')`` writes.

    Exists only so code comparing against an existing naive column has a
    correct option that is not ``datetime.utcnow()``. Prefer ``utc_now()``
    everywhere else: a naive value carries no evidence of which clock it came
    from, which is the whole defect this module exists to prevent.
    """
    return datetime.now(timezone.utc).replace(tzinfo=None)


def utc_iso(dt: datetime | None = None) -> str:
    """ISO-8601 in UTC with a Z suffix — the trace store's column shape."""
    d = dt or utc_now()
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_to_utc(value: str, assume: str = "utc") -> datetime:
    """Parse a timestamp string to an AWARE UTC datetime.

    ``assume`` decides what a string with no offset means. It defaults to
    'utc' because that is what okuro's own columns are, but pass 'local' for a
    foreign clock that is known to be local — `git log --date=iso` without an
    offset, for instance. Making the caller state the assumption is the point:
    ``datetime.fromisoformat`` silently produces a naive value and the
    ambiguity survives into the comparison.
    """
    v = value.strip().replace("Z", "+00:00")
    dt = datetime.fromisoformat(v)
    if dt.tzinfo is None:
        if assume == "local":
            dt = dt.astimezone()
        else:
            dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def age_seconds(value: str | datetime, assume: str = "utc") -> float:
    """Seconds between a timestamp and now, both normalised to UTC.

    The elapsed-time computations that produced the false "stuck" alarms were
    all hand-rolled subtractions of two values from different clocks. This is
    the one that cannot be.
    """
    dt = parse_to_utc(value, assume=assume) if isinstance(value, str) else value
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (utc_now() - dt).total_seconds()


# SQLite expressions. Bind the modifier (e.g. '-7 days') as the parameter so
# the expression stays a constant on the right of the comparison and the index
# is still usable.
#
# SQL_UTC_ISO matches columns written as ISO-8601 with a T and a Z — the trace
# store's shape. Comparing those against datetime('now') is the ' ' < 'T' trap:
# SQLite renders a space separator, space sorts below 'T', so every row on the
# boundary day matches regardless of its time and the window runs up to 24h
# wide. Always inclusive, never noticed.
SQL_UTC_ISO = "strftime('%Y-%m-%dT%H:%M:%SZ', 'now', ?)"

# SQL_UTC_NAIVE matches columns written by SQLite's own datetime('now') —
# 'YYYY-MM-DD HH:MM:SS', naive UTC. Correct for those; wrong for ISO columns.
SQL_UTC_NAIVE = "datetime('now', ?)"


_TZ_CACHE: list = []


def _profile_tz_name() -> str | None:
    """The user's DECLARED timezone, or None.

    Uses ``get_profile_raw`` — the dict accessor. The first cut of this called
    ``get_profile()``, which returns rendered MARKDOWN, so ``.get("identity")``
    raised on a str, the except swallowed it, and resolution fell silently
    through to system detection. Detection returned the right answer on this
    machine, so the broken primary path looked like it worked. That is the same
    "a fallback that succeeds hides a broken primary" shape as the recall
    outage, and it is why the test below asserts the profile path SPECIFICALLY
    rather than just checking the final value.

    Why the profile wins over the system zone: a laptop carried to another
    country reports the local zone, but the user's working hours, reminders and
    "today" are still anchored to the zone they declared.
    """
    try:
        from okuro.yu.profile import get_profile_raw

        prof = get_profile_raw() or {}
        if not isinstance(prof, dict):
            return None
        ident = prof.get("identity")
        if isinstance(ident, dict) and ident.get("timezone"):
            return str(ident["timezone"])
        return str(prof["timezone"]) if prof.get("timezone") else None
    except Exception:  # noqa: BLE001 — display must never break on a bad profile
        return None


def local_tz():
    """The user's timezone, resolved once — profile, then system, then UTC.

    Cached because it is read on every render and the profile lookup touches
    the database. Restart to pick up a change; a timezone does not move often
    enough to justify a per-call query.
    """
    if _TZ_CACHE:
        return _TZ_CACHE[0]
    name = _profile_tz_name()
    if not name:
        try:
            from okuro.yu.detection import detect_timezone

            name = detect_timezone()
        except Exception:  # noqa: BLE001
            name = None
    tz = timezone.utc
    if name and name != "unknown":
        try:
            from zoneinfo import ZoneInfo

            tz = ZoneInfo(name)
        except Exception:  # noqa: BLE001 — unknown zone name, stay on UTC
            tz = timezone.utc
    _TZ_CACHE.append(tz)
    return tz


def to_local(value: str | datetime, assume: str = "utc") -> datetime:
    """Convert any stored timestamp to the user's local zone, DST-correct."""
    dt = parse_to_utc(value, assume=assume) if isinstance(value, str) else value
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(local_tz())


def local_now() -> datetime:
    """Current time in the user's zone. For display and for 'is it today?'."""
    return utc_now().astimezone(local_tz())


def fmt_local(value: str | datetime | None = None, with_zone: bool = True) -> str:
    """Render a timestamp the way it should reach a human.

    ALWAYS carries the zone abbreviation by default. A bare '15:47' invites
    exactly the mistake that started this module: an agent read `git log`
    (local) beside transcript times (UTC), saw no marker on either, and
    reported that an unattended session had committed on its own.
    """
    dt = to_local(value) if value is not None else local_now()
    return dt.strftime("%Y-%m-%d %H:%M %Z" if with_zone else "%Y-%m-%d %H:%M")


def _self_check() -> None:  # pragma: no cover - import-time sanity
    assert utc_now().tzinfo is not None
    assert utc_now_naive().tzinfo is None
    assert utc_iso().endswith("Z")
    assert abs(age_seconds(utc_iso())) < 5
    assert abs((utc_now() - utc_now_naive().replace(
        tzinfo=timezone.utc)).total_seconds()) < 5
    _ = timedelta  # re-exported shape kept explicit for readers
