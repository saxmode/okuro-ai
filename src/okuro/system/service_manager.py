# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Cross-platform service manager for okuro background services.
# index:
#   imports
#   class TimerSpec
#   class ServiceSpec
#   def _shell_join
#   class ServiceManager
#   class LinuxServiceManager
#   class DarwinServiceManager
#   class ForegroundManager
#   class WindowsServiceManager
#   def get_service_manager
#   def _self_test
# AGENT_HEADER_END -->
"""Cross-platform service manager for okuro background services.

Abstracts systemd (Linux), launchd (macOS) and Task Scheduler (Windows)
into one install / start / stop / status surface. ``spec.to_systemd_unit``,
``spec.to_launchd_plist`` and ``spec.to_scheduled_task_xml`` render the
platform idioms; ``get_service_manager()`` picks the backend at runtime.

Typical use
-----------

    from okuro.system.service_manager import ServiceSpec, get_service_manager

    spec = ServiceSpec(
        name="okuro-orchestrator",
        description="okuro-orchestrator — merged API + web",
        exec_start=[sys.executable, "-m", "okuro.orchestrator.api.serve"],
        working_directory=Path.home() / "okuro",
    )
    mgr = get_service_manager()
    mgr.install(spec)
    mgr.enable("okuro-orchestrator")
    mgr.start("okuro-orchestrator")

Both backends accept the same ``ServiceSpec`` and resolve the platform
idioms (systemd unit vs launchd plist). ``spec.to_systemd_unit()`` and
``spec.to_launchd_plist()`` are public so callers can inspect or write
files without going through a manager (useful for tests and for
generating reference configs during ``okuro init``).
"""

from __future__ import annotations

import logging
import os
import platform
import plistlib
import shutil
import signal
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Optional
from xml.sax.saxutils import escape as _xml_escape
from okuro.db.engine import okuro_home

logger = logging.getLogger("okuro.system.service_manager")


# --- TimerSpec ---------------------------------------------------------


@dataclass
class TimerSpec:
    """Platform-agnostic description of a periodic fire schedule.

    Two modes:

    1. **Interval** (``interval_seconds`` set, ``calendar`` is None):
       "every N seconds" — the common okuro pattern.
       systemd → ``OnBootSec`` + ``OnUnitActiveSec``
       launchd → ``StartInterval``

    2. **Calendar** (``calendar`` set):
       systemd ``OnCalendar=`` expression (e.g. ``"Fri *-*-* 03:00:00"``).
       launchd → ``StartCalendarInterval`` dict (hour, minute, weekday).
       ``interval_seconds`` is ignored when ``calendar`` is set.

    See ``man systemd.time`` for the OnCalendar syntax.
    """

    interval_seconds: int = 0
    on_boot_sec: int = 60
    persistent: bool = True
    calendar: Optional[str] = None
    # launchd calendar fields (used when calendar is set)
    launchd_hour: Optional[int] = None
    launchd_minute: Optional[int] = None
    launchd_weekday: Optional[int] = None  # 0=Sunday .. 6=Saturday

    @property
    def is_calendar(self) -> bool:
        return self.calendar is not None


# --- ServiceSpec --------------------------------------------------------


