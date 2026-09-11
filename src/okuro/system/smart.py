# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Cross-platform SMART/disk-health collector + verify-on-fix re-check loop.
# index: imports | backend detection | smartctl backend | smartie backend | get_disk_health | recheck_disk_health
# AGENT_HEADER_END -->
"""Cross-platform SMART / disk-health collector.

Reads real drive health (SATA + NVMe) and normalizes every backend to a
single device shape so the rule table (:mod:`okuro.system.smart_rules`),
the doctor check, and the proactive scanner all consume one contract.

Backend priority (all commercially shippable — see the licensing note):

1. **smartctl** (smartmontools) — invoked as a *separate program* via
   subprocess. GPL-2.0, but the FSF GPL FAQ is explicit that a pipe /
   command-line boundary makes it a separate program, so okuro carries no
   GPL obligation as long as smartctl is **not bundled** — it is an
   optional runtime dependency detected on PATH. Best data quality:
   clean JSON on SATA, NVMe, and USB-SAT bridges alike.
2. **smartie** (MIT, pure-python, zero-dep) — the license-safe backend
   that ships *inside* okuro with no external binary. Verified on the
   target hardware: it enumerates devices on all three OSes but its
   v4.x high-level attribute extraction is immature (returned None for
   model/serial on this host's NVMe + SATA drives), so it is the
   *fallback* — used for hosts without smartctl, and marked partial.
3. OS-native (Windows WMI / macOS IOKit) — structured seam, not yet
   implemented; the collector interface is deliberately thin so these
   drop in per-platform later without touching callers.

Net licensing verdict: **bundle only smartie (MIT); invoke smartctl as a
separate program when present. Zero GPL code is bundled.**

Privilege boundary: raw SMART reads need root / CAP_SYS_RAWIO. The
collector tries an unprivileged read first, then opportunistically
retries via non-interactive ``sudo -n`` **only when passwordless sudo is
already configured** (never prompts). If neither path can read, the
device is reported ``health="unknown"`` with an honest reason — output
is never faked.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
from functools import lru_cache
from typing import Any, Optional

from okuro.system import smart_rules

log = logging.getLogger(__name__)

_SMARTCTL_TIMEOUT = 20  # seconds per invocation

# ATA SMART attribute id -> normalized key consumed by smart_rules.
_ATA_ID_MAP = {
    5: "reallocated_sector_ct",
    187: "reported_uncorrect",
    194: "temperature",
    197: "current_pending_sector",
    198: "offline_uncorrectable",
    199: "udma_crc_error_count",
}


# ── backend detection ──────────────────────────────────────────────


@lru_cache(maxsize=1)
def _smartctl_path() -> Optional[str]:
    return shutil.which("smartctl")


@lru_cache(maxsize=1)
def _passwordless_sudo() -> bool:
    """True iff ``sudo -n`` runs without prompting (cached)."""
    if not shutil.which("sudo"):
        return False
    try:
        r = subprocess.run(
            ["sudo", "-n", "true"],
            capture_output=True,
            timeout=5,
        )
        return r.returncode == 0
    except Exception:
        return False


def _smartie_available() -> bool:
    try:
        import smartie  # noqa: F401

        return True
    except Exception:
        return False


# ── smartctl backend ───────────────────────────────────────────────


def _run_smartctl(args: list[str], *, sudo: bool) -> tuple[Optional[dict], str]:
    """Run ``smartctl -j <args>``; return (parsed_json_or_None, error_str)."""
    path = _smartctl_path()
    if not path:
        return None, "smartctl not on PATH"
    cmd = (["sudo", "-n"] if sudo else []) + [path, "-j", *args]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=_SMARTCTL_TIMEOUT)
    except Exception as exc:  # pragma: no cover - environment dependent
        return None, f"{type(exc).__name__}: {exc}"
    # smartctl emits JSON on stdout even on partial failure; exit code is a
    # bitmask, not a hard error, so we parse regardless of returncode.
    try:
        return json.loads(r.stdout or "{}"), ""
    except json.JSONDecodeError:
        return None, (r.stderr or r.stdout or "unparseable smartctl output").strip()[:200]


def _permission_denied(doc: Optional[dict]) -> bool:
    if not doc:
        return False
    for m in doc.get("smartctl", {}).get("messages", []) or []:
        s = str(m.get("string", "")).lower()
        if "permission denied" in s or "requires" in s and "privile" in s:
            return True
    return False


def _smartctl_query(args: list[str], *, use_sudo: Optional[bool]) -> tuple[Optional[dict], bool]:
    """Query smartctl, transparently escalating to sudo on permission denial.

    Returns (doc, used_sudo).
    """
    if use_sudo is True:
        doc, _ = _run_smartctl(args, sudo=_passwordless_sudo())
        return doc, _passwordless_sudo()
    # auto / False: try unprivileged first
    doc, _ = _run_smartctl(args, sudo=False)
    if use_sudo is False:
        return doc, False
    if _permission_denied(doc) and _passwordless_sudo():
        doc2, _ = _run_smartctl(args, sudo=True)
        if doc2 is not None:
            return doc2, True
    return doc, False


def _enumerate_smartctl(*, use_sudo: Optional[bool]) -> list[dict[str, str]]:
    doc, _ = _smartctl_query(["--scan"], use_sudo=use_sudo)
    if not doc:
        return []
    out = []
    for d in doc.get("devices", []) or []:
        name = d.get("name")
        if name:
            out.append({"name": name, "type": d.get("type", "")})
    return out


def _protocol_of(doc: dict, scan_type: str) -> str:
    proto = (doc.get("device", {}) or {}).get("protocol", "") or scan_type
    proto = proto.lower()
    if "nvme" in proto:
        return "nvme"
    if "ata" in proto or proto in ("sat", "sata"):
        return "ata"
    if "scsi" in proto or "sas" in proto:
        return "scsi"
    return proto or "unknown"


def _normalize_ata(doc: dict) -> dict[str, Any]:
    attrs: dict[str, Any] = {}
    table = (doc.get("ata_smart_attributes", {}) or {}).get("table", []) or []
    for a in table:
        key = _ATA_ID_MAP.get(a.get("id"))
        if key:
            attrs[key] = (a.get("raw", {}) or {}).get("value")
    return attrs


def _normalize_nvme(doc: dict) -> dict[str, Any]:
    log_ = doc.get("nvme_smart_health_information_log", {}) or {}
    attrs: dict[str, Any] = {
        "critical_warning": log_.get("critical_warning"),
        "percentage_used": log_.get("percentage_used"),
        "available_spare": log_.get("available_spare"),
        "available_spare_threshold": log_.get("available_spare_threshold"),
        "media_errors": log_.get("media_errors"),
        "temperature": log_.get("temperature"),
        "unsafe_shutdowns": log_.get("unsafe_shutdowns"),
        "power_on_hours": log_.get("power_on_hours"),
    }
    spare = log_.get("available_spare")
    thr = log_.get("available_spare_threshold")
    if spare is not None and thr is not None:
        attrs["available_spare_below_threshold"] = spare < thr
    return attrs


def _query_device(name: str, dtype: str, *, use_sudo: Optional[bool]) -> tuple[Optional[dict], bool]:
    args = ["-H", "-A", "-i"]
    if dtype:
        args += ["-d", dtype]
    args.append(name)
    return _smartctl_query(args, use_sudo=use_sudo)


def _read_device_smartctl(name: str, scan_type: str, *, use_sudo: Optional[bool]) -> dict[str, Any]:
    doc, used_sudo = _query_device(name, scan_type, use_sudo=use_sudo)

    # SATA disks behind a controller / USB bridge frequently enumerate as
    # "scsi", which hides their ATA SMART attribute table (reallocated
    # sectors, pending sectors, ...). Re-read through the SAT translation
    # layer to recover real attributes — critical for RAID member drives.
    if doc and _protocol_of(doc, scan_type) == "scsi":
        sat_doc, sat_sudo = _query_device(name, "sat", use_sudo=use_sudo)
        if sat_doc and (sat_doc.get("ata_smart_attributes", {}) or {}).get("table"):
            doc, used_sudo = sat_doc, sat_sudo
            scan_type = "sat"

    dev: dict[str, Any] = {
        "device": name,
        "model": None,
        "serial": None,
        "type": "unknown",
        "smart_passed": None,
        "health": "unknown",
        "reason": None,
        "attributes": {},
        "findings": [],
        "read_via": "sudo smartctl" if used_sudo else "smartctl",
    }

    if not doc or _permission_denied(doc):
        dev["reason"] = (
            "SMART needs elevated privileges — run okuro with sudo or grant "
            "the smartctl binary CAP_SYS_RAWIO"
        )
        return dev

    dev["model"] = doc.get("model_name")
    dev["serial"] = doc.get("serial_number")
    proto = _protocol_of(doc, scan_type)
    dev["type"] = proto
    dev["smart_passed"] = (doc.get("smart_status", {}) or {}).get("passed")

    if proto == "nvme":
        attrs = _normalize_nvme(doc)
    else:
        attrs = _normalize_ata(doc)
    if dev["smart_passed"] is not None:
        attrs["smart_passed"] = dev["smart_passed"]
    dev["attributes"] = attrs

    health, findings = smart_rules.evaluate(proto, attrs)
    dev["health"] = health
    dev["findings"] = findings
    return dev


# ── smartie backend (MIT fallback) ─────────────────────────────────


def _read_devices_smartie() -> list[dict[str, Any]]:
    """Best-effort MIT-licensed fallback when smartctl is absent.

    v4.x smartie reliably *enumerates* devices cross-platform but its
    high-level attribute parsing is immature (verified None on this
    host). We surface the devices with ``health="unknown"`` and an
    honest reason rather than emit fabricated attribute values.
    """
    devices: list[dict[str, Any]] = []
    try:
        from smartie.device import get_all_devices
    except Exception as exc:
        log.debug("smartie import failed: %s", exc)
        return devices
    try:
        for d in get_all_devices():
            path = getattr(d, "path", "?")
            model = serial = None
            try:
                with d:
                    ident = d.identify()
                    model = getattr(ident, "model", None)
                    serial = getattr(ident, "serial", None)
                    model = model.strip() if isinstance(model, str) else model
                    serial = serial.strip() if isinstance(serial, str) else serial
            except Exception:  # SenseError / unsupported bridge
                pass
            devices.append(
                {
                    "device": str(path),
                    "model": model,
                    "serial": serial,
                    "type": "unknown",
                    "smart_passed": None,
                    "health": "unknown",
                    "reason": "smartie backend enumerates only — install smartmontools (smartctl) for full SMART attributes",
                    "attributes": {},
                    "findings": [],
                    "read_via": "smartie",
                }
            )
    except Exception as exc:
        log.debug("smartie enumeration failed: %s", exc)
    return devices


# ── public API ─────────────────────────────────────────────────────


def get_disk_health(*, use_sudo: Optional[bool] = None) -> dict[str, Any]:
    """Collect SMART health for every physical disk on this host.

    Args:
        use_sudo: ``None`` = auto (escalate to ``sudo -n`` only if an
            unprivileged read hits permission-denied and passwordless
            sudo is configured). ``True`` forces sudo, ``False`` forbids.

    Returns a dict::

        {
          "backend": "smartctl" | "smartie" | "none",
          "note": str,               # human-readable collector status
          "devices": [ <device>, ... ],
          "summary": {"ok": n, "warn": n, "fail": n, "unknown": n},
        }

    Each ``<device>`` carries ``health`` (ok/warn/fail/unknown),
    normalized ``attributes`` (the why-drilldown evidence), and
    ``findings`` (fired rules, worst-first — each a plain-language item).
    """
    devices: list[dict[str, Any]] = []
    backend = "none"
    note = ""

    if _smartctl_path():
        backend = "smartctl"
        for entry in _enumerate_smartctl(use_sudo=use_sudo):
            devices.append(
                _read_device_smartctl(entry["name"], entry["type"], use_sudo=use_sudo)
            )
        readable = [d for d in devices if d["health"] != "unknown"]
        if devices and not readable:
            note = (
                f"{len(devices)} disk(s) found but SMART is unreadable without "
                "elevated privileges — run okuro with sudo or grant smartctl CAP_SYS_RAWIO"
            )
        else:
            via = "sudo smartctl" if any(d.get("read_via") == "sudo smartctl" for d in devices) else "smartctl"
            note = f"read {len(readable)}/{len(devices)} disk(s) via {via}"
    elif _smartie_available():
        backend = "smartie"
        devices = _read_devices_smartie()
        note = (
            "smartctl not installed — using MIT smartie fallback (enumeration only). "
            "Install smartmontools for full SMART attributes."
        )
    else:
        note = "no SMART backend available — install smartmontools (smartctl) or the smartie package"

    summary = {"ok": 0, "warn": 0, "fail": 0, "unknown": 0}
    for d in devices:
        summary[d["health"]] = summary.get(d["health"], 0) + 1

    return {"backend": backend, "note": note, "devices": devices, "summary": summary}


def recheck_disk_health(device_ref: str) -> dict[str, Any]:
    """Verify-on-fix loop — the piece okuro was missing.

    The user says "I replaced the drive" (web ack button or a chat "I
    fixed it"). This re-runs the collector for ``device_ref`` (serial or
    /dev path) and reconciles any open ``smart:<ref>`` signal:

    * device now healthy  → resolve the signal (discarded, audit reason).
    * device gone/replaced → resolve (treated as fixed).
    * still warn/fail      → keep the signal open, report it is unresolved.

    One function, two entry points (MCP verb + web ack) — DP10.
    """
    from okuro.sense.signals import signal_discard, signal_list

    report = get_disk_health()
    match: Optional[dict[str, Any]] = None
    for d in report["devices"]:
        if device_ref in (d.get("serial"), d.get("device")):
            match = d
            break

    ref = f"smart:{device_ref}"
    open_sigs = [s for s in signal_list(status="open", limit=200) if s.get("source_ref") == ref]

    def _resolve(reason: str) -> list[str]:
        resolved = []
        for s in open_sigs:
            try:
                signal_discard(s["id"], reason=reason)
                resolved.append(s["id"])
            except Exception as exc:  # already closed / race
                log.debug("recheck could not discard %s: %s", s["id"], exc)
        return resolved

    if match is None:
        resolved = _resolve(f"resolved: device {device_ref} no longer present (replaced)")
        return {
            "device_ref": device_ref,
            "present": False,
            "resolved": True,
            "health": None,
            "findings": [],
            "resolved_signals": resolved,
            "message": f"Drive {device_ref} is no longer present — treating it as replaced/resolved.",
        }

    if match["health"] == "ok":
        resolved = _resolve("resolved: SMART re-check passed")
        return {
            "device_ref": device_ref,
            "present": True,
            "resolved": True,
            "health": "ok",
            "findings": [],
            "resolved_signals": resolved,
            "message": f"SMART re-check passed for {match.get('model') or device_ref} — marked resolved.",
        }

    return {
        "device_ref": device_ref,
        "present": True,
        "resolved": False,
        "health": match["health"],
        "findings": match["findings"],
        "open_signals": [s["id"] for s in open_sigs],
        "message": (
            f"Still detecting a problem on {match.get('model') or device_ref} "
            f"({match['health']}) — keeping the item open."
        ),
    }
