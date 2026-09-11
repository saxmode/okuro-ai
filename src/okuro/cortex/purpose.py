# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: LLM purpose generation for files with weak docstrings — uses bridge fast-draft
# index:
#   imports
#   ENRICHMENT_LOG
#   FAILURES_LOG
#   FAILURE_RETRY_BUDGET
#   FAILURE_RETRY_WINDOW_DAYS
#   BATCH_SIZE
#   MAX_PER_CYCLE
#   WEAK_PURPOSE_PATTERNS
#   def is_weak_purpose
#   def _build_prompt
#   def _parse_response
#   def _log_enrichment
#   def log_failure
#   def recent_failure_count
#   def generate_purposes
#   def enrich_sidecar
# AGENT_HEADER_END -->
"""
LLM purpose generation for files with weak docstrings.

Routes to cheapest available model via bridge (capability=fast-draft).
Default route: local inference (qwen), zero subscription tokens.
Falls back to docstring-only if no bridge providers available.
"""

import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from . import sidecar as sidecar_mod
from .sidecar import SidecarEntry
from okuro.db.engine import okuro_home

log = logging.getLogger(__name__)

ENRICHMENT_LOG = okuro_home() / "cortex" / "enrichment.jsonl"
FAILURES_LOG = okuro_home() / "cortex" / "failures.jsonl"
FAILURE_RETRY_BUDGET = 3
FAILURE_RETRY_WINDOW_DAYS = 7
BATCH_SIZE = 10
MAX_PER_CYCLE = 1000

# Purposes that indicate the parser couldn't extract a meaningful description
WEAK_PURPOSE_PATTERNS = re.compile(
    r"^("
    r"\w+ module"          # "utils module", "config module"
    r"|module$"            # just "module"
    r"|\w+ \w+ \w+$"      # 3-word path fragments like "okuro cortex scanner"
    r")$",
    re.IGNORECASE,
)


def is_weak_purpose(purpose: str) -> bool:
    """Check if a purpose string is too generic to be useful."""
    if not purpose or len(purpose) < 5:
        return True
    if WEAK_PURPOSE_PATTERNS.match(purpose.strip()):
        return True
    return False


def _build_prompt(file_path: Path, content: str, symbols: list[str]) -> str:
    """Build a purpose generation prompt for a single file."""
    # Detect language from extension
    ext_to_lang = {
        ".py": "Python", ".js": "JavaScript", ".ts": "TypeScript",
        ".jsx": "React JSX", ".tsx": "React TSX", ".go": "Go",
        ".rs": "Rust", ".java": "Java", ".sh": "Shell",
        ".yaml": "YAML", ".yml": "YAML", ".toml": "TOML",
        ".md": "Markdown", ".sql": "SQL", ".css": "CSS",
    }
    language = ext_to_lang.get(file_path.suffix.lower(), file_path.suffix)

    lines = content.split("\n")[:50]
    first_50 = "\n".join(lines)

    symbols_str = ", ".join(symbols[:15]) if symbols else "none"

    return (
        "Given this source file, write a one-line purpose description (under 100 chars).\n"
        "Focus on WHAT it does and WHY, not HOW.\n"
        "\n"
        f"File: {file_path.name}\n"
        f"Language: {language}\n"
        f"Key symbols: {symbols_str}\n"
        f"First 50 lines:\n{first_50}\n"
        "\n"
        "Purpose:"
    )


def _parse_response(output: str) -> Optional[str]:
    """Extract clean purpose from LLM response."""
    if not output:
        return None
    # Take first non-empty line, strip quotes and whitespace
    for line in output.strip().split("\n"):
        line = line.strip().strip('"').strip("'").strip()
        if line and len(line) > 5:
            # Truncate to 100 chars
            return line[:100]
    return None


def _log_enrichment(
    file_path: Path,
    old_purpose: str,
    new_purpose: str,
    provider: str,
    model: str,
    duration: float,
) -> None:
    """Append enrichment record for the web dashboard."""
    ENRICHMENT_LOG.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "file": str(file_path),
        "old_purpose": old_purpose,
        "new_purpose": new_purpose,
        "provider": provider,
        "model": model,
        "duration_s": round(duration, 2),
    }
    try:
        with open(ENRICHMENT_LOG, "a") as f:
            f.write(json.dumps(record) + "\n")
    except OSError:
        pass  # Non-critical — don't break enrichment over logging


def log_failure(file_path: Path, stage: str, error: str) -> None:
    """Append one failure record for a file.

    ``stage`` is a coarse bucket: ``bridge``, ``parse``, ``weak``,
    ``read``, ``write``. Used by :func:`recent_failure_count` to apply a
    retry budget — files that exceed it get skipped for a week so we
    don't burn LLM cycles on the same broken file every cron tick.
    """
    FAILURES_LOG.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "file": str(file_path),
        "stage": stage,
        "error": str(error)[:200],
    }
    try:
        with open(FAILURES_LOG, "a") as f:
            f.write(json.dumps(record) + "\n")
    except OSError:
        pass


