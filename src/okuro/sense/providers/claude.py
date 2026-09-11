# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Claude Code provider adapter.
# index: imports | class ClaudeAdapter
# AGENT_HEADER_END -->
"""Claude Code provider adapter."""

import json
import logging
import os
import shutil
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path

try:
    import fcntl  # POSIX-only; falls back to no-lock on Windows
except ImportError:  # pragma: no cover - non-POSIX
    fcntl = None  # type: ignore[assignment]

from . import register
from .base import ProviderAdapter


log = logging.getLogger(__name__)


# THE single declaration of every hook script okuro writes under
# `~/.claude/hooks/`: filename -> a callable returning that script's body.
# Three things read it and none of them may disagree with the others:
#
#   install_hooks()        writes exactly these files, in this order
#   _strip_okuro_from_event  recognises de-tagged entries by these basenames
#   _purge_stale_project_overrides  archives project-local copies of them
#
# Bodies are callables so this table can sit above the constants and builders
# it names — each name resolves at call time, not at import time.
#
# WHY DERIVED, AND NOT A HAND-WRITTEN SET (the defect this replaced).
# The filename set below used to be typed out by hand. `check-system.py` and
# `check-store.py` were added to the installer without being added to it, so
# the marker-free basename sweep could not see de-tagged copies of those two.
# Something rewrites settings.json through a schema that drops unknown keys —
# okuro's `_okuro` / `meta` markers included — and every install then re-added
# a tagged copy beside the stale untagged one it could no longer recognise.
# Measured 2026-08-27: 92 PreToolUse matchers, check-system.py x44 and
# check-store.py x44, i.e. 88 hook processes per Bash call, doubling roughly
# every 16 days. The nine names that WERE listed all sat at count 1.
# Adding a hook here now updates the installer, the sweep and the purge in one
# edit; there is no second list left to forget.
_OKURO_HOOK_SCRIPTS: tuple[tuple[str, Callable[[], str]], ...] = (
    # Grep → cortex redirect.
    ("check-grep.py", lambda: build_check_grep_hook()),
    # System-state shell reads → okuro sysinfo tools. COMPILED from the
    # tool_routing contract, so this hook's routes and the bootstrap packet's
    # routing table are the same definition.
    ("check-system.py", lambda: build_check_system_hook()),
    # Direct reads of okuro's own store → the typed MCP tool. The interpreter
    # path: python/sqlite3/node reaching okuro.db, which no command-name gate
    # can cover. Compiles to an empty body when the contract yields no store
    # guard — then the file is not written, but its path stays registered.
    ("check-store.py", lambda: build_check_store_hook()),
    # Nested agents must bootstrap for themselves. The middleware's A8 gate is
    # keyed on the transport, so a subagent inherits it already satisfied;
    # only the hook layer can see `agent_id` and tell the callers apart.
    ("check-agent-bootstrap.py", lambda: build_check_agent_bootstrap_hook()),
    # Read(full file) → cortex_read_header for large files in indexed
    # projects (the A9b enforcement gap).
    ("check-read.py", lambda: CHECK_READ_HOOK),
    # Session compliance reminder.
    ("session-compliance.py", lambda: SESSION_COMPLIANCE_HOOK),
    # Per-turn profile context injection via Claude Code's UserPromptSubmit
    # `additionalContext` channel, so style-critical profile rules are present
    # before the model drafts the answer.
    ("user-prompt-profile.py", lambda: USER_PROMPT_PROFILE_HOOK),
    # Profile-compliance Stop hook. Reads ~/.okuro/profile-hook-rules.json at
    # runtime; install_hooks() writes that cache from the active profile.
    ("profile-compliance.py", lambda: PROFILE_COMPLIANCE_HOOK),
    # Codebase-intelligence bypass Stop hook. Shebang baked to the okuro
    # interpreter (sys.executable) so its thin body can import the shared
    # provider-agnostic detector core.
    (
        "codebase-intel.py",
        lambda: CODEBASE_INTEL_HOOK.replace("__OKURO_PYTHON__", sys.executable, 1),
    ),
    # PreCompact marker writer — drops a session-keyed marker before context
    # compression so the next UserPromptSubmit hook can inject a save-now
    # directive. Two-step because PreCompact hooks cannot themselves reach the
    # model on the next turn.
    ("precompact-save.py", lambda: PRECOMPACT_SAVE_HOOK),
    # UserPromptSubmit marker reader — consumes the marker precompact-save.py
    # dropped and injects the directive via additionalContext. Sibling to
    # user-prompt-profile.py: separate concerns, separate hook scripts.
    ("user-prompt-compaction.py", lambda: USER_PROMPT_COMPACTION_HOOK),
)


# DERIVED — never hand-edit. Project-local copies with these names are stale
# overrides — see `_purge_stale_project_overrides`.
_OKURO_HOOK_FILENAMES = frozenset(name for name, _ in _OKURO_HOOK_SCRIPTS)


# Marker used to identify okuro-managed hook entries inside settings.json. We
# tag both the matcher and each inner hook so a user duplicating the matcher
# name (e.g. their own "Grep" matcher) cannot be confused with ours.
_OKURO_MARKER_KEY = "_okuro"
_OKURO_MARKER_VALUE = "managed"


class SettingsCorruptError(RuntimeError):
    """Raised when ~/.claude/settings.json cannot be parsed.

    A copy of the unparseable file is written alongside the original (so the
    user can recover) and the path is exposed as `corrupt_copy_path`. Refusing
    to write protects the user from a single corrupt char nuking their config.
    """

    def __init__(self, settings_path: str, corrupt_copy_path: str, parse_error: Exception):
        self.settings_path = settings_path
        self.corrupt_copy_path = corrupt_copy_path
        self.parse_error = parse_error
        super().__init__(
            f"Refusing to overwrite unparseable {settings_path}: {parse_error}. "
            f"Original copied to {corrupt_copy_path} for recovery."
        )


# Canonical stub content written to `~/.claude/projects/*/memory/MEMORY.md`.
# Claude Code auto-injects this file into every session; by replacing its
# contents we turn the per-project memory folder from "active secondary
# memory store" into "pointer to okuro". A1: single memory store.
# AUTO-INJECTED INTO EVERY SESSION, so every line is agent-addressed or gone.
# Removed: the folder's history ("historically used by Claude Code's
# per-project auto-memory" — the agent was not there and cannot act on it);
# the archive path with "recover from there if needed", which is a recovery
# instruction to the owner; the "every 5 min" refresh interval, which is okuro's
# internal cadence; and the self-referential "(other than this MEMORY.md
# stub)". What remains is the rule and the two calls that satisfy it.
_CLAUDE_MEMORY_STUB = """\
# Memory lives in okuro.db

Do NOT write files into this folder. Anything you create here is moved out
automatically and will never be read back.

## Read
```
mcp__okuro__read_memory(query="...", topic="gotcha|convention|decision|learning|architecture")
```

## Write
```
mcp__okuro__write_memory(topic="gotcha", content="<what you learned>", project="<slug>")
```
"""


# Instruction body now lives in providers/template.py — see build_instructions().
# This adapter retains only Claude-specific concerns (hooks, memory stub, etc.).


# The cortex-coverage probe MOVED to _gate_contract.py. It is argv- and
# path-level detection with nothing Claude-specific in it, and while it
# lived here the cortex-first rule reached exactly one provider.
# Re-exported under the old name so both splice points below keep working.
from ._gate_contract import CORTEX_COVERAGE_SRC as _CORTEX_DETECT_FN  # noqa: E402


# The compiler moved to _gate_contract.py when Antigravity needed the same
# routes: leaving it here would have meant an adapter importing another
# adapter, or two copies of the projection. Re-exported so existing
# importers and tests keep working.
from ._gate_contract import compile_system_state_routes  # noqa: E402


CHECK_SYSTEM_HOOK_TEMPLATE = '''\
#!/usr/bin/env python3
# PreToolUse hook: system-state shell commands -> okuro sysinfo tools
# Auto-generated by okuro from data/agent_rules.yaml. Do not edit manually.
# Exit 0 = allow, Exit 2 = block with feedback
#
# COMPILED, NOT HAND-WRITTEN. The routes below are projected from the
# `tool_routing` actions that declare `protects: [system_state]`. The packet
# renderer builds its table from the same list, so the rule an agent reads and
# the rule enforced here are one definition. Add an action to the yaml and this
# file gains a route on the next install.
#
# Scope is deliberately narrow. Each route is an unambiguous state READ with an
# exact okuro equivalent, so a deny is never a judgement call and the agent is
# never left without a route — the failure mode that turns a gate into a thing
# people switch off.
import sys, json, re, shlex, os

_ROUTES = {routes!r}

__MATCHER__

try:
    data = json.load(sys.stdin)
    command = (data.get('tool_input') or {{}}).get('command', '')
except Exception:
    sys.exit(0)

if not command:
    sys.exit(0)

hit = _match(command)
if hit:
    native, call = hit
    print(
        f"`{{native}}` is a system-state read. okuro answers it directly: "
        f"{{call}}\\n\\n"
        f"That returns parsed, structured state instead of text you have to "
        f"scrape, and it works the same on every provider. This route is "
        f"generated from okuro's tool_routing contract — the same source as "
        f"the routing table in your bootstrap packet.",
        file=sys.stderr,
    )
    sys.exit(2)

sys.exit(0)
'''


def build_check_system_hook() -> str:
    """Render the system-state hook with the compiled routes embedded.

    The matcher body is spliced from ``_gate_contract`` rather than written
    here: Cursor's emitter needs the identical route-matching semantics behind
    a different payload and denial shape, and two hand-written copies of "what
    counts as a system-state read" is the drift this contract exists to end.
    """
    from ._gate_contract import SHELL_ROUTE_MATCHER_SRC
    rendered = CHECK_SYSTEM_HOOK_TEMPLATE.format(
        routes=compile_system_state_routes()
    )
    # Blank lines around the splice reproduce the previous inline layout
    # exactly, so the extraction is provably a no-op on the emitted hook.
    return rendered.replace(
        "__MATCHER__", "\n" + SHELL_ROUTE_MATCHER_SRC.strip() + "\n"
    )


