# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: okuro audit — compliance, health, and pre-publish safety audit.
# index:
#   imports
#   def audit
#   def _check_no_env_secrets
#   def _check_db_current
#   def _check_keyring
#   def _check_gpu
#   def _check_design
#   def _check_roles
#   def _check_cortex
#   def _run_pre_publish
#   _LEAK_PATTERNS
#   _DANGEROUS_GLOBS
# AGENT_HEADER_END -->
"""okuro audit — compliance, health, and pre-publish safety audit."""

import click
import re

from .output import console, ok, warn, fail, heading, info, data_table


@click.command()
@click.option("--json", "as_json", is_flag=True, help="Output as JSON.")
@click.option("--pre-publish", "pre_publish", is_flag=True,
              help="Scan source tree for secrets, hardcoded paths, and machine-specific values that must not ship.")
def audit(as_json, pre_publish):
    """Run compliance and health audit across all subsystems."""
    if pre_publish:
        _run_pre_publish(as_json)
        return

    results = []

    checks = [
        ("secrets_in_env", _check_no_env_secrets),
        ("db_migrations", _check_db_current),
        ("keyring_initialized", _check_keyring),
        ("gpu_available", _check_gpu),
        ("design_profiles", _check_design),
        ("roles_loaded", _check_roles),
        ("cortex_indexed", _check_cortex),
    ]

    for check_id, check_fn in checks:
        try:
            status, detail = check_fn()
            results.append({"check": check_id, "status": status, "detail": detail})
        except Exception as e:
            results.append({"check": check_id, "status": "error", "detail": str(e)})

    if as_json:
        import json
        console.print(json.dumps(results, indent=2))
        return

    heading("Audit Results")
    for r in results:
        fn = ok if r["status"] == "pass" else warn if r["status"] == "warn" else fail
        fn(f"{r['check']}: {r['detail']}")

    passed = sum(1 for r in results if r["status"] == "pass")
    total = len(results)
    console.print(f"\n  Score: {passed}/{total}")

    if passed < total:
        raise SystemExit(1)


def _check_no_env_secrets():
    """Check no secrets leaked in .env files."""
    from pathlib import Path

    env_files = list(Path.cwd().rglob(".env"))
    if env_files:
        return "warn", f"{len(env_files)} .env files found (use okuro keys instead)"
    return "pass", "no .env files"


def _check_db_current():
    try:
        from .db_helpers import get_db
        db = get_db()
        pending = db.migrate()
        db.close()
        if pending:
            return "warn", f"{len(pending)} migrations were pending (now applied)"
        return "pass", "up to date"
    except Exception as e:
        return "fail", str(e)


def _check_keyring():
    try:
        from okuro.keyring import KeyringStorage
        store = KeyringStorage()
        if store.is_initialized:
            return "pass", "initialized"
        return "warn", "not initialized"
    except Exception as e:
        return "fail", str(e)


def _check_gpu():
    try:
        from okuro.system.gpu import get_gpu_status
        gpus = get_gpu_status().get("gpus", [])
        if gpus:
            return "pass", f"{len(gpus)} GPU(s)"
        return "warn", "no GPUs"
    except Exception:
        return "warn", "detection unavailable"


def _check_design():
    """Design systems the engine can open. Counts KITS, not v0 profiles.

    The question ("does this install have a design system") was always right;
    only the store was wrong. okuro ships okuro-ds, so zero here means the
    package is broken rather than that the user authored nothing.
    """
    try:
        from okuro.design_engine import store
        kits = store.list_kits()
        if kits:
            return "pass", f"{len(kits)} design systems"
        return "fail", "no design systems — okuro-ds should always be present"
    except Exception as e:
        return "fail", str(e)


def _check_roles():
    try:
        from okuro.roles import list_roles
        roles = list_roles()
        if roles:
            return "pass", f"{len(roles)} roles"
        return "warn", "no roles loaded"
    except Exception as e:
        return "fail", str(e)


def _check_cortex():
    try:
        from okuro.cortex.vectorstore import VectorStore
        vs = VectorStore()
        count = vs.count() if hasattr(vs, "count") else "?"
        return "pass", f"{count} indexed"
    except Exception as e:
        return "warn", str(e)


