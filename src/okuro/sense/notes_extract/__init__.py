# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Note extractor — okuro-notes → typed, tagged todos/signals/thoughts.
# index: extract_once
# AGENT_HEADER_END -->
"""Note extraction package.

Reads okuro-notes (migration 071), lifts out typed items via the bridge, and
routes each to its natural home:

    todo      → todos       (eager: checkboxes AND prose commitments)
    signal    → signals     (source='notes')
    question  → signals     (severity=info)
    idea      → thoughts    (category=idea)

Every item carries the same tag block wherever it lands — topic, entities,
context, rationale — so a list row can always answer "what is this about, where
did it come from, and why is it here?".

Public surface: :func:`extract_once` — called by
``okuro.daemon._handlers:notes_extract`` on the cron schedule.
"""

from okuro.sense.notes_extract.engine import extract_once

__all__ = ["extract_once"]
