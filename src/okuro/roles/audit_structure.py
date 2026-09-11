# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Audit every live role against the canonical structure in designer.py.
# index: imports | def audit | def main
# AGENT_HEADER_END -->
"""Audit every live role against the canonical structure.

Answers one question with a reproducible number: how many roles currently in
the registry would fail `validate_role_structure`, and which ones. Run before
changing the gate in `store_designed_role` from soft to hard.

    .venv/bin/python -m okuro.roles.audit_structure
    .venv/bin/python -m okuro.roles.audit_structure --json
"""

import argparse
import json

from okuro.roles.designer import validate_role_structure
from okuro.roles.registry import get_role, list_roles


def audit() -> dict:
    """Validate every role in the registry. Returns a structured verdict."""
    results = []
    for meta in list_roles():
        role_id = meta["id"]
        # One read, then the RAW columns. Do NOT pass level= and read "prompt":
        # get_role returns all three columns plus a fallback chain, so a
        # per-grade loop reading "prompt" checks the full markdown three times.
        got = get_role(role_id, level="full") or {}
        content = {
            "full": got.get("prompt") or "",
            "lean": got.get("lean_prompt") or "",
            "micro": got.get("micro_prompt") or "",
        }
        verdict = validate_role_structure(content)
        results.append(
            {
                "role_id": role_id,
                "domain": meta.get("domain"),
                "maturity": meta.get("maturity"),
                "ok": verdict["ok"],
                "missing": verdict["missing"],
                "chars": {g: len(v) for g, v in content.items()},
            }
        )

    failing = [r for r in results if not r["ok"]]
    return {
        "total": len(results),
        "passing": len(results) - len(failing),
        "failing": len(failing),
        "roles": results,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--json", action="store_true", help="emit raw JSON")
    args = ap.parse_args()

    report = audit()
    if args.json:
        print(json.dumps(report, indent=2))
        return

    print(
        f"canonical-structure audit: {report['failing']} of {report['total']} "
        f"live roles would fail (passing: {report['passing']})\n"
    )
    print(f"{'role':<34}{'domain':<14}{'maturity':<10}missing")
    for r in sorted(report["roles"], key=lambda x: (x["ok"], x["role_id"])):
        if r["ok"]:
            continue
        flat = "; ".join(
            f"{g}: {', '.join(items)}" for g, items in r["missing"].items()
        )
        print(f"{r['role_id']:<34}{str(r['domain']):<14}{str(r['maturity']):<10}{flat}")


if __name__ == "__main__":
    main()