# ---------------------------------------------------------------------------
# Pre-publish safety scanner
# ---------------------------------------------------------------------------
# Scans all .py and .md files under src/okuro/ for patterns that must never
# appear in a published package. Catches hardcoded home paths, machine
# domains, API keys, database files, and encrypted vaults.
#
# Exit code 1 if any FAIL-level match is found.
# Designed to run in CI as: okuro audit --pre-publish --json
# ---------------------------------------------------------------------------

# Each tuple: (pattern_name, compiled_regex, severity, description)
# severity: "fail" = blocks publish, "warn" = review recommended
_LEAK_PATTERNS: list[tuple[str, "re.Pattern[str]", str, str]] = [
    (
        "hardcoded_home",
        re.compile(r"/home/[a-zA-Z0-9_]+/", re.IGNORECASE),
        "fail",
        "Hardcoded home directory path",
    ),
    (
        "lan_domain",
        re.compile(r"[a-zA-Z0-9][\w.-]*\.(?:lan|internal)\b", re.IGNORECASE),
        "warn",
        "LAN domain reference (may be intentional in defaults/docs)",
    ),
    (
        "api_key_literal",
        re.compile(
            r"""(?:sk-[a-zA-Z0-9]{20,}"""        # OpenAI-style
            r"""|AIza[a-zA-Z0-9_-]{35}"""          # Google API key
            r"""|ghp_[a-zA-Z0-9]{36}"""            # GitHub PAT
            r"""|xoxb-[0-9]{10,}-[a-zA-Z0-9]+"""   # Slack bot token
            r"""|AKIA[A-Z0-9]{16})"""               # AWS access key
        ),
        "fail",
        "Embedded API key or token",
    ),
    (
        "private_ip",
        re.compile(r"\b(?:192\.168\.\d{1,3}\.\d{1,3}|10\.\d{1,3}\.\d{1,3}\.\d{1,3})\b"),
        "warn",
        "Private IP address",
    ),
    (
        "enc_file_ref",
        re.compile(r"\.enc\b"),
        "warn",
        "Reference to encrypted file (verify not shipping actual vault)",
    ),
    (
        "password_literal",
        re.compile(r"""(?:password|passwd|secret)\s*=\s*["'][^"']{8,}["']""", re.IGNORECASE),
        "fail",
        "Hardcoded password or secret value",
    ),
    (
        # Catches the class the prefixed-key pattern misses: a constant whose
        # NAME signals a credential (KEY/TOKEN/SECRET/PASS/CRED) assigned a
        # long quoted literal, and bare 32+ hex blobs (e.g. a raw REST-API
        # key). This is what let _DEFAULT_API_KEY = "<64-hex>" slip the gate.
        "high_entropy_literal",
        re.compile(
            r"""[A-Za-z_]*(?:KEY|TOKEN|SECRET|PASS|CRED)[A-Za-z_]*\s*=\s*["'][A-Za-z0-9+/_=-]{24,}["']"""
            r"""|["'][0-9a-fA-F]{32,}["']""",
            re.IGNORECASE,
        ),
        "fail",
        "High-entropy literal (possible embedded secret/key)",
    ),
    # --- Portability / model-capability (tier degradation) ---
    # okuro ships to heterogeneous hosts incl. CPU-only laptops. The retrieval
    # tier (air|advanced|pro) is the capability gate: air must run WITHOUT any
    # neural model. Code that hardcodes a GPU device or loads a heavy model
    # unconditionally will CRASH on a host that can't run it. These are warns
    # (a legitimately capability-gated call also matches) — the auditor verifies
    # each hit sits behind a hardware/tier check (see okuro.embed detect_hw /
    # the embed-tier config) and falls back instead of raising.
    (
        "ungated_gpu_device",
        re.compile(r"""(?:\.cuda\(\)|\.to\(\s*["']cuda|device\s*=\s*["']cuda)""", re.IGNORECASE),
        "warn",
        "Hardcoded CUDA device — must degrade to CPU/air tier on GPU-less hosts",
    ),
    (
        "unguarded_model_load",
        re.compile(r"""\b(?:SentenceTransformer|AutoModelForCausalLM|AutoModelForSequenceClassification|AutoModel|CrossEncoder)\s*\(""" ),
        "warn",
        "Model load — verify it is tier/capability-gated (air tier must not require a model the host cannot run)",
    ),
]

# Files that must never appear in the build output
_DANGEROUS_GLOBS = [
    "*.db", "*.db-journal", "*.enc", ".env", ".env.*",
    "keys.enc", "salt", "*.pem", "*.key",
]