@dataclass
class ServiceSpec:
    """Platform-agnostic description of a background service.

    Covers the subset of systemd/launchd features okuro actually uses:
    long-running or one-shot process, restart policy, working directory,
    environment variables, network-target dependency, optional timer.
    """

    name: str
    description: str
    exec_start: list[str]
    working_directory: Optional[Path] = None
    restart: bool = True
    restart_sec: int = 5
    environment: dict[str, str] = field(default_factory=dict)
    after_network: bool = True
    service_type: Literal["simple", "oneshot"] = "simple"
    timer: Optional[TimerSpec] = None
    # Darwin-only: bind this service to the user's GUI (Aqua) session so it
    # inherits the same keychain access the user's Terminal has. Without
    # this, launchd UserAgents run as the user but are NOT attached to the
    # Aqua audit session — they cannot read keychain entries (like Claude
    # Code's auth credentials) whose ACL is scoped to GUI-launched
    # processes. The user logs in claude → ``claude auth status`` says
    # logged-in from Terminal but logged-OUT from the orchestrator, every
    # CLI invocation fails with "Not logged in", decomposition errors out.
    # Setting this on services that spawn auth-scoped CLIs (orchestrator,
    # daemon) closes that gap. Cost: the service only runs while the user
    # has an active GUI login session — which matches okuro's UX (the
    # dashboard has nobody to talk to when the user is logged out).
    # No-op on Linux (systemd has its own session model).
    requires_gui_session: bool = False

    # ------------------------------------------------------------------

    @property
    def launchd_label(self) -> str:
        """Reverse-DNS label used by launchctl (``com.okuro.<stem>``)."""
        stem = self.name
        if stem.startswith("okuro-"):
            stem = stem[len("okuro-") :]
        return f"com.okuro.{stem}"

    # ------------------------------------------------------------------

    def to_systemd_unit(self) -> str:
        is_oneshot = self.service_type == "oneshot" or self.timer is not None

        lines: list[str] = ["[Unit]", f"Description={self.description}"]
        if self.after_network and not is_oneshot:
            lines.append("After=network.target")
        # Crash-loop guardrails: cap restart storms on long-running services
        # at 5 failures per 5 minutes. Above that systemd stops trying
        # instead of looping every RestartSec forever, which would flood
        # journald (default rate-limit 10000/30s drops messages) and hide
        # the root cause. StartLimitBurst/IntervalSec belong in [Unit].
        if self.restart and not is_oneshot:
            lines.append("StartLimitBurst=5")
            lines.append("StartLimitIntervalSec=300")
        lines.append("")
        lines.append("[Service]")
        lines.append("Type=oneshot" if is_oneshot else "Type=simple")
        lines.append(f"ExecStart={_shell_join(self.exec_start)}")
        if self.working_directory:
            lines.append(f"WorkingDirectory={self.working_directory}")
        # Restart policy only applies to long-running services. A oneshot
        # that fails should surface the failure, not loop.
        if self.restart and not is_oneshot:
            # Restart=always, not on-failure: systemd's on-failure EXCLUDES
            # termination by SIGTERM/SIGINT/SIGHUP/SIGPIPE, so a stray `kill`
            # (or a session teardown that SIGTERMs the unit) left okuro-embed
            # dead — which froze the MCP via the in-process embedding fallback.
            # `always` revives it on any non-clean exit yet still honours an
            # explicit `systemctl stop` (update.sh's quiesce path), and the
            # StartLimitBurst cap below still defuses crash loops. This also
            # matches the launchd KeepAlive{SuccessfulExit:false} behaviour.
            lines.append("Restart=always")
            lines.append(f"RestartSec={self.restart_sec}")
            # StartLimitAction=none: when the burst cap trips, stop the
            # unit. Anything else (reboot/poweroff) would be destructive
            # on a user-session unit.
            lines.append("StartLimitAction=none")
        for k, v in sorted(self.environment.items()):
            # systemd Environment= accepts quoted values
            lines.append(f'Environment="{k}={v}"')

        # Timer-backed services don't install directly; the .timer unit
        # owns the enable state.
        if self.timer is None:
            lines.extend(["", "[Install]", "WantedBy=default.target"])
        lines.append("")
        return "\n".join(lines)

    def to_systemd_timer_unit(self) -> Optional[str]:
        """Return a sibling ``.timer`` unit when this spec has a timer, else None."""
        if self.timer is None:
            return None
        lines: list[str] = [
            "[Unit]",
            f"Description={self.description} (timer)",
            "",
            "[Timer]",
        ]
        if self.timer.is_calendar:
            lines.append(f"OnCalendar={self.timer.calendar}")
        else:
            lines.append(f"OnBootSec={self.timer.on_boot_sec}")
            lines.append(f"OnUnitActiveSec={self.timer.interval_seconds}")
        if self.timer.persistent:
            lines.append("Persistent=true")
        lines.extend(["", "[Install]", "WantedBy=timers.target", ""])
        return "\n".join(lines)

    # ------------------------------------------------------------------

    def to_launchd_plist(self) -> str:
        """Return a launchd property list (XML) for this spec.

        Uses plistlib-compatible structure and hand-rolls the XML so the
        output is stable / diffable. The generated plist is validated by
        parsing it back via ``plistlib.loads`` in the unit test below.
        """
        args_xml = "\n".join(
            f"        <string>{_xml_escape(a)}</string>" for a in self.exec_start
        )
        out_lines: list[str] = [
            '<?xml version="1.0" encoding="UTF-8"?>',
            '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
            '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">',
            '<plist version="1.0">',
            "<dict>",
            "    <key>Label</key>",
            f"    <string>{_xml_escape(self.launchd_label)}</string>",
            "    <key>ProgramArguments</key>",
            "    <array>",
            args_xml,
            "    </array>",
        ]

        if self.working_directory:
            out_lines.append("    <key>WorkingDirectory</key>")
            out_lines.append(
                f"    <string>{_xml_escape(str(self.working_directory))}</string>"
            )

        out_lines.append("    <key>RunAtLoad</key>")
        out_lines.append("    <true/>")

        # Bind to the GUI/Aqua session when this service spawns CLIs whose
        # keychain entries are ACL-scoped to GUI-launched processes (claude
        # is the live example). See ``ServiceSpec.requires_gui_session``
        # docstring for the full rationale.
        if self.requires_gui_session:
            out_lines.append("    <key>LimitLoadToSessionType</key>")
            out_lines.append("    <string>Aqua</string>")

        # Timer-backed services get StartInterval or StartCalendarInterval
        # and skip KeepAlive — they exit and launchd re-fires them.
        if self.timer is not None and self.timer.is_calendar:
            out_lines.append("    <key>StartCalendarInterval</key>")
            out_lines.append("    <dict>")
            if self.timer.launchd_hour is not None:
                out_lines.append("        <key>Hour</key>")
                out_lines.append(f"        <integer>{self.timer.launchd_hour}</integer>")
            if self.timer.launchd_minute is not None:
                out_lines.append("        <key>Minute</key>")
                out_lines.append(f"        <integer>{self.timer.launchd_minute}</integer>")
            if self.timer.launchd_weekday is not None:
                out_lines.append("        <key>Weekday</key>")
                out_lines.append(f"        <integer>{self.timer.launchd_weekday}</integer>")
            out_lines.append("    </dict>")
        elif self.timer is not None:
            out_lines.append("    <key>StartInterval</key>")
            out_lines.append(f"    <integer>{self.timer.interval_seconds}</integer>")
        elif self.restart:
            out_lines.extend([
                "    <key>KeepAlive</key>",
                "    <dict>",
                "        <key>SuccessfulExit</key>",
                "        <false/>",
                "    </dict>",
                # launchd's closest analogue to systemd's StartLimitBurst.
                # 30s floor between relaunch attempts — a fast-crashing
                # service no longer hammers the system at launchd's 10s
                # default. Coarser than a burst-over-window, same intent:
                # don't thrash.
                "    <key>ThrottleInterval</key>",
                "    <integer>30</integer>",
            ])

        # Always emit EnvironmentVariables on Darwin — launchd user agents
        # start with PATH/HOME/USER stripped, which makes the orchestrator
        # blind to anything outside /usr/bin:/bin (claude in /opt/homebrew/bin
        # or ~/.nvm, for instance, becomes invisible — observed on the
        # 2026-04-28 Mac install report). _augmented_launchd_environment fills
        # in the user-context vars so launchd-spawned services see what the
        # user's shell sees.
        env_to_emit = _augmented_launchd_environment(self.environment)
        if env_to_emit:
            out_lines.append("    <key>EnvironmentVariables</key>")
            out_lines.append("    <dict>")
            for k, v in sorted(env_to_emit.items()):
                out_lines.append(f"        <key>{_xml_escape(k)}</key>")
                out_lines.append(f"        <string>{_xml_escape(v)}</string>")
            out_lines.append("    </dict>")

        # Logs go under ~/Library/Logs/okuro/ — Apple's documented user-scoped
        # log location. /tmp survives reboots on macOS but a strict tmpwatch
        # cleaner (or `sudo periodic daily`) can wipe logs mid-session, and
        # /tmp gets cleared on every reboot with `sudo nvram boot-args=...`
        # tweaks. Library/Logs is also the directory Console.app reads from,
        # so users see okuro output where they expect it. The directory is
        # created by DarwinServiceManager.install() before bootstrap.
        log_dir = str(_darwin_logs_dir())
        out_lines.extend([
            "    <key>StandardOutPath</key>",
            f"    <string>{_xml_escape(log_dir)}/{_xml_escape(self.name)}.log</string>",
            "    <key>StandardErrorPath</key>",
            f"    <string>{_xml_escape(log_dir)}/{_xml_escape(self.name)}.err</string>",
            "</dict>",
            "</plist>",
            "",
        ])
        return "\n".join(out_lines)

    # ------------------------------------------------------------------

    def to_scheduled_task_xml(self) -> str:
        """Return a Windows Task Scheduler definition (XML) for this spec.

        Trigger / logon-type selection mirrors the launchd Aqua binding:

        - ``requires_gui_session`` → ``LogonTrigger`` + ``InteractiveToken``.
          The task runs inside the user's interactive desktop session — the
          Windows analogue of launchd's ``LimitLoadToSessionType=Aqua``.
          Required for orchestrator/daemon: the claude/codex/gemini CLIs
          they spawn read auth tokens from the per-user credential store,
          which a Session-0 / S4U task is not guaranteed to reach. Cost:
          like the Aqua agent, it only runs while the user is logged on.
        - timer-backed → ``TimeTrigger``/``CalendarTrigger`` + ``S4U``.
        - otherwise (long-running, no GUI need) → ``BootTrigger`` + ``S4U``,
          so it survives logout/reboot without a stored password.

        Task Scheduler has no per-task environment block, so when the spec
        carries env vars the action is wrapped in ``cmd.exe /c "set …"`` —
        the only way to inject them through a single Exec action.

        Hand-rolled (not via ``xml.dom``) to match the stable, diffable
        style of ``to_systemd_unit`` / ``to_launchd_plist``; the output is
        validated by parsing it back in the unit tests.
        """
        is_oneshot = self.service_type == "oneshot" or self.timer is not None

        # --- Action: command + arguments (+ optional env wrapper) ------
        interp, rest = self.exec_start[0], self.exec_start[1:]
        if self.environment:
            set_parts = [f'set "{k}={v}"' for k, v in sorted(self.environment.items())]
            inner = " && ".join(set_parts + [_win_arg_join([interp, *rest])])
            command, arguments = "cmd.exe", f'/c "{inner}"'
        else:
            command, arguments = interp, _win_arg_join(rest)

        # Fixed past StartBoundary: Task Scheduler requires one on time /
        # calendar triggers; a past date with a Repetition just means
        # "started long ago, keep repeating". Hardcoded so the XML stays
        # deterministic / diffable (no wall-clock in the generator).
        triggers: list[str] = []
        if self.timer is not None:
            logon_type = "S4U"
            if self.timer.is_calendar:
                hh = self.timer.launchd_hour if self.timer.launchd_hour is not None else 3
                mm = self.timer.launchd_minute if self.timer.launchd_minute is not None else 0
                triggers += [
                    "    <CalendarTrigger>",
                    f"      <StartBoundary>2024-01-01T{hh:02d}:{mm:02d}:00</StartBoundary>",
                    "      <Enabled>true</Enabled>",
                ]
                if self.timer.launchd_weekday is not None:
                    triggers += [
                        "      <ScheduleByWeek>",
                        "        <DaysOfWeek>",
                        f"          <{_WIN_WEEKDAYS[self.timer.launchd_weekday]} />",
                        "        </DaysOfWeek>",
                        "        <WeeksInterval>1</WeeksInterval>",
                        "      </ScheduleByWeek>",
                    ]
                else:
                    triggers += [
                        "      <ScheduleByDay>",
                        "        <DaysInterval>1</DaysInterval>",
                        "      </ScheduleByDay>",
                    ]
                triggers.append("    </CalendarTrigger>")
            else:
                triggers += [
                    "    <TimeTrigger>",
                    "      <StartBoundary>2024-01-01T00:00:00</StartBoundary>",
                    "      <Enabled>true</Enabled>",
                    "      <Repetition>",
                    f"        <Interval>{_iso8601_duration(max(_WIN_MIN_INTERVAL_S, self.timer.interval_seconds))}</Interval>",
                    "        <StopAtDurationEnd>false</StopAtDurationEnd>",
                    "      </Repetition>",
                    "    </TimeTrigger>",
                ]
        elif self.requires_gui_session:
            logon_type = "InteractiveToken"
            triggers += ["    <LogonTrigger>", "      <Enabled>true</Enabled>", "    </LogonTrigger>"]
        else:
            logon_type = "S4U"
            triggers += ["    <BootTrigger>", "      <Enabled>true</Enabled>", "    </BootTrigger>"]

        # Settings: restart policy mirrors systemd Restart=always /
        # launchd KeepAlive. Long-running tasks never time out; oneshots
        # get a finite limit so a hang surfaces instead of wedging.
        settings = [
            "    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>",
            "    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>",
            "    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>",
            "    <StartWhenAvailable>true</StartWhenAvailable>",
            "    <Enabled>true</Enabled>",
        ]
        if is_oneshot:
            settings.append("    <ExecutionTimeLimit>PT1H</ExecutionTimeLimit>")
        else:
            settings.append("    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>")
            if self.restart:
                settings += [
                    "    <RestartOnFailure>",
                    f"      <Interval>{_iso8601_duration(max(_WIN_MIN_INTERVAL_S, self.restart_sec))}</Interval>",
                    "      <Count>5</Count>",
                    "    </RestartOnFailure>",
                ]

        exec_lines = [
            "    <Exec>",
            f"      <Command>{_xml_escape(command)}</Command>",
            f"      <Arguments>{_xml_escape(arguments)}</Arguments>",
        ]
        if self.working_directory:
            exec_lines.append(
                f"      <WorkingDirectory>{_xml_escape(str(self.working_directory))}</WorkingDirectory>"
            )
        exec_lines.append("    </Exec>")

        xml = [
            '<?xml version="1.0" encoding="UTF-16"?>',
            '<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">',
            "  <RegistrationInfo>",
            f"    <Description>{_xml_escape(self.description)}</Description>",
            f"    <URI>\\okuro\\{_xml_escape(self.name)}</URI>",
            "  </RegistrationInfo>",
            "  <Triggers>",
            *triggers,
            "  </Triggers>",
            "  <Principals>",
            '    <Principal id="Author">',
            f"      <LogonType>{logon_type}</LogonType>",
            "      <RunLevel>LeastPrivilege</RunLevel>",
            "    </Principal>",
            "  </Principals>",
            "  <Settings>",
            *settings,
            "  </Settings>",
            '  <Actions Context="Author">',
            *exec_lines,
            "  </Actions>",
            "</Task>",
            "",
        ]
        return "\n".join(xml)


