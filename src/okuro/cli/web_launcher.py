# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Shared helpers for launching the FastAPI/uvicorn server from the CLI.
# index:
#   imports
#   def pick_free_port
#   def start_server_thread
#   def wait_for_ready
#   def fetch_onboarding_state
#   def has_display
#   def open_in_browser
#   def open_in_webview
#   def serve_until_signal
# AGENT_HEADER_END -->
"""Shared helpers for launching the FastAPI/uvicorn server from the CLI.

Used by `okuro init` (wizard mode) and `okuro dashboard` to:
  - pick a free port (preferred → fallback range → OS-assigned)
  - start uvicorn in a background thread
  - wait for /api/health to respond before opening the browser
  - route the browser to /onboarding or /dashboard based on state
  - degrade gracefully when no DISPLAY is available (SSH / headless)
"""

from __future__ import annotations

import logging
import os
import platform
import socket
import sys
import threading
import time
import webbrowser
from pathlib import Path
from typing import Optional
from urllib.error import URLError
from urllib.request import urlopen
from okuro.db.engine import okuro_home

log = logging.getLogger("okuro.cli.web_launcher")


# Self-heal a poisoned webview profile. A caching service worker from an older
# build can keep serving a stale UI bundle that survives every reload (the SW
# intercepts the fetch) — and pywebview exposes no DevTools to unregister it.
# On each page load we check for Cache Storage: the current no-op SW never
# creates a cache, so the mere presence of one means a stale caching SW
# poisoned the profile. In that case purge every cache, unregister every SW,
# and reload ONCE. Steady state (no caches) → this is a no-op, no reload loop.
_SW_SELF_HEAL_JS = """
(async () => {
  try {
    if (!window.caches) return;
    const keys = await caches.keys();
    if (!keys.length) return;
    await Promise.all(keys.map((k) => caches.delete(k)));
    if ('serviceWorker' in navigator) {
      const regs = await navigator.serviceWorker.getRegistrations();
      await Promise.all(regs.map((r) => r.unregister()));
    }
    location.reload();
  } catch (e) {}
})();
"""


# ─── macOS Dock identity override ──────────────────────────────────────────
#
# Why this exists: when a Python process imports pyobjc / AppKit, AppKit
# resolves ``NSBundle.mainBundle()`` by walking up the executing binary's
# path looking for an enclosing ``.app`` containing ``Contents/Info.plist``.
# Python.framework ships with ``Resources/Python.app`` (CFBundleIdentifier
# ``org.python.python``, CFBundleIconFile ``PythonInterpreter.icns`` —
# the rocket). Because Python.framework's binary loader resolves the main
# bundle via the framework's own structure rather than the externally
# COPIED binary path, our renamed copy at
# ``Okuro.app/Contents/MacOS/okuro-cli`` does NOT win the resolution —
# AppKit still picks Python.app and the Dock shows "Python" with a rocket.
#
# Verified empirically on 2026-05-10 macOS 26.4.1 + python@3.13:
#     >>> from AppKit import NSBundle
#     >>> NSBundle.mainBundle().bundlePath()
#     '/opt/homebrew/.../Python.framework/.../Resources/Python.app'
#
# py2app gets around this by building a compiled C launcher that calls
# ``Py_Main`` AFTER manually setting bundle context. We don't use py2app.
# Instead, override the two user-visible identity surfaces via public
# PyObjC APIs:
#
#   1. ``NSProcessInfo.processInfo().setProcessName_("Okuro")`` — replaces
#      the Dock title and cmd-tab name. Must run BEFORE NSApplication is
#      ever created or the original "Python" name has already been cached.
#   2. ``NSApp.setApplicationIconImage_(NSImage('okuro.icns'))`` — replaces
#      the Dock icon AND the Activity Monitor icon column. Must run AFTER
#      NSApp.sharedApplication() exists (which pywebview triggers during
#      window creation).
#
# Bundle-association via Contents/MacOS/ binary placement remains useful
# for kernel ``proc_name`` and for service binaries that don't go through
# AppKit (e.g. pure CLI commands), so we keep the bundle layout fix on
# top of these AppKit-level overrides.


_OKURO_PROCESS_NAME = "Okuro"


