# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: okuro-slides package — recipient-tailored deck builder storage.
# index: re-exports from storage
# AGENT_HEADER_END -->
"""okuro-slides — the recipient-tailored deck builder.

Phase 1 shipped the smart-animate renderer (frontend ``components/slides``).
This package is the persistence layer: a SQLite-backed deck store + change-feed,
mirroring ``okuro.flow_designer``. Surfaced in okuro.web at ``/slides``.
"""

from okuro.slides.storage import (
    DeckDoc,
    delete_deck,
    events_since,
    get_deck,
    latest_seq,
    list_decks,
    save_deck,
    slugify,
)

__all__ = [
    "DeckDoc",
    "delete_deck",
    "events_since",
    "get_deck",
    "latest_seq",
    "list_decks",
    "list_variants",
    "retailor_deck",
    "save_deck",
    "slugify",
]


def __getattr__(name: str):
    # Lazy re-export of the re-tailor surface (avoids importing generate/persons
    # at package import time — they pull in the LLM bridge).
    if name in ("retailor_deck", "list_variants"):
        from okuro.slides import retailor

        return getattr(retailor, name)
    raise AttributeError(f"module 'okuro.slides' has no attribute '{name}'")
