# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: okuro.assets (資) — the brand "assets" leg: icons now, illustrations later.
# index: none
# AGENT_HEADER_END -->
"""okuro.assets — asset providers for the brand assets leg.

A brand composes design + stack + principles + **assets**. This package owns
the asset providers. `okuro.assets.icons` is the first: a native Python icon
search engine (FTS5 + sqlite-vec, embeddings via okuro.embed) over a licensed
icon library that lives in a separate data-dir SQLite file (never in the repo).

Future: `okuro.assets.illustrations` follows the same shape.
"""