# NO BENCHMARK IN THE EMITTED MESSAGES. Both denial prints below once carried
# "Measured on 25 ground-truth queries in this codebase: cortex recall@5 0.92
# vs ripgrep 0.28". That is the evidence which justified BUILDING this gate —
# a maintainer's number. Quoted at the agent it invites the cost-benefit
# judgement the rule does not offer, and 0.28 is a figure an agent can weigh
# against its own confidence. The SIZE claim (~2 KB vs ~25 KB) stays: that is
# a fact about what the route returns, which is the agent's business.
#
# THIS COMMENT LIVES OUTSIDE THE STRING ON PURPOSE. The hook body is a
# triple-quoted literal, so a `#` line written inside it is emitted into
# ~/.claude/hooks/check-grep.py — which is how rationale meant for a
# maintainer ends up shipped. Measured: the first version of this very fix
# wrote its explanation inside the literal and leaked it.
CHECK_GREP_HOOK_TEMPLATE = """\
#!/usr/bin/env python3
# PreToolUse hook: redirects broad code hunting to cortex_search
# Auto-generated by okuro. Do not edit manually.
# Exit 0 = allow, Exit 2 = block with feedback
#
# Bound to the ACTION (hunting for code), not to a tool NAME. Registered on
# BOTH `Grep` and `Bash`, because binding to `Grep` alone bound nothing: the
# Grep tool went to 0 uses in 30 days (from 1792/month) while `grep -r` and
# `find` inside Bash carried 4290 unpoliced hunts over the same window. The
# capability simply moved to the unmatched surface. Third hole of this class
# in this hook -- see the two comments in _cortex_covers below for holes 1-2.
import sys, json, os, re, shlex
""" + _CORTEX_DETECT_FN + """

__HUNT_DETECT__


__CORTEX_SEQUENCE__


try:
    data = json.load(sys.stdin)
    ti = data.get('tool_input', {})
    path = ti.get('path', '')
    pattern = ti.get('pattern', '')
    command = ti.get('command', '')
    # Resolve relative paths against the SESSION's cwd, which the payload
    # carries, not the hook process's -- they are not guaranteed to match, and
    # every directory test below depends on getting this right.
    session_cwd = data.get('cwd') or os.getcwd()
except Exception:
    sys.exit(0)

# Cortex has been tried in this session -- grep is the sanctioned fallback now.
if _cortex_already_tried():
    sys.exit(0)

if command:
    # Bash surface. Anything that is not a recognised tree hunt passes.
    hunted = _bash_hunt_target(command, session_cwd)
    if hunted is None:
        sys.exit(0)
    if not _cortex_covers(hunted):
        sys.exit(0)
    print('cortex indexes this path. That shell hunt reaches the same code via '
          'cortex_search(query, project=...) for meaning, or '
          'cortex_search_code(pattern) for an exact symbol or string -- both '
          'search the whole indexed project and return ~2 KB instead of ~25 KB. '
          'Once you have asked cortex, grep is the sanctioned fallback for what '
          'it could not answer. Piping (`... | grep x`) and grep on one named '
          'file are never blocked.',
          file=sys.stderr)
    sys.exit(2)

# Allow: targeting one specific file — that is what Grep is for.
if path and os.path.isfile(path):
    sys.exit(0)

# A directory search is BROAD regardless of depth. The previous rule allowed
# any path that was not the repo root, so `Grep(path="src")` sailed through
# and the blocker only ever fired on a bare repo-root search — which agents
# rarely issue. That hole is why the hook produced no measurable behaviour
# change in the okuro repo itself.
target = path or os.getcwd()
if _cortex_covers(target):
    print(f'cortex indexes this path. Use cortex_search("{pattern}", project=...) '
          f'for meaning, or cortex_search_code("{pattern}") for an exact symbol '
          f'or string — both search the whole indexed project and return ~2 KB '
          f'instead of ~25 KB. '
          f'Once you have asked cortex, Grep is the sanctioned fallback for '
          f'what it could not answer, and it is always correct for a single '
          f'named file.',
          file=sys.stderr)
    sys.exit(2)

sys.exit(0)
"""


CHECK_STORE_HOOK_SRC = '#!/usr/bin/env python3\n# PreToolUse hook: direct reads of okuro\'s store -> the typed MCP tool\n# Auto-generated by okuro from data/agent_rules.yaml. Do not edit manually.\n# Exit 0 = allow, Exit 2 = block with feedback\n#\n# THE INTERPRETER PATH. Gating command NAMES cannot close this: an agent\n# reaches the store through python, sqlite3, node, perl, jq -- the set of\n# programs that can open a SQLite file is unbounded because a shell is\n# Turing-complete. What IS bounded is the RESOURCE, so this matches the\n# store\'s path anywhere in the command text, INCLUDING inside a heredoc body\n# where argv parsing never looks. Every real violation this session hid the\n# path in a `python3 - <<PY` body.\n#\n# WHY THE MARKER CARRIES A DIRECTORY. `okuro.db` alone is also the dotted\n# module path, and okuro\'s own documented migration command is\n# `python -c "... from okuro.db import get_db; ... migrate()"`. Matching the\n# bare string would deny a documented operational procedure. A file reference\n# always carries `.okuro/okuro.db`; an import never does.\nimport sys, json\n\n_MARKER = __MARKER__\n_CALL = __CALL__\n\n# Operational commands that legitimately touch the store FILE rather than\n# querying it for information an MCP tool serves. Backups, migrations and\n# integrity checks have no okuro-tool equivalent, so denying them would leave\n# the agent with no route -- which is how a gate stops being trusted.\n_OPERATIONAL = (\'migrate\', \'backup\', \'.dump\', \'vacuum\', \'PRAGMA integrity_check\',\n                \'sqlite3_backup\', \'cp \', \'rsync\')\n\n\ntry:\n    data = json.load(sys.stdin)\n    command = (data.get(\'tool_input\') or {}).get(\'command\', \'\')\nexcept Exception:\n    sys.exit(0)\n\nif not command or _MARKER not in command:\n    sys.exit(0)\n\nif any(op in command for op in _OPERATIONAL):\n    sys.exit(0)\n\nprint(\n    "That reads okuro\'s own store directly. Use the typed tool for what "\n    "you need: "\n    + _CALL + "\\n\\n"\n    "A typed tool carries the caveats a raw query drops -- window bounds, "\n    "echo classification, counting notes. Operational work on the file "\n    "(migrate, backup, integrity check) is not blocked.",\n    file=sys.stderr,\n)\nsys.exit(2)\n'


def build_check_store_hook() -> str:
    """Render the store guard with its marker and route embedded."""
    from ._gate_contract import compile_store_guard
    guard = compile_store_guard()
    if not guard:
        return ""
    return CHECK_STORE_HOOK_SRC.replace(
        "__MARKER__", repr(guard["marker"])
    ).replace("__CALL__", repr(guard["call"]))


# Tools an unbootstrapped caller may still reach. Mirrors
# mcp_middleware._PRE_BOOTSTRAP_ALLOWLIST -- the middleware owns the top-level
# session, this hook owns nested agents, and the two must not disagree about
# what bootstrap itself needs.
_AGENT_GATE_ALLOWLIST = ("bootstrap", "get_profile", "list_projects", "get_project")


CHECK_AGENT_BOOTSTRAP_HOOK = """\
#!/usr/bin/env python3
# PreToolUse hook: nested agents must bootstrap before reaching okuro tools.
# Auto-generated by okuro. Do not edit manually.
# Exit 0 = allow, Exit 2 = block with feedback
#
# WHY THIS EXISTS AT THE HOOK LAYER AND NOT IN mcp_middleware.
#
# The middleware's A8 gate reads get_session_state()["bootstrapped"], and
# session_state is keyed by TRANSPORT. Under stdio there is exactly one key, so
# a nested agent spawned in the client's own process arrives at a gate the
# parent already opened -- it reaches every okuro tool having never received
# the behavioural contract, the routing table or the Recent section. That is
# pinned by tests/sense/test_bootstrap_gate_nesting.py, whose strict xfail is
# the desired behaviour this hook supplies.
#
# The middleware cannot fix it: over one transport it cannot tell callers
# apart. Four detection strategies were evaluated and rejected (artifact
# aad04e81); the decisive objection was that a second bootstrap on a live
# transport is also what a human produces after /clear, so gating on it would
# trade a machine-only bug for a human-facing regression.
#
# The hook layer does not have that problem. Measured 2026-07-28: the
# PreToolUse payload carries `agent_id` and `agent_type` for a nested agent and
# omits them for the top-level session, while session_id, transcript_path, cwd,
# prompt_id and the whole CLAUDE_* environment are IDENTICAL for both. agent_id
# is therefore an unambiguous discriminator, and a human after /clear never has
# one. Confirmed in the same run: PreToolUse matchers do fire on mcp__okuro__*
# tool names, and settings.json is re-read live.
#
# Scope discipline: absent agent_id this hook exits 0 immediately. The
# top-level session keeps the middleware's gate untouched, and okuro's
# provider-agnostic _NONINTERACTIVE_ENV spawn contract is not involved at all.
import sys, json, os, re, time

_ALLOWLIST = __ALLOWLIST__
_PREFIX = 'mcp__okuro__'
_STATE_DIR = os.path.expanduser('~/.cache/okuro-agent-bootstrap')
_TTL = 86400  # agent ids are single-use; forget them after a day


def _marker(agent_id):
    return os.path.join(_STATE_DIR, agent_id)


def _sweep():
    # Unbounded growth otherwise: every subagent mints a fresh id. Best-effort
    # and never fatal -- a failed sweep must not block a tool call.
    try:
        now = time.time()
        for name in os.listdir(_STATE_DIR):
            p = os.path.join(_STATE_DIR, name)
            try:
                if now - os.stat(p).st_mtime > _TTL:
                    os.unlink(p)
            except OSError:
                pass
    except OSError:
        pass


try:
    data = json.load(sys.stdin)
except Exception:
    sys.exit(0)

agent_id = data.get('agent_id')
tool = data.get('tool_name') or ''

# WHO IS CALLING -- published for the MCP server, which cannot tell.
#
# Under stdio the server is a child of the client, so the parent session and
# every nested agent share one connection and therefore one compliance state
# dict. Rotating that dict on each new bootstrap stopped children inheriting a
# parent's counters, but left the mirror defect: when the parent resumes after
# a child exits, it is holding the CHILD's state -- its own memory_consulted,
# tool_calls and compliance flags are gone. Forcing subagents to bootstrap made
# that the common case rather than the rare one.
#
# The prior analysis concluded a fix "needs a parent signal stdio does not
# carry". It does not carry one -- but this hook has it. agent_id is present
# for a nested agent and absent for the top-level session, and PreToolUse runs
# BEFORE the server handles the call, so writing it here is ordered correctly.
#
# Single-writer by construction: Claude Code blocks the parent while a child
# runs, and parallel calls within one agent share its id.
if tool.startswith(_PREFIX):
    try:
        os.makedirs(_STATE_DIR, exist_ok=True)
        with open(os.path.join(_STATE_DIR, '.current'), 'w') as _cf:
            _cf.write(str(agent_id or ''))
    except OSError:
        pass  # the server falls back to one shared dict -- today's behaviour

# Top-level session -- mcp_middleware's A8 gate already covers it.
if not agent_id or not tool.startswith(_PREFIX):
    sys.exit(0)

# Defensive: the id becomes a filename. Anything unexpected, allow rather than
# risk a path escape or a spurious refusal.
if not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', str(agent_id)):
    sys.exit(0)

short = tool[len(_PREFIX):]

if short in _ALLOWLIST:
    if short == 'bootstrap':
        # Marked optimistically: PreToolUse runs before the call, so a failed
        # bootstrap still clears the gate. Acceptable -- the agent sees the
        # error itself, and the alternative is refusing an agent that DID
        # bootstrap because okuro happened to be briefly unavailable.
        try:
            os.makedirs(_STATE_DIR, exist_ok=True)
            open(_marker(agent_id), 'w').close()
            _sweep()
        except OSError:
            pass
    sys.exit(0)

if os.path.exists(_marker(agent_id)):
    sys.exit(0)

print(
    'REJECTED: `' + tool + '` is not available to a subagent before bootstrap.'
    + chr(10) + chr(10)
    + '>> CALL THIS NOW:' + chr(10)
    + "   mcp__okuro__bootstrap(task_hint='<your task>', provider='claude-code')"
    + chr(10) + chr(10)
    + '>> You are a SUBAGENT. You did not inherit your parent bootstrap -- it'
    + chr(10)
    + '   loaded the contract into the parent context, not yours. Without your'
    + chr(10)
    + '   own call you have no behavioural contract, no tool-routing table and'
    + chr(10)
    + '   no record of what changed recently.' + chr(10) + chr(10)
    + '>> Do NOT work around this by importing okuro Python directly or reading'
    + chr(10)
    + '   ~/.okuro/okuro.db yourself. Skipping bootstrap means working without'
    + chr(10)
    + '   the rules, not merely without the data.',
    file=sys.stderr,
)
sys.exit(2)
"""