_LAUNCHD_BASELINE_PATH_DIRS: tuple[str, ...] = (
    # Apple Silicon Homebrew default
    "/opt/homebrew/bin",
    "/opt/homebrew/sbin",
    # Intel Homebrew + system-wide installs
    "/usr/local/bin",
    "/usr/local/sbin",
    # Apple defaults (always present, but we want them ordered last)
    "/usr/bin",
    "/bin",
    "/usr/sbin",
    "/sbin",
)


def _nvm_version_key(name: str) -> tuple:
    """Parse an nvm version directory name into a semver-comparable tuple.

    nvm names directories ``v<major>.<minor>.<patch>`` (or plain version like
    ``v22.21.0``). A plain string sort breaks at the second component:
    ``v22.9.0`` > ``v22.21.0`` lexicographically because ``'9' > '2'``.
    Parsing each component as int gives ``(22, 21, 0) > (22, 9, 0)`` as
    expected. Non-numeric directory names (``system``, ``lts/iron``) sort
    last as ``(0, 0, 0)`` so a real version always wins.
    """
    stripped = name.lstrip("v")
    parts = stripped.split(".")
    parsed: list[int] = []
    for part in parts:
        try:
            parsed.append(int(part))
        except ValueError:
            return (0, 0, 0)
    # Pad to length 3 so (22,) and (22, 0, 0) compare equal at major level.
    while len(parsed) < 3:
        parsed.append(0)
    return tuple(parsed)


def _user_path_dirs(home: Optional[Path] = None) -> list[str]:
    """Return the list of user-context PATH directories at the time of call.

    Computed dynamically — every caller gets the CURRENT layout of node
    version managers, package manager bin dirs, and Homebrew prefixes.
    Used by:

      * ``_augmented_launchd_environment`` — at plist render / spawn time
      * ``okuro.system.cli_installer._resolve_user_binary`` — at every
        dashboard refresh, so a CLI installed AFTER the orchestrator
        started shows up without a service reinstall

    Order matters — earlier entries win in ``shutil.which``. Newest nvm
    Node bin is prepended (most users `npm install -g` against the
    currently-active Node), then fnm/asdf shims, then Homebrew prefixes,
    then user-local bins, then the n version manager.

    Non-existent dirs are tolerated; we list candidates rather than
    filter at construction time so the same caller can pass us to
    ``shutil.which(..., path=":".join(_user_path_dirs()))`` and have it
    apply ``os.access`` on the real lookup. Filtering during construction
    would be redundant work.
    """
    if home is None:
        try:
            home = Path.home()
        except (OSError, RuntimeError):
            home = Path("/tmp")

    dirs: list[str] = []

    # Newest nvm-managed Node bin (prepended so an nvm node wins over
    # any older Homebrew node when binaries collide).
    nvm_dir = home / ".nvm" / "versions" / "node"
    if nvm_dir.is_dir():
        try:
            versions = sorted(
                (p for p in nvm_dir.iterdir() if p.is_dir()),
                key=lambda p: _nvm_version_key(p.name),
                reverse=True,
            )
            for v in versions:
                bin_dir = v / "bin"
                if bin_dir.is_dir():
                    dirs.append(str(bin_dir))
                    break  # newest only
        except OSError:
            pass

    # `n` (alternative Node version manager). System install at /usr/local/n.
    n_dir = Path("/usr/local/n/versions/node")
    if n_dir.is_dir():
        try:
            n_versions = sorted(
                (p for p in n_dir.iterdir() if p.is_dir()),
                key=lambda p: _nvm_version_key(p.name),
                reverse=True,
            )
            for v in n_versions:
                bin_dir = v / "bin"
                if bin_dir.is_dir():
                    dirs.append(str(bin_dir))
                    break
        except OSError:
            pass

    # `fnm` (Fast Node Manager). Two install layouts in the wild —
    # XDG-spec (~/.local/share/fnm) and home-dotted (~/.fnm).
    for fnm_root in (
        home / ".local" / "share" / "fnm",
        home / ".fnm",
    ):
        default_bin = fnm_root / "aliases" / "default" / "bin"
        if default_bin.is_dir():
            dirs.append(str(default_bin))
            break

    # `asdf` shims — single dir, every shimmed binary lands here.
    asdf_shims = home / ".asdf" / "shims"
    if asdf_shims.is_dir():
        dirs.append(str(asdf_shims))

    # Homebrew prefixes (Apple Silicon then Intel/Linux).
    dirs.extend(["/opt/homebrew/bin", "/opt/homebrew/sbin"])
    dirs.extend(["/usr/local/bin", "/usr/local/sbin"])

    # User-local bins (pipx, poetry, npm-global, cargo, volta).
    dirs.append(str(home / ".local" / "bin"))
    dirs.append(str(home / ".npm-global" / "bin"))
    dirs.append(str(home / ".cargo" / "bin"))
    dirs.append(str(home / ".volta" / "bin"))

    # Anthropic's Claude Code install script (curl …/install.sh) lands here.
    dirs.append(str(home / ".claude" / "local" / "bin"))

    # System defaults (always present, ordered last).
    dirs.extend(["/usr/bin", "/bin", "/usr/sbin", "/sbin"])

    return dirs


def _augmented_launchd_environment(env: dict) -> dict:
    """Inject PATH/HOME/USER into a launchd plist EnvironmentVariables block.

    macOS launchd user agents (``LaunchAgents``) start with a stripped
    environment — PATH defaults to ``/usr/bin:/bin`` (often less),
    ``HOME`` is empty, ``USER`` is empty. Any tool the orchestrator
    wants to invoke that lives in ``/opt/homebrew/bin``, ``~/.nvm/.../bin``,
    ``~/.local/share/fnm/.../bin`` or ``~/.asdf/shims`` becomes
    invisible. ``shutil.which`` returns None and ``subprocess.run([
    'claude', ...])`` raises ``FileNotFoundError``.

    PATH layout (high→low priority):

      1. Capture-time ``os.environ['PATH']`` (the install-time interactive
         shell — covers exotic version managers if the user had them
         active when running install.sh).
      2. ``_user_path_dirs()`` — recomputed at call time, so callers that
         re-invoke this (e.g. ``spawn_orchestrator`` per task) pick up
         CLIs installed after the plist was rendered.

    HOME and USER come from the OS user database (``pwd``) so subagent-
    scoped ``$HOME`` overrides don't leak into services.

    Spec-supplied values always win — operators can override any of these
    by setting them in ``ServiceSpec.environment`` directly.
    """
    out = dict(env)

    if "PATH" not in out:
        captured = os.environ.get("PATH", "")
        candidates: list[str] = []
        if captured:
            candidates.extend(captured.split(":"))
        candidates.extend(_user_path_dirs())
        # Deduplicate while preserving order.
        seen: set[str] = set()
        deduped: list[str] = []
        for d in candidates:
            if d and d not in seen:
                seen.add(d)
                deduped.append(d)
        out["PATH"] = ":".join(deduped)

    if "HOME" not in out:
        try:
            import pwd
            out["HOME"] = pwd.getpwuid(os.getuid()).pw_dir
        except (KeyError, ImportError):
            out["HOME"] = str(Path.home())

    if "USER" not in out:
        try:
            import pwd
            out["USER"] = pwd.getpwuid(os.getuid()).pw_name
        except (KeyError, ImportError):
            out["USER"] = os.environ.get("USER", "")

    return out


def _darwin_logs_dir() -> Path:
    """Return the macOS user-log directory for okuro services.

    Resolved at call time (not import time) so tests can patch ``Path.home``.
    """
    return Path.home() / "Library" / "Logs" / "okuro"


def _shell_join(argv: list[str]) -> str:
    """Join argv for a systemd ExecStart line.

    systemd parses ExecStart with simple whitespace splitting and supports
    double-quoted arguments. We only quote args that contain whitespace or
    special chars. This is not a general-purpose shell quoter — it's
    exactly enough for the command shapes okuro spawns.
    """
    parts: list[str] = []
    for a in argv:
        if any(c in a for c in " \t'\"$"):
            parts.append('"' + a.replace('"', '\\"') + '"')
        else:
            parts.append(a)
    return " ".join(parts)


# --- Windows Task Scheduler helpers ------------------------------------

_WIN_WEEKDAYS: tuple[str, ...] = (
    "Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday",
)

# Task Scheduler rejects RestartOnFailure/Interval and Repetition/Interval
# below 1 minute ("value ... out of range"). systemd/launchd accept seconds,
# so okuro's restart_sec=5 is fine there but must be floored for the XML.
_WIN_MIN_INTERVAL_S = 60


