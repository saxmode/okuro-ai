# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Bootstrap channel — queues messages for the next agent bootstrap() call.
# index: imports | class BootstrapChannel | def drain_pending
# AGENT_HEADER_END -->
"""Bootstrap channel — queues messages for the next agent bootstrap() call."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from . import register_channel
from .base import Channel, DeliveryResult, DeliveryStatus, Message
from okuro.db.engine import okuro_home

log = logging.getLogger("okuro.sense.channels.bootstrap")

_PENDING_FILE = okuro_home() / "pending_bootstrap_messages.json"


@register_channel
class BootstrapChannel(Channel):
    """Passive channel that stores messages for the next bootstrap() call.

    Messages are appended to a JSON file. When bootstrap() runs, it calls
    drain_pending() to read and clear all queued messages.
    """

    _name = "bootstrap"

    @property
    def capabilities(self) -> set[str]:
        return {"rich", "persistent"}

    def available(self) -> bool:
        return True

    def send(self, message: Message) -> DeliveryResult:
        try:
            _PENDING_FILE.parent.mkdir(parents=True, exist_ok=True)

            pending = []
            if _PENDING_FILE.exists():
                try:
                    pending = json.loads(_PENDING_FILE.read_text())
                except (json.JSONDecodeError, OSError):
                    pending = []

            entry = {
                "title": message.title,
                "body": message.body,
                "urgency": message.urgency,
                "format": message.format,
                "source": message.source,
                "source_id": message.source_id,
                "context": message.context,
                "acknowledge_id": message.acknowledge_id,
                "queued_at": datetime.now(timezone.utc).isoformat(),
            }
            pending.append(entry)

            tmp = _PENDING_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps(pending, indent=2))
            tmp.rename(_PENDING_FILE)

            log.debug("Queued bootstrap message: %s", message.title)
            return DeliveryResult(
                channel=self._name,
                status=DeliveryStatus.SENT,
                payload={"queue_size": len(pending)},
            )

        except Exception as e:
            log.error("Failed to queue bootstrap message: %s", e)
            return DeliveryResult(
                channel=self._name,
                status=DeliveryStatus.FAILED,
                error=str(e),
            )


def drain_pending() -> list[dict]:
    """Read and clear all pending bootstrap messages.

    Called by bootstrap() during session init.
    """
    if not _PENDING_FILE.exists():
        return []

    try:
        pending = json.loads(_PENDING_FILE.read_text())
        _PENDING_FILE.unlink()
        log.info("Drained %d pending bootstrap messages", len(pending))
        return pending
    except (json.JSONDecodeError, OSError) as e:
        log.warning("Failed to drain pending messages: %s", e)
        return []
