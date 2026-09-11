# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Channel infrastructure — base models and ABC for notification delivery.
# index: imports | class DeliveryStatus | class Message | class DeliveryResult | class Channel
# AGENT_HEADER_END -->
"""Channel infrastructure — base models and ABC for notification delivery."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum


class DeliveryStatus(str, Enum):
    SENT = "sent"
    FAILED = "failed"
    BOUNCED = "bounced"
    ACKNOWLEDGED = "acknowledged"


@dataclass
class Message:
    """Channel-agnostic notification payload."""
    title: str
    body: str
    urgency: int = 3
    format: str = "brief"
    source: str = "system"
    source_id: str | None = None
    context: dict = field(default_factory=dict)
    acknowledge_id: str | None = None


@dataclass
class DeliveryResult:
    """Result from a single channel delivery attempt."""
    channel: str
    status: DeliveryStatus
    payload: dict = field(default_factory=dict)
    response: dict = field(default_factory=dict)
    error: str | None = None


class Channel(ABC):
    """Base class for notification channels."""

    _name: str = "unknown"

    @property
    @abstractmethod
    def capabilities(self) -> set[str]:
        """Capabilities: {"text", "rich", "interactive", "persistent"}."""
        ...

    @abstractmethod
    def send(self, message: Message) -> DeliveryResult:
        ...

    def available(self) -> bool:
        """Check if this channel is currently reachable."""
        return True

    def supports_acknowledge(self) -> bool:
        return "interactive" in self.capabilities