def _iso8601_duration(seconds: int) -> str:
    """Seconds → ISO-8601 duration (``PT…``) for Task Scheduler fields.

    Used for ``<Repetition><Interval>`` and ``<RestartOnFailure><Interval>``.
    Floors at 1s so a zero never produces the invalid bare ``PT``.
    """
    s = max(1, int(seconds))
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    out = "PT"
    if h:
        out += f"{h}H"
    if m:
        out += f"{m}M"
    if sec or out == "PT":
        out += f"{sec}S"
    return out


def _win_arg_join(argv: list[str]) -> str:
    """Join argv into a Windows command-line string, quoting as needed.

    Mirrors ``_shell_join`` for the Task Scheduler ``<Arguments>`` field:
    only args containing whitespace or a quote get wrapped. Not a general
    Windows quoter — exactly enough for the command shapes okuro spawns.
    """
    parts: list[str] = []
    for a in argv:
        if not a or any(c in a for c in ' \t"'):
            parts.append('"' + a.replace('"', '\\"') + '"')
        else:
            parts.append(a)
    return " ".join(parts)


# --- ServiceManager API ------------------------------------------------


class ServiceManager:
    """Platform-agnostic interface. Use ``get_service_manager()`` to pick."""

    def install(self, spec: ServiceSpec) -> Path:
        raise NotImplementedError

    def uninstall(self, name: str) -> None:
        raise NotImplementedError

    def start(self, name: str) -> None:
        raise NotImplementedError

    def stop(self, name: str) -> None:
        raise NotImplementedError

    def restart(self, name: str) -> None:
        raise NotImplementedError

    def reload(self, name: str) -> None:
        """Signal a running service to reload its config in place (SIGHUP).

        No restart, no dropped in-flight work — the process re-reads its
        config where it stands. Only meaningful for services that install a
        SIGHUP handler (e.g. okuro-daemon → scheduler.reload()).
        """
        raise NotImplementedError

    def enable(self, name: str) -> None:
        raise NotImplementedError

    def disable(self, name: str) -> None:
        raise NotImplementedError

    def run_now(self, name: str) -> None:
        """Fire the .service unit immediately, bypassing any timer."""
        raise NotImplementedError

    def status(self, name: str) -> dict:
        raise NotImplementedError


class LinuxServiceManager(ServiceManager):
    """systemd --user backend.

    User-level services (no root required). Units live under
    ``~/.config/systemd/user/`` and run via ``systemctl --user``.

    Timer-backed services are expressed as a ``<name>.service`` +
    ``<name>.timer`` pair. Start/stop/enable/disable/status automatically
    dispatch to the ``.timer`` when it exists, so the caller can use the
    same short name regardless of whether the service is long-running or
    timer-triggered.
    """

    UNIT_DIR = Path.home() / ".config" / "systemd" / "user"

    def _service_path(self, name: str) -> Path:
        return self.UNIT_DIR / f"{name}.service"

    def _timer_path(self, name: str) -> Path:
        return self.UNIT_DIR / f"{name}.timer"

    def _target(self, name: str) -> str:
        """Return ``<name>.timer`` if the timer file exists, else ``<name>.service``."""
        if self._timer_path(name).exists():
            return f"{name}.timer"
        return f"{name}.service"

    def ensure_linger(self) -> tuple[bool, str]:
        """Enable systemd user-linger so services survive the user logging out.

        Without linger the user's systemd instance is torn down on logout and
        ``okuro-orchestrator`` / ``okuro-daemon`` die with it. CEO/CTO
        installing on a laptop and closing the lid would see the daemon stop.

        Returns ``(enabled, message)``. Idempotent — safe to call on every
        install. Tries without sudo first (some distros ship a polkit rule
        allowing the session user), then ``sudo -n`` (non-interactive), then
        gives up with a clear manual instruction.
        """
        import getpass

        if not shutil.which("loginctl"):
            return False, "loginctl not found (non-systemd environment?)"

        user = getpass.getuser()
        probe = subprocess.run(
            ["loginctl", "show-user", user, "--property=Linger"],
            capture_output=True,
            text=True,
        )
        if "Linger=yes" in probe.stdout:
            return True, "already enabled"

        for argv in (
            ["loginctl", "enable-linger", user],
            ["sudo", "-n", "loginctl", "enable-linger", user],
        ):
            if argv[0] == "sudo" and not shutil.which("sudo"):
                continue
            r = subprocess.run(argv, capture_output=True, text=True)
            if r.returncode == 0:
                return True, "enabled via " + ("polkit" if argv[0] != "sudo" else "sudo")

        return (
            False,
            f"could not auto-enable; run manually: sudo loginctl enable-linger {user}",
        )

    def install(self, spec: ServiceSpec) -> Path:
        self.UNIT_DIR.mkdir(parents=True, exist_ok=True)
        service_path = self._service_path(spec.name)
        service_path.write_text(spec.to_systemd_unit())

        timer_unit = spec.to_systemd_timer_unit()
        if timer_unit is not None:
            timer_path = self._timer_path(spec.name)
            timer_path.write_text(timer_unit)

        subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
        return service_path

    def uninstall(self, name: str) -> None:
        # Stop/disable both the .timer (if present) and the .service so we
        # don't leave dangling units after the files are removed.
        for unit in (f"{name}.timer", f"{name}.service"):
            subprocess.run(["systemctl", "--user", "disable", unit], check=False)
            subprocess.run(["systemctl", "--user", "stop", unit], check=False)

        for path in (self._timer_path(name), self._service_path(name)):
            if path.exists():
                path.unlink()

        subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)

    def _reset_failed(self, name: str) -> None:
        """Clear systemd's start-rate limit before (re)starting.

        A unit whose binary vanished — the re-clone renames the old checkout
        while its services still run — restarts into 203/EXEC until systemd
        stops it with "Start request repeated too quickly" and refuses every
        later `start` with exit 1. Measured 2026-09-12 on a Linux upgrade:
        the updater's `service start okuro-embed` was refused for that reason
        alone and the whole update aborted with every service stopped.
        reset-failed is idempotent and harmless on a healthy unit.
        """
        subprocess.run(
            ["systemctl", "--user", "reset-failed", self._target(name)],
            check=False, capture_output=True,
        )

    def start(self, name: str) -> None:
        self._reset_failed(name)
        subprocess.run(["systemctl", "--user", "start", self._target(name)], check=True)

    def stop(self, name: str) -> None:
        subprocess.run(["systemctl", "--user", "stop", self._target(name)], check=True)

    def restart(self, name: str) -> None:
        self._reset_failed(name)
        subprocess.run(["systemctl", "--user", "restart", self._target(name)], check=True)

    def reload(self, name: str) -> None:
        # SIGHUP the main PID only (--kill-whom=main), never worker threads,
        # and always target the .service (a .timer has nothing to reload).
        subprocess.run(
            ["systemctl", "--user", "kill", "-s", "HUP", "--kill-whom=main",
             f"{name}.service"],
            check=True,
        )

    def enable(self, name: str) -> None:
        subprocess.run(["systemctl", "--user", "enable", self._target(name)], check=True)

    def disable(self, name: str) -> None:
        subprocess.run(["systemctl", "--user", "disable", self._target(name)], check=True)

    def run_now(self, name: str) -> None:
        subprocess.run(["systemctl", "--user", "start", f"{name}.service"], check=True)

    def status(self, name: str) -> dict:
        target = self._target(name)
        r = subprocess.run(
            ["systemctl", "--user", "is-active", target],
            capture_output=True,
            text=True,
        )
        state = r.stdout.strip() or "unknown"
        return {
            "active": state == "active",
            "state": state,
            "backend": "systemd",
            "target": target,
        }


