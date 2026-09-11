# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Notification channel registry — loads enabled channels, dispatches messages.
# index: def register_channel | class ChannelRegistry | def _ensure_loaded
# AGENT_HEADER_END -->
"""Notification channel registry — loads enabled channels, dispatches messages."""

from .base import Channel, Message, DeliveryResult, DeliveryStatus

_REGISTRY: dict[str, type[Channel]] = {}


def register_channel(cls: type[Channel]):
    """Class decorator that registers a channel."""
    _REGISTRY[cls._name] = cls
    return cls


class ChannelRegistry:
    """Manages available channels and dispatches messages."""

    def __init__(self):
        _ensure_loaded()
        self._channels: dict[str, Channel] = {}
        for name, cls in _REGISTRY.items():
            try:
                instance = cls()
                if instance.available():
                    self._channels[name] = instance
            except Exception:
                continue

    def send(self, channel_names: list[str], message: Message) -> list[DeliveryResult]:
        """Send a message through the specified channels."""
        results = []
        for name in channel_names:
            ch = self._channels.get(name)
            if ch:
                try:
                    result = ch.send(message)
                    results.append(result)
                except Exception as e:
                    results.append(DeliveryResult(
                        channel=name,
                        status=DeliveryStatus.FAILED,
                        error=str(e),
                    ))
            else:
                results.append(DeliveryResult(
                    channel=name,
                    status=DeliveryStatus.FAILED,
                    error=f"Channel '{name}' not available",
                ))
        return results

    def available_channels(self) -> list[str]:
        return list(self._channels.keys())


_loaded = False


def _ensure_loaded():
    global _loaded
    if not _loaded:
        from . import desktop  # noqa: F401
        from . import bootstrap  # noqa: F401
        _loaded = True


__all__ = [
    "Channel", "Message", "DeliveryResult", "DeliveryStatus",
    "ChannelRegistry", "register_channel",
]
