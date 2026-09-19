# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Install identity on the wire — who owns a loopback port, and is it us.
# index:
#   imports
#   def this_install
#   def install_identity
#   def identity_from_payload
#   def is_own_install
#   def describe_install
#   def socket_owner_uid
#   def probe_identity
#   def verify_port
#   def holder_line
# AGENT_HEADER_END -->
"""Install identity on the wire.

THE CLASS THIS CLOSES. okuro binds fixed loopback ports (orchestrator,
embed, daemon HTTP-MCP) with no notion of which INSTALL owns them. Two
installs on one host — a second Linux account, a test account, a
container — collide silently:

* the loser dies with a bare ``[Errno 98] address already in use`` that
  names a port and nothing else;
* the winner answers every probe as if it were the caller's own install;
* no client verifies whom it talked to, so a bearer token minted by
  install A is posted to install B's API, and B's answers are rendered
  as A's state;
* a stdio MCP session keeps working (it is a child of the client, not of
  the daemon), so the outage is invisible from inside a session.

Measured 2026-09-16: after a reboot a second account's install won the
race for 3090/13333/13334, the first account's three user units hit the
restart limit, and the second install's 768-dim embed service answered
the first install's queries against a 1024-dim column.

THE MECHANISM. Every okuro HTTP service publishes one identity block on
its health endpoint under the key :data:`IDENTITY_KEY`. Every in-repo
client of those ports compares the answering identity against its own
before trusting — or, crucially, before sending it a credential.

FALLBACK THAT MATTERS. An install running code older than this module
publishes no identity block. "No identity" must NOT read as "ours":
:func:`socket_owner_uid` reads the listening socket's owning uid from
``/proc/net/tcp`` (read-only, no privileges, no process column needed),
which settles ownership even against a pre-identity install. That is the
exact case the 2026-09-16 incident presents, so it is the case the
mechanism is designed around rather than an afterthought.
"""

from __future__ import annotations

import json
import logging
import os
import re
import socket
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

log = logging.getLogger("okuro.system.install_identity")

#: Key under which the identity block is nested in every health payload.
IDENTITY_KEY = "install"

#: Health paths probed, in order, when asking "who is on this port".
#: The orchestrator serves ``/api/health`` (the SPA owns ``/health`` as a
#: client-side route); daemon and embed serve ``/health``.
HEALTH_PATHS = ("/api/health", "/health")

#: Logical service names this module knows how to talk about.
SERVICE_ORCHESTRATOR = "orchestrator"
SERVICE_EMBED = "embed"
SERVICE_DAEMON = "daemon"

#: Unit name per logical service — for the operator-facing lines.
UNIT_NAMES = {
    SERVICE_ORCHESTRATOR: "okuro-orchestrator",
    SERVICE_EMBED: "okuro-embed",
    SERVICE_DAEMON: "okuro-daemon",
}

_PROBE_TIMEOUT_S = 1.5


# ---------------------------------------------------------------------------
# This install
# ---------------------------------------------------------------------------

def _install_home() -> str:
    """The data home of THIS install — ``$OKURO_HOME`` or ``~/.okuro``."""
    from okuro.db.engine import okuro_home

    return str(okuro_home())


def _install_root() -> Optional[str]:
    """Repo root of the CODE this process is running, best effort.

    ``src/okuro/system/install_identity.py`` → repo root. This is what an
    operator recognises (the repo path), so it leads the
    collision line; the data home is the machine-readable key.
    """
    try:
        return str(Path(__file__).resolve().parents[3])
    except Exception:  # pragma: no cover — path shape is fixed in-repo
        return None


def _username(uid: int) -> Optional[str]:
    try:
        import pwd

        return pwd.getpwuid(uid).pw_name
    except Exception:
        return None


def this_install() -> dict:
    """Identity keys of THIS install — the two fields a match turns on.

    ``install_home`` + ``uid``. Deliberately NOT the code root: two
    worktrees of the same repo under one account are the same install and
    must not read as a collision.
    """
    uid = os.getuid()
    return {
        "install_home": _install_home(),
        "install_root": _install_root(),
        "uid": uid,
        "username": _username(uid),
    }


def _process_started_at() -> Optional[str]:
    """ISO-8601 UTC timestamp of this process's start, or None."""
    try:
        from okuro.system.code_version import _process_start

        started = _process_start()
    except Exception:
        started = None
    if started is None:
        return None
    return datetime.fromtimestamp(started, timezone.utc).isoformat()