class DarwinServiceManager(ServiceManager):
    """launchd user-agent backend.

    Agents live under ``~/Library/LaunchAgents/com.okuro.<stem>.plist``
    and are controlled via ``launchctl``.

    ``load -w`` / ``unload -w`` are deprecated on macOS 11+ in favor of
    ``bootstrap`` / ``bootout`` on ``gui/<uid>``. We detect the version
    once at import time: 11+ takes the modern path, older stays on ``-w``.
    Both paths are idempotent and degrade to "already-loaded / not-loaded"
    without raising.
    """

    AGENTS_DIR = Path.home() / "Library" / "LaunchAgents"

    @staticmethod
    def _use_bootstrap() -> bool:
        """True when we should prefer ``bootstrap``/``bootout`` (macOS 11+).

        Uses ``platform.mac_ver()`` so tests don't rely on the host actually
        being Darwin. Falls back to legacy ``-w`` when the version can't be
        parsed.
        """
        try:
            ver = platform.mac_ver()[0]
            if not ver:
                return False
            major = int(ver.split(".")[0])
            return major >= 11
        except Exception:
            return False

    # Cached domain string — set by the first successful bootstrap, then
    # reused by start/stop/status so kickstart targets the same domain we
    # actually loaded into. None until probed.
    _DOMAIN_CACHE: Optional[str] = None

    @classmethod
    def _candidate_domains(cls) -> list[str]:
        """Domains to try, in preference order.

        ``user/<uid>`` first because newer macOS (Tahoe / Sequoia) rejects
        ``gui/<uid>`` bootstrap with "Bad request"/EIO unless the calling
        process has full TCC + GUI-session privileges. ``user/<uid>`` is
        the broader user-context domain and accepts service registration
        from any session (Terminal, ssh, the wizard's uvicorn worker).
        """
        uid = os.getuid()
        return [f"user/{uid}", f"gui/{uid}"]

    @classmethod
    def _gui_domain(cls) -> str:
        """Back-compat alias — returns the cached resolved domain or first candidate."""
        return cls._DOMAIN_CACHE or cls._candidate_domains()[0]

    def _plist_path(self, name: str) -> Path:
        stem = name[len("okuro-"):] if name.startswith("okuro-") else name
        return self.AGENTS_DIR / f"com.okuro.{stem}.plist"

    def _label(self, name: str) -> str:
        stem = name[len("okuro-"):] if name.startswith("okuro-") else name
        return f"com.okuro.{stem}"

    def _service_target(self, name: str) -> str:
        """``<domain>/<label>`` — launchctl service target. Uses the cached
        domain that previous bootstrap actually succeeded with.

        Prefer ``_resolve_target`` for stop/restart — that probes via
        ``launchctl print`` and returns the domain the service is actually
        registered in (which may differ from the cache after macOS
        promotes a plist marked ``LimitLoadToSessionType=Aqua`` from
        ``user/<uid>`` to ``gui/<uid>``).
        """
        return f"{self._gui_domain()}/{self._label(name)}"

    def _resolve_target(self, name: str) -> Optional[str]:
        """Find the domain the service is actually registered in.

        Probes ``launchctl print <domain>/<label>`` for each candidate
        domain (cached first, then ``user/<uid>``, then ``gui/<uid>``)
        and returns the first ``<domain>/<label>`` whose probe exits 0.
        Updates ``_DOMAIN_CACHE`` to the resolved domain. Returns
        ``None`` if the service isn't registered in any candidate domain.

        Why: plists pinned to ``LimitLoadToSessionType=Aqua`` register
        only under ``gui/<uid>`` even when bootstrapped via ``user/<uid>``
        (commit c63cd83 pinned com.okuro.orchestrator + com.okuro.daemon
        to Aqua). A fresh CLI process has an empty ``_DOMAIN_CACHE`` and
        ``_service_target`` returns ``user/<uid>/<label>``; ``stop`` then
        exits 113 ("Could not find service in domain for uid <uid>").
        """
        label = self._label(name)
        domains_to_try: list[str] = []
        if type(self)._DOMAIN_CACHE:
            domains_to_try.append(type(self)._DOMAIN_CACHE)
        for d in self._candidate_domains():
            if d not in domains_to_try:
                domains_to_try.append(d)
        for domain in domains_to_try:
            target = f"{domain}/{label}"
            rc = subprocess.run(
                ["launchctl", "print", target],
                capture_output=True, text=True,
            )
            if rc.returncode == 0:
                type(self)._DOMAIN_CACHE = domain
                return target
        return None

    def _load(self, plist_path: Path) -> None:
        """Bootstrap a plist into the user's launchd domain.

        Tries ``user/<uid>`` first, falls back to ``gui/<uid>``, then to the
        legacy ``launchctl load -w`` path. Caches the first domain that
        succeeded so subsequent start/stop/status target the same one.

        Raises ``RuntimeError`` with a combined stderr diagnostic if every
        path fails. Previously this used ``check=False`` and silently
        swallowed bootstrap failures — install would write the plist and
        return, then ``start`` exited 113 because the service wasn't in any
        domain (observed on macOS 26.4 Tahoe). The wizard now sees a real
        exception and surfaces it via OnboardingState.services_install.
        """
        attempts: list[tuple[str, str, str]] = []  # (cmd, stdout, stderr)

        if self._use_bootstrap():
            for domain in self._candidate_domains():
                rc = subprocess.run(
                    ["launchctl", "bootstrap", domain, str(plist_path)],
                    capture_output=True, text=True,
                )
                attempts.append((f"bootstrap {domain}", rc.stdout or "", rc.stderr or ""))
                # rc.returncode == 0 is the obvious success path; "service already
                # bootstrapped" is also success-equivalent — keep the cache stable.
                already_loaded = (
                    rc.returncode != 0
                    and "already" in (rc.stderr or "").lower()
                )
                if rc.returncode == 0 or already_loaded:
                    type(self)._DOMAIN_CACHE = domain
                    logger.info("launchctl bootstrap %s succeeded for %s", domain, plist_path.name)
                    return

        # Legacy fallback — `load -w` doesn't take a domain string and works
        # back to macOS 10.10. Less rich state semantics than bootstrap but
        # gets the agent registered when the modern path keeps refusing.
        rc = subprocess.run(
            ["launchctl", "load", "-w", str(plist_path)],
            capture_output=True, text=True,
        )
        attempts.append(("load -w (legacy)", rc.stdout or "", rc.stderr or ""))
        if rc.returncode == 0:
            # No domain to cache — start/stop will probe state via `launchctl list <label>`.
            type(self)._DOMAIN_CACHE = None
            logger.info("launchctl load -w succeeded for %s (legacy path)", plist_path.name)
            return

        # All paths exhausted — raise with the actual launchctl errors.
        msg_lines = [f"launchctl could not register {plist_path.name}:"]
        for cmd, out, err in attempts:
            tail = (err or out).strip().splitlines()[-2:]
            msg_lines.append(f"  - {cmd} -> {' / '.join(tail) or '(no output)'}")
        msg_lines.append(
            "If the failure is 'Bad request' or EIO, macOS may require approval "
            "in System Settings → General → Login Items & Extensions; otherwise "
            "verify the venv python is not quarantined: `xattr -l "
            f"{plist_path}`"
        )
        raise RuntimeError("\n".join(msg_lines))

    def _unload(self, plist_path: Path) -> None:
        """Bootout a plist. Idempotent — silently OK if nothing to unload.

        Tries the cached domain first (set by a prior _load), then walks the
        candidate list, then falls back to legacy `unload -w`. Failures are
        non-fatal — _unload runs before _load on every install to clear any
        prior registration.
        """
        if self._use_bootstrap():
            domains_to_try: list[str] = []
            if type(self)._DOMAIN_CACHE:
                domains_to_try.append(type(self)._DOMAIN_CACHE)
            for d in self._candidate_domains():
                if d not in domains_to_try:
                    domains_to_try.append(d)
            for domain in domains_to_try:
                rc = subprocess.run(
                    ["launchctl", "bootout", domain, str(plist_path)],
                    capture_output=True, text=True,
                )
                if rc.returncode == 0:
                    return
        # Legacy fallback — also fine to no-op.
        subprocess.run(
            ["launchctl", "unload", "-w", str(plist_path)],
            capture_output=True, text=True,
        )

    def install(self, spec: ServiceSpec, load: bool = True) -> Path:
        self.AGENTS_DIR.mkdir(parents=True, exist_ok=True)
        # Logs dir must exist before launchctl bootstraps the plist —
        # launchd will refuse to write StandardOutPath / StandardErrorPath
        # to a missing directory and the service ends up "loaded but never
        # runs". The plist points at ~/Library/Logs/okuro/ (see
        # _darwin_logs_dir), so we mkdir it here.
        _darwin_logs_dir().mkdir(parents=True, exist_ok=True)
        plist_path = self._plist_path(spec.name)
        plist_path.write_text(spec.to_launchd_plist())
        if not load:
            return plist_path
        # If the plist is already loaded from a previous install, unload
        # first so the new definition takes effect. Non-fatal.
        self._unload(plist_path)
        self._load(plist_path)
        return plist_path

    def uninstall(self, name: str) -> None:
        plist_path = self._plist_path(name)
        if plist_path.exists():
            self._unload(plist_path)
            plist_path.unlink()

    def start(self, name: str) -> None:
        """Start the service.

        Walks candidate domains (cached first, then user/<uid> + gui/<uid>)
        looking for where launchd actually registered the plist. macOS may
        auto-promote a service into ``gui/<uid>`` even when we bootstrapped
        via ``user/<uid>`` (visible via ``LimitLoadToSessionType = Aqua`` in
        ``launchctl list`` output). If the probe finds ``state = running``,
        return clean — RunAtLoad=true means the service auto-starts at
        bootstrap, so kickstart-on-already-running returns 113 and we'd
        report a spurious "start failed" otherwise.
        """
        if not self._use_bootstrap():
            subprocess.run(["launchctl", "start", self._label(name)], check=True)
            return

        label = self._label(name)
        domains_to_try: list[str] = []
        if type(self)._DOMAIN_CACHE:
            domains_to_try.append(type(self)._DOMAIN_CACHE)
        for d in self._candidate_domains():
            if d not in domains_to_try:
                domains_to_try.append(d)

        last_err: str = ""
        for domain in domains_to_try:
            target = f"{domain}/{label}"
            print_rc = subprocess.run(
                ["launchctl", "print", target],
                capture_output=True, text=True,
            )
            if print_rc.returncode != 0:
                # Service isn't in this domain — try the next one.
                continue
            # Found the right domain. Cache it for future ops.
            type(self)._DOMAIN_CACHE = domain
            if "state = running" in (print_rc.stdout or ""):
                return  # already running (RunAtLoad=true auto-started it)
            ks_rc = subprocess.run(
                ["launchctl", "kickstart", target],
                capture_output=True, text=True,
            )
            if ks_rc.returncode == 0:
                return
            last_err = (ks_rc.stderr or ks_rc.stdout or "").strip()
        raise RuntimeError(
            f"launchctl could not start {label}: "
            + (last_err or f"service not found in any of {domains_to_try}")
        )

    def stop(self, name: str) -> None:
        if self._use_bootstrap():
            # Resolve the actual domain via `launchctl print` — services
            # pinned to LimitLoadToSessionType=Aqua only register under
            # gui/<uid>, so the cached/default user/<uid> would 113.
            target = self._resolve_target(name)
            if target is None:
                raise RuntimeError(
                    f"launchctl could not find {self._label(name)} in any of "
                    f"{self._candidate_domains()}"
                )
            # `kill -TERM <target>` is the modern stop verb; the legacy
            # `launchctl stop` still works on 11+ but the modern one makes
            # it clear we're signalling and not asking KeepAlive to restart.
            subprocess.run(
                ["launchctl", "kill", "TERM", target],
                check=True,
            )
        else:
            subprocess.run(["launchctl", "stop", self._label(name)], check=True)

    def reload(self, name: str) -> None:
        """SIGHUP the service via ``launchctl kill HUP <target>`` (no restart)."""
        if self._use_bootstrap():
            target = self._resolve_target(name)
            if target is None:
                raise RuntimeError(
                    f"launchctl could not find {self._label(name)} in any of "
                    f"{self._candidate_domains()}"
                )
            subprocess.run(["launchctl", "kill", "HUP", target], check=True)
        else:
            subprocess.run(["launchctl", "kill", "SIGHUP", self._label(name)], check=True)

    def restart(self, name: str) -> None:
        """Atomic restart via ``launchctl kickstart -k <target>``.

        ``-k`` sends SIGTERM and re-launches in one operation against the
        resolved target — no race window between stop and start, and no
        chance of stop hitting one domain and start probing another.
        Falls back to stop+start on the legacy (``load -w``) path.
        """
        if self._use_bootstrap():
            target = self._resolve_target(name)
            if target is None:
                # Service isn't registered in any candidate domain — defer
                # to start() to walk the candidates and surface a useful
                # error if it isn't installed at all.
                self.start(name)
                return
            rc = subprocess.run(
                ["launchctl", "kickstart", "-k", target],
                capture_output=True, text=True,
            )
            if rc.returncode != 0:
                raise RuntimeError(
                    f"launchctl kickstart -k {target} failed: "
                    + (rc.stderr or rc.stdout or "").strip()
                )
            return
        self.stop(name)
        self.start(name)

    def enable(self, name: str) -> None:
        # On 11+, `enable` flips a persistent disabled-flag; bootstrapping
        # the plist already clears it. Calling `enable` additionally is
        # harmless and helpful if the service was previously disabled by
        # another process.
        if self._use_bootstrap():
            subprocess.run(
                ["launchctl", "enable", self._service_target(name)],
                check=False,
            )
        else:
            plist_path = self._plist_path(name)
            subprocess.run(
                ["launchctl", "load", "-w", str(plist_path)], check=False
            )

    def run_now(self, name: str) -> None:
        self.start(name)

    def disable(self, name: str) -> None:
        if self._use_bootstrap():
            subprocess.run(
                ["launchctl", "disable", self._service_target(name)],
                check=False,
            )
        else:
            plist_path = self._plist_path(name)
            subprocess.run(
                ["launchctl", "unload", "-w", str(plist_path)], check=False
            )

    def status(self, name: str) -> dict:
        # Plist-existence first so callers can distinguish "we never
        # installed this" from "we installed it but it's not running".
        # Symmetric with LinuxServiceManager, which returns
        # ``state="not-found"`` for never-installed names. Without this,
        # the legacy-cleanup loop in ``cmd_init._install_services`` flips
        # to stop/disable/uninstall on every absent service name and
        # produces a stderr cascade of "Could not find service ..." from
        # launchctl on a completely fresh install.
        plist_path = self._plist_path(name)
        if not plist_path.exists():
            return {"active": False, "state": "not-found", "backend": "launchd"}
        label = self._label(name)
        # `launchctl list <label>` prints a plist-shaped dict on stdout
        # and exits 0 if the service is loaded, nonzero if not.
        r = subprocess.run(
            ["launchctl", "list", label], capture_output=True, text=True
        )
        loaded = r.returncode == 0
        has_pid = loaded and '"PID"' in r.stdout
        return {
            "active": has_pid,
            "state": "active" if has_pid else ("loaded" if loaded else "inactive"),
            "backend": "launchd",
        }


