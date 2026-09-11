# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: PEP 562 lazy package re-exports — keep `from okuro.x import y` working
#   without importing y's subsystem at package-import time.
# index: imports | def install
# AGENT_HEADER_END -->
"""Deferred package re-exports (PEP 562).

WHY THIS EXISTS, measured 2026-07-29. Importing ``a.b.c`` executes
``a/__init__.py`` and ``a/b/__init__.py`` first. Every MCP tool module lives
inside a package whose ``__init__`` eagerly re-exported its whole subsystem, so
merely REGISTERING the tool surface dragged the subsystem into memory:

  * 103 of the MCP server's 138 boot-loaded files (75%) came from package
    ``__init__`` execution; only 35 were genuinely needed
  * ``okuro.prism.workflow``'s ``__init__`` alone pulled in 31 files, purely to
    re-export ``PrismWorkflow`` and friends
  * everything loaded is what the strict-freshness gate can be made stale by,
    and that gate refuses server-wide — 129 of 235 residual refusals over 30
    days touched ONLY files that a package ``__init__`` had dragged in

Deleting the re-exports would have fixed it and broken ~99 call sites for
``okuro.sense`` alone. This keeps every call site working and defers the cost
to first attribute access, which for a tool that is never called is never.

Usage in a package ``__init__``::

    from okuro._lazy import install as _install

    _EXPORTS = {"write_memory": ("okuro.sense.memory", "write_memory")}
    __getattr__, __dir__ = _install(globals(), _EXPORTS)

``from okuro.sense import write_memory`` then triggers ``__getattr__`` and
resolves exactly as before. The resolved value is written back into the
package globals, so ``__getattr__`` fires once per name per process.

NOT A DROP-IN FOR EVERY PACKAGE. ``okuro/bridge/__init__.py`` re-exports
``invoke`` OVER its own submodule name, and tests work around the resulting
shadowing with ``importlib.import_module``. Making that lazy changes WHEN the
shadowing happens. Left alone deliberately; see the monkeypatch gotchas in the
brain before touching it.
"""

from typing import Callable, Iterable, Mapping


def install(
    package_globals: dict,
    exports: Mapping[str, tuple[str, str]],
    optional: Iterable[str] = (),
) -> tuple[Callable[[str], object], Callable[[], list]]:
    """Build ``__getattr__`` / ``__dir__`` for deferred re-exports.

    Args:
        package_globals: the package's own ``globals()``. Resolved values are
            cached here, so each name costs one import at most.
        exports: ``{public_name: (module_path, attribute_name)}``. The pair
            form carries aliases — ``okuro.cortex`` exposes ``sidecar.load``
            as ``load_sidecar``.
        optional: names whose module may legitimately be absent. These resolve
            to ``None`` instead of raising, matching the ``try/except
            ImportError`` blocks this replaces.

    Returns:
        ``(__getattr__, __dir__)`` to assign in the package body.
    """
    pkg = package_globals.get("__name__", "<package>")
    optional_names = frozenset(optional)

    def __getattr__(name: str):
        try:
            module_path, attr = exports[name]
        except KeyError:
            # Must raise AttributeError, not KeyError — `hasattr`, pickle and
            # `from pkg import missing` all rely on that exact type.
            raise AttributeError(
                f"module {pkg!r} has no attribute {name!r}"
            ) from None

        from importlib import import_module

        try:
            value = getattr(import_module(module_path), attr)
        except ImportError:
            if name in optional_names:
                package_globals[name] = None
                return None
            raise

        # Cache in the package namespace: __getattr__ is only consulted when
        # normal lookup fails, so this name resolves directly from here on.
        package_globals[name] = value
        return value

    def __dir__() -> list:
        return sorted(set(package_globals) | set(exports))

    return __getattr__, __dir__
