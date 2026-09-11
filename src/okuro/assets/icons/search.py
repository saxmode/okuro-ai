# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Pure search helpers (FTS query build + RRF fusion) — testable without a DB.
# index: imports | RRF_K | build_fts_query | rrf_fuse
# AGENT_HEADER_END -->
"""Pure search helpers, ported from tm-icon-manager src/mcp/hybrid.ts.

Kept side-effect-free so ranking logic can be unit-tested without opening a
library. The DB-bound matching + orchestration lives in service.py.
"""

from __future__ import annotations

import re

# Reciprocal-rank-fusion constant. Matches the TS engine (hybrid.ts RRF_K=60)
# so ported behavior is identical: contribution of a list at rank i is
# 1 / (RRF_K + i + 1).
RRF_K = 60

_TOKEN_SPLIT = re.compile(r"\s+")


def build_fts_query(raw: str) -> str:
    """Turn a raw phrase into an FTS5 prefix-OR query.

    "user profile" -> "user* OR profile*". Quotes are stripped (they would
    otherwise be parsed as FTS5 phrase delimiters). Empty input -> "".
    """
    cleaned = raw.replace('"', " ").replace("'", " ").strip()
    if not cleaned:
        return ""
    tokens = [f"{t}*" for t in _TOKEN_SPLIT.split(cleaned) if t]
    return " OR ".join(tokens)


def rrf_fuse(
    fts: list[tuple[str, float]],
    vec: list[tuple[str, float]],
) -> dict[str, dict]:
    """Reciprocal-rank fusion of two ranked lists keyed by icon_id.

    Args:
        fts: [(icon_id, bm25_score)] in BM25 order (best first).
        vec: [(icon_id, distance)]   in KNN order  (nearest first).

    Returns: {icon_id: {"score", "vec_score", "fts_score"}}.
    score is the fused RRF score; vec_score/fts_score are the raw per-engine
    values for display (None if the icon wasn't in that list).
    """
    fused: dict[str, dict] = {}
    for idx, (icon_id, bm25) in enumerate(fts):
        cur = fused.setdefault(icon_id, {"score": 0.0, "vec_score": None, "fts_score": None})
        cur["score"] += 1.0 / (RRF_K + idx + 1)
        cur["fts_score"] = bm25
    for idx, (icon_id, distance) in enumerate(vec):
        cur = fused.setdefault(icon_id, {"score": 0.0, "vec_score": None, "fts_score": None})
        cur["score"] += 1.0 / (RRF_K + idx + 1)
        # cosine distance in [0,2] -> similarity in [-1,1] (display only;
        # fusion uses rank, not this value).
        cur["vec_score"] = 1.0 - distance / 2.0
    return fused
