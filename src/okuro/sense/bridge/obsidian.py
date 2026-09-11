# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Obsidian bridge — watches a notes directory and ingests to okuro thought DB.
# index:
#   imports
#   def _notes_dir
#   def _load_hashes
#   def _save_hashes
#   def _hash_content
#   def _should_skip
#   def _strip_agent_header
#   def _strip_frontmatter
#   def _qualify_content
#   def _rejection_reason
#   def _ingest_file
#   def sync_once
#   def main
# AGENT_HEADER_END -->
"""Obsidian bridge — watches a notes directory and ingests to okuro thought DB.

Design: one Obsidian note = one thought row. When a note is edited the
existing thought row is UPDATED (upsert on source_path), not duplicated.
Notes are qualified before insertion — empty, frontmatter-only, and
trivially short content is dropped.

Ported from tm-launcher bridge/obsidian.py.
"""

import hashlib
import json
import logging
import os
import re
import time
from pathlib import Path
from okuro.db.engine import okuro_home

log = logging.getLogger("okuro.sense.bridge.obsidian")

# No default — the Obsidian bridge is opt-in. Set OKURO_NOTES_DIR to the
# absolute path of your vault to enable it. When unset, _notes_dir() returns
# None and every bridge entry point no-ops gracefully.
_DEFAULT_NOTES_DIR: Path | None = None
_CACHE_DIR = okuro_home() / "cache"
_HASH_FILE = _CACHE_DIR / "obsidian-hashes.json"

_SKIP_DIRS = {".obsidian", "templates", ".trash"}
_MIN_CONTENT_LEN = 20
_MAX_CONTENT_LEN = 5000

_SPEC_HEADING_WORDS = {
    "protocol", "architecture", "specification", "department",
    "system", "version", "blackai", "migration", "schema",
}


def _notes_dir() -> Path | None:
    env = os.environ.get("OKURO_NOTES_DIR")
    if env:
        return Path(env)
    return _DEFAULT_NOTES_DIR


