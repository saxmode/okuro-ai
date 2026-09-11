### SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Distill-then-delete — qualification ledger, cold archive, and the retention gate.
# index: submodules
# AGENT_HEADER_END -->
"""Distill-then-delete.

A transcript may only leave ``agent_events`` after it has been distilled into
the ledger and archived somewhere the archive can be read back. Two modules
carry that:

:mod:`.archive`
    Per-session zstd JSONL on cold storage, append-only, digest recorded.

:mod:`.gate`
    The only route by which ``agent_events`` rows can be deleted at all — the
    schema guard in migration 136 aborts every other one.

:mod:`.facets`
    One session read out of ``agent_events`` as MESSAGES rather than rows,
    with the tier-1 truncation contract.

:mod:`.triage`
    Tier-0: deterministic, free, no model call.

:mod:`.corpus`
    Tier-1: a cheap facet per session, embedded and clustered.

:mod:`.judge`
    Tier-2: the rubric and the invocation path. Dormant — it refuses to write
    a verdict until a human-labelled calibration batch exists.

:mod:`.pipeline`
    The daemon entry point that runs tier-0 + tier-1 inside a token budget.

:mod:`.mining`
    Cluster-level defect mining. One session asserts, the cluster corroborates;
    one quality-tier call per cluster that has a corroborated finding.

:mod:`.lessons`
    The ACE lifecycle for what mining produced — idempotent evidence counters,
    hysteretic status transitions, and emission onto the EXISTING
    ``interaction_improvements`` lifecycle. No new injection channel.

:mod:`.rubric`
    Rubric evolution plumbing: the human-only deletion-floor setter, and which
    sessions a rubric change implicates (selection only, never execution).

Nothing here schedules deletion, and nothing here writes
``distill_config.deletion_enabled``. That switch is the owner's (Phase 4); it
ships 0, so every real invocation of the gate refuses today.

Nothing in :mod:`.mining` or :mod:`.lessons` writes ``session_distillations``
either. That table is the retention gate's deletion licence, tier-2 stays
dormant until a human calibration batch exists (migration 137), and a lesson
is not a verdict about any session.
"""

# The rubric a distillation was produced under. ``gate.py`` compares it against
# ``distill_config.min_deletable_rubric_version`` before permitting a delete,
# so this is the version stamped on every ledger row either tier writes. It
# lives here rather than in :mod:`.judge` because tier-0 writes ledger rows too
# and must not import the judge to learn the number.
RUBRIC_VERSION = 1
