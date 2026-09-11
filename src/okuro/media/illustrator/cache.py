# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: provides scene caching functionality to skip LLM calls for repeat topics
# index: imports | def _key | def cache_path | def load_or_compose | def invalidate
# AGENT_HEADER_END -->
"""Scene cache. Skip the LLM call on repeat topics."""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path

from .scene import Scene
from .compose import compose_via_bridge

CACHE_ROOT = Path(
    os.environ.get("TM_ILL_CACHE",
                   str(Path.home() / ".cache" / "tm-illustrator" / "scenes"))
)


def _key(topic: str, category: str | None, seed: int | None) -> str:
    payload = json.dumps(
        {"topic": topic, "category": category or "", "seed": seed},
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode()).hexdigest()[:24]


def cache_path(topic: str, category: str | None = None,
               seed: int | None = None) -> Path:
    return CACHE_ROOT / f"{_key(topic, category, seed)}.json"


def load_or_compose(topic: str, category: str | None = None,
                    seed: int | None = None,
                    force: bool = False) -> tuple[Scene, bool]:
    """Return (scene, cache_hit)."""
    p = cache_path(topic, category, seed)
    if p.exists() and not force:
        try:
            return Scene.model_validate_json(p.read_text()), True
        except Exception:
            pass  # fall through and recompose
    scene = compose_via_bridge(topic, category=category, seed=seed)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(scene.model_dump_json(indent=2))
    return scene, False


def invalidate(topic: str, category: str | None = None,
               seed: int | None = None) -> bool:
    p = cache_path(topic, category, seed)
    if p.exists():
        p.unlink()
        return True
    return False
