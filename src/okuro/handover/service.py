# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: okuro-handover service — the thin orchestration over IR + registry.
#   list eligible targets for a selection, then validate + produce a handover.
#   Shared by the MCP surface, the REST router, and any agent.
# index:
#   def targets_for / def contract_for
#   class HandoverError
#   def handover
# AGENT_HEADER_END -->
"""okuro·handover — resolve-and-produce.

Two calls the whole feature rests on:

  ``targets_for(ir)``  → which tools this selection can go to, + their intake
                          contracts (so the UI knows what to ask).
  ``handover(ir, target, inputs)`` → validate the required ``ask`` fields are
                          satisfied, then run the target's producer.
"""

from __future__ import annotations

from typing import Any

from okuro.handover import registry
from okuro.handover.ir import ContentIR


class HandoverError(ValueError):
    """A handover could not proceed (unknown target, wrong kind, missing field)."""


def targets_for(ir: ContentIR) -> list[dict[str, Any]]:
    return registry.public_contract(ir.kind)


def contract_for(target_id: str) -> dict[str, Any]:
    t = registry.get_target(target_id)
    if not t:
        raise HandoverError(f"unknown target '{target_id}'")
    return t.public()


def _missing_required(target, inputs: dict) -> list[str]:
    """Names of ``ask`` (non-optional) fields with no usable value."""
    missing: list[str] = []
    for f in target.requires:
        if f.mode != "ask":
            continue
        val = inputs.get(f.key)
        if f.type == "recipient":
            ok = isinstance(val, dict) and val.get("kind") and val.get("id")
        else:
            ok = bool(val)
        if not ok:
            missing.append(f.key)
    return missing


def handover(ir: ContentIR, target_id: str, inputs: dict = None) -> dict[str, Any]:
    inputs = inputs or {}
    target = registry.get_target(target_id)
    if not target:
        raise HandoverError(f"unknown target '{target_id}'")
    if ir.kind not in target.accepts:
        raise HandoverError(
            f"target '{target_id}' does not accept '{ir.kind}' content "
            f"(accepts: {', '.join(target.accepts)})")
    missing = _missing_required(target, inputs)
    if missing:
        raise HandoverError(
            f"target '{target_id}' needs: {', '.join(missing)}")
    result = target.produce(ir, inputs)
    result.setdefault("success", True)
    return result