def build_check_grep_hook() -> str:
    """Splice the SHARED sequence check into the grep gate.

    Same reason `build_check_system_hook` splices the matcher: the emitter owns
    the payload and allow shape, the contract owns the DECISION.

    The template alone is NOT a runnable hook -- it still carries the
    `__CORTEX_SEQUENCE__` placeholder. That is why the constant is named
    `..._TEMPLATE`: a test wrote the raw string to disk and got a NameError at
    the placeholder line, which is the right failure but only because the name
    was wrong first.
    """
    from ._gate_contract import CORTEX_SEQUENCE_SRC

    from ._gate_contract import HUNT_DETECT_SRC

    return CHECK_GREP_HOOK_TEMPLATE.replace(
        "__HUNT_DETECT__", HUNT_DETECT_SRC.strip()
    ).replace("__CORTEX_SEQUENCE__", CORTEX_SEQUENCE_SRC.strip())


def build_check_agent_bootstrap_hook() -> str:
    return CHECK_AGENT_BOOTSTRAP_HOOK.replace(
        "__ALLOWLIST__", repr(_AGENT_GATE_ALLOWLIST)
    )


CHECK_READ_HOOK = """\
#!/usr/bin/env python3
# PreToolUse hook: redirects large Read calls to cortex_read_header + cortex_read_section
# Auto-generated by okuro. Do not edit manually.
# Exit 0 = allow, Exit 2 = block with feedback
#
# Rationale: `Read(full_file)` on large source files blows token budget when
# `cortex_read_header(path)` + `cortex_read_section(path, start, end)` does
# the same job with 5-10x less context. This hook blocks large in-project
# Reads so agents reach for cortex first. Small files (configs, READMEs,
# short source) pass through — cortex header on a 50-line file is overkill.
import sys, json, os
""" + _CORTEX_DETECT_FN + """

_LINE_THRESHOLD = 200  # above this, prefer cortex
_ALWAYS_OK_EXTENSIONS = {
    '.md', '.txt', '.json', '.yaml', '.yml', '.toml', '.cfg', '.ini',
    '.env', '.sh', '.sql',
}

try:
    data = json.load(sys.stdin)
    ti = data.get('tool_input', {})
    file_path = ti.get('file_path', '')
    offset = ti.get('offset')
    limit = ti.get('limit')
except Exception:
    sys.exit(0)

if not file_path:
    sys.exit(0)

# Allow: caller asked for a bounded window. `Read(offset, limit)` with a
# small `limit` is functionally equivalent to `cortex_read_section` and is
# required by Edit's "must read first" contract. Only full-file Reads are
# the target of this hook.
if isinstance(limit, int) and limit > 0 and limit <= 100:
    sys.exit(0)

project_root = os.getcwd()

# Allow: Read outside the project tree (user data, temp files, logs).
try:
    if not os.path.realpath(file_path).startswith(os.path.realpath(project_root) + os.sep):
        sys.exit(0)
except OSError:
    sys.exit(0)

# Allow: cortex not available — no alternative to route to.
if not _cortex_covers(file_path or project_root):
    sys.exit(0)

# Allow: non-source files where cortex headers don't add value.
ext = os.path.splitext(file_path)[1].lower()
if ext in _ALWAYS_OK_EXTENSIONS:
    sys.exit(0)

# Allow: file is small enough that Read is cheaper than cortex round-trip.
try:
    with open(file_path, 'rb') as f:
        line_count = sum(1 for _ in f)
except (OSError, ValueError):
    sys.exit(0)

if line_count <= _LINE_THRESHOLD:
    sys.exit(0)

# Block: large source file in an indexed project. Route to cortex.
rel = os.path.relpath(file_path, project_root)
print(
    f'Use cortex_read_header("{rel}") to get this file\\'s purpose + section '
    f'index ({line_count} lines), then cortex_read_section(path, start, end) '
    f'for the specific range you need. Read(full_file) on files > {_LINE_THRESHOLD} '
    f'lines wastes tokens when cortex can return a targeted slice.',
    file=sys.stderr,
)
sys.exit(2)
"""


USER_PROMPT_PROFILE_HOOK = r"""#!/usr/bin/env python3
# UserPromptSubmit hook: inject concise user-profile context for this turn.
# Auto-generated by okuro. Do not edit manually.
import sys, json, os

CONTEXT_PATH = os.path.expanduser("~/.okuro/profile-turn-context.txt")


def _exit_clean():
    sys.exit(0)


if not os.path.isfile(CONTEXT_PATH):
    _exit_clean()

try:
    with open(CONTEXT_PATH) as _f:
        context = _f.read().strip()
except Exception:
    _exit_clean()

if not context:
    _exit_clean()

try:
    json.load(sys.stdin)
except Exception:
    pass

print(json.dumps({
    "hookSpecificOutput": {
        "hookEventName": "UserPromptSubmit",
        "additionalContext": context,
    }
}))
sys.exit(0)
"""


# ALL THREE CLOSE-OUT CALLS, UNCONDITIONALLY. The reminder used to read
# "Also: write_memory() if you learned something, log_progress() if you did
# real work" — making two of the three conditional on the agent's own
# judgement, while the packet and every provider file call all three
# mandatory. An agent resolves that split toward the weaker statement, and
# this hook is the LAST thing it reads before ending.
SESSION_COMPLIANCE_HOOK = """\
#!/usr/bin/env python3
# Stop hook: session compliance reminder
# Auto-generated by okuro. Do not edit manually.
# Exit 0 always (advise, never block).
import sys, json, os, time

COOLDOWN = 1800  # 30 minutes between reminders
MIN_SESSION_AGE = 600  # 10 minutes before first reminder

# Check for session marker
markers = ['/tmp/.tm-session-claude-code', '/tmp/.okuro-session-claude-code']
marker_path = None
for m in markers:
    if os.path.isfile(m):
        marker_path = m
        break

if not marker_path:
    sys.exit(0)

try:
    with open(marker_path) as f:
        marker = json.load(f)
except Exception:
    sys.exit(0)

# Already reported? Done.
reported_markers = ['/tmp/.tm-session-reported-claude-code', '/tmp/.okuro-session-reported-claude-code']
if any(os.path.isfile(r) for r in reported_markers):
    sys.exit(0)

elapsed = time.time() - marker.get('ts', time.time())
if elapsed < MIN_SESSION_AGE:
    sys.exit(0)

marker_ts = str(marker.get('ts', '0')).replace('.', '_')
flag = f'/tmp/.okuro-compliance-reminded-{marker_ts}'
if os.path.isfile(flag):
    try:
        last_reminded = float(open(flag).read().strip())
        if time.time() - last_reminded < COOLDOWN:
            sys.exit(0)
    except Exception:
        pass

try:
    with open(flag, 'w') as f:
        f.write(str(time.time()))
except Exception:
    pass

print('Session close-out. Call all three before you end:\\n'
      '  write_memory() . log_progress() . session_report()',
      file=sys.stderr)
sys.exit(0)
"""