def _macos_icon_path() -> Optional[Path]:
    """Locate the Okuro icon for ``setApplicationIconImage_``.

    Search order:
      1. ``Okuro.app/Contents/Resources/okuro.icns`` — if running from
         inside the bundle (the production case), this is right next to
         the executable. Build via:
         ``Path(sys.executable).parent.parent / "Resources" / "okuro.icns"``
      2. Repo's ``frontend/public/icons/icon-512.png`` — dev fallback
         when running from a checkout, no .icns generated yet. NSImage
         accepts .png too.
    """
    candidates: list[Path] = []
    try:
        candidates.append(
            Path(sys.executable).resolve().parent.parent
            / "Resources"
            / "okuro.icns"
        )
    except OSError:
        pass

    # Repo fallback — walk up from this file to find the icon.
    here = Path(__file__).resolve()
    for ancestor in here.parents:
        candidate = ancestor / "src" / "okuro" / "web" / "frontend" / "public" / "icons" / "icon-512.png"
        if candidate.is_file():
            candidates.append(candidate)
            break

    for path in candidates:
        if path.is_file():
            return path
    return None


def _set_macos_process_name() -> None:
    """Override ``NSProcessInfo.processName`` BEFORE pyobjc creates NSApp.

    Foundation imports do NOT initialize NSApplication; only AppKit
    imports trigger that. Calling this before ``import webview`` keeps
    the override deterministic — by the time pywebview reaches
    ``NSApplication.sharedApplication()``, the process name is already
    ``Okuro`` and gets cached into NSApp's display name.
    """
    if platform.system() != "Darwin":
        return
    try:
        from Foundation import NSProcessInfo  # noqa: PLC0415

        NSProcessInfo.processInfo().setProcessName_(_OKURO_PROCESS_NAME)
    except Exception as exc:  # noqa: BLE001
        log.debug("setProcessName_ failed (%s) — Dock title will say 'Python'", exc)


def _set_macos_app_icon() -> None:
    """Override ``NSApp.applicationIconImage`` so the Dock shows okuro.icns.

    Re-applies on every call (cheap NSImage load + setter) because
    pywebview's NSApp initialization can clobber an icon set too early.
    Call once at create_window time AND again from the webview.start()
    callback so we win regardless of when NSApp was finalized.
    """
    if platform.system() != "Darwin":
        return
    icon_path = _macos_icon_path()
    if icon_path is None:
        log.debug("no okuro.icns / icon-512.png on disk — Dock icon will be Python rocket")
        return
    try:
        from AppKit import NSApplication, NSImage  # noqa: PLC0415

        image = NSImage.alloc().initWithContentsOfFile_(str(icon_path))
        if image is None:
            log.debug("NSImage init failed for %s — keeping Python rocket", icon_path)
            return
        nsapp = NSApplication.sharedApplication()
        nsapp.setApplicationIconImage_(image)
        # Force the Dock to redraw — without this the change waits for
        # the next periodic redraw and the user briefly sees the rocket
        # before our icon takes over.
        try:
            nsapp.dockTile().display()
        except Exception:  # noqa: BLE001
            pass
    except Exception as exc:  # noqa: BLE001
        log.debug("setApplicationIconImage_ failed (%s) — Dock icon stays Python", exc)


def _macos_post_start_callback() -> None:
    """Runs on the GUI loop right after ``webview.start`` initialises NSApp.

    pywebview's ``start(func=...)`` schedules ``func`` on the AppKit main
    runloop AFTER the NSApplication has been set up, the menu bar has been
    drawn, and the first window is on its way to the screen. That's the
    earliest moment we can reliably override the icon and have it stick
    against any internal AppKit reset that happens during launch sequence.
    Calling ``_set_macos_app_icon`` here is the second of two calls (the
    first is at create_window time as defence in depth).
    """
    _set_macos_app_icon()


def is_server_running(port: int, timeout_s: float = 0.5) -> bool:
    """True when something is already answering /api/health on ``port``."""
    url = f"http://127.0.0.1:{port}/api/health"
    try:
        with urlopen(url, timeout=timeout_s) as resp:
            return resp.status == 200
    except Exception:
        return False


