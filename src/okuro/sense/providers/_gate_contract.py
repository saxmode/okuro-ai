# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Compile the tool_routing contract into per-provider gate routes.
# index: def compile_system_state_routes
# AGENT_HEADER_END -->
"""Shared gate compiler — one contract, many provider emitters.

This module exists so the emitters cannot drift. Every adapter that can block
a tool call reads its routes from HERE, and here reads them from the
`tool_routing` actions in ``data/agent_rules.yaml`` — the same list the
bootstrap packet renders into the routing table an agent is shown.

That is the shape the provider-agnosticism rule actually requires: the SPEC is
shared, only the EMITTER is per-provider. A hook hand-written for one CLI is
lock-in; a hook generated from this is not, and a second provider's emitter can
be added without touching the spec. `improve.py` states that as the decidable
test for whether a proposal is lock-in.

Why it was extracted: the compiler was born inside `claude.py`, where it read
as a Claude Code detail. The moment a second provider needed the same routes,
leaving it there would have meant either an adapter importing another adapter
or two copies of the projection — and two copies is exactly the divergence this
whole contract was introduced to end.
"""

from __future__ import annotations


def compile_system_state_routes() -> dict[str, str]:
    """Project the tool_routing contract into {native command: okuro call}.

    Only actions declaring ``protects: [system_state]`` compile, and only when
    they name the native command they replace via ``instead_of``. Adding a row
    to the yaml adds enforcement on the next ``install_hooks()`` for EVERY
    provider with an emitter; no gate code changes anywhere.

    ``enforceable: false`` opts an action out of the gate while keeping it in
    the packet. Not every good routing rule is safely enforceable: ``curl
    localhost`` is real guidance but a general-purpose HTTP client, so a gate
    cannot tell a health check from exercising an endpoint and would deny
    correct work. A gate that denies correct work is one people switch off,
    taking the routes that do hold with it.

    Deliberately NOT compiled here: ``indexed_roots`` actions. Their detection
    is path-coverage (is this tree cortex-indexed), not command identity, and
    it already lives in check-grep's ``_cortex_covers``. Folding two detection
    strategies into one emitter would trade a real invariant for tidiness.
    """
    from okuro.sense.bootstrap.rules import get_actions

    routes: dict[str, str] = {}
    for action in get_actions("tool_routing"):
        if "system_state" not in (action.get("protects") or []):
            continue
        if action.get("enforceable") is False:
            continue
        native = (action.get("instead_of") or "").strip("`").strip()
        call = (action.get("call") or "").strip("`").strip()
        if native and call:
            routes[native] = call
    return routes


# ---------------------------------------------------------------------------
# Shared emitter body
# ---------------------------------------------------------------------------
#
# The ROUTE MATCHING is provider-agnostic; only the I/O contract around it is
# not. Cursor's payload puts the command at top-level `command` and denies with
# {"permission": "deny"}; Claude Code's puts it at `tool_input.command` and
# denies with exit 2. Everything between those two edges — argv extraction,
# pipeline handling, multi-word route matching, default-allow — is identical,
# and a second hand-written copy of it is how two gates that claim to enforce
# one contract end up enforcing two different things.
#
# So the matcher is defined once, here, and each adapter's template splices it
# in via __MATCHER__. An emitter owns its provider's payload shape and denial
# shape. It does not own what counts as a system-state read.
SHELL_ROUTE_MATCHER_SRC = '''
def _leading_argv(command):
    """First segment of a pipeline, as argv. `x | nvidia-smi` is not a state read."""
    head = re.split(r'\\||;|&&|\\n', command, maxsplit=1)[0].strip()
    if not head:
        return []
    try:
        argv = shlex.split(head)
    except ValueError:
        return []
    while argv and ('=' in argv[0] or argv[0] in ('sudo', 'time', 'nice', 'command')):
        argv = argv[1:]
    return argv


def _match(command):
    """Return (native, okuro_call) when the command IS one of the routes.

    Matches on the leading command plus enough of its arguments to be sure:
    `docker ps` routes, `docker build` does not. DEFAULT IS ALLOW — an
    unrecognised command is none of this hook's business.
    """
    argv = _leading_argv(command)
    if not argv:
        return None
    prog = os.path.basename(argv[0])
    joined = ' '.join([prog] + argv[1:])
    for native, call in _ROUTES.items():
        parts = native.split()
        if parts[0] != prog:
            continue
        # Multi-word route (`docker ps`, `df -h`): every declared token must be
        # present, in order, at the start of the invocation.
        if joined == native or joined.startswith(native + ' '):
            return native, call
        if len(parts) == 1:
            return native, call
    return None
'''


