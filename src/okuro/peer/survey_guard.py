# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: peer.survey_guard — refuse to hand out a survey in a language whose
#   items nobody has reviewed, unless the caller says so explicitly.
# index: UnreviewedLanguage | require_deliverable_language
# AGENT_HEADER_END -->
"""The review flag, enforced at the moment a survey is handed out.

WHY A GUARD AND NOT A COMMENT. ``reviewed=False`` on a catalogue is a fact
about words nobody has checked. Without enforcement it is a note that loses
an argument to a deadline. These are psychometric items — NCS-6, REI, SNS,
SGL — and a rendering that drifts measures something other than the scale it
is named after, while ``score_survey`` still records the axis at
``CONF_DIRECT`` 0.95. The failure is silent and arrives wearing high
confidence, which is exactly the overtrust shape P3.4 exists to catch.

WHERE IT FIRES: at CREATION — minting a token, generating an offline HTML —
never at consumption. Refusing to render ``/q/{token}`` would break a link
already in someone's inbox and punish the recipient for a decision the
sender made.

THE OVERRIDE IS EXPLICIT AND NAMED. ``allow_unreviewed=True`` is a caller
saying "I know, send it anyway", which is a legitimate thing to want for a
dry run against a synthetic recipient. It is a parameter rather than a
config flag so it appears at the call site, in the diff, next to the person
it affects.
"""

from __future__ import annotations

from okuro.peer.flags import people_strict_enabled, warn_lenient
from okuro.peer.survey_i18n import available_languages, catalogue


class UnreviewedLanguage(ValueError):
    """Raised when a survey would go out in an unchecked translation."""


def require_deliverable_language(
    lang: str | None,
    *,
    allow_unreviewed: bool = False,
    context: str = "survey",
) -> str:
    """Resolve ``lang`` and refuse it if its items have not been reviewed.

    Returns the resolved language code — which may differ from ``lang``, since
    an unknown code falls back to English rather than failing.

    Gated by ``people.strict`` like every other behaviour change in this
    module: the lenient branch warns and proceeds, so the guard is one config
    line away from being rolled back if it ever blocks real work.
    """
    cat = catalogue(lang)
    if cat.reviewed or allow_unreviewed:
        return cat.lang

    if not people_strict_enabled():
        warn_lenient(
            f"survey_guard.{context}",
            f"survey handed out in unreviewed language {cat.lang!r}",
        )
        return cat.lang

    reviewed = [l for l in available_languages() if catalogue(l).reviewed]
    # A catalogue a native speaker has REJECTED is not the same risk as one
    # nobody has read, and the caller deciding whether to override deserves
    # to be told which one they are looking at.
    if cat.review_failures:
        last = cat.review_failures[-1]
        opening = (
            f"The {cat.lang!r} survey translation has FAILED native review "
            f"{len(cat.review_failures)} time(s) — most recently {last['date']} "
            f"by {last['reviewer']}: \"{last['verdict']}\" "
            f"Overriding sends wording a native speaker has already rejected — "
            f"and a survey answered against a drifted item is still recorded "
            f"at 0.95 confidence, so the damage is a confident number nobody "
            f"can tell apart from a good one. "
        )
    else:
        opening = (
            f"The {cat.lang!r} survey translation has not been reviewed by "
            f"anyone who speaks it, so its items may no longer measure what "
            f"they are named after — and a survey answered against a drifted "
            f"item is still recorded at 0.95 confidence. "
        )
    raise UnreviewedLanguage(
        opening +
        f"Reviewed right now: {reviewed or ['(none)']}. "
        f"Either have it reviewed (set reviewed=True on the catalogue in "
        f"peer/survey_i18n.py, with reviewed_by), or pass "
        f"allow_unreviewed=True to send it knowingly."
        + (f" Catalogue note: {cat.note}" if cat.note else "")
    )


__all__ = ["UnreviewedLanguage", "require_deliverable_language"]
