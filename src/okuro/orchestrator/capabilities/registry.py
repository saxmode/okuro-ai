# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Capability registry — load, save, search, and manage capabilities.
# index: imports | class CapabilityRegistry
# AGENT_HEADER_END -->
"""Capability registry — load, save, search, and manage capabilities."""
from pathlib import Path
from typing import Optional
import yaml
import logging

from .models import Capability
from okuro.orchestrator.yamlfast import yload

logger = logging.getLogger(__name__)


class CapabilityRegistry:
    """File-based registry for reusable capabilities."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._capabilities: dict[str, Capability] = {}
        self._load()

    def _load(self):
        """Load capabilities from YAML file."""
        if not self.path.exists():
            self._capabilities = {}
            return
        try:
            data = yload(self.path.read_text()) or {}
            caps = data.get("capabilities", [])
            self._capabilities = {
                c["id"]: Capability.from_dict(c) for c in caps
            }
        except Exception as e:
            logger.error("Failed to load capabilities from %s: %s", self.path, e)
            self._capabilities = {}

    def _save(self):
        """Persist capabilities to YAML file."""
        data = {
            "capabilities": [
                cap.to_dict() for cap in sorted(
                    self._capabilities.values(),
                    key=lambda c: c.discovered_at,
                )
            ]
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".yaml.tmp")
        tmp.write_text(yaml.dump(data, default_flow_style=False, sort_keys=False))
        tmp.rename(self.path)

    def add(self, cap: Capability) -> bool:
        """Add or update a capability. Returns True if new, False if updated."""
        is_new = cap.id not in self._capabilities
        self._capabilities[cap.id] = cap
        self._save()
        return is_new

    def add_many(self, caps: list[Capability]) -> int:
        """Add multiple capabilities. Returns count of new ones."""
        new_count = 0
        for cap in caps:
            if cap.id not in self._capabilities:
                new_count += 1
            self._capabilities[cap.id] = cap
        if caps:
            self._save()
        return new_count

    def get(self, cap_id: str) -> Optional[Capability]:
        """Get a capability by ID."""
        return self._capabilities.get(cap_id)

    def all(self) -> list[Capability]:
        """Return all capabilities sorted by discovery time."""
        return sorted(self._capabilities.values(), key=lambda c: c.discovered_at)

    def find_by_type(self, cap_type: str) -> list[Capability]:
        """Find capabilities by type."""
        return [c for c in self._capabilities.values() if c.type == cap_type]

    def find_by_tags(self, tags: list[str]) -> list[Capability]:
        """Find capabilities matching any of the given tags."""
        tag_set = set(t.lower() for t in tags)
        return [
            c for c in self._capabilities.values()
            if tag_set & set(t.lower() for t in c.tags)
        ]

    def search(self, query: str) -> list[Capability]:
        """Fuzzy search across name, description, and tags."""
        terms = query.lower().split()
        results = []
        for cap in self._capabilities.values():
            searchable = f"{cap.name} {cap.description} {' '.join(cap.tags)} {' '.join(cap.reusable_for)}".lower()
            score = sum(1 for t in terms if t in searchable)
            if score > 0:
                results.append((score, cap))
        results.sort(key=lambda x: x[0], reverse=True)
        return [cap for _, cap in results]

    def unharvested_since(self, since_iso: Optional[str] = None) -> list[Capability]:
        """Find capabilities that haven't been used for idea generation yet."""
        return [
            c for c in self._capabilities.values()
            if c.last_ideation_at is None or (since_iso and c.last_ideation_at < since_iso)
        ]

    def mark_ideated(self, cap_ids: list[str], timestamp: str):
        """Mark capabilities as having been used for idea generation."""
        changed = False
        for cap_id in cap_ids:
            if cap_id in self._capabilities:
                self._capabilities[cap_id].last_ideation_at = timestamp
                changed = True
        if changed:
            self._save()

    def format_for_prompt(self, max_caps: int = 20) -> str:
        """Format capabilities for injection into LLM prompts."""
        caps = self.all()[:max_caps]
        if not caps:
            return "_No capabilities registered yet._"
        return "\n".join(cap.format_short() for cap in caps)

    def format_relevant(self, query: str, max_caps: int = 5) -> str:
        """Format capabilities relevant to a query for role prompt injection.

        Emits POINTERS (name + type + interface), not full cards.

        ``search`` scores by counting query terms anywhere across
        name/description/tags/reusable_for, which is a weak filter: a
        person-registration subtask matched "Rackmount Case Constraint
        Specification". Injecting ``format_full`` for five such matches spent
        ~3.9 KB of every single spawn on cards that are mostly irrelevant.

        What a subagent needs to REUSE a capability is its interface
        signature — that is retained verbatim. Constraints / reusable-for /
        tags / confidence are advisory prose that only matters once the agent
        has decided a capability is on its critical path, and they are one
        file read away (``capabilities.yaml``). This is the same
        progressive-disclosure shape the role-skill cards already use.
        """
        relevant = self.search(query)[:max_caps]
        if not relevant:
            return ""
        lines = ["## Reusable Capabilities (pointers)\n"]
        for cap in relevant:
            lines.append(f"- **{cap.name}** ({cap.type}) — `{cap.interface}`")
        lines.append(
            f"\nFull records (constraints, reuse notes) live in `{self.path}` — "
            "read it only for a capability you have decided to reuse. "
            "If your work creates a new reusable capability, document its "
            "interface clearly in your artifact so it can be harvested."
        )
        return "\n".join(lines)

    @property
    def count(self) -> int:
        return len(self._capabilities)