def install_identity(unit: Optional[str] = None) -> dict:
    """The block a service publishes on its health endpoint.

    One shape, one helper — every service nests exactly this under
    :data:`IDENTITY_KEY` so a client can compare without knowing which
    service answered.

    Every optional field degrades to ``None`` rather than raising: a
    health endpoint that can be taken down by its own diagnostics is
    worse than one that reports an unknown edition.
    """
    ident = this_install()

    code_rev = None
    try:
        from okuro.system.code_version import loaded_code_version

        code_rev = loaded_code_version()
    except Exception as exc:  # noqa: BLE001
        log.debug("install_identity: code rev unavailable (%s)", exc)

    edition = None
    try:
        from okuro.ai_models.edition import detect_edition

        edition = detect_edition()
    except Exception as exc:  # noqa: BLE001
        log.debug("install_identity: edition unavailable (%s)", exc)

    ident.update(
        {
            "code_rev": code_rev,
            "edition": edition,
            "unit": unit,
            "pid": os.getpid(),
            "started_at": _process_started_at(),
        }
    )
    return ident


# ---------------------------------------------------------------------------
# Comparing
# ---------------------------------------------------------------------------

def identity_from_payload(payload: object) -> Optional[dict]:
    """Pull the identity block out of a health payload, or None."""
    if not isinstance(payload, dict):
        return None
    block = payload.get(IDENTITY_KEY)
    if not isinstance(block, dict):
        return None
    if "install_home" not in block and "uid" not in block:
        return None
    return block


def is_own_install(remote: Optional[dict], mine: Optional[dict] = None) -> bool:
    """True when ``remote`` is this install.

    Match is ``install_home`` AND ``uid``. A remote missing either key
    cannot be confirmed, and an unconfirmable identity is NOT ours —
    fails closed, because the whole point is to stop trusting a stranger.
    """
    if not isinstance(remote, dict):
        return False
    mine = mine or this_install()
    r_home = remote.get("install_home")
    r_uid = remote.get("uid")
    if r_home is None or r_uid is None:
        return False
    try:
        r_uid = int(r_uid)
    except (TypeError, ValueError):
        return False
    return str(r_home) == str(mine["install_home"]) and r_uid == int(mine["uid"])


def describe_install(remote: Optional[dict], uid: Optional[int] = None) -> str:
    """Human-readable "who is this" — the tail of the collision line.

    Prefers the code root an operator would recognise, falls back to the
    data home, and finally to the bare uid when the holder publishes no
    identity at all (an install older than this mechanism). In that last
    case it tries ``/proc`` for the root and MARKS it inferred, because a
    path an operator is about to act on must not be presented as measured
    when it was deduced from a sibling process.
    """
    if isinstance(remote, dict):
        where = remote.get("install_root") or remote.get("install_home")
        r_uid = remote.get("uid")
        r_user = remote.get("username")
        if where and r_uid is not None:
            who = f"uid {r_uid}" + (f", {r_user}" if r_user else "")
            return f"{where} ({who})"
        if where:
            return str(where)
    if uid is not None:
        user = _username(uid)
        who = f"uid {uid}" + (f", {user}" if user else "")
        root = holder_root_from_proc(uid)
        if root:
            return f"{root} ({who}, install path inferred from /proc)"
        return f"uid {uid}" + (f" ({user})" if user else "")
    return "unknown"


# ---------------------------------------------------------------------------
# Who holds a port
# ---------------------------------------------------------------------------

def socket_owner_uid(port: int, host: str = "127.0.0.1") -> Optional[int]:
    """uid owning the LISTENING socket on ``port``, or None.

    Reads ``/proc/net/tcp`` (+ ``tcp6``). No privileges, no ``ss``, and —
    unlike ``ss -ltnp`` as a non-root user — it works across uids: the
    process column is empty for another user's socket, but the uid column
    is not. This is what makes the mechanism work against an install too
    old to publish an identity block.

    Returns None on non-Linux, on a parse failure, or when nothing is
    listening.
    """
    try:
        packed = socket.inet_aton(host)
    except OSError:
        return None
    # /proc/net/tcp prints the local address little-endian hex.
    want_addr = "".join(f"{b:02X}" for b in reversed(packed))
    want = f"{want_addr}:{port:04X}"
    wildcard = f"00000000:{port:04X}"

    for path in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            with open(path, "r", encoding="ascii", errors="replace") as fh:
                next(fh, None)  # header
                for line in fh:
                    cols = line.split()
                    if len(cols) < 8:
                        continue
                    if cols[3] != "0A":  # TCP_LISTEN
                        continue
                    local = cols[1]
                    if path.endswith("tcp6"):
                        # ::  / ::ffff:127.0.0.1 — compare the port only;
                        # a v6 listener on this port still holds it for v4.
                        if not local.endswith(f":{port:04X}"):
                            continue
                    elif local not in (want, wildcard):
                        continue
                    try:
                        return int(cols[7])
                    except ValueError:
                        continue
        except (OSError, StopIteration):
            continue
    return None