# --- ForegroundManager (audit Sprint 3E #28) ---------------------------


def _foreground_root() -> Path:
    """Root directory for ForegroundManager bookkeeping (PID files, recipes, logs).

    Resolved at call time so tests can monkey-patch ``Path.home``.
    """
    return okuro_home() / "foreground"


def _recipe_for_spec(spec: "ServiceSpec", log_path: Path, pid_path: Path) -> str:
    """Render a copy-paste shell recipe a user can run to start the service.

    The recipe targets ``tmux`` first (preserves a real terminal so the
    user can attach later) and falls back to ``nohup`` (no tmux required).
    Both paths route stdout/stderr to the log file and persist the PID.
    """
    cmd = _shell_join(spec.exec_start)
    cwd = str(spec.working_directory) if spec.working_directory else str(Path.home())
    env_lines = "".join(
        f"export {k}={v!r}\n" for k, v in sorted(spec.environment.items())
    )
    return (
        f"#!/usr/bin/env bash\n"
        f"# okuro foreground recipe — {spec.name}\n"
        f"# {spec.description}\n"
        f"#\n"
        f"# This script is the systemd-less fallback for non-systemd Linux\n"
        f"# (Alpine / Void / Devuan / WSL1 / bare Docker). Run it once to\n"
        f"# start the service in the background; it stays alive until the\n"
        f"# host reboots or you stop it via the matching pid file.\n"
        f"#\n"
        f"# Pid file:  {pid_path}\n"
        f"# Log file:  {log_path}\n"
        f"set -euo pipefail\n"
        f"\n"
        f"# 1. cd to the working directory the service expects.\n"
        f"cd {cwd!r}\n"
        f"\n"
        f"# 2. export the env the service needs.\n"
        f"{env_lines}"
        f"\n"
        f"# 3. preferred path: launch under tmux so you can `tmux attach` later.\n"
        f"#    If tmux isn't installed we fall back to nohup.\n"
        f"if command -v tmux >/dev/null 2>&1; then\n"
        f"    tmux new-session -d -s {spec.name!r} {cmd!r}\n"
        f"    # Capture the tmux pane PID so we can stop the service cleanly.\n"
        f"    tmux list-panes -t {spec.name!r} -F '#{{pane_pid}}' > {str(pid_path)!r}\n"
        f"    echo 'started under tmux session {spec.name} — attach with: tmux attach -t {spec.name}'\n"
        f"else\n"
        f"    # nohup keeps the process alive after the shell exits; the trailing\n"
        f"    # & detaches it; $! captures the PID; > redirects stdout, 2>&1\n"
        f"    # folds stderr in.\n"
        f"    nohup {cmd} > {str(log_path)!r} 2>&1 &\n"
        f"    echo $! > {str(pid_path)!r}\n"
        f"    echo \"started under nohup — pid $(cat {str(pid_path)!r})\"\n"
        f"fi\n"
        f"\n"
        f"# To stop the service later:\n"
        f"#   kill $(cat {pid_path})\n"
    )