def _load_hashes() -> dict:
    try:
        return json.loads(_HASH_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_hashes(hashes: dict):
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    _HASH_FILE.write_text(json.dumps(hashes))


def _hash_content(content: str) -> str:
    return hashlib.sha256(content.encode()).hexdigest()


def _should_skip(path: str) -> bool:
    parts = path.replace("\\", "/").split("/")
    return any(p in _SKIP_DIRS for p in parts)


def _strip_agent_header(content: str) -> str:
    return re.sub(
        r'<!--\s*AGENT_HEADER.*?AGENT_HEADER_END\s*-->\s*', '', content, flags=re.DOTALL
    ).strip()


def _strip_frontmatter(content: str) -> str:
    return re.sub(r'^---\s*\n.*?\n---\s*\n?', '', content, count=1, flags=re.DOTALL).strip()


def _qualify_content(content: str) -> str | None:
    """Return cleaned content if it qualifies as a meaningful thought, else None."""
    text = _strip_agent_header(content)
    text = _strip_frontmatter(text)

    if not text:
        return None
    if len(text) > _MAX_CONTENT_LEN:
        return None

    prose = re.sub(r'!\[\[.*?\]\]', '', text)
    prose = re.sub(r'\[\[.*?\]\]', '', prose)
    prose = re.sub(r'!\[.*?\]\(.*?\)', '', prose)
    prose = re.sub(r'[-*]\s*\[[ x]\]\s*$', '', prose, flags=re.MULTILINE)
    prose = prose.strip()

    if len(prose) < _MIN_CONTENT_LEN:
        return None

    lines = [ln for ln in text.splitlines() if ln.strip()]
    if lines:
        bullet_lines = sum(1 for ln in lines if re.match(r'\s*[-*]\s', ln))
        if bullet_lines / len(lines) > 0.70:
            return None

    first_heading = re.match(r'^#\s+(.+)', text)
    if first_heading:
        heading_words = set(first_heading.group(1).lower().split())
        if heading_words & _SPEC_HEADING_WORDS:
            return None

    return text


def _rejection_reason(raw: str) -> str:
    text = _strip_agent_header(raw)
    text = _strip_frontmatter(text)
    if not text:
        return "empty after stripping"
    if len(text) > _MAX_CONTENT_LEN:
        return f"too long ({len(text)} chars)"
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if lines:
        bullet_lines = sum(1 for ln in lines if re.match(r'\s*[-*]\s', ln))
        if bullet_lines / len(lines) > 0.70:
            return "bullet dump (>70% bullets)"
    first_heading = re.match(r'^#\s+(.+)', text)
    if first_heading:
        heading_words = set(first_heading.group(1).lower().split())
        if heading_words & _SPEC_HEADING_WORDS:
            return f"spec/reference doc ({first_heading.group(1)[:40]})"
    return "too short or link-only"


def _ingest_file(filepath: str, hashes: dict, notes_dir: str) -> bool:
    """Ingest a markdown file as a thought. Returns True if ingested/updated."""
    try:
        rel_path = os.path.relpath(filepath, notes_dir)
        if _should_skip(rel_path):
            return False

        with open(filepath) as f:
            raw = f.read()

        content_hash = _hash_content(raw)
        if hashes.get(rel_path) == content_hash:
            return False

        content = _qualify_content(raw)
        if content is None:
            hashes[rel_path] = content_hash
            if raw.strip():
                reason = _rejection_reason(raw)
                log.debug("skip %s (%s)", rel_path, reason)
            return False

        filename = os.path.basename(filepath).replace(".md", "")
        if len(content) < 50:
            content = f"{filename}: {content}"

        from okuro.sense.thoughts import upsert_obsidian_thought
        upsert_obsidian_thought(content=content, source_path=rel_path)
        hashes[rel_path] = content_hash
        return True

    except Exception as e:
        log.warning("Error ingesting %s: %s", filepath, e)
        return False


def sync_once() -> int:
    """Single sync pass — check for new/changed files. Returns count of changed files.

    The bridge is opt-in. When ``OKURO_NOTES_DIR`` is unset we no-op
    silently — the daemon fires this every minute and we don't want a log
    line per minute complaining about an unconfigured feature.
    """
    notes_dir = _notes_dir()
    if notes_dir is None:
        return 0
    if not notes_dir.is_dir():
        log.warning("Notes directory not found: %s", notes_dir)
        return 0

    hashes = _load_hashes()
    changed = 0
    notes_str = str(notes_dir)

    for root, dirs, files in os.walk(notes_str):
        dirs[:] = [d for d in dirs if not d.startswith(".") and d not in _SKIP_DIRS]

        for fname in files:
            if not fname.endswith(".md"):
                continue
            filepath = os.path.join(root, fname)
            if _ingest_file(filepath, hashes, notes_str):
                changed += 1

    _save_hashes(hashes)
    return changed


def main():
    """Entry point for okuro-obsidian-sync daemon."""
    notes_dir = _notes_dir()
    if notes_dir is None:
        log.info("OKURO_NOTES_DIR not set — Obsidian bridge disabled, exiting")
        return
    log.info("Obsidian bridge starting — watching %s", notes_dir)

    try:
        from watchdog.observers import Observer
        from watchdog.events import FileSystemEventHandler

        hashes = _load_hashes()
        notes_str = str(notes_dir)

        class NoteHandler(FileSystemEventHandler):
            def on_modified(self, event):
                if event.src_path.endswith(".md"):
                    if _ingest_file(event.src_path, hashes, notes_str):
                        _save_hashes(hashes)

            def on_created(self, event):
                if event.src_path.endswith(".md"):
                    if _ingest_file(event.src_path, hashes, notes_str):
                        _save_hashes(hashes)

        changed = sync_once()
        log.info("Initial sync: %d files ingested", changed)
        hashes = _load_hashes()

        observer = Observer()
        observer.schedule(NoteHandler(), notes_str, recursive=True)
        observer.start()
        log.info("Watching for changes...")

        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            observer.stop()
        observer.join()

    except ImportError:
        log.info("watchdog not installed — running in poll mode (sync every 60s)")
        while True:
            changed = sync_once()
            if changed:
                log.info("Synced %d files", changed)
            time.sleep(60)


if __name__ == "__main__":
    import logging as _logging
    _logging.basicConfig(level=_logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    main()