#: Paths in another install's command line that give away its repo root.
#: ``<root>/.venv/libexec/okuro-orchestrator`` → ``<root>``.
_ROOT_PATTERNS = (
    re.compile(r"(/[^\s'\"]*?)/(?:\.venv|venv)/(?:bin|libexec)/okuro[-a-z]*"),
    re.compile(r"(/[^\s'\"]*?)/src/okuro[/'\"\s]"),
)


def holder_root_from_proc(uid: int) -> Optional[str]:
    """Best-effort repo root of the install running as ``uid`` — INFERRED.

    Used only when the holder publishes no identity block, i.e. it runs
    okuro from before this mechanism existed. It reads ``/proc/*/cmdline``
    (world-readable) and ``/proc/*/status`` for the uid; ``exe``, ``cwd``,
    ``fd`` and ``maps`` are all blocked across uids, so the command line is
    the only evidence available without privileges.

    Deliberately labelled inferred everywhere it surfaces: the listening
    process itself usually has a setproctitle'd command line with no path
    at all, so the root normally comes from a SIBLING process of the same
    install (a multiprocessing forkserver, a spawned CLI). That is strong
    evidence about the install, not proof about the listener.

    Returns None on non-Linux, when nothing matches, or when /proc is
    unreadable.
    """
    try:
        entries = os.listdir("/proc")
    except OSError:
        return None
    for name in entries:
        if not name.isdigit():
            continue
        try:
            with open(f"/proc/{name}/status", "r", errors="replace") as fh:
                for line in fh:
                    if line.startswith("Uid:"):
                        if int(line.split()[1]) != uid:
                            raise StopIteration
                        break
                else:
                    continue
            with open(f"/proc/{name}/cmdline", "rb") as fh:
                cmdline = fh.read().replace(b"\0", b" ").decode("utf-8", "replace")
        except (OSError, StopIteration, ValueError, IndexError):
            continue
        if "okuro" not in cmdline:
            continue
        for pattern in _ROOT_PATTERNS:
            match = pattern.search(cmdline)
            if match:
                return match.group(1)
    return None


@dataclass
class PortProbe:
    """What answered on a port, if anything."""

    port: int
    reachable: bool = False
    identity: Optional[dict] = None
    payload: Optional[dict] = None
    path: Optional[str] = None
    error: Optional[str] = None


def probe_identity(
    port: int,
    host: str = "127.0.0.1",
    timeout: float = _PROBE_TIMEOUT_S,
    paths: tuple[str, ...] = HEALTH_PATHS,
) -> PortProbe:
    """GET the health endpoints on ``port`` and read the identity block.

    Unauthenticated on purpose: every okuro health endpoint is
    auth-exempt, and asking "who are you" must never require handing a
    credential to the party whose identity is in question.
    """
    probe = PortProbe(port=port)
    for path in paths:
        url = f"http://{host}:{port}{path}"
        try:
            with urllib.request.urlopen(url, timeout=timeout) as resp:
                raw = resp.read(65536)
                if not 200 <= resp.status < 300:
                    probe.error = f"http {resp.status}"
                    continue
        except urllib.error.HTTPError as exc:
            probe.reachable = True
            probe.error = f"http {exc.code}"
            continue
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            probe.error = f"{type(exc).__name__}: {exc}"
            continue
        probe.reachable = True
        try:
            payload = json.loads(raw.decode("utf-8", "replace"))
        except (ValueError, UnicodeDecodeError):
            # A 200 that is not JSON is not an answer from a health
            # endpoint. The orchestrator's SPA catch-all serves index.html
            # for /health (a client-side route), so this branch fires on
            # every probe of that port — and must not leave `path` pointing
            # at a body that was never parsed.
            continue
        if not isinstance(payload, dict):
            continue
        probe.path = path
        probe.payload = payload
        ident = identity_from_payload(payload)
        if ident is not None:
            probe.identity = ident
            probe.error = None
            return probe
    return probe


