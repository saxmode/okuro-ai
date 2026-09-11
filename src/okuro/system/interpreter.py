# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Materialize renamed Python interpreter copies so macOS Activity Monitor / launchd / Linux ps show "okuro-<service>" instead of "python", AND so the macOS Activity Monitor icon resolves to Okuro.icns instead of the generic Python rocket.
# index:
#   imports
#   _BUNDLE_RELATIVE_DIR
#   _LIBEXEC_RELATIVE_DIR
#   def ensure_okuro_interpreter
#   def _real_python_binary
#   def _ad_hoc_codesign
#   def _materialize_copy
#   def _ensure_bundle_pyvenv_cfg
# AGENT_HEADER_END -->
"""Renamed Python interpreter for service ExecStart paths.

macOS sets a process's ``proc_name`` (the field Activity Monitor, the
Dock, ``ps -o comm`` and ``launchctl`` all read) ONCE at exec time, from
the basename of the executable image. ``setproctitle`` cannot change it
post-exec — that's a documented kernel limitation, not a library bug
(see py-setproctitle issue #10, claude-code issue #12433).

Symlinks don't help: ``proc_pidpath()`` resolves them to the real
target, so a symlink named ``okuro`` pointing at ``python`` still
shows up as ``python``.

The only working approach is to exec a real binary file whose
*filename on disk* is ``okuro-<service>``. We do that by copying the
venv's Python interpreter to the destination once at install time,
then pointing every service ExecStart / ProgramArguments at that
path instead of ``sys.executable``.

On macOS the copy goes INSIDE ``~/Applications/Okuro.app/Contents/MacOS/``
(when the bundle exists). Two reasons:

* Activity Monitor's icon column resolves a process's icon by walking
  up the binary's filesystem path looking for the enclosing ``.app``
  bundle. A binary at ``<venv>/libexec/okuro`` finds no bundle and
  Activity Monitor falls back to the generic Python interpreter icon.
  A binary at ``Okuro.app/Contents/MacOS/okuro-<service>`` resolves to
  Okuro.app → ``okuro.icns``.
* The bundle's ``Contents/Info.plist`` already declares
  ``CFBundleIdentifier = ai.okuro.app``, so launchctl/Dock/Spotlight
  treat the process as part of the Okuro app for app-association
  purposes (e.g. Force Quit shows it grouped under Okuro).

Because the bundle copy lives outside the venv, ``pyvenv.cfg`` lookup
needs a hand: Python walks up from ``sys.executable``'s parent looking
for ``pyvenv.cfg``. We materialize a copy of the venv's pyvenv.cfg at
``Okuro.app/Contents/pyvenv.cfg`` so the renamed binaries pick up the
venv's site-packages instead of falling back to the system interpreter.

Linux + macOS-without-bundle (e.g. fresh checkout before install.sh
ran) fall back to ``<venv>/libexec/okuro-<service>`` — same renaming
trick, no icon benefit but proc_name still right.

* On macOS we run an ad-hoc codesign on every copy so Gatekeeper
  doesn't refuse the unsigned duplicate (the original interpreter is
  signed; the copy inherits no signature).

* Idempotent: each call compares mtime/size of the source binary
  against the existing copy and only re-copies on change. Safe to
  call on every install / update / service-install.
"""

from __future__ import annotations

import logging
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

logger = logging.getLogger("okuro.system.interpreter")

# Bundle-relative path. The .app bundle is created by install.sh on macOS
# (see install.sh ~ "Okuro.app" block). We place renamed copies in
# Contents/MacOS/ so Activity Monitor resolves their icon to Okuro.icns.
_BUNDLE_RELATIVE_DIR = "Applications/Okuro.app/Contents/MacOS"

# Linux + macOS fallback. libexec/ keeps the renamed binary off $PATH so it
# can never shadow the user-facing CLI script at <venv>/bin/okuro, while
# still being inside the venv so pyvenv.cfg discovery picks up <venv>/pyvenv.cfg
# one level above the binary's directory.
_LIBEXEC_RELATIVE_DIR = "libexec"


