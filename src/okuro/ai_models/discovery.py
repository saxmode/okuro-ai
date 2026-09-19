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

#: Every request identifies okuro.
#:
#: THIS IS WHY CIVITAI DISCOVERY HAS ALWAYS RETURNED ZERO. Civitai sits behind
#: Cloudflare, and Cloudflare answers the default `Python-urllib/3.x` agent
#: with 403 and `error code: 1010` — a browser-integrity block, before any
#: credential is even looked at. Measured 2026-09-15 against the live endpoint:
#: identical URL and identical Bearer token, 403 with the default agent and 200
#: with `curl/8.5.0`, `Mozilla/5.0` or this string. A prior session recorded the
#: symptom as "Civitai needs a token"; the token was never the cause, and adding
#: one changed nothing because the request never reached the API.
_USER_AGENT = "okuro/3 (+model-discovery)"


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
    hdrs = {"User-Agent": _USER_AGENT, **(headers or {})}
    req = urllib.request.Request(url, method="GET", headers=hdrs)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except (urllib.error.URLError, urllib.error.HTTPError, ValueError, OSError):
        return None


#: Fields requested from the HF list endpoint.
#:
#: `expand` REPLACES `full=true` rather than adding to it — measured against
#: the live API on 2026-09-15, a request carrying both returns only the
#: expanded fields. So this list must name everything `catalog.from_hf_model`
#: reads, and it does. The reason to move off `full=true` at all is
#: `safetensors`: it is the only place HF publishes an exact parameter count
#: and dtype breakdown on a LIST response, and without it every vision, 3d,
#: music and video candidate arrives with no footprint and is dropped by the
#: runnability gate before anyone sees it.
_HF_EXPAND = (
    "safetensors", "cardData", "tags", "library_name", "sha", "lastModified",
    "downloads", "likes", "trendingScore", "createdAt", "gated", "pipeline_tag",
    "author",
)


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
        "expand": list(_HF_EXPAND),
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
    # P4 — closes interpretation gap (e): vision and 3d had no source at all,
    # so `discover(modality='3d')` fell through to querying both sources with
    # no pipeline hint and returned the generic text firehose.
    "vision": ("hf",),
    "3d": ("hf",),
}

