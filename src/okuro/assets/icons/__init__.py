# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: okuro.assets.icons — native icon search engine (FTS5 + sqlite-vec + okuro.embed).
# index: none
# AGENT_HEADER_END -->
"""okuro.assets.icons — the icon provider for the brand assets leg.

Ported from tm-icon-manager (TypeScript/Electron) to pure Python:
  - db.py       open the licensed library DB, load sqlite-vec, schema
  - search.py   hybrid FTS5 + vector search with RRF fusion (set-filter in SQL)
  - service.py  search_icons / get_icon / list_sets / list_tags / library_stats
  - ingest.py   import a library pack, re-embed icons with okuro.embed

The icon LIBRARY (35k SVGs + vectors) is a separate data-dir SQLite file —
licensed, never committed. Vectors are produced by okuro.embed's active tier
model so query and document embeddings share one space.
"""
