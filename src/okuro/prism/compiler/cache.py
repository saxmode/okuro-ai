# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: okuro·prism compiler STAGE CACHE — content-hashed intermediate results
#   so a build can resume --from <stage> and `retailor` can re-run profile-onward
#   without re-mining. A stage's cache key is the hash of its stage name + the
#   content of its inputs; identical inputs => cache hit => stage skipped.
# index: StageCache | key_for | get | put | invalidate_from | STAGE_ORDER
# AGENT_HEADER_END -->
"""Content-hashed per-stage IR cache.

Deterministic key = ``sha256(stage_name || canonical_json(inputs))``. Because the
key is derived from CONTENT, resuming is automatic: if a stage's inputs are byte
-identical to a prior run, its cached output is reused; if any upstream output
changed, the hash changes and the stage recomputes. ``--from <stage>`` is
implemented as ``invalidate_from`` (drop that stage and everything downstream so
they recompute even on identical inputs — e.g. to re-roll a non-deterministic
LLM stage). ``retailor`` invalidates from ``profile`` (mine output is kept).

Storage is a flat directory of ``<stage>.<key12>.json`` files under a per-deck
cache root; no DB, no locking assumptions (single-writer per deck build).
"""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Canonical stage order — the dependency chain. `invalidate_from(s)` drops s and
# everything after it.
STAGE_ORDER: tuple[str, ...] = (
    "mine", "profile", "project", "outline", "author", "plan", "compose",
)


def _canonical(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _hash(*parts: str) -> str:
    h = hashlib.sha256()
    for p in parts:
        h.update(p.encode("utf-8"))
        h.update(b"\x1f")
    return h.hexdigest()


class StageCache:
    """A directory-backed cache of stage IRs for one deck build."""

    def __init__(self, root: str | Path, *, order: tuple[str, ...] = STAGE_ORDER):
        self.root = Path(root)
        # The node chain `invalidate_from` walks. Defaults to the compiler's
        # STAGE_ORDER; the top-down workflow passes its own node order so ONE
        # cache mechanism serves both pipelines instead of each growing its own.
        self.order = tuple(order)
        self.root.mkdir(parents=True, exist_ok=True)

    # ── keys ────────────────────────────────────────────────────────────────
    def key_for(self, stage: str, inputs: Any) -> str:
        """Content hash for ``stage`` given its ``inputs`` (any JSON-able IR)."""
        return _hash(stage, _canonical(inputs))

    def _path(self, stage: str, key: str) -> Path:
        return self.root / f"{stage}.{key[:12]}.json"

    # ── get / put ───────────────────────────────────────────────────────────
    def get(self, stage: str, inputs: Any) -> Optional[Any]:
        key = self.key_for(stage, inputs)
        p = self._path(stage, key)
        if not p.exists():
            return None
        try:
            payload = json.loads(p.read_text())
        except (ValueError, OSError) as exc:
            logger.warning("cache: unreadable %s (%s) — miss", p.name, exc)
            return None
        logger.info("cache HIT %s (%s)", stage, key[:12])
        return payload["output"]

    def put(self, stage: str, inputs: Any, output: Any) -> str:
        key = self.key_for(stage, inputs)
        p = self._path(stage, key)
        p.write_text(_canonical({"stage": stage, "key": key, "output": output}))
        logger.info("cache PUT %s (%s)", stage, key[:12])
        return key

    # ── resume control ────────────────────────────────────────────────────────
    def invalidate_from(self, stage: str) -> list[str]:
        """Drop cached outputs for ``stage`` and every downstream stage.

        Returns the list of stages cleared. Used by ``--from`` (re-run this stage
        onward) and ``retailor`` (``invalidate_from('profile')``).
        """
        if stage not in self.order:
            raise ValueError(f"unknown stage {stage!r}; expected one of {self.order}")
        idx = self.order.index(stage)
        cleared: list[str] = []
        for s in self.order[idx:]:
            removed = False
            for f in self.root.glob(f"{s}.*.json"):
                f.unlink()
                removed = True
            if removed:
                cleared.append(s)
        logger.info("cache invalidate_from %s -> cleared %s", stage, cleared)
        return cleared

    def clear(self) -> None:
        for f in self.root.glob("*.json"):
            f.unlink()
