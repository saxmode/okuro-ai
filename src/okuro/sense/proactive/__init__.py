# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Proactive suggester — scans okuro state hourly and emits signals.
# index: scan_once
# AGENT_HEADER_END -->
"""Proactive suggester package.

Hourly scanner that emits `source='proactive'` signals into the
`signals` table for triage on the Now page. Heuristic-only in P1;
LLM-backed judging is reserved for P2 (bridge_invoke).

Public surface: :func:`scan_once` — called by
``okuro.daemon._handlers:proactive_scan`` on the cron schedule.
"""

from okuro.sense.proactive.advisor import find_continuations
from okuro.sense.proactive.engine import scan_once

__all__ = ["scan_once", "find_continuations"]