@dataclass
class PortVerdict:
    """Verify-before-trust result for one service's port."""

    service: str
    port: int
    #: ours | foreign | unidentified | down
    state: str
    identity: Optional[dict] = None
    holder_uid: Optional[int] = None
    payload: Optional[dict] = None
    detail: Optional[str] = None
    extra: dict = field(default_factory=dict)

    @property
    def ours(self) -> bool:
        return self.state == "ours"

    @property
    def usable(self) -> bool:
        """Safe to send this install's credentials to."""
        return self.state == "ours"

    def line(self) -> str:
        return holder_line(self)

    def to_dict(self) -> dict:
        return {
            "service": self.service,
            "port": self.port,
            "state": self.state,
            "identity": self.identity,
            "holder_uid": self.holder_uid,
            "detail": self.detail,
        }


def verify_port(
    service: str,
    port: int,
    host: str = "127.0.0.1",
    timeout: float = _PROBE_TIMEOUT_S,
) -> PortVerdict:
    """Who holds ``port`` — us, another install, or nobody.

    Order matters. The identity block is authoritative when present. When
    it is absent the socket's owning uid decides, because an install of
    the same okuro at an older commit answers health perfectly well and
    would otherwise read as "ours". Only when BOTH are unavailable is the
    verdict ``unidentified``, and an unidentified holder is never trusted
    with a credential.
    """
    mine = this_install()
    probe = probe_identity(port, host=host, timeout=timeout)
    holder_uid = socket_owner_uid(port, host=host)

    if probe.identity is not None:
        state = "ours" if is_own_install(probe.identity, mine) else "foreign"
        return PortVerdict(
            service=service,
            port=port,
            state=state,
            identity=probe.identity,
            holder_uid=holder_uid,
            payload=probe.payload,
        )

    if not probe.reachable and holder_uid is None:
        return PortVerdict(
            service=service,
            port=port,
            state="down",
            holder_uid=None,
            detail=probe.error,
        )

    if holder_uid is not None:
        if holder_uid == int(mine["uid"]):
            # Our own uid, but the answer carries no identity: this install
            # is running code older than the identity mechanism. Ours.
            return PortVerdict(
                service=service,
                port=port,
                state="ours",
                holder_uid=holder_uid,
                payload=probe.payload,
                detail="no identity block — service predates install identity",
            )
        return PortVerdict(
            service=service,
            port=port,
            state="foreign",
            holder_uid=holder_uid,
            payload=probe.payload,
            detail="holder publishes no identity block; uid read from /proc/net/tcp",
        )

    return PortVerdict(
        service=service,
        port=port,
        state="unidentified",
        holder_uid=None,
        payload=probe.payload,
        detail=probe.error or "answered without an identity block",
    )


def holder_line(verdict: PortVerdict) -> str:
    """ONE line. Never silent, never a bare errno.

    This is the string the incident was missing. Every surface that can
    render text renders this same sentence, so the answer does not depend
    on which surface the operator happened to be looking at.
    """
    unit = UNIT_NAMES.get(verdict.service, f"okuro-{verdict.service}")
    if verdict.state == "ours":
        return f"{verdict.service}: up (this install) on port {verdict.port}"
    if verdict.state == "down":
        return f"{verdict.service}: down (nothing listening on port {verdict.port})"
    if verdict.state == "foreign":
        who = describe_install(verdict.identity, verdict.holder_uid)
        return (
            f"port {verdict.port} is held by another okuro install: {who} — "
            f"this install's {verdict.service} is not running "
            f"(unit {unit})"
        )
    return (
        f"port {verdict.port} is held by an unidentified listener — "
        f"this install's {verdict.service} cannot be confirmed running "
        f"(unit {unit})"
    )


def bind_failure_line(service: str, port: int, exc: BaseException) -> str:
    """The line a service logs when its own bind fails.

    ``[Errno 98] address already in use`` names a port and stops. This
    resolves the holder the same way every client does and says who it
    is, before the process exits — so the answer is in the journal at the
    moment of failure rather than in a later investigation.
    """
    verdict = verify_port(service, port)
    if verdict.state in ("foreign", "unidentified"):
        return f"{verdict.line()} [bind failed: {exc}]"
    return (
        f"{service}: bind to 127.0.0.1:{port} failed ({exc}); "
        f"holder could not be resolved"
    )


def verify_all(
    ports: Optional[dict] = None, timeout: float = _PROBE_TIMEOUT_S
) -> dict:
    """Verdicts for every okuro service port on this install.

    ``{service: PortVerdict}``. Ports default to this install's declared
    ports (env → config → built-in default).
    """
    if ports is None:
        from okuro.system.port_registry import service_ports

        ports = service_ports()
    out: dict[str, PortVerdict] = {}
    for service, port in ports.items():
        if not port:
            continue
        out[service] = verify_port(service, int(port), timeout=timeout)
    return out
