# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: okuro.embed (埋込) — embeddings.
# index: none
# AGENT_HEADER_END -->
"""okuro.embed (埋込) — embeddings."""

from .client import embed, embed_one

__all__ = ["embed", "embed_one"]
