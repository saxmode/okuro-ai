# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Tree-sitter code-graph ingestor — extracts structural facts (imports, defines, calls, inherits) per file for the okuro KG.
# index:
#   from .models
#   from .ingestor
# AGENT_HEADER_END -->
"""Tree-sitter code-graph ingestor for cortex.

Produces stable JSON facts consumed by KG (sense/kg.py) and the
architectural-layer classifier.
"""

from .models import CodeFacts, Symbol, ImportEdge, CallEdge, InheritEdge
from .ingestor import (
    TreesitterIngestor,
    ingest_file,
    ingest_project,
    discover_project_files,
    AVAILABLE_LANGUAGES,
)
from .architecture import (
    DEFAULT_LAYERS,
    LayerClassifier,
    LayerRule,
    classify_file,
    classify_project,
    write_layers_to_kg,
)
from .insights import (
    god_nodes,
    detect_communities,
    write_communities_to_kg,
    insights_summary,
    index_audit,
)
from .tiers import tier_of, classify_edge, TIER_CONFIDENCE
from .crossrepo import (
    build_cross_repo_graph,
    cross_repo_connections,
    cross_repo_search,
    cross_repo_insights,
    anchor_index,
    cross_repo_index,
)

__all__ = [
    "CodeFacts",
    "Symbol",
    "ImportEdge",
    "CallEdge",
    "InheritEdge",
    "TreesitterIngestor",
    "ingest_file",
    "ingest_project",
    "discover_project_files",
    "AVAILABLE_LANGUAGES",
    "DEFAULT_LAYERS",
    "LayerClassifier",
    "LayerRule",
    "classify_file",
    "classify_project",
    "write_layers_to_kg",
    "god_nodes",
    "detect_communities",
    "write_communities_to_kg",
    "insights_summary",
    "index_audit",
    "tier_of",
    "classify_edge",
    "TIER_CONFIDENCE",
    "build_cross_repo_graph",
    "cross_repo_connections",
    "cross_repo_search",
    "cross_repo_insights",
    "anchor_index",
    "cross_repo_index",
]
