# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: process entrypoint for the orchestrator API — imports the app ONCE
# index: def main
# AGENT_HEADER_END -->
"""Launcher for the orchestrator API.

WHY THIS MODULE EXISTS. The service used to run
``python -m okuro.orchestrator.api.main``, which makes runpy execute that
module as ``__main__``. Its entrypoint then called
``uvicorn.run("okuro.orchestrator.api.main:app", ...)`` with an IMPORT STRING,
so uvicorn imported the same 7,549-line file a second time under its real
name — paying the top-level imports and the MCP registry build twice (measured
14.7s of a 83.4s boot before the tracemalloc removal, ~0.7s after) and, worse,
leaving TWO live module objects.

The two-copies part is the real defect and it is silent. ``sys.modules`` ends
up holding the canonical-name copy, which is the one uvicorn serves, while the
``__main__`` copy keeps its own ``event_watcher``, ``ws_manager``,
``_API_TOKEN`` and ``_STATE_CHANGE_EVENTS``. Today the ~20 runtime lazy imports
(``api/bridge.py:43``, ``api/preview.py:217``, ``orchestrator/state.py:117``,
``system/restart_guard.py:131``, …) resolve to the served copy by accident.

Passing the ``app`` OBJECT to ``uvicorn.run`` without this launcher would break
that accident in the worst possible direction: the canonical name would never
be bound at boot, and the FIRST request touching any lazy import would import
the module fresh INSIDE the request — a preview log stream would then broadcast
through a ``ws_manager`` with zero connections and an ``event_watcher`` whose
``start()`` was never called. No error, no log line.

So this module imports the app under its CANONICAL name first, and only then
hands the object to uvicorn. One module object, registered under the name every
absolute import resolves, and the identity is asserted at boot rather than
assumed.
"""

from __future__ import annotations

import logging
import os
import sys

logger = logging.getLogger("okuro.orchestrator.api.serve")


def main() -> None:
    import setproctitle

    setproctitle.setproctitle("okuro-orchestrator-api")

    import uvicorn

    # This import is the whole point: it executes main.py exactly once, under
    # its canonical name, and registers it in sys.modules there.
    from okuro.orchestrator.api.main import HOST, PORT, app

    canonical = sys.modules.get("okuro.orchestrator.api.main")
    if canonical is None:
        raise RuntimeError(
            "okuro.orchestrator.api.main is not in sys.modules after importing "
            "it — every lazy `from okuro.orchestrator.api.main import ...` at "
            "runtime would re-execute the module inside a request"
        )
    if getattr(canonical, "app", None) is not app:
        raise RuntimeError(
            "the app object being served is not the one bound to "
            "sys.modules['okuro.orchestrator.api.main'].app — there are two "
            "copies of the module and their event_watcher / ws_manager / "
            "_API_TOKEN state has diverged"
        )
    logger.info(
        "serving the canonical okuro.orchestrator.api.main app (single import)"
    )

    if os.environ.get("OKURO_DEV") == "1":
        # --reload requires an import string: the reloader re-imports the app
        # in each worker process, so it cannot be handed an object. This path
        # therefore keeps the double import, which is acceptable for a dev
        # loop and is the only way reload works.
        from pathlib import Path

        uvicorn.run(
            "okuro.orchestrator.api.main:app",
            host=HOST,
            port=PORT,
            reload=True,
            reload_dirs=[str(Path(canonical.__file__).parent)],
            proxy_headers=False,
        )
        return

    uvicorn.run(
        app,
        host=HOST,
        port=PORT,
        # Do NOT trust X-Forwarded-For. uvicorn's default (proxy_headers=True,
        # trust loopback) rewrites request.client to the XFF value with port 0,
        # which (a) makes a Caddy-proxied phone appear as its LAN IP → fails the
        # loopback check on /api/auth/token, and (b) hides Caddy's real peer
        # socket UID so the OKURO_PROXY_UID allowlist can't match. With proxy
        # headers off, a proxied request presents as Caddy's real loopback
        # socket (UID = caddy/997), which the allowlist accepts.
        proxy_headers=False,
    )


if __name__ == "__main__":
    main()