def pick_free_port(
    preferred: int | None = None,
    fallback_range: tuple[int, int] | None = None,
    reserved: set[int] | None = None,
) -> int:
    """Return a free TCP port on 127.0.0.1.

    Tries ``preferred`` first, then each port in ``fallback_range`` (inclusive).
    Falls back to an OS-assigned ephemeral port if the whole range is taken.

    Defaults come from :mod:`okuro.system.port_registry` so we never accidentally
    hand out a port reserved for a long-running plist (orchestrator on 13333,
    embed on 13334). Any port in ``reserved`` is skipped even if free — used
    by the dashboard fallback so it can never squat on the embed port between
    embed-service restarts.
    """
    from okuro.system.port_registry import (
        orchestrator_port,
        wizard_fallback_range,
        reserved_okuro_ports,
    )

    if preferred is None:
        preferred = orchestrator_port()
    if fallback_range is None:
        fallback_range = wizard_fallback_range()
    # Always honour the canonical reserved set; callers can extend via the arg.
    skip = set(reserved_okuro_ports()) | (reserved or set())
    skip.discard(preferred)  # caller asked for it explicitly, that wins.

    def _is_free(p: int) -> bool:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind(("127.0.0.1", p))
                return True
            except OSError:
                return False

    if _is_free(preferred):
        return preferred
    lo, hi = fallback_range
    for p in range(lo, hi + 1):
        if p in skip:
            continue
        if _is_free(p):
            return p
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def start_server_thread(host: str, port: int) -> threading.Thread:
    """Start uvicorn serving okuro.orchestrator.api.main:app on a daemon thread."""
    import uvicorn

    config = uvicorn.Config(
        "okuro.orchestrator.api.main:app",
        host=host,
        port=port,
        log_level="warning",
        access_log=False,
    )
    server = uvicorn.Server(config)

    t = threading.Thread(target=server.run, name="okuro-uvicorn", daemon=True)
    t.start()
    return t


def wait_for_ready(port: int, timeout_s: float = 30.0, poll_s: float = 0.2) -> bool:
    """Poll /api/health until 200 or timeout. Returns True if ready."""
    url = f"http://127.0.0.1:{port}/api/health"
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            with urlopen(url, timeout=1.0) as resp:
                if resp.status == 200:
                    return True
        except (URLError, ConnectionError, OSError):
            pass
        time.sleep(poll_s)
    return False


def fetch_onboarding_state(port: int) -> Optional[dict]:
    """GET /api/onboarding/state. Returns parsed dict or None on error."""
    import json

    url = f"http://127.0.0.1:{port}/api/onboarding/state"
    try:
        with urlopen(url, timeout=2.0) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        log.debug("onboarding state fetch failed: %s", e)
        return None


def has_display() -> bool:
    """Heuristic: can this environment actually open a browser window?

    Returns False on typical headless servers (SSH without X forwarding,
    container with no desktop). On macOS we always say True — `open`
    works without DISPLAY. Windows ditto.
    """
    import sys

    if sys.platform == "darwin" or sys.platform.startswith("win"):
        return True
    # Linux: needs DISPLAY or WAYLAND_DISPLAY
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def open_in_browser(url: str) -> bool:
    """Open ``url`` in the user's default browser. Returns True on success."""
    try:
        return webbrowser.open(url, new=1, autoraise=True)
    except Exception as e:
        log.warning("webbrowser.open failed: %s", e)
        return False


def _grant_gtk_media_permissions(window_uid: str) -> bool:
    """GTK/WebKit denies ``getUserMedia`` by default (no permission-request
    handler), so the intake-drawer mic fails with "request is not allowed".
    Enable media-stream + auto-grant the user-media permission on the WebKit
    webview for this window. No-op (returns False) on non-GTK backends.
    """
    try:
        import gi

        from webview.platforms.gtk import BrowserView

        for ver in ("4.1", "4.0"):
            try:
                gi.require_version("WebKit2", ver)
                break
            except Exception:
                continue
        from gi.repository import WebKit2 as webkit  # noqa: E402

        bv = getattr(BrowserView, "instances", {}).get(window_uid)
        wv = getattr(bv, "webview", None) if bv else None
        if wv is None:
            return False

        def _on_perm(_wv, request):
            try:
                if isinstance(request, webkit.UserMediaPermissionRequest):
                    request.allow()
                    return True
            except Exception:  # noqa: BLE001
                pass
            return False

        settings = wv.get_settings()
        for prop in ("enable-media-stream", "enable-webrtc", "enable-media", "enable-mediasource"):
            try:
                settings.set_property(prop, True)
            except Exception:  # noqa: BLE001
                pass
        wv.connect("permission-request", _on_perm)
        log.info("granted GTK WebKit media (mic) permissions for window %s", window_uid)
        return True
    except Exception as exc:  # noqa: BLE001 — non-GTK backend / import failure
        log.debug("GTK media permission grant skipped: %r", exc)
        return False


