# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Reminder eval entry point for systemd timer.
# index: def main
# AGENT_HEADER_END -->
"""Reminder eval entry point for systemd timer.

Called every minute by okuro-reminder.timer:
  python -m okuro.sense.reminders.eval
"""

import logging
import sys

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
log = logging.getLogger("okuro.sense.reminders.eval")


def main():
    try:
        from .engine import evaluate_due
        result = evaluate_due()
        sent = result.get("sent", 0) if isinstance(result, dict) else 0
        if sent:
            log.info("Fired %d cascade steps", sent)
    except Exception as e:
        log.error("Eval loop failed: %s", e)

    # Reminder-suggestion auto-generation is OFF, on the user's own behaviour
    # (DP07 observe-not-ask). Measured 2026-07-15 over the full history:
    # 111 suggestions produced, 21 explicitly REJECTED, 90 ignored, **0 ever
    # accepted**. A producer with a zero acceptance rate and an explicit
    # rejection record is not under-tuned, it is unwanted — and it was firing
    # every 5 minutes forever.
    #
    # Only the automatic firing is removed. generate_suggestions() still
    # exists in sense/reminders/suggestions.py:87 and is re-exported from
    # sense/reminders/__init__.py, but NOTHING calls it: there is no MCP tool
    # and no API route that fires it (verified 2026-08-18 — mcp_tools exposes
    # only accept_suggestion / reject_suggestion, and api/reminders.py exposes
    # only GET /suggestions plus accept/reject). Recovery is a Python import or
    # restoring these lines; no data migration either way.
    # accept_suggestion / reject_suggestion are untouched.


if __name__ == "__main__":
    main()