# modality → HF pipeline_tag hint. Every tag verified live 2026-09-15.
_MODALITY_HF_PIPELINE = {
    "text": "text-generation",
    "embedding": "feature-extraction",
    "audio": "text-to-speech",
    "image": "text-to-image",
    "video": "image-to-video",
    "vision": "image-text-to-text",
    "3d": "image-to-3d",
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


# --- the wanted categories --------------------------------------------------
#
# Interpretation gap (e): the modalities okuro SCANNED (text, embedding, audio,
# image, video) were not the categories the user WANTS, and vision and 3d had
# no scanner at all despite both being installed on this host. This table is
# that list, and it is config-declarable so a bucket can be added or retuned
# without a release.
#
# One entry = one source query. Fields:
#   name          the category, and what lands in model_discoveries.category
#   source        "hf" | "civitai"
#   modality      what rank() filters on; omit to take the source's own verdict
#   pipeline_tag  hf only — a string or a list, each fetched and merged
#   query         free-text search, both sources
#   types / nsfw  civitai only
#   requires_tags keep only candidates carrying one of these variant tags
#   min_downloads override the reputation floor for THIS bucket only
#
# `requires_tags` is NOT the drop ruling 3 forbids: it keeps a dedicated bucket
# on topic. Nothing it excludes is lost, because the plain bucket for the same
# modality runs with no tag filter at all and the toggle is what hides a
# variant at READ time.
#
# ORDER IS MEANINGFUL: the first category that publishes a candidate in a run
# owns it, so a variant model that the generic query happens to surface lands
# in the plain bucket with its tags on it rather than being written twice.
CATEGORIES_CONVENTION_KEY = "ai_models.discovery_categories"

DEFAULT_CATEGORIES: tuple[dict, ...] = (
    {"name": "text", "source": "hf", "modality": "text",
     "pipeline_tag": "text-generation"},
    {"name": "text-abliterated", "source": "hf", "modality": "text",
     "pipeline_tag": "text-generation", "query": "abliterated",
     "requires_tags": ["abliterated", "uncensored"], "min_downloads": 100},
    {"name": "image", "source": "hf", "modality": "image",
     "pipeline_tag": "text-to-image"},
    {"name": "image-nsfw", "source": "civitai", "modality": "image",
     "types": ["Checkpoint"], "nsfw": True, "requires_tags": ["nsfw"]},
    {"name": "video", "source": "hf", "modality": "video",
     "pipeline_tag": ["image-to-video", "text-to-video"]},
    # Civitai has no video TYPE and its nsfw video work is almost all LoRAs
    # over a base video model, not checkpoints. Measured against the live API
    # 2026-09-15 with types=[LORA, Checkpoint] and nsfw=true: query='wan' gives
    # 19 video candidates of which 18 survive the gate tagged nsfw,
    # query='hunyuan video' gives 7, and query='video' gives 1 — the search is
    # token-based and the base-model name is what the titles actually carry.
    # Retune this in config when the dominant video family changes.
    {"name": "video-nsfw", "source": "civitai", "modality": "video",
     "types": ["LORA", "Checkpoint"], "nsfw": True, "query": "wan",
     "requires_tags": ["nsfw"]},
    # text-to-audio is where the music generators live (YuE, MiniMax-Music,
    # stable-audio); audio-to-audio carries the stem/remix models. Both
    # verified against the live HF API 2026-09-15.
    {"name": "music", "source": "hf", "modality": "audio",
     "pipeline_tag": ["text-to-audio", "audio-to-audio"], "min_downloads": 500},
    {"name": "vision", "source": "hf", "modality": "vision",
     "pipeline_tag": "image-text-to-text"},
    {"name": "3d", "source": "hf", "modality": "3d",
     "pipeline_tag": ["image-to-3d", "text-to-3d"], "min_downloads": 100},
    {"name": "embedding", "source": "hf", "modality": "embedding",
     "pipeline_tag": ["feature-extraction", "sentence-similarity"]},
)


def categories() -> list[dict]:
    """The wanted categories — the host's override, else the shipped default.

    A malformed override falls back to the default rather than scanning
    nothing: a typo in a config key must not silently turn the weekly scan off.
    """
    try:
        from okuro.yu.conventions import get_convention

        raw = get_convention(CATEGORIES_CONVENTION_KEY, None)
    except Exception:
        raw = None
    if not raw:
        return [dict(c) for c in DEFAULT_CATEGORIES]
    if not isinstance(raw, list) or not all(
            isinstance(c, dict) and c.get("name") for c in raw):
        import logging

        logging.getLogger("okuro.ai_models.discovery").warning(
            "%s must be a list of {name, source, ...} — using the shipped default",
            CATEGORIES_CONVENTION_KEY)
        return [dict(c) for c in DEFAULT_CATEGORIES]
    return [dict(c) for c in raw]


def search_category(cat: dict, *, limit: int = 20, sort: str = "trending",
                    storage=None,
                    search_hf_fn=None, search_civitai_fn=None) -> list[CatalogEntry]:
    """Fetch one category from its declared source. Deduped by catalog_id.

    Gathering only — the gate is :func:`catalog.rank` and the tag filter is the
    caller's, so this function can be checked against a live source without a
    database or a ranking policy in the way.
    """
    hf_fn = search_hf_fn or search_hf
    civitai_fn = search_civitai_fn or search_civitai
    sortmap = _SORT_MAP.get(sort, _SORT_MAP["popular"])
    query = cat.get("query") or None
    seen: set[str] = set()
    out: list[CatalogEntry] = []

    if (cat.get("source") or "hf") == "civitai":
        found = civitai_fn(query, types=cat.get("types") or None, limit=limit,
                           sort=sortmap["civitai"], nsfw=bool(cat.get("nsfw")),
                           storage=storage)
        batches = [found]
    else:
        tags = cat.get("pipeline_tag")
        tags = tags if isinstance(tags, (list, tuple)) else [tags]
        batches = [hf_fn(query, pipeline_tag=t or None, limit=limit,
                         sort=sortmap["hf"], storage=storage) for t in tags]

    for found in batches:
        for e in found or []:
            if e.catalog_id in seen:
                continue
            seen.add(e.catalog_id)
            out.append(e)
    return out


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