def open_in_webview(
    url: str,
    title: str = "Okuro",
    width: int = 1440,
    height: int = 900,
    private_mode: bool = False,
) -> None:
    """Open ``url`` in a native webview window — no browser chrome, no "Not
    secure" banner. BLOCKS the calling thread until the window closes.

    Must be called on the **main thread** (pywebview/GTK/WKWebView/WebView2
    require the UI loop on the main thread). Uvicorn runs on a daemon
    background thread, so closing the window ends the process.

    Falls back to ``open_in_browser`` + ``serve_until_signal`` when pywebview
    or its native backend isn't available (e.g. headless host, WebKit
    bindings missing).

    ``private_mode`` (default False) controls WKWebView / WebView2 / WebKitGTK
    persistent storage. False = persistent (cookies, localStorage,
    IndexedDB, SW registrations, **back-forward cache including
    scroll-restoration** survive across launches; needed for the dashboard's
    People-graph layout). True = ephemeral (every launch is a fresh state;
    correct for one-shot flows like the onboarding wizard, where persistent
    scroll-restoration would otherwise land the user mid-flow on relaunch).

    audit 2026-04-27: macOS install report — wizard opened on page 5 inside
    pywebview while the same URL in Safari started at the splash. Cause:
    WKWebView's persistent WKWebsiteDataStore restored the back-forward
    cache state from a previous (failed) install attempt, including the
    scroll position. Browser opened separately uses a different data store
    so didn't see the cached state. cmd_init now passes private_mode=True
    for the wizard.
    """
    # macOS: override Dock title BEFORE pyobjc creates NSApp. Without this,
    # the Dock and cmd-tab show "Python" because NSBundle.mainBundle()
    # resolves to Python.framework's Resources/Python.app — bundle-layout
    # tricks (renamed binary inside Okuro.app/Contents/MacOS) don't beat
    # Python.framework's own bundle resolution.
    _set_macos_process_name()

    try:
        import webview
    except Exception as e:
        log.warning("pywebview import failed (%s); falling back to default browser", e)
        open_in_browser(url)
        serve_until_signal()
        return

    class _WindowApi:
        """JS bridge for the custom window chrome. The React shell renders its
        own top-bar (drag region + min/max/close icons) and calls these via
        ``window.pywebview.api.*``. Paired with ``frameless=True`` below to
        remove the OS title bar.

        Frameless GTK windows don't get WM-assisted edge-resize on GNOME
        Mutter, so the shell renders its own 8 resize handles and drives
        them through ``get_bounds`` + ``resize_to``.
        """

        MIN_W = 640
        MIN_H = 400

        def __init__(self) -> None:
            self.window = None
            self._closing = False
            # Authoritative maximized state is kept in sync with the
            # window's actual OS-level state via pywebview event hooks
            # (see _wire_events below). Both user-button-initiated AND
            # WM-initiated transitions (Super+Up, double-click titlebar,
            # display manager shortcut) fire events.maximized /
            # events.restored — hooking both closes the old drift where
            # clicking our button after a WM maximize did nothing because
            # the local flag said we weren't maximized when we actually
            # were.
            self._maximized = False
            self._saved_bounds: dict | None = None

        # Called by open_in_webview once self.window is assigned.
        def _wire_events(self) -> None:
            if not self.window:
                return
            try:
                # The `+=` operator on pywebview Event subscribes a callable;
                # it fires on every state flip.
                self.window.events.maximized += lambda: self._on_window_event(True)
                self.window.events.restored += lambda: self._on_window_event(False)
            except Exception as exc:  # noqa: BLE001
                log.warning("failed to hook pywebview window events: %s", exc)

        def _on_window_event(self, maximized: bool) -> None:
            self._maximized = maximized

        def minimize(self) -> None:
            if self.window:
                self.window.minimize()

        def toggle_maximize(self) -> None:
            if not self.window:
                return
            # Query OS-level truth rather than trusting the local flag —
            # keeps us correct even if the subscribe above missed an event
            # or fires after this call.
            currently_max = self._is_currently_maximized()
            if currently_max:
                self._unmaximize()
            else:
                self._save_bounds()
                self.window.maximize()

        def _is_currently_maximized(self) -> bool:
            """Platform-aware query of the real window state.

            GTK exposes ``is_maximized()`` on Gtk.Window. Cocoa's pywebview
            ``maximize`` is actually a resize-to-screen — so on mac we
            detect by comparing window dimensions to screen dimensions.
            """
            import sys

            if sys.platform == "darwin":
                # Cocoa: pywebview's maximize just resizes to the screen.
                try:
                    import Cocoa  # type: ignore

                    screen = Cocoa.NSScreen.mainScreen().frame().size
                    return (
                        int(self.window.width) >= int(screen.width) - 2
                        and int(self.window.height) >= int(screen.height) - 2
                    )
                except Exception:  # noqa: BLE001
                    return self._maximized
            else:
                try:
                    from webview.platforms.gtk import BrowserView

                    bv = BrowserView.instances.get(self.window.uid)
                    return bool(bv and bv.window.is_maximized())
                except Exception:  # noqa: BLE001
                    return self._maximized

        def _save_bounds(self) -> None:
            try:
                self._saved_bounds = {
                    "x": int(self.window.x),
                    "y": int(self.window.y),
                    "width": int(self.window.width),
                    "height": int(self.window.height),
                }
            except Exception:  # noqa: BLE001
                self._saved_bounds = None

        def _unmaximize(self) -> None:
            """Exit maximized state cleanly on each platform.

            GTK: ``Gtk.Window.unmaximize()`` is the only call that actually
            clears the WM-level maximized flag. pywebview's public
            ``Window.restore()`` runs ``deiconify + present`` which is
            WRONG for this — it only unminimizes, leaving the maximized
            state intact and silently swallowing subsequent resize/move
            calls. We reach past the public API and call the GTK method
            directly.

            Cocoa: pywebview's maximize is a plain resize, so reversing
            it is also a plain resize back to the saved bounds.
            """
            import sys

            b = self._saved_bounds or {"x": 100, "y": 100, "width": 1200, "height": 800}

            if sys.platform == "darwin":
                self.window.resize(int(b["width"]), int(b["height"]))
                self.window.move(int(b["x"]), int(b["y"]))
                return

            # Linux / GTK path: GLib-dispatch the real unmaximize onto the
            # main thread (UI calls from non-main threads silently fail on
            # Mutter), then resize+move to the user's saved bounds. The
            # resize is a no-op while the window is still maximized, so
            # we schedule it AFTER the unmaximize returns via the same
            # idle queue.
            try:
                from webview.platforms.gtk import BrowserView
                from gi.repository import GLib  # type: ignore

                bv = BrowserView.instances.get(self.window.uid)
                if bv is None:
                    raise RuntimeError("pywebview BrowserView instance missing")

                def _unmax_then_resize():
                    try:
                        bv.window.unmaximize()
                    except Exception as exc:  # noqa: BLE001
                        log.warning("Gtk.Window.unmaximize failed: %s", exc)
                    # Resize/move on a fresh idle tick so GTK processes
                    # the unmaximize first and treats these as
                    # user-space dimensions, not contested with the WM.
                    GLib.idle_add(self._apply_saved_bounds, b)
                    return False  # don't re-run

                GLib.idle_add(_unmax_then_resize)
                return
            except Exception as exc:  # noqa: BLE001
                log.warning("GTK unmaximize path failed (%s); falling back to raw resize", exc)

            # Safety net when we can't reach GTK (import error etc.):
            # raw resize. Not ideal on a still-maximized GTK window, but
            # better than a dead button.
            self.window.resize(int(b["width"]), int(b["height"]))
            self.window.move(int(b["x"]), int(b["y"]))

        def _apply_saved_bounds(self, b: dict) -> bool:
            try:
                self.window.resize(int(b["width"]), int(b["height"]))
                self.window.move(int(b["x"]), int(b["y"]))
            except Exception as exc:  # noqa: BLE001
                log.warning("restore resize/move failed: %s", exc)
            return False

        def close(self) -> None:
            """Close the window and exit the process.

            The naive `self.window.destroy()` from here crashed macOS
            WKWebView builds: invoking destroy while we're still inside
            the js_api callback leaves the JS-side Promise in flight,
            WebKit tries to resolve it against a view that's being torn
            down, and the app dies (user had to force-quit).

            Fix: defer destroy to a worker thread so the callback returns
            first, then schedule destroy on pywebview's own main-thread
            dispatcher. A 2 s watchdog force-exits the process if
            webview.start() doesn't release — happens on some macOS
            builds where the runloop holds on even after all windows
            are destroyed, leaving a zombie process the user can't
            reach without Activity Monitor.
            """
            if self._closing:
                return
            self._closing = True
            import threading

            def _teardown() -> None:
                import os
                import time

                # Let the js_api Promise resolve before tearing down the
                # webview it was invoked from.
                time.sleep(0.15)
                try:
                    if self.window:
                        self.window.destroy()
                except Exception as exc:  # noqa: BLE001
                    log.warning(
                        "pywebview destroy failed (%s); force-exiting", exc
                    )
                    os._exit(0)
                    return
                # Fallback: if destroy succeeded but webview.start() hasn't
                # returned within 2 s, the runloop is stuck — exit the whole
                # process. Harmless when destroy DOES return cleanly (the
                # process has already exited by the time this sleep ends
                # since main returned and uvicorn is a daemon thread).
                time.sleep(2.0)
                log.info("pywebview did not release after close; force-exiting")
                os._exit(0)

            threading.Thread(
                target=_teardown, daemon=True, name="okuro-close"
            ).start()

        def get_bounds(self) -> dict | None:
            if not self.window:
                return None
            return {
                "x": int(self.window.x),
                "y": int(self.window.y),
                "width": int(self.window.width),
                "height": int(self.window.height),
            }

        def resize_to(self, w, h, x=None, y=None) -> None:
            if not self.window:
                return
            w = max(self.MIN_W, int(w))
            h = max(self.MIN_H, int(h))
            self.window.resize(w, h)
            # West edge sends only x, north edge sends only y, and the NE/SW
            # corners each send exactly one. The previous `x AND y` gate
            # meant those edges silently refused to move the window — so
            # dragging the west/north borders resized purely from the
            # opposite anchor (visually the edge didn't move). Move when
            # EITHER is supplied, substituting the current coord for the
            # missing axis.
            if x is not None or y is not None:
                cur_x = self.window.x
                cur_y = self.window.y
                self.window.move(
                    int(x) if x is not None else cur_x,
                    int(y) if y is not None else cur_y,
                )
            if os.environ.get("OKURO_WEBVIEW_DEBUG"):
                log.info(
                    "resize_to w=%s h=%s x=%s y=%s", w, h, x, y
                )

        def open_external(self, url: str) -> bool:
            """Open ``url`` in the user's default system browser.

            pywebview silently drops ``window.open(url, "_blank")`` and
            ``<a target="_blank">`` clicks on every backend (GTK/Cocoa/Qt)
            unless the host wires a ``new_window`` handler. Rather than
            wrestling per-backend signals, expose a JS-callable bridge
            that routes via Python's ``webbrowser`` module — same path
            as the splash-on-no-display fallback.

            The frontend prefers this over ``window.open`` whenever
            ``window.pywebview.api.open_external`` exists (see
            ``lib/api.ts``). Returns True if the OS reported success.

            Allowlist: http(s) and mailto only. Anything else (file://,
            javascript:, custom schemes) is refused — this bridge is
            callable from any in-page script and we don't want it to
            become a foot-gun.
            """
            if not isinstance(url, str):
                return False
            scheme = url.split(":", 1)[0].lower() if ":" in url else ""
            if scheme not in ("http", "https", "mailto"):
                log.warning("open_external refused scheme=%r", scheme)
                return False
            return open_in_browser(url)

    api = _WindowApi()

    try:
        window = webview.create_window(
            title=title,
            url=url,
            width=width,
            height=height,
            resizable=True,
            frameless=True,
            # easy_drag=True would hijack every mousedown in the window for
            # a move-drag, which blocks our custom edge-resize handlers. Keep
            # it False; drag is driven by the .pywebview-drag-region class on
            # the React title-bar (see window-chrome.tsx).
            easy_drag=False,
            # pywebview defaults text_select=False on the GTK / WebKit backend
            # — drag-to-select is disabled at the webview layer, so no CSS
            # override (user-select: text on body) reaches the user. Force it
            # on so artifact text, prompts, logs, etc. are selectable.
            text_select=True,
            js_api=api,
        )
        api.window = window
        api._wire_events()
        # Self-heal a stale-service-worker-poisoned profile on every load.
        # See _SW_SELF_HEAL_JS — no-op once the profile is clean.
        def _purge_stale_service_worker() -> None:
            try:
                window.evaluate_js(_SW_SELF_HEAL_JS)
            except Exception as exc:  # noqa: BLE001
                log.debug("SW self-heal eval skipped: %r", exc)

        try:
            window.events.loaded += _purge_stale_service_worker
        except Exception as exc:  # noqa: BLE001
            log.debug("could not wire SW self-heal on loaded: %r", exc)
        # Grant mic/media permission on the GTK WebKit webview (once). The
        # BrowserView instance exists by the time `loaded` fires.
        _media_granted = {"done": False}

        def _grant_media_once() -> None:
            if _media_granted["done"]:
                return
            if _grant_gtk_media_permissions(window.uid):
                _media_granted["done"] = True

        try:
            window.events.loaded += _grant_media_once
        except Exception as exc:  # noqa: BLE001
            log.debug("could not wire media-permission grant on loaded: %r", exc)
        # macOS: override Dock icon NOW that NSApp.sharedApplication()
        # exists (pywebview triggers its creation during create_window).
        # Without this, the Dock shows Python.framework's rocket icon
        # because NSBundle.mainBundle() = Python.app, not Okuro.app.
        _set_macos_app_icon()
        # OKURO_WEBVIEW_DEBUG=1 → right-click to open devtools + pywebview
        # logs go to stderr. Useful when users report that custom chrome
        # (min/max/close, edge resize) misbehaves.
        debug = bool(os.environ.get("OKURO_WEBVIEW_DEBUG"))
        # Persistent storage profile so localStorage / cookies survive
        # window close + relaunch — REQUIRED for the dashboard so the
        # People-graph layout etc. survive. EPHEMERAL for the wizard so
        # WKWebView's back-forward cache (including scroll restoration)
        # cannot land the user mid-flow when re-running install.sh after
        # a failed first attempt.
        # ``func`` runs on the GUI runloop AFTER pywebview's NSApp is up.
        # That's the earliest moment to override the Dock icon reliably —
        # calling setApplicationIconImage_ before this can be reset by
        # AppKit's own launch-sequence icon initialisation. Linux + Windows
        # backends accept ``func`` and ignore it gracefully.
        post_start = _macos_post_start_callback if platform.system() == "Darwin" else None
        if private_mode:
            webview.start(
                func=post_start,
                debug=debug,
                private_mode=True,
            )
        else:
            storage_path = str(okuro_home() / "webview")
            os.makedirs(storage_path, exist_ok=True)
            webview.start(
                func=post_start,
                debug=debug,
                private_mode=False,
                storage_path=storage_path,
            )
    except Exception as e:
        log.warning("webview.start failed (%s); falling back to default browser", e)
        open_in_browser(url)
        serve_until_signal()


def serve_until_signal() -> None:
    """Block the main thread until SIGINT/SIGTERM.

    The uvicorn thread is daemonized so the process exits when the main
    thread returns. This function keeps the main thread parked so the
    server stays up.
    """
    import signal

    stop = threading.Event()

    def _handle(signum, frame):
        stop.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _handle)
        except (ValueError, OSError):
            pass

    try:
        while not stop.is_set():
            stop.wait(timeout=1.0)
    except KeyboardInterrupt:
        pass
