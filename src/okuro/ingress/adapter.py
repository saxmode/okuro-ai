# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: IngressAdapter ABC + IngressMessage envelope. Channel-agnostic
#   contract so the supervisor can lifecycle any adapter uniformly and the
#   router can dispatch any envelope uniformly.
# index: imports | IngressMessage | IngressAdapter
# AGENT_HEADER_END -->
"""Ingress adapter contract + canonical message envelope."""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Awaitable, Callable, ClassVar


# Router callback signature: takes an envelope, returns when handled.
OnMessage = Callable[["IngressMessage"], Awaitable[None]]


Replier = Callable[[str], Awaitable[None]]
"""Back-channel callable bound to a single sender — adapter provides
this with the envelope so the router/dispatcher can reply without
knowing the channel transport."""


@dataclass(frozen=True)
class IngressMessage:
    """Canonical envelope produced by every adapter.

    Adapters normalize their native payload (Telegram Update, Slack event,
    SMTP message, …) into this shape so the router only learns one schema.
    """

    channel: str           # adapter name — "telegram", "slack", …
    from_id: str           # platform-native sender id (chat_id, user_id)
    text: str              # plaintext message body, post-STT if voice
    ts: datetime           # event time as reported by the source platform
    raw: dict = field(default_factory=dict)
    """Native payload kept for debugging / future param extraction.
    Routers should prefer the typed fields above."""
    reply: Replier | None = None
    """Async callable to send a text reply back to this sender. Optional
    because tests may construct envelopes without a back-channel."""


class IngressAdapter(ABC):
    """Lifecycle contract for a single channel adapter.

    Each adapter:
      * runs as one long-lived coroutine (``run``) until ``stop_event`` is set
      * yields each inbound message through the ``on_message`` callback
      * reports point-in-time state through ``health()`` for the /health
        endpoint and sysinfo_service_health drill-down

    The supervisor owns the asyncio task; adapters do not spawn their own.
    """

    name: ClassVar[str]
    """Channel identifier (e.g. ``"telegram"``). Must match the row in
    the ``integrations`` table and the keyring namespace prefix."""

    @abstractmethod
    async def run(
        self,
        stop_event: asyncio.Event,
        on_message: OnMessage,
    ) -> None:
        """Run the adapter's main loop. Return when ``stop_event`` is set
        or when a non-recoverable error makes restarting pointless. Any
        exception bubbles to the supervisor's restart-with-backoff."""

    @abstractmethod
    def health(self) -> dict:
        """Return a JSON-serialisable snapshot of adapter state.

        Required fields::

            {
              "name": str,             # channel
              "state": "running" | "starting" | "stopped" | "error",
              "last_poll_at": str | None,    # ISO8601 UTC
              "last_message_at": str | None,
              "last_error": str | None,
              "last_error_at": str | None,
            }
        """


def utc_iso() -> str:
    """ISO8601 UTC string with seconds precision."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()