class ForegroundManager(ServiceManager):
    """Foreground / nohup fallback for platforms without systemd or launchd.

    Targets the audit-Sprint-3E gap (#28): users on Alpine / Void / Devuan
    / WSL1 / bare Docker get a Python traceback today because the factory
    raises ``RuntimeError`` when ``systemctl`` is missing. This manager
    keeps the same surface (``install`` / ``start`` / ``stop`` / ``status``
    / ``enable`` / ``disable`` / ``restart`` / ``run_now``) but delegates to
    plain ``subprocess.Popen`` + a PID file under
    ``~/.okuro/foreground/<service>.pid``, plus a copy-paste shell recipe
    written to ``~/.okuro/foreground/recipes/<service>.sh``.

    Layout
    ------
    ``~/.okuro/foreground/``
        ``recipes/<service>.sh`` — chmod 0755, contains a tmux/nohup launcher
        ``logs/<service>.log``   — combined stdout/stderr from the process
        ``<service>.pid``        — the PID we manage

    Limitations vs systemd/launchd:
        - no auto-restart on crash (the recipe documents how to wrap it)
        - no boot-time autostart (the user runs the recipe themselves)
        - no resource accounting / journal integration

    These are documented in the recipe file the user gets, so the
    degradation is explicit rather than silent.
    """

    def __init__(self, root: Optional[Path] = None) -> None:
        self.root = root or _foreground_root()
        self.recipes_dir = self.root / "recipes"
        self.logs_dir = self.root / "logs"

    # --- paths ---------------------------------------------------------

    def _pid_path(self, name: str) -> Path:
        return self.root / f"{name}.pid"

    def _recipe_path(self, name: str) -> Path:
        return self.recipes_dir / f"{name}.sh"

    def _log_path(self, name: str) -> Path:
        return self.logs_dir / f"{name}.log"

    def _ensure_dirs(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self.recipes_dir.mkdir(parents=True, exist_ok=True)
        self.logs_dir.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _is_alive(pid: int) -> bool:
        """Return True if signal 0 succeeds (process exists, signal-able)."""
        try:
            os.kill(pid, 0)
        except (ProcessLookupError, PermissionError, OSError):
            return False
        return True

    def _read_pid(self, name: str) -> Optional[int]:
        path = self._pid_path(name)
        if not path.exists():
            return None
        try:
            txt = path.read_text().strip()
            return int(txt.splitlines()[0]) if txt else None
        except (ValueError, OSError):
            return None

    # --- ServiceManager surface ---------------------------------------

    def install(self, spec: ServiceSpec) -> Path:
        """Write the recipe file (0755), print it to stdout, return its path.

        No system integration happens — the user can either run the recipe
        themselves or call ``start`` to have okuro spawn the process.
        """
        self._ensure_dirs()
        recipe_path = self._recipe_path(spec.name)
        recipe = _recipe_for_spec(
            spec, log_path=self._log_path(spec.name), pid_path=self._pid_path(spec.name)
        )
        recipe_path.write_text(recipe)
        recipe_path.chmod(0o755)
        # Echo to stdout so the user sees what got written without having
        # to hunt through ~/.okuro. Quiet on tests via stdout-capture.
        print(
            f"[okuro] foreground recipe written: {recipe_path}\n"
            f"        run it once to start {spec.name} under tmux/nohup.",
            file=sys.stderr,
        )
        return recipe_path

    def uninstall(self, name: str) -> None:
        """Stop the service if running, remove the recipe + pid file."""
        try:
            self.stop(name)
        except Exception:
            # stop() is best-effort during uninstall — keep going so the
            # user can clean up after a crashed service too.
            pass
        for path in (self._recipe_path(name), self._pid_path(name)):
            if path.exists():
                try:
                    path.unlink()
                except OSError:
                    pass

    def start(self, name: str) -> None:
        """Launch the service detached, write PID file, route logs.

        Equivalent to ``nohup`` from a shell: stdin /dev/null, stdout/stderr
        appended to ``logs/<name>.log``, child detached from controlling
        terminal via ``start_new_session``.
        """
        # Re-load the spec from the canonical registry so we can launch
        # without the caller passing it in (matches the LinuxServiceManager
        # signature where ``start`` only needs the service name).
        spec = self._lookup_spec(name)
        if spec is None:
            raise RuntimeError(
                f"ForegroundManager.start: no spec registered for {name!r}"
            )

        self._ensure_dirs()
        # If a previous PID is still alive, don't double-launch.
        existing = self._read_pid(name)
        if existing is not None and self._is_alive(existing):
            return

        log_path = self._log_path(name)
        log_fh = open(log_path, "ab")
        try:
            proc = subprocess.Popen(
                spec.exec_start,
                cwd=str(spec.working_directory) if spec.working_directory else None,
                env={**os.environ, **spec.environment},
                stdout=log_fh,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                start_new_session=True,
            )
        finally:
            log_fh.close()
        self._pid_path(name).write_text(f"{proc.pid}\n")

    def stop(self, name: str) -> None:
        """Send SIGTERM to the recorded PID; remove the PID file."""
        pid = self._read_pid(name)
        if pid is None:
            return
        if self._is_alive(pid):
            try:
                os.kill(pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass
        # Best-effort PID-file cleanup; the next status() call would treat
        # the file as authoritative and lie about state.
        try:
            self._pid_path(name).unlink()
        except FileNotFoundError:
            pass

    def restart(self, name: str) -> None:
        self.stop(name)
        self.start(name)

    def reload(self, name: str) -> None:
        """SIGHUP the recorded PID in place (no restart)."""
        pid = self._read_pid(name)
        if pid is not None and self._is_alive(pid):
            try:
                os.kill(pid, signal.SIGHUP)
            except (ProcessLookupError, PermissionError):
                pass

    def enable(self, name: str) -> None:
        # Foreground services have no boot-time integration — "enable" is
        # the same as "install" (writes the recipe). Keep the call cheap
        # and idempotent so callers can always call enable after install.
        spec = self._lookup_spec(name)
        if spec is not None and not self._recipe_path(name).exists():
            self.install(spec)

    def disable(self, name: str) -> None:
        # No persistent enabled-state to flip. ``stop`` is the correct
        # action; ``uninstall`` is the right call if the user wants the
        # recipe gone too.
        return None

    def run_now(self, name: str) -> None:
        # Same semantics as start for foreground services.
        self.start(name)

    def status(self, name: str) -> dict:
        pid = self._read_pid(name)
        alive = pid is not None and self._is_alive(pid)
        return {
            "active": alive,
            "state": "active" if alive else "inactive",
            "backend": "foreground",
            "pid": pid,
            "pid_file": str(self._pid_path(name)),
            "log_file": str(self._log_path(name)),
            "recipe": str(self._recipe_path(name)),
        }

    # --- helpers -------------------------------------------------------

    @staticmethod
    def _lookup_spec(name: str) -> Optional[ServiceSpec]:
        """Resolve a service name to its ServiceSpec via the canonical registry.

        Imported lazily to dodge a circular import (install.py imports from
        service_manager.py).
        """
        try:
            from okuro.system.install import get_service_registry

            return get_service_registry().get(name)
        except Exception:
            return None


class WindowsServiceManager(ServiceManager):
    """Windows Task Scheduler backend (per-user, no elevation).

    Registers each service as a scheduled task under the ``\\okuro\\``
    folder via ``schtasks.exe``. Trigger + logon type come from the
    ServiceSpec (see ``ServiceSpec.to_scheduled_task_xml``):

    - ``requires_gui_session`` services (orchestrator, daemon) →
      LogonTrigger + InteractiveToken: run in the user's interactive
      session so the claude/codex/gemini CLIs they spawn can reach the
      per-user credential store. They start at logon and stop at logout —
      the Windows analogue of launchd's Aqua-pinned LaunchAgent.
    - everything else (embed, timers) → BootTrigger/TimeTrigger + S4U:
      survives logout and reboot with no stored password.

    XML task definitions are written under ``~/.okuro/windows/`` for
    inspection/diffing, mirroring the systemd unit / launchd plist files.

    Caveat: Task Scheduler enforces a 1-minute floor on repetition
    intervals — fine for okuro's minute/calendar timers; sub-minute
    timers would be silently clamped by Windows.
    """

    TASK_FOLDER = "okuro"
    UNIT_DIR = okuro_home() / "windows"

    def _task_name(self, name: str) -> str:
        return f"{self.TASK_FOLDER}\\{name}"

    def _xml_path(self, name: str) -> Path:
        return self.UNIT_DIR / f"{name}.xml"

    def install(self, spec: ServiceSpec) -> Path:
        self.UNIT_DIR.mkdir(parents=True, exist_ok=True)
        xml_path = self._xml_path(spec.name)
        # schtasks /XML imports a UTF-16 document — encode to match the
        # <?xml encoding="UTF-16"?> header in to_scheduled_task_xml().
        xml_path.write_text(spec.to_scheduled_task_xml(), encoding="utf-16")
        subprocess.run(
            ["schtasks", "/Create", "/TN", self._task_name(spec.name),
             "/XML", str(xml_path), "/F"],
            check=True,
        )
        return xml_path

    def uninstall(self, name: str) -> None:
        tn = self._task_name(name)
        # End a running instance, then delete; both tolerant of absence so
        # uninstall is idempotent (mirrors the systemd/launchd managers).
        subprocess.run(["schtasks", "/End", "/TN", tn], check=False)
        subprocess.run(["schtasks", "/Delete", "/TN", tn, "/F"], check=False)
        xml_path = self._xml_path(name)
        if xml_path.exists():
            xml_path.unlink()

    def start(self, name: str) -> None:
        subprocess.run(["schtasks", "/Run", "/TN", self._task_name(name)], check=True)

    def stop(self, name: str) -> None:
        subprocess.run(["schtasks", "/End", "/TN", self._task_name(name)], check=True)

    def restart(self, name: str) -> None:
        tn = self._task_name(name)
        subprocess.run(["schtasks", "/End", "/TN", tn], check=False)
        subprocess.run(["schtasks", "/Run", "/TN", tn], check=True)

    def enable(self, name: str) -> None:
        subprocess.run(
            ["schtasks", "/Change", "/TN", self._task_name(name), "/ENABLE"], check=True
        )

    def disable(self, name: str) -> None:
        subprocess.run(
            ["schtasks", "/Change", "/TN", self._task_name(name), "/DISABLE"], check=True
        )

    def run_now(self, name: str) -> None:
        subprocess.run(["schtasks", "/Run", "/TN", self._task_name(name)], check=True)

    def status(self, name: str) -> dict:
        tn = self._task_name(name)
        r = subprocess.run(
            ["schtasks", "/Query", "/TN", tn, "/FO", "LIST"],
            capture_output=True,
            text=True,
        )
        state = "unknown"
        for line in r.stdout.splitlines():
            if line.strip().lower().startswith("status:"):
                state = line.split(":", 1)[1].strip() or "unknown"
                break
        # schtasks reports Running / Ready / Disabled.
        return {
            "active": state.lower() == "running",
            "state": state,
            "backend": "taskscheduler",
            "target": tn,
        }


# --- dispatch ----------------------------------------------------------


def get_service_manager() -> ServiceManager:
    """Return the service manager matching the current platform.

    Dispatch table:
        - Linux + systemctl on PATH → ``LinuxServiceManager`` (systemd-user)
        - Linux without systemctl   → ``ForegroundManager`` (Alpine, WSL1,
          Devuan, bare Docker — degrades to nohup/tmux instead of crashing)
        - Darwin + launchctl        → ``DarwinServiceManager`` (launchd)
        - Anything else             → ``ForegroundManager`` with a warning
          that the platform isn't first-class

    Audit Sprint 3E (#28): historically this raised ``RuntimeError`` on
    non-systemd Linux, which propagated up through the wizard as a Python
    traceback. The fallback keeps okuro usable on every Linux flavour.
    """
    system = platform.system()
    if system == "Linux":
        if shutil.which("systemctl"):
            return LinuxServiceManager()
        # Common cases hitting this path: WSL1 (no systemd at all), WSL2
        # without `systemd=true` in /etc/wsl.conf, Alpine (OpenRC), Void
        # (runit), Devuan (sysvinit), Docker / podman containers without
        # --systemd. We fall through to ForegroundManager — the user gets
        # a working install with a copy-paste tmux/nohup recipe instead of
        # a stack trace.
        logger.warning(
            "systemctl not found — falling back to ForegroundManager "
            "(no auto-restart, no boot-time autostart). See "
            "~/.okuro/foreground/recipes/ for the launcher script."
        )
        return ForegroundManager()
    if system == "Darwin":
        if not shutil.which("launchctl"):
            raise RuntimeError(
                "launchctl not found on PATH — this doesn't look like macOS"
            )
        return DarwinServiceManager()
    if system == "Windows":
        # Task Scheduler backend (per-user, no elevation). orchestrator/
        # daemon get an interactive-logon task (credential-store access);
        # embed + timers get S4U so they survive logout/reboot. See
        # ServiceSpec.to_scheduled_task_xml for the trigger/logon rationale.
        if shutil.which("schtasks"):
            return WindowsServiceManager()
        logger.warning(
            "schtasks not found — falling back to ForegroundManager. "
            "okuro background services won't auto-start on this Windows box."
        )
        return ForegroundManager()
    # Anything else: ForegroundManager is the safest fallback so the user
    # can at least run okuro under nohup. The console warning makes the
    # second-class status explicit.
    logger.warning(
        "okuro service management is not first-class on %r — falling back "
        "to ForegroundManager (no auto-restart, no boot-time autostart).",
        system,
    )
    return ForegroundManager()


# --- self test (for import-time sanity + CI) ---------------------------


def _self_test() -> dict:
    """Build ServiceSpecs (long-running + timer-backed), round-trip through
    both generators, validate. Returns the produced artifacts for
    inspection. Does not touch the filesystem, does not shell out.
    """
    import sys

    # Case A: long-running orchestrator
    _test_dir = Path("/tmp/okuro-selftest")
    long_spec = ServiceSpec(
        name="okuro-orchestrator",
        description="okuro-orchestrator — merged API + SPA",
        exec_start=[sys.executable, "-m", "okuro.orchestrator.api.serve"],
        working_directory=_test_dir,
        environment={"OKURO_PORT": "13333"},
    )

    unit = long_spec.to_systemd_unit()
    assert "[Unit]" in unit and "[Service]" in unit and "[Install]" in unit
    assert "Type=simple" in unit
    assert "Description=okuro-orchestrator — merged API + SPA" in unit
    assert f"ExecStart={sys.executable}" in unit
    assert "Restart=always" in unit
    assert 'Environment="OKURO_PORT=13333"' in unit
    # Crash-loop guardrails (H1): long-running services get burst cap in
    # [Unit] and StartLimitAction=none in [Service].
    assert "StartLimitBurst=5" in unit
    assert "StartLimitIntervalSec=300" in unit
    assert "StartLimitAction=none" in unit
    assert long_spec.to_systemd_timer_unit() is None

    plist_xml = long_spec.to_launchd_plist()
    parsed = plistlib.loads(plist_xml.encode())
    assert parsed["Label"] == "com.okuro.orchestrator"
    assert parsed["ProgramArguments"][0] == sys.executable
    assert parsed["ProgramArguments"][1:] == ["-m", "okuro.orchestrator.api.serve"]
    assert parsed["WorkingDirectory"] == str(_test_dir)
    assert parsed["RunAtLoad"] is True
    assert parsed["KeepAlive"]["SuccessfulExit"] is False
    # Launchd parallel to systemd burst cap: ThrottleInterval=30 on
    # KeepAlive services.
    assert parsed["ThrottleInterval"] == 30
    assert parsed["EnvironmentVariables"]["OKURO_PORT"] == "13333"
    assert "StartInterval" not in parsed
    expected_log = str(_darwin_logs_dir() / "okuro-orchestrator.log")
    assert parsed["StandardOutPath"] == expected_log, (
        f"unexpected StandardOutPath {parsed['StandardOutPath']!r} (want {expected_log!r})"
    )

    # Case B: timer-backed refresh
    timer_spec = ServiceSpec(
        name="okuro-refresh",
        description="okuro-refresh — regenerate agent context files",
        exec_start=[
            sys.executable,
            "-c",
            "from okuro.sense.providers import generate_all; generate_all()",
        ],
        working_directory=Path("/tmp/okuro-root"),
        timer=TimerSpec(interval_seconds=300, on_boot_sec=60),
    )

    timer_unit_service = timer_spec.to_systemd_unit()
    timer_unit_timer = timer_spec.to_systemd_timer_unit()
    # Oneshot service: no [Install] (timer owns it), no Restart, Type=oneshot
    assert "Type=oneshot" in timer_unit_service
    assert "[Install]" not in timer_unit_service
    assert "Restart=" not in timer_unit_service
    # Burst cap is restart-only; oneshots should NOT carry it.
    assert "StartLimitBurst" not in timer_unit_service
    assert "StartLimitAction" not in timer_unit_service
    # Timer unit: OnBootSec / OnUnitActiveSec / Persistent / timers.target
    assert timer_unit_timer is not None
    assert "[Timer]" in timer_unit_timer
    assert "OnBootSec=60" in timer_unit_timer
    assert "OnUnitActiveSec=300" in timer_unit_timer
    assert "Persistent=true" in timer_unit_timer
    assert "WantedBy=timers.target" in timer_unit_timer

    # Launchd: timer services embed StartInterval, drop KeepAlive
    timer_plist_xml = timer_spec.to_launchd_plist()
    timer_parsed = plistlib.loads(timer_plist_xml.encode())
    assert timer_parsed["Label"] == "com.okuro.refresh"
    assert timer_parsed["StartInterval"] == 300
    assert "KeepAlive" not in timer_parsed
    # ThrottleInterval is paired with KeepAlive — timers shouldn't have it.
    assert "ThrottleInterval" not in timer_parsed

    # Case C: calendar-based schedule (e.g. maintenance every Friday 3am)
    cal_spec = ServiceSpec(
        name="okuro-maintenance",
        description="okuro-maintenance — weekly cleanup",
        exec_start=[sys.executable, "-c", "from okuro.sense.maintenance import run; run()"],
        timer=TimerSpec(
            calendar="Fri *-*-* 03:00:00",
            launchd_hour=3,
            launchd_minute=0,
            launchd_weekday=5,  # Friday
        ),
    )

    cal_service = cal_spec.to_systemd_unit()
    cal_timer = cal_spec.to_systemd_timer_unit()
    assert "Type=oneshot" in cal_service
    assert cal_timer is not None
    assert "OnCalendar=Fri *-*-* 03:00:00" in cal_timer
    assert "OnBootSec" not in cal_timer
    assert "OnUnitActiveSec" not in cal_timer
    assert "Persistent=true" in cal_timer

    cal_plist_xml = cal_spec.to_launchd_plist()
    cal_parsed = plistlib.loads(cal_plist_xml.encode())
    assert "StartCalendarInterval" in cal_parsed
    assert cal_parsed["StartCalendarInterval"]["Hour"] == 3
    assert cal_parsed["StartCalendarInterval"]["Minute"] == 0
    assert cal_parsed["StartCalendarInterval"]["Weekday"] == 5
    assert "StartInterval" not in cal_parsed

    return {
        "long_unit": unit,
        "long_plist": plist_xml,
        "long_parsed": parsed,
        "timer_service_unit": timer_unit_service,
        "timer_timer_unit": timer_unit_timer,
        "timer_plist": timer_plist_xml,
        "timer_parsed": timer_parsed,
        "cal_service_unit": cal_service,
        "cal_timer_unit": cal_timer,
        "cal_plist": cal_plist_xml,
        "cal_parsed": cal_parsed,
    }


if __name__ == "__main__":
    out = _self_test()
    print("=== systemd unit (long-running) ===")
    print(out["long_unit"])
    print("=== systemd service unit (timer-backed, oneshot) ===")
    print(out["timer_service_unit"])
    print("=== systemd timer unit ===")
    print(out["timer_timer_unit"])
    print("=== launchd plist (timer-backed, StartInterval=300) ===")
    print(out["timer_plist"])