def recent_failure_count(
    file_path: Path,
    *,
    window_days: int = FAILURE_RETRY_WINDOW_DAYS,
) -> int:
    """Return number of failures recorded for ``file_path`` in the last N days.

    Reads ``FAILURES_LOG`` tail (~200 KB) to keep this O(recent), not O(all
    time). Used by the enrichment loop to apply ``FAILURE_RETRY_BUDGET``.
    """
    if not FAILURES_LOG.is_file():
        return 0
    target = str(file_path)
    cutoff_dt = datetime.now(timezone.utc).timestamp() - window_days * 86400
    count = 0
    try:
        size = FAILURES_LOG.stat().st_size
        with open(FAILURES_LOG, "rb") as f:
            if size > 200_000:
                f.seek(size - 200_000)
                f.readline()
            for raw in f:
                try:
                    rec = json.loads(raw)
                except (ValueError, json.JSONDecodeError):
                    continue
                if rec.get("file") != target:
                    continue
                ts = rec.get("timestamp", "")
                try:
                    rec_dt = datetime.strptime(
                        ts.replace("Z", "+0000"), "%Y-%m-%dT%H:%M:%S%z"
                    ).timestamp()
                except ValueError:
                    continue
                if rec_dt >= cutoff_dt:
                    count += 1
    except OSError:
        return 0
    return count


def generate_purposes(
    files: list[tuple[Path, str, list[str], str]],
    max_files: int = MAX_PER_CYCLE,
) -> list[dict]:
    """Generate LLM purposes for a batch of files.

    Args:
        files: list of (file_path, content, symbols, old_purpose) tuples
        max_files: cap per cycle

    Returns:
        list of {file_path, purpose, provider, model, success} dicts
    """
    from okuro.bridge.invoke import invoke

    results = []
    batch = files[:max_files]

    for file_path, content, symbols, old_purpose in batch:
        prompt = _build_prompt(file_path, content, symbols)

        try:
            response = invoke(prompt, capability="fast-draft", timeout=30)
        except Exception as e:
            log.warning("bridge invoke failed for %s: %s", file_path.name, e)
            log_failure(file_path, "bridge", str(e))
            results.append({
                "file_path": file_path,
                "purpose": None,
                "provider": "error",
                "model": "",
                "success": False,
            })
            continue

        if not response.get("success"):
            log.debug(
                "purpose generation failed for %s: %s",
                file_path.name,
                response.get("error", "unknown"),
            )
            log_failure(
                file_path,
                "bridge",
                response.get("error", "no_success"),
            )
            results.append({
                "file_path": file_path,
                "purpose": None,
                "provider": response.get("provider", "unknown"),
                "model": response.get("model", ""),
                "success": False,
            })
            continue

        purpose = _parse_response(response["output"])
        if not purpose or is_weak_purpose(purpose):
            log.debug("LLM returned weak purpose for %s, skipping", file_path.name)
            log_failure(file_path, "weak", purpose or "empty")
            results.append({
                "file_path": file_path,
                "purpose": None,
                "provider": response.get("provider", ""),
                "model": response.get("model", ""),
                "success": False,
            })
            continue

        provider = response.get("provider", "unknown")
        model = response.get("model", "unknown")

        _log_enrichment(
            file_path, old_purpose, purpose, provider, model,
            response.get("duration", 0.0),
        )

        results.append({
            "file_path": file_path,
            "purpose": purpose,
            "provider": provider,
            "model": model,
            "success": True,
        })

    return results


def enrich_sidecar(directory: Path, root: Optional[Path] = None) -> dict:
    """Enrich weak-purpose entries in a directory's sidecar.

    Scans the sidecar for entries with weak purposes, generates better ones
    via LLM, and updates the sidecar in place.

    Returns:
        {enriched: int, skipped: int, failed: int, no_bridge: bool}
    """
    entries = sidecar_mod.load(directory)
    if not entries:
        return {"enriched": 0, "skipped": 0, "failed": 0, "no_bridge": False}

    # Collect files needing enrichment, skipping any that have exceeded the
    # retry budget — keeps the LLM from chewing on the same broken file
    # every cron tick. Budget resets after FAILURE_RETRY_WINDOW_DAYS.
    to_enrich: list[tuple[Path, str, list[str], str]] = []
    over_budget = 0
    for filename, entry in entries.items():
        if not is_weak_purpose(entry.purpose):
            continue
        file_path = directory / filename
        if not file_path.exists():
            continue
        if recent_failure_count(file_path) >= FAILURE_RETRY_BUDGET:
            over_budget += 1
            continue
        try:
            content = file_path.read_text()
        except (OSError, UnicodeDecodeError) as e:
            log_failure(file_path, "read", str(e))
            continue
        to_enrich.append((file_path, content, entry.index, entry.purpose))

    if not to_enrich:
        return {"enriched": 0, "skipped": len(entries), "failed": 0, "no_bridge": False}

    # Check bridge availability before batch
    try:
        from okuro.bridge.providers import resolve_provider
        resolve_provider("fast-draft", None, None)
    except Exception:
        log.info(
            "No LLM providers available — cortex search quality improves "
            "with an LLM configured. Skipping purpose enrichment."
        )
        return {
            "enriched": 0,
            "skipped": len(to_enrich),
            "failed": 0,
            "no_bridge": True,
        }

    results = generate_purposes(to_enrich)

    enriched = 0
    failed = 0
    for result in results:
        if result["success"] and result["purpose"]:
            filename = result["file_path"].name
            if filename in entries:
                entries[filename].purpose = result["purpose"]
                entries[filename].generated_by = (
                    f"bridge/{result['model']}"
                )
                enriched += 1
        else:
            failed += 1

    if enriched > 0:
        sidecar_mod.save(directory, entries)
        log.info(
            "enriched %d/%d purposes in %s",
            enriched, len(to_enrich), directory,
        )

    return {
        "enriched": enriched,
        "skipped": len(entries) - len(to_enrich),
        "failed": failed,
        "over_budget": over_budget,
        "no_bridge": False,
    }