def _real_python_binary(interpreter: str = sys.executable) -> Path:
    """Resolve symlinks to the real Python interpreter binary on disk.

    macOS proc_pidpath resolves symlinks at exec time, so a hard link or
    copy must be made of the *real* binary, not a symlink chain.
    """
    return Path(os.path.realpath(interpreter))


def _venv_root() -> Path:
    """Best-effort venv root. Returns parent of sys.executable's dir.

    For a venv layout like ``<venv>/bin/python3.12``, this is ``<venv>``.

    Important: do NOT ``.resolve()`` here. The venv's python is a symlink
    chain that ends at the system interpreter (``/usr/bin/python3.12``);
    resolving would walk us out of the venv to ``/`` (whose libexec we
    have no business writing to). ``sys.executable`` is already the venv
    path on a venv-activated process — use it as-is.
    """
    return Path(sys.executable).parent.parent


def _ad_hoc_codesign(target: Path) -> None:
    """Run ``codesign --force --sign - <target>`` on macOS.

    Without this Gatekeeper refuses to exec the unsigned interpreter
    copy ("killed: 9" on launchd, "code signature invalid" in
    Console.app). Ad-hoc signing (``--sign -``) bypasses the team-id
    requirement; the resulting signature has no team and won't
    propagate trust, but launchd accepts it.
    """
    if platform.system() != "Darwin":
        return
    try:
        subprocess.run(
            ["codesign", "--force", "--sign", "-", str(target)],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
    except FileNotFoundError:
        logger.warning(
            "codesign not on PATH — interpreter copy at %s may be refused "
            "by Gatekeeper. Install Xcode Command Line Tools.",
            target,
        )
    except subprocess.CalledProcessError as exc:
        logger.warning(
            "codesign failed on %s: %s — Activity Monitor will still show "
            "the right name but launchd may refuse to start the service.",
            target,
            exc.stderr.decode("utf-8", errors="replace").strip(),
        )


def _materialize_copy(src: Path, target: Path) -> None:
    """Copy ``src`` to ``target`` if the source mtime/size has changed.

    Skip-on-match is stat-only — no hash. Source binary is ~5-25 MB; a
    full hash on every service install is wasteful and the mtime+size
    signal catches every realistic upgrade path.

    Caller must catch OSError. We re-raise it so callers can surface the
    failure as a fallback to ``sys.executable``.
    """
    target.parent.mkdir(parents=True, exist_ok=True)

    if target.exists():
        src_stat = src.stat()
        tgt_stat = target.stat()
        if (
            src_stat.st_size == tgt_stat.st_size
            and int(src_stat.st_mtime) == int(tgt_stat.st_mtime)
        ):
            return

    # copy2 preserves mode + mtime so the skip-on-match check stays
    # accurate across runs.
    shutil.copy2(src, target)
    target.chmod(0o755)
    _ad_hoc_codesign(target)


def _ensure_bundle_pyvenv_cfg(bundle_macos_dir: Path) -> None:
    """Mirror the venv's pyvenv.cfg into the .app bundle.

    Python's site initialization walks up from ``sys.executable``'s
    parent directory looking for ``pyvenv.cfg``. For a renamed copy at
    ``Okuro.app/Contents/MacOS/okuro-orchestrator``, the walk visits
    ``Contents/MacOS/`` then ``Contents/`` — so we place the cfg at
    ``Contents/pyvenv.cfg`` and Python finds it. Without it the renamed
    copy falls back to the system interpreter's site-packages and
    ``import okuro`` fails (the user-visible "ModuleNotFoundError: no
    module named 'okuro'" symptom from the 2026-05-10 mac install).

    Copy AS-IS — do NOT rewrite ``home =``. ``home`` points at the BASE
    Python's bin dir (where stdlib lives one level up). Rewriting it to
    the venv's bin dir broke stdlib resolution because the venv's
    ``lib/pythonM.N/`` only contains site-packages, not the standard
    library. Site-packages discovery is handled separately via the
    Contents/lib/ symlink (see ``_ensure_bundle_lib_symlink``).
    """
    src_cfg = _venv_root() / "pyvenv.cfg"
    dst_cfg = bundle_macos_dir.parent / "pyvenv.cfg"  # Contents/pyvenv.cfg

    if src_cfg.exists():
        shutil.copy2(src_cfg, dst_cfg)
    else:
        # System Python install with no venv (unusual). Compiled-in PREFIX
        # in the binary handles stdlib; we still want sys.prefix away from
        # the bundle so site-packages discovery finds Contents/lib/.
        venv_bin = Path(sys.executable).parent
        dst_cfg.write_text(
            f"home = {venv_bin}\ninclude-system-site-packages = false\n"
        )


def _ensure_bundle_lib_symlink(bundle_macos_dir: Path) -> None:
    """Symlink the venv's ``lib/`` into the bundle's ``Contents/``.

    Python's site-packages lookup uses ``sys.prefix`` (set from the
    pyvenv.cfg containing directory). With pyvenv.cfg at
    ``Contents/pyvenv.cfg``, ``sys.prefix = <bundle>/Contents/``, so
    ``site.py`` looks for ``Contents/lib/pythonM.N/site-packages/``.
    Without this symlink that path is empty and ``import okuro`` fails
    even though stdlib resolution (which uses ``sys.base_prefix`` from
    pyvenv.cfg's ``home =``) works.

    Symlink (not copy) so subsequent ``pip install`` updates in the
    venv take effect immediately — the bundle stays in sync without
    having to re-run install.sh on every dependency change.
    """
    venv_lib = _venv_root() / "lib"
    bundle_lib = bundle_macos_dir.parent / "lib"

    if not venv_lib.exists():
        return

    if bundle_lib.is_symlink():
        try:
            if os.readlink(bundle_lib) == str(venv_lib):
                return  # already correct, nothing to do
            bundle_lib.unlink()
        except OSError:
            return  # can't read or remove — give up silently
    elif bundle_lib.exists():
        # A real directory at that path means somebody (user? prior
        # install?) materialized it deliberately. Don't clobber.
        return

    try:
        bundle_lib.symlink_to(venv_lib)
    except OSError as exc:
        logger.warning(
            "could not symlink %s -> %s (%s) — site-packages may be "
            "unreachable from the bundle's renamed Python copies, causing "
            "ImportError on launch.",
            bundle_lib,
            venv_lib,
            exc,
        )


def _bundle_macos_dir() -> Path:
    """``~/Applications/Okuro.app/Contents/MacOS`` (no I/O)."""
    return Path.home() / _BUNDLE_RELATIVE_DIR


def _bundle_available() -> bool:
    """True iff the ``.app`` bundle skeleton exists on disk.

    install.sh creates the bundle on macOS before any service install,
    so this is normally True after a successful install. False during
    early bootstrap or on a dev box that hasn't run install.sh yet —
    callers should fall back to libexec/ in that case.
    """
    return _bundle_macos_dir().parent.parent.is_dir()


# Canonical bundle launcher script. Sourced into ``Contents/MacOS/okuro``
# (lowercase — matches CFBundleExecutable, dodges case-insensitive APFS
# conflicts with a separate ``Okuro`` capital file). Bash sets up brew's
# PATH (launchd/Finder strip it) and execs the renamed Python copy living
# inside the same directory. Because the python binary IS inside the
# bundle, AppKit's NSBundle.mainBundle() resolves to Okuro.app and the
# Dock pulls okuro.icns instead of Python.framework's rocket icon.
_BUNDLE_LAUNCHER_SCRIPT = """#!/bin/bash
eval "$(/opt/homebrew/bin/brew shellenv 2>/dev/null)" || \\
eval "$(/usr/local/bin/brew shellenv 2>/dev/null)" || true
exec "$(dirname "$0")/okuro-cli" -m okuro.cli.main "$@"
"""

# Canonical Info.plist for Okuro.app. Written verbatim on every
# ``ensure_okuro_interpreter`` call so the bundle self-heals when an
# install.sh run aborts between binary materialization and heredoc
# (the 2026-05-10 Mac install regression). CFBundleExecutable matches
# the lowercase launcher filename.
_BUNDLE_INFO_PLIST = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleExecutable</key>
    <string>okuro</string>
    <key>CFBundleIdentifier</key>
    <string>ai.okuro.app</string>
    <key>CFBundleName</key>
    <string>Okuro</string>
    <key>CFBundleDisplayName</key>
    <string>Okuro</string>
    <key>CFBundleIconFile</key>
    <string>okuro</string>
    <key>CFBundlePackageType</key>
    <string>APPL</string>
    <key>CFBundleShortVersionString</key>
    <string>3.0.0</string>
    <key>CFBundleVersion</key>
    <string>3.0.0</string>
    <key>LSMinimumSystemVersion</key>
    <string>10.15</string>
    <key>NSHighResolutionCapable</key>
    <true/>
</dict>
</plist>
"""

_LSREGISTER_PATH = (
    "/System/Library/Frameworks/CoreServices.framework/Frameworks/"
    "LaunchServices.framework/Support/lsregister"
)


def _find_repo_icon_png() -> Path | None:
    """Locate the canonical 512x512 brand icon shipped with the package.

    Uses the import path of the ``okuro`` package so it works in both
    editable (``pip install -e .``) and real installs. Returns ``None``
    if the PNG can't be found — caller falls back to whatever icns is
    already on disk.
    """
    try:
        import okuro  # noqa: PLC0415

        pkg_root = Path(okuro.__file__).resolve().parent
        candidate = (
            pkg_root / "web" / "frontend" / "public" / "icons" / "icon-512.png"
        )
        if candidate.is_file():
            return candidate
    except Exception:  # noqa: BLE001
        pass
    return None


def _ensure_bundle_icns(resources_dir: Path) -> None:
    """Regenerate ``Contents/Resources/okuro.icns`` from the current PNG.

    Without this, an updated brand icon never reaches the Dock —
    install.sh's heredoc icns block can be skipped (the same abort path
    that stranded users on the 2026-05-10 stale-bundle bug), and
    ``setApplicationIconImage_`` reads the .icns from disk so the user
    keeps seeing the old render.

    Compares mtimes: if the source PNG is newer than the icns (or the
    icns doesn't exist), rebuild via ``sips`` + ``iconutil``. Falls back
    to copying the PNG if either tool is missing — NSImage accepts
    ``.icns`` and ``.png`` interchangeably as input to
    ``setApplicationIconImage_``.

    No-op on non-Darwin and when the source PNG is unreachable from the
    install (e.g. wheel build that excluded static assets).
    """
    if platform.system() != "Darwin":
        return

    src_png = _find_repo_icon_png()
    if src_png is None:
        return

    icns_path = resources_dir / "okuro.icns"
    if (
        icns_path.is_file()
        and icns_path.stat().st_mtime >= src_png.stat().st_mtime
    ):
        return  # already fresh

    try:
        resources_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        logger.warning("could not create %s (%s)", resources_dir, exc)
        return

    sips_ok = shutil.which("sips") is not None
    iconutil_ok = shutil.which("iconutil") is not None
    if not (sips_ok and iconutil_ok):
        try:
            shutil.copy2(src_png, icns_path)
        except OSError as exc:
            logger.warning("icns fallback copy failed (%s)", exc)
        return

    import tempfile  # noqa: PLC0415

    with tempfile.TemporaryDirectory() as td:
        iconset = Path(td) / "okuro.iconset"
        iconset.mkdir()
        # Apple iconset spec: 16/32/128/256/512 base sizes + @2x retina
        # variants. iconutil tolerates a partial set, so per-size sips
        # failures don't abort the whole rebuild.
        for sz in (16, 32, 128, 256, 512):
            for is_retina in (False, True):
                actual = sz * 2 if is_retina else sz
                suffix = "@2x" if is_retina else ""
                out = iconset / f"icon_{sz}x{sz}{suffix}.png"
                subprocess.run(
                    [
                        "sips", "-z", str(actual), str(actual),
                        str(src_png), "--out", str(out),
                    ],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
        rc = subprocess.run(
            ["iconutil", "-c", "icns", str(iconset), "-o", str(icns_path)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if rc.returncode != 0:
            # Final fallback so we always end with SOMETHING fresh.
            try:
                shutil.copy2(src_png, icns_path)
            except OSError as exc:
                logger.warning(
                    "icns build failed and PNG copy failed (%s)", exc
                )


def _try_compile_native_launcher(target: Path) -> bool:
    """Compile ``launcher.c`` into a native Mach-O at ``target``.

    Why: framework Python (Apple's, Homebrew's) re-execs to its own
    ``Python.framework/Versions/X/Resources/Python.app/Contents/MacOS/Python``
    binary on every venv invocation. After that re-exec the kernel's
    ``_NSGetExecutablePath`` reports the framework path, so AppKit's
    ``NSBundle.mainBundle()`` always resolves to ``Python.app`` — Dock
    title shows "Python", icon shows the rocket. Renaming the binary,
    placing it inside Okuro.app/Contents/MacOS/, mutating pyvenv.cfg,
    nothing fixes this from inside Python — we ARE the framework once
    that re-exec runs.

    A C launcher that EMBEDS Python via ``Py_Main`` skips the re-exec
    entirely: dyld loads ``libpython.dylib`` into THIS process, and
    ``_NSGetExecutablePath`` returns our path inside Okuro.app. AppKit
    walks up MacOS/ → Contents/ → Okuro.app and pulls CFBundleName +
    CFBundleIconFile from the right plist. Tested empirically:

        $ /tmp/Test.app/Contents/MacOS/Test ...
        _NSGetExecutablePath: /tmp/Test.app/Contents/MacOS/Test
        mainBundle: /tmp/Test.app

    On success ``target`` is a real Mach-O binary linked against the
    venv's Python framework. Returns False (and logs a warning) if
    clang isn't available, ``python3-config`` can't be found, or the
    build fails — caller falls back to the bash trampoline + renamed
    Python copy approach (which fixes proc_name + icon but not the
    Dock title because of the framework forwarding above).
    """
    if platform.system() != "Darwin":
        return False
    if shutil.which("clang") is None:
        return False

    src_c = Path(__file__).resolve().parent / "launcher.c"
    if not src_c.is_file():
        return False

    # Skip the compile when the existing binary is at least as new as the
    # source. Without this every service install + every update.sh run
    # would re-invoke clang and re-codesign — wasted I/O and an annoying
    # codesign warning every time. mtime comparison matches the rest of
    # the helpers (no hash; the source is small enough that any edit
    # reliably bumps mtime).
    if (
        target.is_file()
        and target.stat().st_mtime >= src_c.stat().st_mtime
        # Guard against a previous install copying a Mach-O whose mtime
        # was preserved from a different machine. If the file isn't a
        # Mach-O at all (e.g. the bash trampoline from a fallback path),
        # always rebuild so we replace it with the native launcher.
        and target.stat().st_size > 1024
    ):
        return True

    # Locate python3-config. The venv's bin/ doesn't always symlink it
    # (Homebrew venv on the 2026-05-10 test box did not), so fall back
    # to the framework's bin via sysconfig.
    import sysconfig  # noqa: PLC0415

    py_ver = f"{sys.version_info.major}.{sys.version_info.minor}"
    config_path = Path(sys.executable).parent / f"python{py_ver}-config"
    if not config_path.is_file():
        bindir = sysconfig.get_config_var("BINDIR") or ""
        if bindir:
            config_path = Path(bindir) / f"python{py_ver}-config"
    if not config_path.is_file():
        logger.warning(
            "python%s-config not found near %s — cannot compile native launcher",
            py_ver,
            sys.executable,
        )
        return False

    try:
        cflags = subprocess.check_output(
            [str(config_path), "--cflags"],
            text=True,
            stderr=subprocess.PIPE,
        ).split()
        # ``--embed`` is required on Python 3.8+ to get -lpython linked
        # against an embedding host (vs the default --ldflags which is
        # for extension modules and omits -lpython).
        ldflags = subprocess.check_output(
            [str(config_path), "--embed", "--ldflags"],
            text=True,
            stderr=subprocess.PIPE,
        ).split()
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        logger.warning("python%s-config invocation failed: %s", py_ver, exc)
        return False

    try:
        target.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        logger.warning("could not create %s (%s)", target.parent, exc)
        return False

    cmd = ["clang", *cflags, str(src_c), "-o", str(target), *ldflags]
    rc = subprocess.run(cmd, capture_output=True, text=True)
    if rc.returncode != 0:
        logger.warning(
            "clang build failed (rc=%d): %s\nstderr: %s",
            rc.returncode,
            " ".join(cmd),
            (rc.stderr or "").strip()[:500],
        )
        return False

    try:
        target.chmod(0o755)
    except OSError:
        pass
    _ad_hoc_codesign(target)
    return True


def _ensure_macos_bundle_launcher(bundle_macos_dir: Path) -> None:
    """Idempotently rewrite the bundle launcher + Info.plist + LaunchServices.

    Self-healing wrapper around what install.sh's heredocs were meant to
    do. The 2026-05-10 Mac install regression showed install.sh can abort
    between ``ensure_okuro_interpreter`` and the launcher heredoc, leaving
    the bundle with a stale lowercase ``okuro`` bash shim that runs
    ``$VENV/bin/okuro`` (a #!python console-script). The kernel resolves
    that shebang and execs bare ``$VENV/bin/python`` — proc_name reads
    ``python`` and AppKit falls back to Python.framework's rocket icon.

    This function makes every ``ensure_okuro_interpreter('okuro-cli')``
    call repair the bundle, so users no longer depend on install.sh's
    shell heredocs running to completion.

    Steps:
      * Overwrite ``Contents/MacOS/okuro`` with the canonical bash
        trampoline that exec's the renamed python copy.
      * Overwrite ``Contents/Info.plist`` with the canonical plist (the
        existing one may have stale CFBundleExecutable case or other
        drift).
      * Run ``lsregister -f`` to refresh LaunchServices' bundle cache so
        Spotlight launches the new launcher rather than a cached pointer
        to the old shim.

    All steps are best-effort; failures are logged but never raised.
    Bundle launcher is irrelevant for non-CLI service binaries; this
    helper still runs because re-pointing each service install at the
    bundle is the cheapest place to keep the launcher fresh.
    """
    contents_dir = bundle_macos_dir.parent
    launcher_path = bundle_macos_dir / "okuro"
    plist_path = contents_dir / "Info.plist"

    # Prefer the compiled native launcher (defeats Python.framework's
    # __PYVENV_LAUNCHER__ re-exec, gives correct Dock title + icon AND
    # cmd-tab name). Fall back to the bash trampoline + renamed Python
    # copy if clang / python-config aren't available — that path fixes
    # proc_name and icon but NOT the Dock title, which is documented in
    # _try_compile_native_launcher's docstring.
    if not _try_compile_native_launcher(launcher_path):
        try:
            launcher_path.write_text(_BUNDLE_LAUNCHER_SCRIPT)
            launcher_path.chmod(0o755)
        except OSError as exc:
            logger.warning(
                "could not write bundle launcher %s (%s) — Spotlight "
                "launches may still hit the stale bash shim and show "
                "'python' in Activity Monitor.",
                launcher_path,
                exc,
            )
            return  # If launcher write failed, plist refresh is moot.

    try:
        plist_path.write_text(_BUNDLE_INFO_PLIST)
    except OSError as exc:
        logger.warning(
            "could not write bundle Info.plist %s (%s) — bundle metadata "
            "may be stale; Spotlight icon and Force Quit grouping may be "
            "wrong.",
            plist_path,
            exc,
        )
        return

    # Refresh okuro.icns from the package's icon-512.png if the source
    # is newer. install.sh's heredoc icns block can be skipped (same
    # abort path that left the launcher stale); doing it here makes the
    # icon update self-heal on every service install.
    _ensure_bundle_icns(contents_dir / "Resources")

    # Refresh LaunchServices so Spotlight + Dock pick up the new plist
    # AND new icns immediately rather than after the next periodic rescan.
    # Best-effort — missing or non-executable lsregister is non-fatal.
    bundle_root = contents_dir.parent  # ~/Applications/Okuro.app
    if Path(_LSREGISTER_PATH).is_file():
        try:
            subprocess.run(
                [_LSREGISTER_PATH, "-f", str(bundle_root)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
            )
        except (subprocess.SubprocessError, OSError) as exc:
            logger.debug("lsregister -f failed (%s); LaunchServices cache "
                         "will refresh on next periodic rescan", exc)


def ensure_okuro_interpreter(service_name: str = "okuro") -> Path:
    """Materialize a renamed Python interpreter copy and return its path.

    On macOS, prefers ``~/Applications/Okuro.app/Contents/MacOS/<name>``
    so Activity Monitor shows both the right proc_name AND the Okuro
    icon (binary lives inside the bundle → bundle-association icon
    mapping). Falls back to ``<venv>/libexec/<name>`` if the bundle
    isn't present.

    On Linux, always uses ``<venv>/libexec/<name>``.

    Idempotent: re-copies only when the source binary's mtime/size has
    changed (e.g. after a Python point-release upgrade). Returns the
    path either way.

    Falls back to ``sys.executable`` if the copy can't be created
    (read-only filesystem, permission error, weird interpreter setup
    where ``sys.executable`` points at something exotic). Logs a
    warning so the failure is visible without breaking the install.
    """
    src = _real_python_binary()

    # ── macOS bundle path (preferred when the bundle exists) ─────────
    if platform.system() == "Darwin" and _bundle_available():
        bundle_dir = _bundle_macos_dir()
        target = bundle_dir / service_name
        try:
            _materialize_copy(src, target)
            _ensure_bundle_pyvenv_cfg(bundle_dir)
            _ensure_bundle_lib_symlink(bundle_dir)
            _ensure_macos_bundle_launcher(bundle_dir)
            return target
        except OSError as exc:
            logger.warning(
                "could not materialize %s in bundle (%s) — falling back "
                "to <venv>/libexec/. Activity Monitor will still show "
                "%s as the process name but the icon will be the generic "
                "Python rocket instead of Okuro.",
                target,
                exc,
                service_name,
            )
            # fall through to libexec path

    # ── Linux + macOS fallback ───────────────────────────────────────
    target = _venv_root() / _LIBEXEC_RELATIVE_DIR / service_name
    try:
        _materialize_copy(src, target)
        return target
    except OSError as exc:
        logger.warning(
            "could not materialize %s (%s) — falling back to sys.executable. "
            "On macOS this means Activity Monitor will keep showing the "
            "service as 'python' instead of '%s'.",
            target,
            exc,
            service_name,
        )
        return Path(sys.executable)