# Denial copy, shared for the same reason the matcher is: an agent that meets
# this gate on two providers must be told the same thing, or the rule reads as
# provider trivia rather than as okuro's contract.
# THE SEQUENCE RULE, shared for the same reason the matcher is.
#
# The owner, 2026-09-09: "grep can be used if cortex has been tried first." That
# makes the routing rule a SEQUENCE, and a sequence needs one bit of state the
# gate must read: has this session called a cortex_* tool yet.
#
# WHY IT LIVES HERE AND NOT IN claude.py. It was written into claude.py first.
# Measured the same day: `grep -c 'hunt\|_cortex_covers' cursor.py` returns 0.
# Cursor's gate carries NO hunt detection at all — it enforces the store marker
# and the system-state routes, nothing else. So the sequence rule did not
# diverge across two implementations. It exists in exactly one, and the
# contract it claims to serve is unenforced everywhere else.
#
# That is the sharper version of the class this module was extracted to end: a
# gate whose DECISION is authored per-provider reaches whoever the author
# remembered, and its ABSENCE is silent. The matcher was shared and stayed
# correct on every provider. Every rule left inside an emitter is a rule with
# one provider's worth of coverage and nothing that says so out loud.
#
# An emitter owns its provider's payload shape and its allow shape. It does not
# own WHEN the gate lets a hunt through.
#
# FAILS CLOSED by construction: every error path returns False, so a missing or
# unreadable channel keeps the redirect. A missing marker costs one redirect; a
# wrongly-present one costs the rule.
CORTEX_SEQUENCE_SRC = '''
def _cortex_already_tried():
    """Has this session used cortex? The rule is a sequence, not a ban.

    grep is legal AFTER cortex has been tried -- cortex does not index a log
    file, a data dump or a fresh untracked tree, and an agent that already
    asked and got nothing needs a next move rather than a wall.

    okuro's MCP server writes `cortex-used.<key>` when a cortex_* tool
    succeeds, keyed by the same identity the bootstrap hook publishes in
    `.current`, and clears it on bootstrap so the permission cannot outlive
    the session that earned it.
    """
    try:
        state_dir = os.path.expanduser(
            os.environ.get('OKURO_AGENT_STATE_DIR')
            or '~/.cache/okuro-agent-bootstrap'
        )
        try:
            with open(os.path.join(state_dir, '.current')) as _cf:
                raw = _cf.read().strip()
        except OSError:
            raw = ''
        key = 'agent:' + raw if raw else 'stdio'
        safe = ''.join(c if c.isalnum() or c in '-_' else '_' for c in key)[:80]
        return os.path.exists(os.path.join(state_dir, 'cortex-used.' + safe))
    except Exception:
        return False
'''


ROUTE_DENIAL_TEMPLATE = (
    "`{native}` is a system-state read. okuro answers it directly: {call}\n\n"
    "That returns parsed, structured state instead of text you have to "
    "scrape."
    # REMOVED: "it works the same on every provider" (okuro's portability
    # claim) and "generated from okuro's tool_routing contract — the same
    # source as the routing table in your bootstrap packet" (okuro's build
    # architecture). Both are true and both are addressed to whoever
    # maintains the contract. The agent needs the route and the reason the
    # route is better; how the route was compiled changes nothing it does.
)


# The one path that identifies okuro's store, chosen to survive a collision
# that a naive match walks straight into.
#
# `okuro.db` alone is WRONG. It is also the dotted MODULE path, and okuro's own
# documented migration command is:
#     python -c "... from okuro.db import get_db; print(get_db().migrate())"
# Matching the bare string would deny a documented operational procedure — the
# precise shape of "a gate that denies correct work", which then gets switched
# off along with everything that did work.
#
# The directory component is the discriminator: a FILE reference always carries
# `.okuro/okuro.db`, an IMPORT never does. Verified against both this session's
# real violations (`file:$HOME/.okuro/okuro.db?mode=ro`) and the
# documented migrate command.
OKURO_STORE_PATH_MARKER = ".okuro/okuro.db"