# ---------------------------------------------------------------------------
# License-compliance check (Apache-2.0)
# ---------------------------------------------------------------------------
# okuro ships under Apache-2.0. A serious push must not regress that, so this
# gate is the deterministic source of truth the `license-auditor` role defers
# to instead of re-deriving header state by eye. Verifies:
#   - LICENSE present and is the Apache-2.0 text
#   - NOTICE present (Apache §4(d) attribution carrier)
#   - pyproject declares license = "Apache-2.0"
#   - every shipping .py under src/okuro carries the SPDX header, set to
#     Apache-2.0 (a foreign SPDX id = a mis-relicensed or vendored file)
_SPDX_EXPECTED = "Apache-2.0"
_SPDX_RE = re.compile(r"SPDX-License-Identifier:\s*(\S+)")
_LICENSE_SKIP_DIRS = {"__pycache__", ".git", "node_modules", ".venv", "dist", "build"}


def _check_license_compliance(src_dir: "Path", repo_root: "Path") -> list[dict]:
    """Return FAIL findings for any Apache-2.0 licensing regression."""
    out: list[dict] = []

    def add(check: str, file: str, detail: str, match: str = "") -> None:
        out.append({
            "check": check, "severity": "fail", "file": file,
            "line": 0, "match": match, "detail": detail,
        })

    lic = repo_root / "LICENSE"
    if not lic.is_file():
        add("license_missing", "LICENSE", "LICENSE file is missing")
    else:
        text = lic.read_text(errors="replace")
        if "Apache License" not in text or "Version 2.0" not in text:
            add("license_not_apache", "LICENSE",
                "LICENSE is not the Apache-2.0 text")

    if not (repo_root / "NOTICE").is_file():
        add("notice_missing", "NOTICE",
            "NOTICE file is missing (Apache §4(d) attribution carrier)")

    pyproject = repo_root / "pyproject.toml"
    if pyproject.is_file():
        if not re.search(r'license\s*=\s*"Apache-2\.0"', pyproject.read_text(errors="replace")):
            add("pyproject_license", "pyproject.toml",
                'license must be declared as "Apache-2.0"')

    for path in src_dir.rglob("*.py"):
        if any(part in _LICENSE_SKIP_DIRS for part in path.parts):
            continue
        rel = str(path.relative_to(repo_root))
        try:
            with path.open(errors="replace") as fh:
                head = "".join(next(fh, "") for _ in range(5))
        except OSError:
            continue
        m = _SPDX_RE.search(head)
        if not m:
            add("spdx_header_missing", rel,
                "Source file missing SPDX-License-Identifier header")
        elif m.group(1) != _SPDX_EXPECTED:
            add("spdx_header_wrong", rel,
                f"SPDX header is {m.group(1)}, expected {_SPDX_EXPECTED}",
                match=m.group(1))
    return out


