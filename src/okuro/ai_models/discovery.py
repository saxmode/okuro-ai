# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: OSS model discovery — query HF Hub + Civitai into CatalogEntry list.
# index:
#   imports
#   def _get_json
#   def search_hf
#   def search_civitai
#   def discover
# AGENT_HEADER_END -->
"""Discovery clients — turn a user's intent into ranked-able CatalogEntry
records by querying HuggingFace Hub and Civitai.

Best-effort + model-agnostic: network failures return [] (never raise), auth
is optional (anonymous discovery works; a token only lifts rate limits and
unlocks gated models). All HTTP goes through ``_get_json`` so callers/tests
can substitute canned payloads without live calls.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Optional

from .catalog import CatalogEntry, from_civitai_model, from_hf_model
from .credentials import auth_header

_HF_API = "https://huggingface.co/api/models"
_CIVITAI_API = "https://civitai.com/api/v1/models"


def _get_json(
    url: str,
    params: Optional[dict] = None,
    headers: Optional[dict] = None,
    timeout: int = 15,
) -> Any:
    """GET JSON. Returns parsed body, or None on any error (best-effort)."""
    if params:
        # Civitai accepts repeated + spaces; urlencode with quote_via handles both.
        url = f"{url}?{urllib.parse.urlencode(params, doseq=True)}"
    req = urllib.request.Request(url, method="GET", headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except (urllib.error.URLError, urllib.error.HTTPError, ValueError, OSError):
        return None


def search_hf(
    query: Optional[str] = None,
    *,
    pipeline_tag: Optional[str] = None,
    limit: int = 20,
    sort: str = "downloads",
    storage=None,
) -> list[CatalogEntry]:
    """Search HuggingFace Hub → CatalogEntry list (empty on failure)."""
    params: dict[str, Any] = {
        "limit": limit,
        "sort": sort,
        "direction": -1,
        "full": "true",
    }
    if query:
        params["search"] = query
    if pipeline_tag:
        params["pipeline_tag"] = pipeline_tag
    data = _get_json(_HF_API, params, headers=auth_header("huggingface", storage))
    if not isinstance(data, list):
        return []
    return [from_hf_model(m) for m in data if isinstance(m, dict)]


def search_civitai(
    query: Optional[str] = None,
    *,
    types: Optional[list[str]] = None,
    limit: int = 20,
    sort: str = "Most Downloaded",
    nsfw: bool = False,
    storage=None,
) -> list[CatalogEntry]:
    """Search Civitai → CatalogEntry list (empty on failure)."""
    params: dict[str, Any] = {"limit": limit, "sort": sort, "nsfw": str(nsfw).lower()}
    if query:
        params["query"] = query
    if types:
        params["types"] = types  # doseq → repeated ?types=Checkpoint&types=LORA
    data = _get_json(_CIVITAI_API, params, headers=auth_header("civitai", storage))
    if not isinstance(data, dict):
        return []
    items = data.get("items")
    if not isinstance(items, list):
        return []
    return [from_civitai_model(m) for m in items if isinstance(m, dict)]


def get_hf_model(repo_id: str, storage=None) -> Optional[CatalogEntry]:
    """Fetch one HF model's detail → CatalogEntry (None on failure)."""
    data = _get_json(
        f"{_HF_API}/{repo_id}",
        {"full": "true"},
        headers=auth_header("huggingface", storage),
    )
    return from_hf_model(data) if isinstance(data, dict) else None


def get_civitai_model(model_id: str | int, storage=None) -> Optional[CatalogEntry]:
    """Fetch one Civitai model's detail → CatalogEntry (None on failure)."""
    data = _get_json(
        f"{_CIVITAI_API}/{model_id}", headers=auth_header("civitai", storage)
    )
    return from_civitai_model(data) if isinstance(data, dict) else None


def resolve_entry(catalog_id: str, storage=None) -> Optional[CatalogEntry]:
    """Resolve a '<source>:<ref>' catalog_id to a CatalogEntry via detail fetch."""
    if ":" not in catalog_id:
        # bare repo id → assume HF
        return get_hf_model(catalog_id, storage)
    source, ref = catalog_id.split(":", 1)
    if source == "huggingface":
        return get_hf_model(ref, storage)
    if source == "civitai":
        return get_civitai_model(ref, storage)
    return None


# modality → which source(s) to query
_MODALITY_SOURCES = {
    "text": ("hf",),
    "embedding": ("hf",),
    "audio": ("hf",),
    "image": ("civitai", "hf"),
    "video": ("civitai", "hf"),
}

# modality → HF pipeline_tag hint
_MODALITY_HF_PIPELINE = {
    "text": "text-generation",
    "embedding": "feature-extraction",
    "audio": "text-to-speech",
    "image": "text-to-image",
}


# Discovery sort mode → per-source sort key. "trending" is the "new AND
# broadly reviewed" feed (HF trendingScore / Civitai Highest Rated) — models
# gaining traction now, which is what the weekly scan wants: fresh releases
# people are actually adopting, not the pure-recency firehose ("new" =
# createdAt, mostly test uploads) nor the all-time leaderboard ("popular").
_SORT_MAP = {
    "popular": {"hf": "downloads", "civitai": "Most Downloaded"},
    "trending": {"hf": "trendingScore", "civitai": "Highest Rated"},
    "new": {"hf": "createdAt", "civitai": "Newest"},
}


def discover(
    query: Optional[str] = None,
    *,
    modality: Optional[str] = None,
    limit: int = 20,
    sort: str = "popular",
    storage=None,
) -> list[CatalogEntry]:
    """Unified discovery across the sources appropriate for ``modality``.

    Routes by modality (text→HF, image→Civitai+HF); dedupes by catalog_id.
    ``sort='new'`` surfaces recently-published models instead of the popular
    default. Ranking (catalog.rank) is a separate concern — this only gathers.
    """
    sources = _MODALITY_SOURCES.get(modality or "", ("hf", "civitai"))
    sortmap = _SORT_MAP.get(sort, _SORT_MAP["popular"])
    seen: set[str] = set()
    out: list[CatalogEntry] = []
    for src in sources:
        if src == "hf":
            found = search_hf(
                query,
                pipeline_tag=_MODALITY_HF_PIPELINE.get(modality or ""),
                limit=limit,
                sort=sortmap["hf"],
                storage=storage,
            )
        else:
            found = search_civitai(query, limit=limit, sort=sortmap["civitai"], storage=storage)
        for e in found:
            if e.catalog_id in seen:
                continue
            seen.add(e.catalog_id)
            out.append(e)
    return out