def compile_store_guard() -> dict[str, object]:
    """Project the `okuro_store` action into a path-matching guard.

    Second resource class, and the one that closes the interpreter path.
    Command-name gating cannot cover it: the set of programs that can open a
    SQLite file is unbounded, so this matches the RESOURCE — the store's path
    appearing anywhere in the command text, heredoc bodies included.

    Returns {} when the contract carries no such action, so an emitter that
    calls this before the yaml is updated degrades to no guard rather than
    crashing.
    """
    from okuro.sense.bootstrap.rules import get_actions

    for action in get_actions("tool_routing"):
        if "okuro_store" not in (action.get("protects") or []):
            continue
        if action.get("enforceable") is False:
            continue
        return {
            "marker": OKURO_STORE_PATH_MARKER,
            "call": (action.get("call") or "").strip(),
            "okuro": list(action.get("okuro") or []),
        }
    return {}


# ---------------------------------------------------------------------------
# Rule coverage — capability is not coverage
# ---------------------------------------------------------------------------
#
# `enforcement_capability()` answers "can this provider block a tool call".
# That question is a BOOLEAN, and a boolean cannot say WHICH rules a provider
# actually enforces. Measured 2026-09-09: Cursor answers `blocking_pre_tool:
# True` and emits a real gate — covering the system-state routes and the store
# guard, and carrying NO hunt detection at all. The cortex-first rule exists in
# exactly one emitter. Nothing failed, nothing logged, and the capability
# record read as full coverage.
#
# That is the class, stated at its own grain: a rule authored inside a provider
# emitter has one provider's worth of reach, and its ABSENCE everywhere else is
# silent. Rule 4 of the tier model says adapters degrade LOUDLY — it was
# written for a missing SURFACE and never applied to a missing RULE.
#
# So coverage is MEASURED, never declared: `gate_coverage()` renders what a
# provider actually emits and looks for the rule's signature in it. A provider
# cannot claim a rule it does not ship, because nothing here reads a claim.
# A genuine gap is then declared once, in `GATE_GAPS`, where a test can see it.
GATE_RULES: dict[str, dict[str, str]] = {
    "system_state_routes": {
        "what": "shell reads of system state route to the sysinfo tools",
        "signature": "_ROUTES",
        "shared": "SHELL_ROUTE_MATCHER_SRC",
    },
    "okuro_store": {
        "what": "direct reads of okuro's store route to the typed MCP tool",
        "signature": "_OPERATIONAL",
        "shared": "compile_store_guard",
    },
    "cortex_first": {
        "what": "a tree hunt in an indexed path waits until cortex was tried",
        "signature": "_cortex_already_tried",
        "shared": "CORTEX_SEQUENCE_SRC",
    },
    # Not a PreToolUse rule — this one fires AFTER a draft exists, on the
    # provider's stop/end-of-turn event. It is in this registry anyway,
    # because the class is the same and does not care which event carries it:
    # the four blocking detectors in `_DETECTOR_VALIDITY` are refusals okuro
    # claims to make, and they are emitted by one adapter.
    "compliance_block": {
        "what": "a reply violating a blocking profile detector is refused",
        "signature": "_block_keys",
        "shared": "_profile_compliance.blockable_detectors",
    },
}


# A gap is a rule a provider does not enforce AND has a reason not to. It is
# recorded here rather than in the adapter so that the contract — not the
# emitter — remains the one place the rule set is known, and so that adding a
# rule surfaces every provider that lacks it in one diff.
#
# An entry is a promise that someone LOOKED. It is not permission to stay
# uncovered: the parity test prints the reason on every run.
# The key "*" means EVERY rule, one reason — for a provider that emits no gate
# at all. Spelling three identical reasons out per provider would make the
# record longer without making it truer, and the moment a fourth rule is added
# the wildcard covers it too rather than quietly reading as enforced.
GATE_GAPS: dict[str, dict[str, str]] = {
    "codex": {
        "*": (
            "Capability and input shape confirmed, but okuro emits no gate for "
            "Codex — enforcement is instructions-only until the deny contract "
            "is OBSERVED rather than inferred. Coverage would be 4 tool "
            "handlers (shell, unified_exec, apply_patch, mcp), not all tools."
        ),
    },
    "antigravity": {
        "*": (
            "Capability confirmed and the deny contract read out of the "
            "installed build's own hook documentation, but okuro emits no gate "
            "for Antigravity yet — enforcement is instructions-only until an "
            "emitter ships."
        ),
    },
    "cursor": {
        # A SURFACE gap, not an unported rule. Cursor's seven blocking events
        # (preToolUse, beforeShellExecution, beforeMCPExecution,
        # beforeReadFile, beforeSubmitPrompt, subagentStart, beforeTabFileRead)
        # all fire BEFORE something runs. None fires after a reply exists, so
        # there is no event on which a finished answer could be refused.
        # Porting the detectors would produce a hook with nothing to bind to.
        "compliance_block": (
            "no end-of-turn event exists on Cursor — all seven blocking events "
            "fire before an action, none after a reply is drafted. The "
            "detectors reach Cursor as per-turn INSTRUCTION only, via the "
            "profile context, and cannot be enforced there."
        ),
    },
}


