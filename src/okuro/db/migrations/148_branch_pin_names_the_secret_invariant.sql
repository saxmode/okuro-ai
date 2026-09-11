-- ORCH-BRANCH-PIN gains SECRET: eight invariants, not seven.
--
-- Migration 147 corrected "Four invariants" to seven, because the guards had
-- grown CONTENT, ADOPT and PUBLISH while the principle still named the original
-- four. An agent refused by one of the three it did not name was told by its own
-- briefing that the guard did not exist.
--
-- SECRET was added on 2026-09-09 and this keeps the principle level with the
-- code in the same commit, rather than repeating the drift 147 was written to
-- fix. That is the whole point of the free-rider: the count is not decoration,
-- it is the list an agent checks itself against.
--
-- WHY SECRET EXISTS. "ALWAYS load secrets via keyring — NEVER a .env file,
-- NEVER a hardcoded credential" ships in the CORE packet, in all four provider
-- instruction files and in TOOL-PROTOCOL.md. The 2026-09-08 cross-surface audit
-- found it was the ONLY rule in the contradiction matrix stated on three
-- surfaces with no gate and no hook anywhere. SCOPE denies a `.env` FILE by
-- path; it cannot see `API_KEY = "sk-…"` inside a .py, which is the shape an
-- agent actually writes.
--
-- Idempotent: matches on id; the new text differs only by the SECRET clause and
-- the count, so a re-run rewrites identical content.

UPDATE principles
SET description =
    'Branch identity is a property of the filesystem PATH, not of session '
    || 'state. One worktree = one branch, recorded in `.okuro-worktree` at '
    || 'that tree''s root; create trees with `okuro wt add <topic>`. Never '
    || 'branch-switch in a repo''s MAIN worktree; never commit from a worktree '
    || 'onto any branch but its bound one. Enforcement is in git hooks '
    || '(`okuro setup-git-guards`), NOT per-provider instruction files, '
    || 'because every provider — Claude Code, Codex, Gemini, Cursor, cron, a '
    || 'human shell — shells out to `git`, so the hook fires regardless of who '
    || 'spawned it. Eight invariants: PIN (main tree commits only to main) · '
    || 'BIND (worktree commits only to its bound branch) · SCOPE (envs, '
    || 'caches, databases, secrets never NEWLY added) · CONTENT (staged '
    || 'additions carry no real brand or person tokens) · SECRET (staged '
    || 'additions carry no credential — put it in the keyring and read it at '
    || 'runtime) · ADOPT (no silent commit of agent-authored files you did not '
    || 'write) · PROTECT (no force-push or deletion of main) · PUBLISH (a push '
    || 'exposes no non-product path). A refused commit is a REAL collision — '
    || 'fix what it names, or move the work to its own tree.'
WHERE id = 'ORCH-BRANCH-PIN';
