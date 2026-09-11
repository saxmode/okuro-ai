# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: okuro·prism package — multi-depth progressive-disclosure comms tool storage.
# index: re-exports from storage
# AGENT_HEADER_END -->
"""okuro·prism — one graph, four depths (Glance/Brief/Working/Expert).

A facet TREE (see ``okuro.prism.storage.PrismDoc``): every facet carries
content at all 4 rungs of the shared depth ladder locked in the 3.1 MVP
contract. Persistence is a SQLite-backed doc store + change-feed, mirroring
``okuro.slides`` / ``okuro.flow_designer``. Surfaced in okuro.web at ``/prism``.
"""

from okuro.prism.storage import (
    PrismDoc,
    delete_doc,
    events_since,
    get_doc,
    latest_seq,
    list_docs,
    save_doc,
    slugify,
    update_doc_meta,
)

__all__ = [
    "PrismDoc",
    "delete_doc",
    "events_since",
    "get_doc",
    "latest_seq",
    "list_docs",
    "retailor_facets",
    "save_doc",
    "slugify",
    "update_doc_meta",
]


def __getattr__(name: str):
    # Lazy re-export of the re-tailor surface (avoids importing generate/persons
    # at package import time — they pull in the LLM bridge).
    if name == "retailor_facets":
        from okuro.prism import retailor

        return getattr(retailor, name)
    raise AttributeError(f"module 'okuro.prism' has no attribute '{name}'")