def gate_coverage(bodies: dict[str, str]) -> dict[str, bool]:
    """Which contract rules the given gate bodies actually enforce.

    ``bodies`` is {name: rendered source} for every gate a provider emits.
    Coverage is the UNION across them: Claude splits the rules over several
    hook files, Cursor puts two in one, and the contract cares only that the
    rule is reachable on that provider.

    A provider that emits nothing returns every rule False — which is correct
    and is exactly what a provider with no hook surface should report.
    """
    joined = "\n".join(bodies.values())
    return {rid: (spec["signature"] in joined) for rid, spec in GATE_RULES.items()}


def undeclared_gate_gaps(provider: str, bodies: dict[str, str]) -> list[str]:
    """Rules this provider neither enforces nor declares a gap for.

    Empty list is the passing state. A non-empty list is the silent absence
    this whole section exists to make loud.
    """
    covered = gate_coverage(bodies)
    declared = GATE_GAPS.get(provider, {})
    if "*" in declared:
        return []
    return sorted(r for r, ok in covered.items() if not ok and r not in declared)


def describe_gate_coverage(adapters) -> str:
    """One sentence per provider, derived from what each actually emits.

    The onboarding surface used to carry a hand-written paragraph naming which
    provider enforced what. It was corrected twice — once when it still called
    enforcement "Claude-only" after a second adapter gained hooks, once when it
    told users Cursor was hooks-less while Cursor shipped a GA hook system.
    Both times the prose was the last thing updated, because a string is not
    reachable by any coverage-based selection.

    The test that guards it derives WHICH ADAPTERS install hooks — a boolean
    per adapter. That is the same grain problem one level up: codex and
    antigravity install a per-turn injection hook and no gate at all, and both
    satisfy "installs hooks". So the note stayed true by accident.

    This renders the RULE grain instead, from the same measurement the parity
    test uses. A rule ported to a second provider changes this text on the next
    render, with no prose to remember.
    """
    lines = []
    for adapter in adapters:
        covered = [r for r, ok in gate_coverage(adapter.gate_bodies()).items() if ok]
        if not covered:
            lines.append(f"{adapter.name}: no gate — advisory only")
            continue
        missing = sorted(set(GATE_RULES) - set(covered))
        tail = f"; not enforced here: {', '.join(missing)}" if missing else ""
        lines.append(f"{adapter.name}: enforces {', '.join(sorted(covered))}{tail}")
    return " · ".join(lines)


