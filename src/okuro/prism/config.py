# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: okuro·prism strict-mode flag — ONE switch and ONE escape hatch for
#   every hard-fail the overhaul adds (engine-A HTTP seal, variety quotas, shape
#   defaults, capacity bounds, recipient-context refusals).
# index: PrismStrictError | strict_enabled | collect_warnings | warn_or_fail
# AGENT_HEADER_END -->
"""The ``prism.strict`` feature flag and its warn-or-fail helper.

Every hard-fail added by the prism overhaul routes through :func:`warn_or_fail`
so there is exactly one rollback: set ``prism.strict: false`` in
``~/.okuro/config.yaml`` (or ``OKURO_PRISM_STRICT=0``) and every new refusal
degrades to a LOUD structured warning instead of an exception. The warning is
never swallowed — it is logged and, inside :func:`collect_warnings`, collected
so the caller ships it with the deck.

Default is ON: a new deck should refuse rather than silently ship a defect. The
OFF path exists to unblock a build, not to hide one.
"""
from __future__ import annotations

import logging
import os
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Any
from okuro.db.engine import okuro_home

logger = logging.getLogger(__name__)

STRICT_DEFAULT = True
_ENV = "OKURO_PRISM_STRICT"
_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}

#: Collected warnings for the innermost active :func:`collect_warnings` scope.
_sink: ContextVar[list[dict[str, Any]] | None] = ContextVar("prism_warnings", default=None)


class PrismStrictError(RuntimeError):
    """A refusal raised because ``prism.strict`` is on.

    Carries the machine-readable ``code`` and the structured ``fields`` so an
    API layer can render it without re-parsing the message.
    """

    def __init__(self, code: str, message: str, **fields: Any):
        self.code = code
        self.fields = fields
        super().__init__(message)


def _config_path() -> Path:
    return okuro_home() / "config.yaml"


def strict_enabled() -> bool:
    """``prism.strict`` — env override wins, then ``~/.okuro/config.yaml``,
    then :data:`STRICT_DEFAULT`. Any read failure falls back to the default
    rather than turning enforcement off by accident."""
    env = os.environ.get(_ENV)
    if env is not None:
        v = env.strip().lower()
        if v in _TRUE:
            return True
        if v in _FALSE:
            return False
    try:
        import yaml

        data = yaml.safe_load(_config_path().read_text()) or {}
        block = data.get("prism")
        if isinstance(block, dict) and "strict" in block:
            return bool(block["strict"])
    except Exception:  # noqa: BLE001 — a missing/broken config must not disable the gate
        pass
    return STRICT_DEFAULT


@contextmanager
def collect_warnings() -> Iterator[list[dict[str, Any]]]:
    """Collect every non-strict :func:`warn_or_fail` raised inside the block.

    The list is yielded live, so a caller can attach it to its result even when
    the body raises. Scopes nest; the innermost one receives the warnings.
    """
    found: list[dict[str, Any]] = []
    token = _sink.set(found)
    try:
        yield found
    finally:
        _sink.reset(token)


def warn_or_fail(code: str, message: str, **fields: Any) -> None:
    """Refuse under ``prism.strict``; otherwise log loudly and record.

    ``code`` is a stable machine identifier (``shape_missing``,
    ``quota_adjacent_repeat``, …) — assert on it in tests, never on the prose.
    """
    if strict_enabled():
        raise PrismStrictError(code, message, **fields)
    logger.warning("prism.strict OFF — %s: %s %s", code, message, fields or "")
    sink = _sink.get()
    if sink is not None:
        sink.append({"code": code, "message": message, **fields})


__all__ = [
    "PrismStrictError",
    "STRICT_DEFAULT",
    "collect_warnings",
    "strict_enabled",
    "warn_or_fail",
]
