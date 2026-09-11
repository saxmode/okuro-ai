# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Managed-corpus package — register non-git document sources
#   (Confluence spaces, local folders) as projects → cortex roots, so they are
#   searchable through the existing cortex_* tools.
# index: from paths | from lifecycle | from adapters
# AGENT_HEADER_END -->
"""Managed-corpus lifecycle package.

The non-git sibling of ``okuro.repos``. Public surface re-exported for MCP
tools and the API router:

  corpora_root, corpus_path        — portable path resolution (paths.py)
  add_corpus, sync_corpus,         — lifecycle operations (lifecycle.py)
  remove_corpus, list_corpora, get_corpus
  adapter_types, describe_types    — the source registry (adapters/)
"""

from okuro.corpus.paths import corpora_root, corpus_path
from okuro.corpus.adapters import adapter_types, build_adapter, describe_types
from okuro.corpus.lifecycle import (
    add_corpus,
    sync_corpus,
    remove_corpus,
    list_corpora,
    get_corpus,
)

__all__ = [
    "corpora_root",
    "corpus_path",
    "adapter_types",
    "build_adapter",
    "describe_types",
    "add_corpus",
    "sync_corpus",
    "remove_corpus",
    "list_corpora",
    "get_corpus",
]
