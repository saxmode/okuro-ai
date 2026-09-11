# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Does a subtask's own description say it will write files? One reader, three consumers.
# index:
#   def has_write_intent
#   def declared_paths
#   def undeclared_write_intent
# AGENT_HEADER_END -->
"""A subtask that says it will change files, and declares none.

Measured live 2026-08-01 (task-20260801-184809): the decomposer wrote
"normalize shebang lines by adding '#!/usr/bin/env bash' to any script missing
one" into a subtask's OWN description, then scored it ``risk: LOW`` with
``outputs: []`` and ``target_paths: []``. Nothing checked the declaration
against the description. Consequences, in order:

  * the blast-radius approval gate reads DECLARED outputs, and is only
    consulted at risk MED/HIGH — a LOW score skips it entirely;
  * fast-track requires every subtask in the phase to be LOW, so the same
    score also skipped the LLM critic (both rounds published in ~2 ms, zero
    findings);
  * eight files were rewritten outside any task workspace, and the subtask
    finished ``status: done`` with ``review_state: not_reviewed``.

Three tasks in one day under-declared this way, and none of them involved a
lying subagent — the DECOMPOSER did it, which is why the fix belongs at plan
time rather than in the dispatcher or the reviewer.

The heuristic is deliberately conservative: a write VERB *and* a file/path
NOUN. "Summarize the naming conventions used by shell scripts" has the noun
and no verb, and must not trip; "write a report" has the verb and no file
noun, and must not trip either. What must trip is a description that names an
edit and a thing on disk in the same breath.

Consumers: the decomposer (plan validation + risk floor), and the reviewer
pipeline (fast-track eligibility). Kept in its own module with no heavy imports
so both can read it without pulling the LLM bridge in — the alternative, which
this replaces, was the same tuple copied into two files with a note asking the
next reader to keep them in sync.
"""

from __future__ import annotations

import re
from typing import Any

# Verbs that change something on disk. "create" and "generate" are here because
# a subtask that creates a file still has to say where.
WRITE_INTENT_VERBS = (
    "edit", "edits", "editing",
    "normalize", "normalizes", "normalizing", "normalise", "normalising",
    "fix", "fixes", "fixing",
    "write", "writes", "writing",
    "save", "saves", "saving",
    "delete", "deletes", "deleting",
    "remove", "removes", "removing",
    "rename", "renames", "renaming",
    "modify", "modifies", "modifying",
    "update", "updates", "updating",
    "patch", "patches", "patching",
    "replace", "replaces", "replacing",
    "rewrite", "rewrites", "rewriting",
    "refactor", "refactors", "refactoring",
    "create", "creates", "creating",
    "generate", "generates", "generating",
    "append", "appends", "appending",
    "move", "moves", "moving",
    "copy", "copies", "copying",
    "chmod", "touch", "migrate", "migrates", "migrating",
    "tidy", "tidies", "tidying",
    "clean", "cleans", "cleaning",
    "format", "formats", "formatting",
    "add", "adds", "adding",
    "install", "installs", "installing",
)

# Things that live on disk. A verb alone is not intent — "write a summary" is
# every research subtask ever.
FILE_NOUNS = (
    "file", "files", "script", "scripts", "path", "paths",
    "directory", "directories", "folder", "folders",
    "config", "configs", "configuration", "configurations",
    "repo", "repos", "repository", "repositories",
    # "source" is deliberately ABSENT: "a working source URL per claim" is
    # research phrasing, and pairing it with the verb "write" made every
    # sourced-report subtask read as a file mutation. "source file" / "source
    # code" still trip via "file" and "codebase".
    "codebase", "module", "modules",
    "line", "lines", "shebang", "shebangs",
    "dotfile", "dotfiles", "manifest", "manifests",
    "yaml", "yml", "json", "toml", "sql", "css", "html",
)

_VERB_RE = re.compile(
    r"\b(" + "|".join(re.escape(v) for v in WRITE_INTENT_VERBS) + r")\b",
    re.IGNORECASE,
)
_NOUN_RE = re.compile(
    r"\b(" + "|".join(re.escape(n) for n in FILE_NOUNS) + r")\b",
    re.IGNORECASE,
)
# An explicit path or extension is a file noun by itself — "tidy up
# /etc/caddy/Caddyfile" names no noun from the list above.
_PATH_RE = re.compile(r"(^|\s)(/|~/|\./)\S|\.\w{1,4}\b")


def has_write_intent(description: Any) -> bool:
    """True when this description says it will change something on disk."""
    text = str(description or "")
    if not text.strip():
        return False
    if not _VERB_RE.search(text):
        return False
    return bool(_NOUN_RE.search(text) or _PATH_RE.search(text))


def declared_paths(subtask: Any) -> list[str]:
    """``outputs`` + ``target_paths``, from a dict or a Subtask object."""
    out: list[str] = []
    for key in ("outputs", "target_paths"):
        if isinstance(subtask, dict):
            src = subtask.get(key)
        else:
            src = getattr(subtask, key, None)
        if isinstance(src, (list, tuple)):
            out.extend(str(x).strip() for x in src
                       if isinstance(x, str) and x.strip())
    return out


def undeclared_write_intent(subtask: Any) -> bool:
    """The defect: says it will write, declares nothing it will write to."""
    description = (subtask.get("description") if isinstance(subtask, dict)
                   else getattr(subtask, "description", ""))
    return has_write_intent(description) and not declared_paths(subtask)


__all__ = [
    "FILE_NOUNS",
    "WRITE_INTENT_VERBS",
    "declared_paths",
    "has_write_intent",
    "undeclared_write_intent",
]