# ---------------------------------------------------------------------------
# Hunt detection — the cortex-first rule, shared
# ---------------------------------------------------------------------------
#
# Everything below decides WHETHER A COMMAND HUNTS A TREE and whether cortex
# already covers that tree. None of it is provider-specific: it is argv
# parsing, shell tokenising and path coverage. It lived in claude.py, so the
# rule reached Claude Code and nowhere else — Cursor's gate carried none of it.
#
# The emitter still owns its provider's payload shape and denial shape. It does
# not own what counts as a hunt.
CORTEX_COVERAGE_SRC = '''
def _indexed_roots():
    # Ask the BRAIN which roots are indexed, not the filesystem whether a
    # sidecar marker happens to sit in cwd. The marker test answered
    # "is there an .okuro-index.yaml under this cwd" — which is False in
    # /tmp, in a worktree, and anywhere the agent was launched from outside
    # the repo, so the blocker went silently inert exactly where an agent is
    # most likely to grep blindly. Measured 2026-07-19: claude made ZERO
    # cortex calls across two runs with this gate in place.
    #
    # Cached for 60s in the user's cache dir: this runs on EVERY Grep/Read,
    # so it must not open the 8 GB brain each time.
    import json as _j, time as _t
    cache = os.path.expanduser('~/.cache/okuro-cortex-roots.json')
    try:
        st = os.stat(cache)
        if _t.time() - st.st_mtime < 60:
            return _j.load(open(cache))
    except Exception:
        pass
    roots = []
    try:
        import sqlite3
        dbp = os.path.expanduser('~/.okuro/okuro.db')
        con = sqlite3.connect(f'file:{dbp}?mode=ro', uri=True, timeout=1.0)
        # Same query cortex.roots.registered_roots() uses: `indexed`, not
        # `active` — this asks what cortex may SCAN, not what agents may see.
        roots = [r[0] for r in con.execute(
            'SELECT path FROM projects WHERE indexed = 1 AND path IS NOT NULL')
            if r[0]]
        con.close()
    except Exception:
        return []
    try:
        os.makedirs(os.path.dirname(cache), exist_ok=True)
        _j.dump(roots, open(cache, 'w'))
    except Exception:
        pass
    return roots


def _marker_present(root):
    # Original signal, KEPT: a tree carrying `.okuro-index.yaml` (or a legacy
    # cortex layout) is indexed, and this needs no database. Correct whenever
    # it fires; the defect was that it was the ONLY signal, so it went silent
    # the moment the agent worked on a path outside its own cwd.
    for sub in ('', 'src', 'tests', 'scripts', 'lib', 'app'):
        if os.path.isfile(os.path.join(root, sub, '.okuro-index.yaml')):
            return True
    for legacy in (
        os.path.join(root, '.okuro-cortex', 'purpose-cache.json'),
        os.path.join(root, '.tm-cortex', 'purpose-cache.json'),
        os.path.join(root, '.tm-cortex', 'cortex.db'),
    ):
        if os.path.isfile(legacy):
            return True
    return False


def _cortex_covers(target):
    # UNION of two signals, because either alone has a blind spot:
    #   * the registered-roots query knows the whole indexed set, including
    #     paths the agent is touching from a different cwd (the /tmp case);
    #   * the on-disk marker works with no database and covers trees that are
    #     indexed but not registered as projects.
    # Replacing the marker with the DB query broke 16 tests that were right to
    # object: a tree with a sidecar IS indexed. Add signals, do not swap them.
    try:
        real = os.path.realpath(target or os.getcwd())
    except Exception:
        return False
    probe = real if os.path.isdir(real) else os.path.dirname(real)
    if _marker_present(probe) or _marker_present(os.getcwd()):
        return True
    for root in _indexed_roots():
        try:
            r = os.path.realpath(root)
        except Exception:
            continue
        if real == r or real.startswith(r + os.sep):
            return True
    return False'''


