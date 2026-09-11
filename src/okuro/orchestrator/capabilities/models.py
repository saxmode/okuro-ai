# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Capability model — a reusable building block discovered in a completed task.
# index: imports | class Capability
# AGENT_HEADER_END -->
"""Capability model — a reusable building block discovered in a completed task."""
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional


CAPABILITY_TYPES = {
    "api-client",
    "display-renderer",
    "hardware-bridge",
    "data-pipeline",
    "web-ui",
    "infra-pattern",
    "ai-integration",
    "data-source",
}


@dataclass
class Capability:
    """A reusable building block extracted from a completed task."""

    id: str                              # kebab-case: "pixel-renderer-64x64"
    type: str                            # one of CAPABILITY_TYPES
    name: str                            # human-readable: "64x64 Pixel Renderer"
    description: str                     # what it does (1-2 sentences)
    origin_task: str                     # task ID that first produced this
    origin_project: str                  # project slug (e.g. "sales-display")
    interface: str                       # function signature or API contract
    constraints: list[str] = field(default_factory=list)
    reusable_for: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    discovered_at: str = ""              # ISO timestamp
    confidence: float = 0.5              # 0.0-1.0 how reusable
    last_ideation_at: Optional[str] = None  # when ideas were last generated from this

    def __post_init__(self):
        if not self.discovered_at:
            self.discovered_at = datetime.now(timezone.utc).isoformat()

    def to_dict(self) -> dict:
        """Serialize to dict for YAML persistence."""
        d = {
            "id": self.id,
            "type": self.type,
            "name": self.name,
            "description": self.description,
            "origin_task": self.origin_task,
            "origin_project": self.origin_project,
            "interface": self.interface,
            "constraints": self.constraints,
            "reusable_for": self.reusable_for,
            "tags": self.tags,
            "discovered_at": self.discovered_at,
            "confidence": self.confidence,
        }
        if self.last_ideation_at:
            d["last_ideation_at"] = self.last_ideation_at
        return d

    @classmethod
    def from_dict(cls, data: dict) -> "Capability":
        """Deserialize from YAML dict."""
        return cls(
            id=data["id"],
            type=data.get("type", ""),
            name=data.get("name", data["id"]),
            description=data.get("description", ""),
            origin_task=data.get("origin_task", ""),
            origin_project=data.get("origin_project", ""),
            interface=data.get("interface", ""),
            constraints=data.get("constraints", []),
            reusable_for=data.get("reusable_for", []),
            tags=data.get("tags", []),
            discovered_at=data.get("discovered_at", ""),
            confidence=data.get("confidence", 0.5),
            last_ideation_at=data.get("last_ideation_at"),
        )

    def format_short(self) -> str:
        """One-line summary for prompt injection."""
        return f"- **{self.name}** ({self.type}): {self.description} [{', '.join(self.tags[:3])}]"

    def format_full(self) -> str:
        """Multi-line detail for role prompts."""
        lines = [
            f"### {self.name}",
            f"- **Type:** {self.type}",
            f"- **Interface:** `{self.interface}`",
            f"- **Constraints:** {', '.join(self.constraints) or 'none'}",
            f"- **Reusable for:** {', '.join(self.reusable_for) or 'unspecified'}",
            f"- **Tags:** {', '.join(self.tags)}",
            f"- **Confidence:** {self.confidence:.1f}",
        ]
        return "\n".join(lines)