# Stop hook that enforces the active user profile's communication preferences.
# Reads ~/.okuro/profile-hook-rules.json (written at install time + on profile
# update). Inspects the last assistant message in the transcript and runs
# deterministic detectors. On a violation: appends a JSONL entry to the
# violations log (~/.okuro/profile-violations.jsonl) and exits clean (0).
# We never emit decision:block — forcing redrafts costs more output tokens
# than the original (already-emitted) violation. Logging gives full
# observability for tuning without doubling the API bill. Also exits
# clean on any error, missing rules file, or `stop_hook_active` flag.
PROFILE_COMPLIANCE_HOOK = r"""#!/usr/bin/env python3
# Stop hook: log user profile communication violations (no redraft).
# Auto-generated by okuro. Do not edit manually.
import sys, json, os, re, time

RULES_PATH = os.path.expanduser("~/.okuro/profile-hook-rules.json")
DEFAULT_LOG_PATH = os.path.expanduser("~/.okuro/profile-violations.jsonl")


def _exit_clean():
    sys.exit(0)


# Soft-fail when rules cache is missing — a stale install must never block.
if not os.path.isfile(RULES_PATH):
    _exit_clean()

try:
    with open(RULES_PATH) as _f:
        rules = json.load(_f)
except Exception:
    _exit_clean()

if not isinstance(rules, dict) or not rules.get("enabled", True):
    _exit_clean()

try:
    data = json.load(sys.stdin)
except Exception:
    _exit_clean()

# Loop guard: Claude Code sets stop_hook_active=True when re-firing after a
# block. We never block twice in a row — one nudge is enforcement, two is a
# trap.
if data.get("stop_hook_active"):
    _exit_clean()

transcript_path = data.get("transcript_path")
if not transcript_path or not os.path.isfile(transcript_path):
    _exit_clean()

last_text = None
try:
    with open(transcript_path) as _t:
        for _line in _t:
            _line = _line.strip()
            if not _line:
                continue
            try:
                rec = json.loads(_line)
            except Exception:
                continue
            if rec.get("type") != "assistant":
                continue
            msg = rec.get("message") or {}
            content = msg.get("content")
            if isinstance(content, list):
                parts = [
                    c.get("text", "")
                    for c in content
                    if isinstance(c, dict) and c.get("type") == "text"
                ]
                joined = "\n".join(p for p in parts if p)
                if joined.strip():
                    last_text = joined
            elif isinstance(content, str) and content.strip():
                last_text = content
except Exception:
    _exit_clean()

if not last_text:
    _exit_clean()

# Escape hatch: agent (or user, by quoting) can flag intentional verbose output.
if "[verbose-ok]" in last_text or "<no-compliance-check>" in last_text:
    _exit_clean()

violations = []

first_line = ""
for _line in last_text.splitlines():
    _line = _line.strip()
    if _line:
        first_line = _line
        break


# SHARED LIST-BLOCK CLASSIFIER — one definition, every detector.
#
# CLASS this fixes: a markdown block classifier that enumerates UNORDERED list
# markers and forgets ORDERED ones. "-*+" was recognised as list syntax; "1."
# and "1)" were not. Two detectors shared the assumption and failed in
# OPPOSITE directions from it:
#
#   items_per_level  under-fired — `^\s*[-*+]\s+\S` never matched "1. ", so a
#                    numbered list of any length never tripped the cap.
#   long_paragraph   over-fired — a block starting with a digit was treated as
#                    prose, and the sentence splitter then counted the markers
#                    themselves ("1. ", "2. ", "3. ") as sentence boundaries.
#                    A 3-item numbered list therefore opened at 3 phantom
#                    sentences against max_sentences=4.
#
# MEASURED 2026-09-08 over 30 days of ~/.okuro/profile-violations.jsonl (3,927
# turns, 16 long_paragraph hits): 8 of the 12 resolvable hits were ordered
# lists — 67% of the signal was the detector punishing the exact format the
# profile ASKS for. Same failure the retired bare-hex show_ids arm was retired
# for: do not penalise the desired output.
#
# Keep both marker sets here. A detector that needs "is this a list?" must ask
# this function, never re-derive a marker set of its own.
_LIST_LINE_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+\S")


def _is_list_line(line):
    # True when `line` opens a markdown list item, ordered or unordered.
    return bool(_LIST_LINE_RE.match(line))


def _is_list_block(para):
    # True when a paragraph's first non-empty line opens a list item.
    for _pl in para.splitlines():
        if _pl.strip():
            return _is_list_line(_pl)
    return False


# SHARED FENCE STRIPPER — one definition, same rule as _is_list_line above.
#
# A detector that needs "is this prose?" must not re-derive fence tracking of
# its own. long_paragraph already tracked fences; section_numbering scanned the
# RAW text, so a Python comment inside a code block --
#
#     ```python
#     # 1. claim the task
#     ```
#
# -- matches ^#{1,6}\\s*\\d+[.)]\\s exactly like a markdown heading does, and
# fired a violation on a code sample. That is the same defect twice retired in
# this file already: the bare-hex show_ids arm, and long_paragraph counting
# ordered-list markers as sentences. Do not penalise the desired output.
#
# Caught 2026-09-09 while measuring section_numbering for block mode. Blocking
# it un-fixed would have refused a turn over a code comment.
def _strip_fences(text):
    # Drop fenced code blocks. An unterminated fence swallows the rest, which
    # is the safe direction: it can only SUPPRESS a finding, never invent one.
    _out, _fence = [], False
    for _line in text.splitlines():
        if _line.lstrip().startswith("```"):
            _fence = not _fence
            continue
        if not _fence:
            _out.append(_line)
    return "\\n".join(_out)


_prose_text = _strip_fences(last_text)

# Detector: numbered section headings ("## 1. Foo", "### 2) Bar")
if rules.get("section_numbering") is False:
    if re.search(r"^#{1,6}\s*\d+[\.\)]\s", _prose_text, re.M):
        violations.append(
            "section_numbering=false but heading like '## 1.' / '### 2)' detected"
        )

# Detector: IDs in body (UUIDs, short hex hashes wrapped in backticks)
if rules.get("show_ids") is False:
    if re.search(
        r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b",
        last_text,
    ):
        violations.append("show_ids=false but UUID present in body")
    # The bare-hex arm was RETIRED 2026-07-19. It fired on 24.8% of all
    # assistant messages (90 of 363 sampled) and every single match was a
    # commit hash, artifact id or handover id doing referential work — the
    # exact identifiers TOOL-PROTOCOL mandates creating and the Working Rules
    # mandate committing. okuro required the id, then logged a violation for
    # reporting it, so compliance made the output strictly worse: the user
    # could no longer locate their own commit or artifact.
    #
    # The regex cannot distinguish USE from MENTION and never could: `a5d6f50a`
    # in "committed a5d6f50a" is the payload, not clutter. It also caught bare
    # decimals — an epoch, a byte count, a max_tokens value.
    #
    # The UUID arm above is kept. That IS the stated pet peeve: a 36-char
    # opaque identifier inlined in prose is clutter with no referential value
    # to a human reader, and okuro's own renderers shorten them to 8 chars.
    #
    # If this is ever reinstated, it must gate on the id being unexplained —
    # not on its mere presence. Do not reintroduce a rule that penalises
    # citing evidence.
    pass

# Detector: contiguous list block exceeds items_per_level
ipl = rules.get("items_per_level")
if isinstance(ipl, int) and ipl > 0:
    run = 0
    max_run = 0
    for _line in last_text.splitlines():
        if _is_list_line(_line):
            run += 1
            if run > max_run:
                max_run = run
        else:
            run = 0
    if max_run > ipl:
        violations.append(
            "items_per_level=" + str(ipl) + " but list block has " + str(max_run) + " items"
        )

# Detector: long prose paragraphs (when profile prefers tables/bullets)
if rules.get("avoid_long_paragraphs"):
    max_sentences = int(rules.get("max_sentences_per_paragraph", 4))
    min_chars = int(rules.get("long_paragraph_min_chars", 400))
    paragraphs = re.split(r"\n\s*\n", last_text)
    # PROSE IS A PROPERTY OF A LINE, NOT OF A PARAGRAPH'S FIRST CHARACTER.
    #
    # The old classifier read `ps[0]` and decided the whole block. That is
    # wrong in both directions, because one blank-line-delimited paragraph
    # routinely holds several block types:
    #
    #   "**What should fire:**\n```\n…code…\n```"   label + fence, one paragraph
    #   "**The point:**\n- a\n- b"                   bold lead-in + list
    #
    # First-char classification either skipped the whole thing on a `*` it
    # did not understand (missing real prose) or, once `*` was removed from
    # the set, counted an entire fenced block as prose. Both were observed.
    #
    # So: walk lines, drop every non-prose line, count what is left. Fence
    # state is tracked across the WHOLE text because a paragraph can open a
    # fence its successor closes.
    _fence = False
    for _p in paragraphs:
        _kept = []
        _in_list = False
        for _l in _p.splitlines():
            _s = _l.strip()
            if _s.startswith("```"):
                _fence = not _fence
                _in_list = False
                continue
            if _fence or not _s:
                continue
            if _is_list_line(_l):
                _in_list = True
                continue
            # An indented line under a list item is that item's continuation,
            # not a new prose sentence.
            if _in_list and _l[:1] in " \t":
                continue
            if _s[0] in "|#>":
                continue
            _in_list = False
            _kept.append(_s)
        ps = " ".join(_kept).strip()
        if not ps:
            continue
        sentence_count = len(re.findall(r"[.!?](?:\s|$)", ps))
        if sentence_count > max_sentences and len(ps) >= min_chars:
            violations.append(
                "long_paragraph: " + str(sentence_count) +
                "-sentence prose paragraph (" + str(len(ps)) + " chars; profile prefers tables/bullets)"
            )
            break

# Detector: profile says lead with answer/no filler, but response opens with filler.
if rules.get("avoid_preamble"):
    if re.match(
        r"(?i)^(sure|certainly|absolutely|of course|happy to|i['’]?d be happy to|"
        r"i can help|let me|great question|good question)\b",
        first_line,
    ):
        violations.append(
            "avoid_preamble=true but response starts with filler/preamble"
        )

# Detector: profile dislikes restating the question/request as the opening.
if rules.get("avoid_question_restatement"):
    if re.match(
        r"(?i)^(you asked|you want(?:ed)?|your request|the request|the question)\b",
        first_line,
    ):
        violations.append(
            "avoid_question_restatement=true but response opens by restating the request"
        )

# Detector: REPLY VOLUME — log-only (2026-07-29, WP5/I12).
#
# The gap this closes: every one of the seven replies the user identified as
# failures fired ZERO detectors. They were not preamble, not long single
# paragraphs, not ID clutter — they were simply too much text, 2,032-3,560
# prose chars each. A stated preference with no instrument cannot be measured,
# improved, or argued with.
#
# PROSE CHARS, not total: fenced code and table rows are the formats the
# profile ASKS for, so counting them would penalise the desired shape. A
# 3,000-char reply that is mostly a table is a good reply.
#
# 2,000 is PROVISIONAL, derived from the labelled session's floor (2,032) and
# nothing else. LOG ONLY — a threshold from one labelled session has no kill
# criteria yet, and reply_volume is deliberately absent from
# blockable_detectors() so no env flip can turn it into a refusal by accident.
_rv_min = int(rules.get("reply_volume_min_chars", 0) or 0)
if _rv_min > 0:
    _prose = 0
    _in_fence = False
    for _line in last_text.splitlines():
        _s = _line.strip()
        if _s.startswith("```"):
            _in_fence = not _in_fence
            continue
        if _in_fence or _s.startswith("|"):
            continue
        _prose += len(_s)
    if _prose >= _rv_min:
        violations.append(
            "reply_volume: " + str(_prose) + " prose chars (excl. code fences "
            "and table rows; log-only threshold " + str(_rv_min) + ")"
        )

# ALWAYS log, violation or not — the record IS the denominator.
#
# This used to exit before logging when a turn was clean, so the log held only
# turns that failed. Every rate computed from it was therefore
# violations-per-violating-turn, i.e. 100% by construction, and the
# failure-modes block could only ever print bare counts. "13" reads as a
# crisis at 30 turns and as noise at 3,000.
log_path = rules.get("violations_log_path") or DEFAULT_LOG_PATH
try:
    log_dir = os.path.dirname(log_path)
    if log_dir:
        os.makedirs(log_dir, exist_ok=True)
    record = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "provider": "claude",
        "transcript_path": transcript_path,
        "first_line": first_line[:200],
        "violations": violations,
    }
    with open(log_path, "a") as _lf:
        _lf.write(json.dumps(record) + "\n")
except Exception:
    pass  # logging is best-effort — never break the user's session

# Mode gate. Global "log" exits clean — the original-violation tokens are
# already paid for and a redraft just doubles cost. Two ways to enforce:
#   mode="block"       -> every violation blocks (opt-in, whole-profile)
#   block_detectors[]  -> only the NAMED detectors block, while the rest keep
#                         logging. The default is DERIVED from
#                         _profile_compliance._DETECTOR_VALIDITY (the two
#                         mechanical checks), where the violation is a fact
#                         about the text rather than a judgement about its
#                         content and cannot be satisfied by rewording.
# Only the blocking violations go in the reason: feeding back a log-only
# violation would ask for a redraft nobody decided to enforce.
# The stop_hook_active loop guard above caps this at one redraft per turn.
_block_keys = rules.get("block_detectors") or []
_blocking = [
    v for v in violations
    if any(str(v).startswith(str(k)) for k in _block_keys)
]
if str(rules.get("mode", "log")).lower() == "block":
    _blocking = violations

if _blocking:
    reason = (
        "Profile-compliance violation(s): "
        + "; ".join(_blocking)
        + ". Re-emit the previous response correcting these issues."
    )
    print(json.dumps({"decision": "block", "reason": reason}))
    sys.exit(0)

_exit_clean()
"""