# Hunt-shape detection. Splice AFTER CORTEX_COVERAGE_SRC — `_argv_hunt_target`
# needs nothing from it, but `_bash_hunt_target`'s callers do.
HUNT_DETECT_SRC = '''
# Leading commands that hunt a TREE. `cat`/`head`/`tail` are deliberately
# absent: they read a named file, which is check-read.py's surface, and
# blocking them here would deny `cat /etc/hosts` for no retrieval gain.
_HUNT_CMDS = {'grep', 'egrep', 'fgrep', 'rg', 'ripgrep', 'ack', 'ag'}
_RECURSIVE_FLAGS = ('-r', '-R', '--recursive', '--include', '--exclude-dir')
# These walk the tree with NO flag and NO path: `rg x` searches everything
# under cwd. Treating them like grep meant a bare `rg pattern` -- the single
# most natural way to hunt -- returned None and sailed through.
_RECURSIVE_BY_DEFAULT = {'rg', 'ripgrep', 'ack', 'ag'}

# Shell operators, as tokenised by shlex(punctuation_chars=True).
_SEQ_OPS = {';', '&&', '||', '&'}     # start a NEW command
_PIPE_OPS = {'|', '|&'}               # continue one pipeline
_REDIR_OPS = {'>', '>>', '<', '<<', '2>', '&>', '>&'}


def _is_recursive_flag(a):
    if a in _RECURSIVE_FLAGS or a.startswith('--include='):
        return True
    # Clustered short flags -- `-rn`, `-nr`, `-Ri` all recurse. The membership
    # test alone saw none of them, so a recursive hunt was only ever caught by
    # the directory fallback below, and `grep -rn p <path-not-on-disk>` (a
    # typo'd or generated path) slipped through entirely.
    return len(a) > 1 and a[0] == '-' and a[1] != '-' and (
        'r' in a[1:] or 'R' in a[1:])


def _shell_segments(command):
    r\"\"\"Every command position that could hunt a tree, quoting-aware.

    Splitting the raw STRING before parsing it -- the previous approach --
    had two proven holes, both measured 2026-07-29 by replaying real session
    commands through the installed hook:

      * `cd <repo> && grep -rn p src/` -- the split took segment one, found
        `cd`, and allowed. The hunt sat in segment two and was never examined.
        Any agent that habitually prefixes `cd <repo> &&` bypassed this gate
        on EVERY call it ever made.
      * `grep -rn "a\\|b" src/` -- the split fired on the pipe INSIDE the
        quoted pattern, leaving an unbalanced quote for shlex.split to raise
        on, and a raise means allow. Same character as the POSIX-ERE gotcha.

    One defect behind both: split-before-parse. shlex with punctuation_chars
    tokenises operators AND honours quoting, so a split can now only happen at
    a real operator -- `git commit -m "a|b && c"` stays one command.

    SEQUENCE operators (`;` `&&` `||`) begin a new command, so every segment
    they produce is examined. A PIPE is different, and the original rule was
    right: only the first stage of a pipeline retrieves, because `ps aux |
    grep x` filters a stream already in hand.
    \"\"\"
    try:
        lex = shlex.shlex(command, posix=True, punctuation_chars=True)
        lex.whitespace_split = True
        tokens = list(lex)
    except ValueError:
        return []

    segments, cur = [], []
    first_stage = True
    skip_next = False
    for t in tokens:
        if skip_next:
            skip_next = False
        elif t in _REDIR_OPS:
            skip_next = True          # drop the operator AND its target
        elif t in _SEQ_OPS or t in _PIPE_OPS:
            if cur and first_stage:
                segments.append(cur)
            first_stage = t in _SEQ_OPS
            cur = []
        else:
            cur.append(t)
    if cur and first_stage:
        segments.append(cur)
    return segments


def _argv_hunt_target(argv, cwd):
    \"\"\"The path this ONE command hunts, or None.\"\"\"
    argv = [a for a in argv if not a.startswith('$(')]
    while argv and ('=' in argv[0] or argv[0] in ('sudo', 'time', 'nice', 'command')):
        argv = argv[1:]
    if not argv:
        return None
    cmd = os.path.basename(argv[0])
    args = argv[1:]

    def _abs(p):
        return p if os.path.isabs(p) else os.path.join(cwd, p)

    if cmd == 'find':
        if not any(a in ('-name', '-iname', '-path', '-regex') for a in args):
            return None
        pos = [a for a in args if not a.startswith('-')]
        return _abs(pos[0]) if pos else cwd

    if cmd not in _HUNT_CMDS:
        return None
    pos = [a for a in args if not a.startswith('-')]
    # A recursive flag is a hunt regardless of where it points.
    if any(_is_recursive_flag(a) for a in args):
        return _abs(pos[1]) if len(pos) > 1 else cwd
    # A tool that recurses by default is a hunt as soon as it has a pattern.
    if cmd in _RECURSIVE_BY_DEFAULT and pos:
        return _abs(pos[1]) if len(pos) > 1 else cwd
    # Otherwise a hunt only if it names a DIRECTORY to walk. `grep x file.py`
    # is the single-named-file case the Grep branch also allows.
    for cand in pos[1:]:
        if os.path.isdir(_abs(cand)):
            return _abs(cand)
    return None


def _bash_hunt_target(command, cwd=None):
    \"\"\"Return the hunted path, or None when this command is not a hunt.

    DEFAULT IS ALLOW. Only a positively-identified tree hunt returns a path.
    This direction matters: the naive fix -- registering the `Bash` matcher
    against the old body -- made every Bash call fall through to the
    `path or os.getcwd()` branch and BLOCKED `git commit`. A gate that denies
    everything is a worse outcome than a gate that denies nothing, because it
    is discovered in seconds and then disabled permanently.

    Holes 4-7 of this class closed here -- see the header and _cortex_covers
    for 1-3. The class: the gate is scoped to an invocation SHAPE, and the
    same capability stays reachable by another shape.
    \"\"\"
    cwd = cwd or os.getcwd()
    for argv in _shell_segments(command):
        hunted = _argv_hunt_target(argv, cwd)
        if hunted is not None:
            return hunted
    return None
'''