def _run_pre_publish(as_json: bool) -> None:
    """Scan the shipping tree for patterns that must not ship publicly.

    Scope = the package PLUS the ops/build surface outside src/okuro/
    (install.sh, scripts/, pyproject.toml). The latter was a blind spot:
    machine paths and prod URLs there shipped unflagged.
    """
    import json as json_mod
    from pathlib import Path

    src_dir = Path(__file__).resolve().parent.parent   # src/okuro/
    repo_root = src_dir.parent.parent                  # repo root
    scan_roots = [
        src_dir,
        repo_root / "scripts",
        repo_root / "installer",
        repo_root / "install.sh",
        repo_root / "bootstrap.sh",
        repo_root / "pyproject.toml",
    ]
    findings: list[dict] = []

    # --- Pattern scan on source files ---
    scan_extensions = {".py", ".md", ".yaml", ".yml", ".toml", ".json", ".html", ".js", ".ts", ".tsx", ".sh"}
    skip_dirs = {"__pycache__", ".git", "node_modules", ".venv", "dist", "build", "tests"}

    # Gather the shipping file set across all roots (deduplicated).
    file_list: list[Path] = []
    seen_paths: set = set()
    for root in scan_roots:
        candidates = [root] if root.is_file() else (root.rglob("*") if root.is_dir() else [])
        for path in candidates:
            if path.is_dir():
                continue
            if any(part in skip_dirs for part in path.parts):
                continue
            if path.suffix not in scan_extensions:
                continue
            if path in seen_paths:
                continue
            seen_paths.add(path)
            file_list.append(path)

    for path in file_list:
        try:
            content = path.read_text(errors="replace")
        except OSError:
            continue

        rel = str(path.relative_to(repo_root))

        for line_no, line in enumerate(content.splitlines(), 1):
            # Skip comments that are documenting the patterns themselves
            stripped = line.strip()
            if stripped.startswith("#") and "LEAK_PATTERN" in stripped:
                continue

            for name, pattern, severity, desc in _LEAK_PATTERNS:
                for match in pattern.finditer(line):
                    # Allowlist: this audit file's own pattern definitions
                    if rel.endswith("cmd_audit.py"):
                        if "re.compile" in line or 'r"' in line or "r'" in line:
                            continue
                        # Glob pattern lists (e.g. _DANGEROUS_GLOBS)
                        if stripped.startswith('"') and stripped.endswith('",'):
                            continue
                    # Allowlist: code that references .enc as a file format
                    # (keyring storage, docs, path construction) — not actual secrets
                    if name == "enc_file_ref" and (
                        any(kw in line for kw in ("Path(", "open(", "suffix", "endswith", "_FILE", "rglob", '/ "', "/ '"))
                        or "keyring/" in rel
                        or rel.endswith("cmd_audit.py")
                    ):
                        continue
                    # Allowlist: a high-entropy match that is really a readable
                    # identifier (a DB/keyring NAME constant), not a secret
                    # value. A real secret is high-entropy: 32+ hex, or mixed
                    # case WITH digits. Snake_case names ("telegram_bot_token")
                    # have neither and are skipped.
                    if name == "high_entropy_literal":
                        _m = re.search(r"""["']([A-Za-z0-9+/=_-]{16,})["']""", match.group())
                        _val = _m.group(1) if _m else ""
                        _is_hex = bool(re.fullmatch(r"[0-9a-fA-F]{32,}", _val))
                        _has_mix = bool(re.search(r"\d", _val)) and bool(re.search(r"[A-Z]", _val))
                        if not (_is_hex or _has_mix):
                            continue
                        # A single repeated character has no entropy: git's
                        # null sha ("000…0") in the pre-push hook template
                        # is not a secret.
                        if len(set(_val)) == 1:
                            continue
                        # Supabase names keys that are SAFE TO SHIP with the
                        # `sb_publishable_` prefix — the same class as a
                        # browser app's anon key, protected by RLS at the far
                        # end, not by secrecy. Flagging one teaches people to
                        # ignore the audit; a `sb_secret_`/service_role key
                        # still fails.
                        if _val.startswith("sb_publishable_"):
                            continue
                    findings.append({
                        "check": name,
                        "severity": severity,
                        "file": rel,
                        "line": line_no,
                        "match": match.group(),
                        "detail": desc,
                    })

    # --- Dangerous file check ---
    for glob_pat in _DANGEROUS_GLOBS:
        for hit in src_dir.rglob(glob_pat):
            if any(part in skip_dirs for part in hit.parts):
                continue
            findings.append({
                "check": "dangerous_file",
                "severity": "fail",
                "file": str(hit.relative_to(src_dir)),
                "line": 0,
                "match": hit.name,
                "detail": f"File type must not ship: {glob_pat}",
            })

    # --- License compliance (Apache-2.0 headers + LICENSE/NOTICE) ---
    findings.extend(_check_license_compliance(src_dir, repo_root))

    # --- Deduplicate (same check+file+line) ---
    seen = set()
    unique: list[dict] = []
    for f in findings:
        key = (f["check"], f["file"], f["line"], f["match"])
        if key not in seen:
            seen.add(key)
            unique.append(f)
    findings = unique

    # --- Output ---
    if as_json:
        console.print(json_mod.dumps(findings, indent=2))
    else:
        if not findings:
            heading("Pre-publish Audit")
            ok("No leaks detected — safe to build")
        else:
            fails = [f for f in findings if f["severity"] == "fail"]
            warns = [f for f in findings if f["severity"] == "warn"]

            heading(f"Pre-publish Audit — {len(fails)} fail, {len(warns)} warn")

            for f in sorted(findings, key=lambda x: (x["severity"] != "fail", x["file"], x["line"])):
                fn = fail if f["severity"] == "fail" else warn
                fn(f"[{f['check']}] {f['file']}:{f['line']} — {f['match']}")

    has_fails = any(f["severity"] == "fail" for f in findings)
    if has_fails:
        raise SystemExit(1)