# --------------------------------------------------------------------------
# PreCompact hook — fires before context compression. Writes a marker file
# the next UserPromptSubmit hook will pick up to inject a "save NOW" directive
# (mempalace-inspired). The PreCompact hook itself can't reach the model, so
# this two-step pattern is the only way to make the agent act after compaction
# without losing the current turn.
#
# Marker location: /tmp/.okuro-compaction-pending-<session_id>
# Auto-cleans on /tmp wipe; UserPromptSubmit hook deletes after consuming.
# --------------------------------------------------------------------------
PRECOMPACT_SAVE_HOOK = r"""#!/usr/bin/env python3
# PreCompact hook: write a compaction-pending marker for next-turn save directive.
# Auto-generated by okuro. Do not edit manually.
# Exit 0 always (advisory; never block compaction).
import sys, json, os, re, time

MARKER_DIR = "/tmp"
MARKER_PREFIX = ".okuro-compaction-pending-"

try:
    data = json.load(sys.stdin)
except Exception:
    sys.exit(0)

session_id = data.get("session_id", "")
trigger = data.get("trigger", "auto")  # "manual" | "auto"

# Sanitize session_id — only alphanumerics, underscore, hyphen.
if not isinstance(session_id, str):
    sys.exit(0)
session_id = re.sub(r"[^a-zA-Z0-9_-]", "", session_id)[:64]
if not session_id:
    sys.exit(0)

marker_path = os.path.join(MARKER_DIR, MARKER_PREFIX + session_id)
try:
    with open(marker_path, "w") as f:
        json.dump({
            "ts": time.time(),
            "trigger": trigger,
            "session_id": session_id,
        }, f)
except OSError:
    pass  # best-effort — never block compaction

sys.exit(0)
"""


# --------------------------------------------------------------------------
# UserPromptSubmit hook — checks for a compaction-pending marker for this
# session_id. When present, injects an explicit save-now directive via
# additionalContext so the model writes memory/progress/session_report
# before resuming work on a context-compressed transcript.
# --------------------------------------------------------------------------
# The emitted directive no longer says "This is a one-shot directive — it will
# not fire again until the next compaction event." That sentence describes the
# HOOK's firing policy to whoever maintains it, and it tells the agent the
# instruction is cheap to ignore because nothing will repeat it. "session_report
# only if the session is winding down" became "if this session is ending" —
# same condition, without inviting the agent to grade its own momentum.
USER_PROMPT_COMPACTION_HOOK = r"""#!/usr/bin/env python3
# UserPromptSubmit hook: surface compaction-save directive after context compression.
# Auto-generated by okuro. Do not edit manually.
import sys, json, os, re

MARKER_DIR = "/tmp"
MARKER_PREFIX = ".okuro-compaction-pending-"

try:
    data = json.load(sys.stdin)
except Exception:
    sys.exit(0)

session_id = data.get("session_id", "")
if not isinstance(session_id, str):
    sys.exit(0)
session_id = re.sub(r"[^a-zA-Z0-9_-]", "", session_id)[:64]
if not session_id:
    sys.exit(0)

marker_path = os.path.join(MARKER_DIR, MARKER_PREFIX + session_id)
if not os.path.isfile(marker_path):
    sys.exit(0)

# Consume the marker so we inject the directive exactly once per compaction.
try:
    os.unlink(marker_path)
except OSError:
    pass

directive = (
    "[okuro·compaction] Your context was just compacted — earlier transcript "
    "detail is now lossy.\n"
    "BEFORE answering this turn:\n"
    "1. write_memory(topic=..., content=...) for any non-obvious learnings.\n"
    "2. log_progress(project=..., status=..., summary=...) if you did "
    "substantive work since last save.\n"
    "3. session_report(...) if this session is ending.\n"
    "Then proceed with the user's prompt."
)

print(json.dumps({
    "hookSpecificOutput": {
        "hookEventName": "UserPromptSubmit",
        "additionalContext": directive,
    }
}))
sys.exit(0)
"""


# Stop hook: codebase-intelligence bypass detector. Unlike the other hooks
# (self-contained by design), this one delegates to the importable, unit-tested
# provider-agnostic core in okuro.sense.providers._codebase_intel — so the
# detection logic is shared with any future provider path, not duplicated. The
# shebang is baked to the okuro interpreter (__OKURO_PYTHON__ → sys.executable
# at install) so `import okuro` always resolves; if it somehow can't, the hook
# no-ops. Log-only, never blocks.
CODEBASE_INTEL_HOOK = """#!__OKURO_PYTHON__
# Stop hook: detect codebase-intelligence bypass (search / code-absence claim
# without cortex_scope). Auto-generated by okuro. Do not edit manually.
import sys, json
try:
    data = json.load(sys.stdin)
except Exception:
    sys.exit(0)
if data.get("stop_hook_active"):
    sys.exit(0)
try:
    from okuro.sense.providers._codebase_intel import run_stop_hook
    run_stop_hook(data)
except Exception:
    pass
sys.exit(0)
"""


