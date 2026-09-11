# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: /api/handover router — the HTTP surface the frontend HandoverDialog
#   uses. `targets` returns the reachable tools + intake contract for a selection;
#   `send` produces the destination doc. Thin wrapper over okuro.handover.
# index: imports | router | models | targets | send
# AGENT_HEADER_END -->
"""okuro·handover API — discover targets, then produce a handover.

Two POSTs mirroring the MCP surface (``handover_targets`` / ``handover_send``):

  POST /api/handover/targets  {content}          → {kind, targets:[{id,label,fields}]}
  POST /api/handover/send      {content, target, inputs} → {success, url, id, …}

Auth is the global bearer middleware, same as every other /api router.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

logger = logging.getLogger("okuro.orchestrator.api.handover")

router = APIRouter(prefix="/api/handover", tags=["handover"])


class TargetsBody(BaseModel):
    content: dict[str, Any]


class SendBody(BaseModel):
    content: dict[str, Any]
    target: str
    inputs: Optional[dict[str, Any]] = None


@router.post("/targets")
def handover_targets(body: TargetsBody) -> dict:
    from okuro.handover import ContentIR, targets_for

    try:
        ir = ContentIR.from_dict(body.content)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return {"kind": ir.kind, "targets": targets_for(ir)}


@router.post("/send")
def handover_send(body: SendBody) -> dict:
    from okuro.handover import ContentIR, HandoverError, handover

    try:
        ir = ContentIR.from_dict(body.content)
        return handover(ir, body.target, body.inputs or {})
    except HandoverError as exc:
        raise HTTPException(422, str(exc))
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except Exception as exc:  # noqa: BLE001
        logger.exception("handover send failed")
        raise HTTPException(500, f"{type(exc).__name__}: {exc}")
