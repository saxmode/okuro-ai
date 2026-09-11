# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: SMART attribute -> plain-language health rules (data table + interpreter).
# index: imports | SMART_RULES | def _passes | def evaluate
# AGENT_HEADER_END -->
"""SMART / disk-health rules as **data**, not code.

Each rule maps one normalized attribute (see ``system.smart`` collector
output) to a severity and a plain-language sentence the owner can act on
without knowing what "Reallocated_Sector_Ct" means.

Keeping the rules as a table (DP10 systematic-not-specific) means adding
coverage for a new failure mode is a one-line data edit, not new control
flow. ``evaluate()`` is the only interpreter.

Attribute names are the *normalized* keys emitted by the collector, so
this table is backend-agnostic (smartctl / smartie / OS-native all feed
the same shape).
"""

from __future__ import annotations

from typing import Any

# Severity ranking — worst wins when several rules fire on one device.
_SEV_RANK = {"ok": 0, "warn": 1, "fail": 2}

# proto: "ata" | "nvme" | "any"  — restricts a rule to the device family
# whose attribute it references (an NVMe drive has no reallocated-sector
# count; an ATA drive has no percentage_used). "any" applies to both.
#
# op semantics (rule.threshold on the right):
#   ">"   attr >  threshold
#   ">="  attr >= threshold
#   "!="  attr != threshold
#   "=="  attr == threshold   (used for boolean flags)
#
# message is .format(value=...)-expanded with the live attribute value.
SMART_RULES: list[dict[str, Any]] = [
    # ── overall self-assessment (both families) ────────────────────
    {
        "id": "smart_overall_fail",
        "attr": "smart_passed",
        "proto": "any",
        "op": "==",
        "threshold": False,
        "severity": "fail",
        "message": "The drive's own SMART self-assessment reports FAILING. Back up now and replace the disk.",
    },
    # ── ATA / SATA spinning + SATA SSD ─────────────────────────────
    {
        "id": "ata_reallocated",
        "attr": "reallocated_sector_ct",
        "proto": "ata",
        "op": ">",
        "threshold": 0,
        "severity": "warn",
        "message": "The drive is remapping bad sectors ({value} reallocated so far) — an early wear sign. Make sure it is backed up and keep an eye on it.",
    },
    {
        "id": "ata_pending",
        "attr": "current_pending_sector",
        "proto": "ata",
        "op": ">",
        "threshold": 0,
        "severity": "warn",
        "message": "The drive has {value} unreadable sector(s) waiting to be reallocated — data on them is at risk. Back up soon.",
    },
    {
        "id": "ata_offline_uncorrectable",
        "attr": "offline_uncorrectable",
        "proto": "ata",
        "op": ">",
        "threshold": 0,
        "severity": "fail",
        "message": "The drive has {value} uncorrectable sector(s) — it is failing. Replace it.",
    },
    {
        "id": "ata_crc",
        "attr": "udma_crc_error_count",
        "proto": "ata",
        "op": ">",
        "threshold": 0,
        "severity": "warn",
        "message": "{value} interface CRC error(s) — usually a bad or loose SATA/USB cable rather than the disk itself. Reseat or replace the cable first.",
    },
    {
        "id": "ata_reported_uncorrect",
        "attr": "reported_uncorrect",
        "proto": "ata",
        "op": ">",
        "threshold": 0,
        "severity": "warn",
        "message": "The drive reported {value} uncorrectable error(s) to the OS — back it up and watch it closely.",
    },
    # ── NVMe SSD ───────────────────────────────────────────────────
    {
        "id": "nvme_critical_warning",
        "attr": "critical_warning",
        "proto": "nvme",
        "op": "!=",
        "threshold": 0,
        "severity": "fail",
        "message": "The SSD is raising a critical-warning flag (0x{value:02x}) — it is failing. Back up and replace it.",
    },
    {
        "id": "nvme_spare_below_threshold",
        "attr": "available_spare_below_threshold",
        "proto": "nvme",
        "op": "==",
        "threshold": True,
        "severity": "fail",
        "message": "The SSD's spare capacity has dropped below its own safety threshold — it is at end of life. Replace it.",
    },
    {
        "id": "nvme_wear",
        "attr": "percentage_used",
        "proto": "nvme",
        "op": ">=",
        "threshold": 90,
        "severity": "warn",
        "message": "The SSD is {value}% through its rated write endurance — start planning a replacement.",
    },
    {
        "id": "nvme_media_errors",
        "attr": "media_errors",
        "proto": "nvme",
        "op": ">",
        "threshold": 0,
        "severity": "warn",
        "message": "The SSD logged {value} media / data-integrity error(s) — back it up and monitor it.",
    },
]


def _passes(op: str, value: Any, threshold: Any) -> bool:
    """True iff ``value <op> threshold`` — the trigger test for one rule.

    Comparisons are guarded: a missing/None attribute never fires a rule.
    """
    if value is None:
        return False
    try:
        if op == ">":
            return value > threshold
        if op == ">=":
            return value >= threshold
        if op == "!=":
            return value != threshold
        if op == "==":
            return value == threshold
    except TypeError:
        return False
    return False


def evaluate(protocol: str, attributes: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    """Apply the rule table to one device's normalized attributes.

    Args:
        protocol: "ata" | "nvme" | anything else (treated as unknown family
            — only ``proto="any"`` rules apply).
        attributes: normalized attribute dict from the collector, e.g.
            ``{"reallocated_sector_ct": 0, "smart_passed": True, ...}``.

    Returns:
        ``(health, findings)`` where ``health`` is "ok" | "warn" | "fail"
        and ``findings`` is the list of fired-rule dicts (empty when ok),
        each ``{id, severity, message, attribute, value}`` — the
        why-drilldown evidence.
    """
    fam = protocol if protocol in ("ata", "nvme") else "any"
    findings: list[dict[str, Any]] = []
    worst = "ok"

    for rule in SMART_RULES:
        if rule["proto"] != "any" and rule["proto"] != fam:
            continue
        value = attributes.get(rule["attr"])
        if not _passes(rule["op"], value, rule["threshold"]):
            continue
        try:
            msg = rule["message"].format(value=value)
        except (ValueError, KeyError):
            msg = rule["message"]
        findings.append(
            {
                "id": rule["id"],
                "severity": rule["severity"],
                "message": msg,
                "attribute": rule["attr"],
                "value": value,
            }
        )
        if _SEV_RANK[rule["severity"]] > _SEV_RANK[worst]:
            worst = rule["severity"]

    # Sort findings worst-first so callers can take findings[0] as the headline.
    findings.sort(key=lambda f: _SEV_RANK[f["severity"]], reverse=True)
    return worst, findings