@register
class ClaudeAdapter(ProviderAdapter):
    name = "claude"

    def detect(self) -> bool:
        return shutil.which("claude") is not None

    def generate_instructions(self) -> list[str]:
        from ..bootstrap.sections import build_behavioral_section
        from .template import build_instructions
        generated = []

        # User-level ~/.claude/CLAUDE.md (auto-loaded by Claude Code for every session)
        behavioral = build_behavioral_section()
        claude_dir = os.path.expanduser("~/.claude")
        os.makedirs(claude_dir, exist_ok=True)
        claude_md_path = os.path.join(claude_dir, "CLAUDE.md")
        with open(claude_md_path, "w") as f:
            f.write(build_instructions("claude-code", behavioral))
        generated.append(claude_md_path)

        # System-tier Output Style — the highest-weight channel. Claude Code
        # APPENDS an output style's body to the SYSTEM prompt, above the
        # user-message tier where CLAUDE.md and the per-turn hook land. This
        # is generated from the SAME profile-driven directive as the hook
        # payload, and activated merge-safely in settings.json. Lives in
        # generate_instructions() (not install_hooks) so the settings-edit
        # regen path — which calls only generate_instructions — keeps it in
        # sync for existing users too.
        generated.extend(self._write_output_style(claude_dir))

        # A1: enforce single memory store. Claude Code's per-project
        # auto-memory folder has historically accumulated stale .md files
        # with references to dead MCPs (tm-launcher, tm-cortex, ...).
        # Archive any non-stub file and (re)write the MEMORY.md pointer.
        generated.extend(self._sync_project_memory(claude_dir))

        # D2b: AGENT-CONTEXT.md is deprecated — bootstrap() is the live
        # context. Delete any copies we can reach.
        generated.extend(self._wipe_agent_context_files())

        return generated

    # -----------------------------------------------------------------
    # System-tier Output Style — highest instruction weight
    # -----------------------------------------------------------------

    # The style name is what settings.json `outputStyle` references and what
    # Claude Code shows in the `/config` picker. Keep it stable — renaming it
    # orphans the activation. Filename == name-without-extension by Claude
    # Code's own default, but we set the frontmatter `name` explicitly.
    _OUTPUT_STYLE_NAME = "Okuro Communication"
    _OUTPUT_STYLE_FILENAME = "okuro-communication.md"

    def _write_output_style(self, claude_dir: str) -> list[str]:
        """Write ~/.claude/output-styles/okuro-communication.md and activate it.

        The body is the shared profile-driven communication directive — the
        same builder that feeds the per-turn hook — so the system-tier and
        context-tier channels can never drift. Activation sets settings.json
        `outputStyle` to the style name, merge-safely (only that one key).

        Best-effort: a failure to write or activate is logged and skipped so
        it never blocks CLAUDE.md / hook generation.
        """
        from ._profile_compliance import build_comm_directive

        touched: list[str] = []
        try:
            styles_dir = os.path.join(claude_dir, "output-styles")
            os.makedirs(styles_dir, exist_ok=True)
            directive = build_comm_directive()
            style_path = os.path.join(styles_dir, self._OUTPUT_STYLE_FILENAME)
            with open(style_path, "w") as f:
                f.write(self._render_output_style(directive))
            touched.append(style_path)
        except OSError as e:
            log.warning("output-style file write failed: %s", e)
            return touched

        settings_path = os.path.join(claude_dir, "settings.json")
        try:
            if self._activate_output_style(settings_path, self._OUTPUT_STYLE_NAME):
                touched.append(settings_path)
        except SettingsCorruptError as e:
            # A corrupt settings.json must not nuke instruction regen. The
            # style file is already written; activation resumes once the user
            # repairs the file (a .corrupt-<ts> copy was dropped alongside).
            log.warning("output-style activation skipped — %s", e)
        except Exception as e:  # noqa: BLE001
            log.warning("output-style activation failed: %s", e)
        return touched

    @classmethod
    def _render_output_style(cls, directive: str) -> str:
        """Render the output-style markdown: YAML frontmatter + directive body.

        ``keep-coding-instructions: true`` is REQUIRED. Without it, activating
        an output style makes Claude Code STRIP its built-in software-
        engineering instructions (scoping, verification, comment discipline) —
        which would cripple code work. We only add a communication layer, so
        the coding instructions must stay.
        """
        body = directive.strip() if directive and directive.strip() else (
            "Lead with the answer. Prefer tables and bullet lists over prose. "
            "Keep replies concise."
        )
        # THIS FILE IS APPENDED TO THE SYSTEM PROMPT — the highest-weight
        # channel okuro owns. It carried, in its first body line, an install
        # instruction addressed to the owner: "Do not edit manually — edits are
        # overwritten on the next profile change or `okuro install`." The model
        # cannot edit the file and cannot run the installer; the sentence did
        # nothing except spend system-prompt weight telling the agent about
        # okuro's build process. Verified first-person: it was readable in a
        # live session's own system prompt.
        #
        # Frontmatter is the ONE place a generated-file note is defensible —
        # it is metadata a human sees when opening the file — but the
        # description string is also rendered in Claude Code's style picker,
        # so it says what the style DOES and nothing about how it was made.
        return (
            "---\n"
            f"name: {cls._OUTPUT_STYLE_NAME}\n"
            "description: Okuro communication protocol — answer-first, "
            "tables/bullets, concise.\n"
            "keep-coding-instructions: true\n"
            "---\n"
            "\n"
            + body
            + "\n"
        )

    def _activate_output_style(self, settings_path: str, style_name: str) -> bool:
        """Merge-safely set ``outputStyle`` in settings.json. Returns True if written.

        Touches ONLY the ``outputStyle`` key — every other key (hooks, user
        config) is preserved. Idempotent: if already set to ``style_name`` we
        skip the rewrite so a burst of regens doesn't churn the file. Shares
        the same advisory lock as the hook merge so the two settings writers
        never race.
        """
        claude_dir = os.path.dirname(settings_path)
        os.makedirs(claude_dir, exist_ok=True)
        lock_path = os.path.join(claude_dir, ".settings.json.okuro.lock")
        with self._settings_lock(lock_path):
            existing = self._read_existing_settings(settings_path)
            if existing.get("outputStyle") == style_name:
                return False
            new_settings = dict(existing)
            new_settings["outputStyle"] = style_name
            self._atomic_write_json(settings_path, new_settings)
        return True

    # -----------------------------------------------------------------
    # A1 — project-memory folder as a pointer, not a store
    # -----------------------------------------------------------------

    def _sync_project_memory(self, claude_dir: str) -> list[str]:
        """Archive legacy .md files under `~/.claude/projects/*/memory/` and
        (re)write the canonical MEMORY.md stub.

        Idempotent: on a fresh install, the project dirs simply don't exist
        and we write nothing. On a system with legacy content, we archive
        all non-stub files under `~/.okuro/archive/claude-memory/<slug>/`
        with per-file collision resolution.
        """
        touched: list[str] = []
        projects_dir = Path(claude_dir) / "projects"
        if not projects_dir.is_dir():
            return touched

        home = Path(os.path.expanduser("~"))
        archive_root = home / ".okuro" / "archive" / "claude-memory"
        timestamp = time.strftime("%Y%m%dT%H%M%S")

        for project_dir in sorted(projects_dir.iterdir()):
            memory_dir = project_dir / "memory"
            if not memory_dir.is_dir():
                continue

            slug = project_dir.name
            for entry in sorted(memory_dir.iterdir()):
                if not entry.is_file():
                    continue
                if entry.name == "MEMORY.md":
                    # The stub is re-written below; legacy MEMORY.md content
                    # is archived too.
                    if entry.read_text() == _CLAUDE_MEMORY_STUB:
                        continue

                dst_dir = archive_root / slug
                dst_dir.mkdir(parents=True, exist_ok=True)
                dst = dst_dir / entry.name
                if dst.exists():
                    dst = dst_dir / f"{entry.stem}.{timestamp}{entry.suffix}"
                shutil.move(str(entry), str(dst))
                touched.append(str(dst))

            # (Re)write the canonical stub.
            stub_path = memory_dir / "MEMORY.md"
            stub_path.write_text(_CLAUDE_MEMORY_STUB)
            touched.append(str(stub_path))

        return touched

    # -----------------------------------------------------------------
    # D2b — wipe AGENT-CONTEXT.md
    # -----------------------------------------------------------------

    # Repo-relative context files to wipe; personal workspaces add theirs
    # via the ``providers.agent_context_paths`` convention.
    @staticmethod
    def _agent_context_paths() -> tuple[str, ...]:
        from okuro.yu.conventions import get_convention

        extra = get_convention("providers.agent_context_paths", []) or []
        return ("AGENT-CONTEXT.md", *[str(p) for p in extra])

    _AGENT_CONTEXT_PATHS = _agent_context_paths()

    def _wipe_agent_context_files(self) -> list[str]:
        touched: list[str] = []
        home = Path(os.path.expanduser("~"))
        for rel in self._AGENT_CONTEXT_PATHS:
            p = home / rel
            if p.is_file():
                p.unlink()
                touched.append(f"deleted:{p}")
        return touched

    def gate_bodies(self) -> dict[str, str]:
        """Rendered from the SAME table install_hooks() writes from.

        Not a second list. A hook added to `_OKURO_HOOK_SCRIPTS` is measured
        for rule coverage on the next test run without touching this method.
        """
        bodies: dict[str, str] = {}
        for name, build in _OKURO_HOOK_SCRIPTS:
            try:
                bodies[name] = build()
            except Exception:  # a body that cannot render enforces nothing
                bodies[name] = ""
        return bodies

    def enforcement_capability(self) -> dict:
        return {
            "provider": self.name,
            "blocking_pre_tool": True,
            "shell_tool": "Bash",
            "command_field": "tool_input.command",
            "deny_contract": "exit 2, message on stderr",
            # Not a claim from documentation: check-grep and check-system are
            # installed here, and tests/hooks exercises their exit codes
            # against real payloads.
            "verified": "2026-07-27 — gates shipped and exercised in tests/hooks",
            "degradation": None,
        }

    def install_hooks(self) -> list[str]:
        generated = []
        hooks_dir = os.path.expanduser("~/.claude/hooks")
        os.makedirs(hooks_dir, exist_ok=True)

        # 1. Write every declared hook script — one loop over ONE declaration.
        #    `_OKURO_HOOK_SCRIPTS` is also what `_OKURO_HOOK_FILENAMES` is
        #    derived from, so the settings.json sweep and the project-override
        #    purge cannot fall behind this installer the way they did for
        #    check-system.py and check-store.py. Per-hook rationale lives with
        #    the declaration; do not re-inline it here.
        #
        #    An empty body means the hook compiled to nothing (check-store.py
        #    when the tool_routing contract yields no store guard). We skip the
        #    write but still keep its path, so the matcher registered below is
        #    unchanged from before this loop existed.
        hook_paths: dict[str, str] = {}
        for hook_filename, build_body in _OKURO_HOOK_SCRIPTS:
            path = os.path.join(hooks_dir, hook_filename)
            hook_paths[hook_filename] = path
            body = build_body()
            if not body:
                continue
            with open(path, "w") as f:
                f.write(body)
            os.chmod(path, 0o755)
            generated.append(path)

        grep_hook_path = hook_paths["check-grep.py"]
        system_hook_path = hook_paths["check-system.py"]
        store_hook_path = hook_paths["check-store.py"]
        agent_gate_path = hook_paths["check-agent-bootstrap.py"]
        read_hook_path = hook_paths["check-read.py"]
        compliance_hook_path = hook_paths["session-compliance.py"]
        user_prompt_hook_path = hook_paths["user-prompt-profile.py"]
        profile_hook_path = hook_paths["profile-compliance.py"]
        codebase_intel_hook_path = hook_paths["codebase-intel.py"]
        precompact_hook_path = hook_paths["precompact-save.py"]
        user_prompt_compaction_path = hook_paths["user-prompt-compaction.py"]

        # 6. Refresh profile caches from the active profile so the hooks have
        #    something to enforce. Hook no-ops when this file is missing, so
        #    a write failure here degrades gracefully.
        try:
            rules_path = self._write_profile_hook_rules()
            generated.append(rules_path)
        except Exception as e:
            log.warning("profile-hook-rules.json write failed: %s", e)

        try:
            turn_context_path = self._write_profile_turn_context()
            generated.append(turn_context_path)
        except Exception as e:
            log.warning("profile-turn-context.txt write failed: %s", e)

        # 7. Merge okuro hook entries into ~/.claude/settings.json. The merge
        #    is locked (fcntl.flock on a sibling lockfile), atomic (write to
        #    temp + os.replace), and preserves any user-authored matchers
        #    under the events we touch. See `_merge_settings_hooks`.
        settings_path = os.path.expanduser("~/.claude/settings.json")
        okuro_entries = self._build_okuro_hook_entries(
            grep_hook_path, system_hook_path, store_hook_path, read_hook_path,
            agent_gate_path,
            compliance_hook_path,
            user_prompt_hook_path, profile_hook_path,
            precompact_hook_path, user_prompt_compaction_path,
            codebase_intel_hook_path,
        )
        self._merge_settings_hooks(settings_path, okuro_entries)
        generated.append(settings_path)

        # Heal registered projects that still carry legacy in-repo hook
        # wiring (.claude/settings.json + .claude/hooks/*.py). Without this,
        # project-level overrides double-execute with the canonical user-level
        # hooks and drift as okuro updates its hook logic.
        generated.extend(self._purge_stale_project_overrides())

        return generated

    # -----------------------------------------------------------------
    # ~/.claude/settings.json merge — atomic, locked, non-destructive
    # -----------------------------------------------------------------

    @staticmethod
    def _okuro_marker(name: str) -> dict:
        return {"managed_by": "okuro", "okuro_hook": name}

    @staticmethod
    def _is_okuro_managed(entry: dict) -> bool:
        """True if this matcher OR inner-hook entry carries the okuro marker."""
        if not isinstance(entry, dict):
            return False
        if entry.get(_OKURO_MARKER_KEY) == _OKURO_MARKER_VALUE:
            return True
        meta = entry.get("meta")
        if isinstance(meta, dict) and meta.get("managed_by") == "okuro":
            return True
        return False

    def _build_okuro_hook_entries(
        self,
        grep_hook_path: str,
        system_hook_path: str,
        store_hook_path: str,
        read_hook_path: str,
        agent_gate_path: str,
        compliance_hook_path: str,
        user_prompt_hook_path: str,
        profile_hook_path: str,
        precompact_hook_path: str,
        user_prompt_compaction_path: str,
        codebase_intel_hook_path: str,
    ) -> dict[str, list[dict]]:
        """Return the okuro-managed matcher entries grouped by event name.

        Each matcher and each inner hook carries an `_okuro: "managed"` tag
        and a `meta.okuro_hook` name so we can identify ours unambiguously
        even if a user later adds their own matcher with the same name.
        """
        def matcher(matcher_name: str, hook_name: str, command: str) -> dict:
            return {
                "matcher": matcher_name,
                _OKURO_MARKER_KEY: _OKURO_MARKER_VALUE,
                "meta": self._okuro_marker(hook_name),
                "hooks": [
                    {
                        "type": "command",
                        "command": os.path.abspath(command),
                        _OKURO_MARKER_KEY: _OKURO_MARKER_VALUE,
                        "meta": self._okuro_marker(hook_name),
                    }
                ],
            }

        return {
            "PreToolUse": [
                matcher("Grep", "check-grep", grep_hook_path),
                # Same hook, second surface. The capability "hunt for code" is
                # reachable through Bash (`grep -r`, `find -name`), and binding
                # only to `Grep` moved the traffic there rather than stopping
                # it. Enforcement must name the ACTION; a matcher names a tool,
                # so every tool that reaches the action needs its own entry.
                matcher("Bash", "check-grep", grep_hook_path),
                # Third gate on the same Bash surface. Separate hook because
                # its detection is command-identity, not path-coverage — see
                # compile_system_state_routes() on why they are not merged.
                matcher("Bash", "check-system", system_hook_path),
                # Fourth gate on Bash. Resource-matched, not command-matched:
                # it is the only one that can see the interpreter path.
                matcher("Bash", "check-store", store_hook_path),
                matcher("Read", "check-read", read_hook_path),
                # Matched on the okuro tool surface itself, not on a native
                # tool: this gate's whole job is to stand in front of okuro's
                # MCP tools for a caller the MCP server cannot distinguish.
                # Verified 2026-07-28 that PreToolUse matchers do fire on
                # mcp__okuro__* names.
                matcher("mcp__okuro__.*", "check-agent-bootstrap", agent_gate_path),
            ],
            "Stop": [
                matcher("", "session-compliance", compliance_hook_path),
                matcher("", "profile-compliance", profile_hook_path),
                matcher("", "codebase-intel", codebase_intel_hook_path),
            ],
            "UserPromptSubmit": [
                matcher("", "user-prompt-profile", user_prompt_hook_path),
                matcher("", "user-prompt-compaction", user_prompt_compaction_path),
            ],
            "PreCompact": [
                matcher("", "precompact-save", precompact_hook_path),
            ],
        }

    @staticmethod
    def _strip_okuro_from_event(matchers: list) -> list:
        """Remove okuro-tagged matchers/hooks from one event's matcher list.

        - Drop matchers that carry the okuro marker outright.
        - For other matchers, drop only the inner hooks that carry the marker
          OR whose ``command`` basename is in ``_OKURO_HOOK_FILENAMES``;
          if all inner hooks were ours, drop the matcher entirely.
        - Non-dict entries are preserved as-is (we don't second-guess shape).

        Why the basename sweep: pre-marker installs (before commit fc28ddb)
        wrote untagged matcher entries whose inner hook command pointed at
        ``~/.claude/hooks/<our-script>.py``. Those entries had no
        ``_okuro: "managed"`` tag and were therefore preserved by every
        subsequent merge — leading to duplicate matchers (untagged stale +
        tagged current) firing the same script twice on every event. The
        basename sweep retires those legacy duplicates without touching
        any user-authored hook (user hooks point at user-owned scripts).
        """
        def _ours(h) -> bool:
            if not isinstance(h, dict):
                return False
            if ClaudeAdapter._is_okuro_managed(h):
                return True
            return ClaudeAdapter._is_okuro_hook_command(h.get("command", ""))

        kept: list = []
        for matcher in matchers:
            if not isinstance(matcher, dict):
                kept.append(matcher)
                continue
            if ClaudeAdapter._is_okuro_managed(matcher):
                continue
            inner = matcher.get("hooks")
            if not isinstance(inner, list):
                kept.append(matcher)
                continue
            # If every inner hook points at one of our scripts, the whole
            # matcher is ours by transitive ownership — drop it even if the
            # matcher itself is untagged. This catches pre-marker installs
            # where the matcher wrapper never received the okuro tag.
            if inner and all(_ours(h) for h in inner):
                continue
            cleaned_inner = [h for h in inner if not _ours(h)]
            if not cleaned_inner and len(inner) > 0:
                # All inner hooks were okuro's — drop the whole matcher.
                continue
            new_matcher = dict(matcher)
            new_matcher["hooks"] = cleaned_inner
            kept.append(new_matcher)
        return kept

    @classmethod
    def _has_user_authored_hooks(cls, hooks_block) -> bool:
        """True if any hook entry in the block lacks the okuro marker."""
        if not isinstance(hooks_block, dict):
            return False
        for matchers in hooks_block.values():
            if not isinstance(matchers, list):
                continue
            for matcher in matchers:
                if not isinstance(matcher, dict):
                    continue
                if cls._is_okuro_managed(matcher):
                    continue
                inner = matcher.get("hooks")
                if isinstance(inner, list):
                    for h in inner:
                        if isinstance(h, dict) and not cls._is_okuro_managed(h):
                            return True
                else:
                    # An odd-shape matcher we don't own — treat as user-authored.
                    return True
        return False

    def _merge_settings_hooks(
        self,
        settings_path: str,
        okuro_entries: dict[str, list[dict]],
    ) -> None:
        """Read existing settings.json, drop okuro-tagged entries from the
        events we manage, re-insert current okuro entries, write atomically
        under an advisory file lock.

        On JSON parse failure: a `.corrupt-<ts>` snapshot of the unreadable
        file is created and `SettingsCorruptError` is raised. We do NOT
        overwrite — better to fail loudly than silently destroy user config.

        On first observed divergence (existing settings.json contains
        non-okuro entries under any event we touch AND no `.okuro.bak`
        snapshot exists yet), a `.okuro.bak` of the original file is
        written for one-shot recovery.
        """
        claude_dir = os.path.dirname(settings_path)
        os.makedirs(claude_dir, exist_ok=True)

        lock_path = os.path.join(claude_dir, ".settings.json.okuro.lock")
        with self._settings_lock(lock_path):
            existing = self._read_existing_settings(settings_path)

            hooks_block = existing.get("hooks") if isinstance(existing, dict) else None
            if not isinstance(hooks_block, dict):
                hooks_block = {}

            # One-shot backup the first time we encounter a non-okuro hook
            # under any event — gives the user a snapshot of pre-okuro state.
            backup_path = settings_path + ".okuro.bak"
            if (
                os.path.isfile(settings_path)
                and not os.path.isfile(backup_path)
                and self._has_user_authored_hooks(hooks_block)
            ):
                try:
                    shutil.copy2(settings_path, backup_path)
                except OSError as e:
                    log.warning("settings.json backup to %s failed: %s", backup_path, e)

            new_hooks_block = dict(hooks_block)
            for event, okuro_matchers in okuro_entries.items():
                current = new_hooks_block.get(event)
                if isinstance(current, list):
                    cleaned = self._strip_okuro_from_event(current)
                else:
                    cleaned = []
                new_hooks_block[event] = cleaned + list(okuro_matchers)

            new_settings = dict(existing) if isinstance(existing, dict) else {}
            new_settings["hooks"] = new_hooks_block

            self._atomic_write_json(settings_path, new_settings)

    def _read_existing_settings(self, settings_path: str) -> dict:
        """Return parsed settings.json, or `{}` if the file is absent.

        Raises `SettingsCorruptError` (after dropping a `.corrupt-<ts>` copy
        of the unparseable file) if the file exists but cannot be parsed.
        """
        if not os.path.isfile(settings_path):
            return {}
        try:
            with open(settings_path) as f:
                data = json.load(f)
        except json.JSONDecodeError as e:
            ts = time.strftime("%Y%m%dT%H%M%S")
            corrupt_copy = f"{settings_path}.corrupt-{ts}"
            try:
                shutil.copy2(settings_path, corrupt_copy)
            except OSError as copy_err:
                log.error(
                    "settings.json corrupt and copy-out failed: %s; original at %s",
                    copy_err, settings_path,
                )
                # Re-raise with what we have — corrupt_copy may not exist.
                raise SettingsCorruptError(settings_path, corrupt_copy, e) from e
            log.error(
                "settings.json corrupt; refusing to overwrite. "
                "Original copied to %s. Parse error: %s",
                corrupt_copy, e,
            )
            raise SettingsCorruptError(settings_path, corrupt_copy, e) from e
        except OSError as e:
            log.warning("settings.json unreadable (%s); treating as empty", e)
            return {}
        if not isinstance(data, dict):
            log.warning(
                "settings.json root is %s, not dict; treating as empty",
                type(data).__name__,
            )
            return {}
        return data

    @staticmethod
    def _atomic_write_json(target_path: str, payload: dict) -> None:
        """Write `payload` as JSON to `target_path` atomically.

        Strategy: write to `<target>.tmp.<pid>.<ts>` in the same directory,
        then `os.replace()` onto the target. `os.replace` is atomic on POSIX
        and on Windows when source and target sit on the same volume — so
        concurrent readers either see the old file or the new one, never a
        truncated mid-write.
        """
        directory = os.path.dirname(target_path) or "."
        ts = time.strftime("%Y%m%dT%H%M%S")
        tmp_path = os.path.join(
            directory,
            f"{os.path.basename(target_path)}.tmp.{os.getpid()}.{ts}",
        )
        try:
            with open(tmp_path, "w") as f:
                json.dump(payload, f, indent=2)
                f.write("\n")
                f.flush()
                try:
                    os.fsync(f.fileno())
                except OSError:
                    pass  # fsync optional — atomicity comes from rename
            os.replace(tmp_path, target_path)
        except Exception:
            # Best-effort tmp cleanup on failure — never leave a stray file.
            try:
                if os.path.exists(tmp_path):
                    os.unlink(tmp_path)
            except OSError:
                pass
            raise

    # -----------------------------------------------------------------
    # Profile-compliance hook rules cache
    # -----------------------------------------------------------------
    # Helpers live in `_profile_compliance` so gemini's AfterAgent hook
    # can refresh the same cache. See that module for the extraction +
    # write logic.

    @staticmethod
    def _write_profile_hook_rules() -> str:
        from ._profile_compliance import write_profile_hook_rules
        return write_profile_hook_rules()

    @staticmethod
    def _write_profile_turn_context() -> str:
        from ._profile_compliance import write_profile_turn_context
        return write_profile_turn_context()

    @staticmethod
    def _settings_lock(lock_path: str):
        """Return a context manager that holds an advisory exclusive lock.

        Uses `fcntl.flock` on a sibling lockfile so concurrent okuro
        processes (CLI + daemon) serialize their settings.json rewrites.
        Falls back to a no-op (with a logged warning) on platforms without
        `fcntl` so the install path stays usable on Windows.
        """
        from contextlib import contextmanager

        @contextmanager
        def _ctx():
            if fcntl is None:
                log.warning(
                    "fcntl unavailable; settings.json merge running without lock"
                )
                yield
                return
            # Open in append+read so the file is created if missing without
            # truncating an existing lockfile.
            fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
            try:
                # Bounded retry: up to ~5 s of contention, then give up loudly.
                deadline = time.monotonic() + 5.0
                while True:
                    try:
                        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        if time.monotonic() >= deadline:
                            raise TimeoutError(
                                f"timed out acquiring settings lock at {lock_path}"
                            )
                        time.sleep(0.05)
                try:
                    yield
                finally:
                    try:
                        fcntl.flock(fd, fcntl.LOCK_UN)
                    except OSError:
                        pass
            finally:
                try:
                    os.close(fd)
                except OSError:
                    pass

        return _ctx()

    # -----------------------------------------------------------------
    # Heal legacy per-project hook overrides
    # -----------------------------------------------------------------

    def _purge_stale_project_overrides(self) -> list[str]:
        """Remove okuro-canonical hook wiring from registered projects' `.claude/`.

        Scope per registered project:
          - `<proj>/.claude/settings.json`: strip `hooks.{PreToolUse,Stop}[*]`
            entries whose command basename matches an okuro-managed hook
            (see `_OKURO_HOOK_FILENAMES`). Preserve other keys and other
            matcher entries. Archive the pre-cleanup file first.
          - `<proj>/.claude/hooks/<name>.py`: if `<name>` matches and the
            file is **untracked** by git, archive + delete. If git-tracked,
            log a warning and leave alone (deleting would dirty the user's
            working tree and fight with git).
          - Archive root: `~/.okuro/archive/project-claude-overrides/<slug>/<ts>/`
        """
        touched: list[str] = []
        home = Path(os.path.expanduser("~"))
        archive_root = home / ".okuro" / "archive" / "project-claude-overrides"
        timestamp = time.strftime("%Y%m%dT%H%M%S")

        try:
            from okuro.db import get_db
            db = get_db()
            rows = db.fetchall(
                "SELECT id, path FROM projects WHERE active = 1 AND path IS NOT NULL"
            )
        except Exception as e:
            log.warning("purge_stale_project_overrides: project query failed: %s", e)
            return touched

        for row in rows:
            slug = row["id"]
            path_str = row["path"]
            if not path_str:
                continue
            proj = Path(path_str)
            if not proj.is_dir():
                continue

            proj_archive = archive_root / slug / timestamp

            # --- settings.json ---
            settings_path = proj / ".claude" / "settings.json"
            if settings_path.is_file():
                cleaned, changed = self._clean_settings_hooks(settings_path)
                if changed:
                    proj_archive.mkdir(parents=True, exist_ok=True)
                    orig_dst = proj_archive / "settings.json.orig"
                    try:
                        shutil.copy2(str(settings_path), str(orig_dst))
                    except OSError as e:
                        log.warning("archive settings.json failed for %s: %s", slug, e)
                    if cleaned is None:
                        settings_path.unlink()
                        touched.append(f"deleted:{settings_path}")
                    else:
                        settings_path.write_text(json.dumps(cleaned, indent=2) + "\n")
                        touched.append(f"cleaned:{settings_path}")

            # --- hook files ---
            hooks_dir = proj / ".claude" / "hooks"
            if hooks_dir.is_dir():
                for entry in sorted(hooks_dir.iterdir()):
                    if not entry.is_file():
                        continue
                    if entry.name not in _OKURO_HOOK_FILENAMES:
                        continue

                    if self._is_git_tracked(proj, entry):
                        log.warning(
                            "project %s has git-tracked stale hook %s; "
                            "commit its removal to eliminate drift (not auto-removed)",
                            slug, entry,
                        )
                        touched.append(f"warn-tracked:{entry}")
                        continue

                    proj_archive.mkdir(parents=True, exist_ok=True)
                    dst = proj_archive / entry.name
                    if dst.exists():
                        dst = proj_archive / f"{entry.stem}.{timestamp}{entry.suffix}"
                    shutil.move(str(entry), str(dst))
                    touched.append(f"archived:{dst}")

        return touched

    @staticmethod
    def _is_git_tracked(proj: Path, file_path: Path) -> bool:
        """True if `file_path` is tracked in the git repo at `proj`."""
        try:
            rel = file_path.relative_to(proj)
        except ValueError:
            return False
        try:
            result = subprocess.run(
                ["git", "-C", str(proj), "ls-files", "--error-unmatch", str(rel)],
                capture_output=True, text=True, timeout=5,
            )
            return result.returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            return False

    @staticmethod
    def _is_okuro_hook_command(cmd) -> bool:
        if not isinstance(cmd, str) or not cmd:
            return False
        return os.path.basename(cmd) in _OKURO_HOOK_FILENAMES

    def _clean_settings_hooks(self, settings_path: Path):
        """Strip okuro-canonical hook entries from a project settings.json.

        Returns `(cleaned_dict_or_None, had_changes)`. `None` signals the
        file should be deleted (became empty after cleanup).
        """
        try:
            original = json.loads(settings_path.read_text())
        except (json.JSONDecodeError, OSError):
            return None, False
        if not isinstance(original, dict):
            return original, False

        data = json.loads(json.dumps(original))  # deep copy
        hooks_block = data.get("hooks")
        if not isinstance(hooks_block, dict):
            return data, False

        cleaned_events: dict = {}
        for event_name, matchers in hooks_block.items():
            if not isinstance(matchers, list):
                cleaned_events[event_name] = matchers
                continue
            cleaned_matchers: list = []
            for matcher in matchers:
                if not isinstance(matcher, dict):
                    cleaned_matchers.append(matcher)
                    continue
                inner = matcher.get("hooks", [])
                if not isinstance(inner, list):
                    cleaned_matchers.append(matcher)
                    continue
                kept = [
                    h for h in inner
                    if not (
                        isinstance(h, dict)
                        and self._is_okuro_hook_command(h.get("command", ""))
                    )
                ]
                if kept:
                    new_matcher = dict(matcher)
                    new_matcher["hooks"] = kept
                    cleaned_matchers.append(new_matcher)
                # else: drop the entire matcher entry — all its hooks were okuro's
            if cleaned_matchers:
                cleaned_events[event_name] = cleaned_matchers

        if cleaned_events:
            data["hooks"] = cleaned_events
        else:
            data.pop("hooks", None)

        had_changes = data != original
        if data == {}:
            return None, had_changes
        return data, had_changes

    def get_subagent_protocol(self, bootstrap_context: str) -> str:
        # `bootstrap_context` is ignored. Subagents are hard-gated by middleware
        # to call bootstrap() themselves as their first action.
        from .template import build_subagent_protocol
        return build_subagent_protocol()
