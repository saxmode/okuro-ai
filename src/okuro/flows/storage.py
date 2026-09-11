# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Flow YAML storage — list/get/save/delete under ~/.okuro/flows/.
# index:
#   imports
#   class RolePlacement
#   class OrchestratorPlacement
#   class Flow
#   def slugify
#   def list_flows
#   def get_flow
#   def save_flow
#   def delete_flow
# AGENT_HEADER_END -->
"""Flow YAML storage — list/get/save/delete under ``~/.okuro/flows/``.

Flow topology is a **star**: one orchestrator node at the center, N role
cards linked to it. Placing a role on the canvas = linking it (no orphans).
The decomposer treats every role in a flow as ``required_roles`` — each
must contribute at least one subtask. Flows are optional; the orchestrator
works fine without one.
"""

from __future__ import annotations

import logging
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import yaml
from okuro.db.engine import okuro_home

logger = logging.getLogger("okuro.flows")

FLOWS_DIR = okuro_home() / "flows"

DEFAULT_ORCHESTRATOR_X = 60.0
DEFAULT_ORCHESTRATOR_Y = 60.0


@dataclass
class RolePlacement:
    role_id: str
    x: float = 0.0
    y: float = 0.0


@dataclass
class OrchestratorPlacement:
    x: float = DEFAULT_ORCHESTRATOR_X
    y: float = DEFAULT_ORCHESTRATOR_Y


@dataclass
class Flow:
    id: str
    name: str
    description: str = ""
    orchestrator: OrchestratorPlacement = field(default_factory=OrchestratorPlacement)
    roles: list[RolePlacement] = field(default_factory=list)
    created_at: str = ""
    updated_at: str = ""

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "orchestrator": asdict(self.orchestrator),
            "roles": [asdict(r) for r in self.roles],
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: dict) -> Flow:
        roles_raw = data.get("roles") or []
        roles: list[RolePlacement] = []
        for r in roles_raw:
            if isinstance(r, str):
                roles.append(RolePlacement(role_id=r))
            elif isinstance(r, dict):
                roles.append(
                    RolePlacement(
                        role_id=r.get("role_id") or r.get("id") or "",
                        x=float(r.get("x", 0.0)),
                        y=float(r.get("y", 0.0)),
                    )
                )
        orch_raw = data.get("orchestrator") or {}
        orchestrator = OrchestratorPlacement(
            x=float(orch_raw.get("x", DEFAULT_ORCHESTRATOR_X)),
            y=float(orch_raw.get("y", DEFAULT_ORCHESTRATOR_Y)),
        )
        return cls(
            id=data.get("id", ""),
            name=data.get("name", ""),
            description=data.get("description", ""),
            orchestrator=orchestrator,
            roles=[r for r in roles if r.role_id],
            created_at=data.get("created_at", ""),
            updated_at=data.get("updated_at", ""),
        )


_SLUG_RE = re.compile(r"[^a-z0-9]+")


def slugify(text: str) -> str:
    s = _SLUG_RE.sub("-", text.strip().lower()).strip("-")
    return s or "flow"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _flow_path(flow_id: str) -> Path:
    return FLOWS_DIR / f"{flow_id}.yaml"


def list_flows() -> list[Flow]:
    if not FLOWS_DIR.exists():
        return []
    flows: list[Flow] = []
    for yaml_file in sorted(FLOWS_DIR.glob("*.yaml")):
        try:
            with open(yaml_file) as f:
                data = yaml.safe_load(f) or {}
            if not data.get("id"):
                data["id"] = yaml_file.stem
            flows.append(Flow.from_dict(data))
        except Exception as e:
            logger.warning("Failed to load flow %s: %s", yaml_file, e)
    flows.sort(key=lambda f: f.updated_at or f.created_at, reverse=True)
    return flows


def get_flow(flow_id: str) -> Flow | None:
    path = _flow_path(flow_id)
    if not path.is_file():
        return None
    try:
        with open(path) as f:
            data = yaml.safe_load(f) or {}
        data.setdefault("id", flow_id)
        return Flow.from_dict(data)
    except Exception as e:
        logger.warning("Failed to load flow %s: %s", flow_id, e)
        return None


def save_flow(flow: Flow) -> Flow:
    if not flow.id:
        flow.id = slugify(flow.name or "flow")
    FLOWS_DIR.mkdir(parents=True, exist_ok=True)

    now = _now_iso()
    existing = get_flow(flow.id)
    if existing and existing.created_at:
        flow.created_at = existing.created_at
    if not flow.created_at:
        flow.created_at = now
    flow.updated_at = now

    path = _flow_path(flow.id)
    tmp = path.with_suffix(".yaml.tmp")
    with open(tmp, "w") as f:
        yaml.dump(flow.to_dict(), f, default_flow_style=False, sort_keys=False)
    tmp.replace(path)
    return flow


def delete_flow(flow_id: str) -> bool:
    path = _flow_path(flow_id)
    if not path.is_file():
        return False
    try:
        path.unlink()
        return True
    except Exception as e:
        logger.warning("Failed to delete flow %s: %s", flow_id, e)
        return False
