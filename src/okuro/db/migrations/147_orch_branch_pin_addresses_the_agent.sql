-- ORCH-BRANCH-PIN: the sentence that started the agent-surface purge.
--
-- Its body carried, verbatim, into 100% of bootstrap packets:
--
--     Instructions are advisory, hooks are not.
--
-- Scoped, it argues one narrow thing: for cross-provider branch enforcement, a
-- git hook beats an instruction file. Delivered inside the packet, an agent
-- reads it as a statement about the packet. MEASURED, first-person and
-- self-reported by the offending session on 2026-09-08:
--
--     "I tore it out of that scope and used it as a general excuse."
--
-- One turn earlier the same session used it to argue against repairing the
-- instruction layer at all — "More instruction text won't work." That is the
-- worse damage: the sentence does not merely license non-compliance, it
-- licenses agents to refuse to fix instructions.
--
-- It is also FALSE for this system. `bootstrap_project`'s gate refuses every
-- other okuro tool until it lands — an instruction that provably binds. okuro
-- has both an instruction tier and an enforcement tier; the true claim is about
-- WHERE enforcement belongs, never about whether instructions bind.
--
-- The engineering rationale is NOT lost. It lives in the module docstring of
-- src/okuro/cli/cmd_git_guards.py, which is read by whoever maintains the
-- guards and by no agent.
--
-- TWO MORE FIXES IN THE SAME BODY:
--
-- 1. "Four invariants" was wrong. cmd_git_guards.py:35-50 documents SEVEN, and
--    has since before this text was written: CONTENT, ADOPT and PUBLISH were
--    missing. An agent refused by one of those three was told by its own
--    briefing that the guard did not exist. Measured this session: CONTENT
--    refused a commit that carried a real customer name in a code comment.
--
-- 2. "Each has an OKURO_ALLOW_* bypass" is a bypass advertisement in the
--    packet, the same class as the refusal messages that named their own
--    escape hatch (mcp_middleware.py:454, removed 2026-09-09). The bypasses
--    still exist for the owner and for scripted runs; they are no longer
--    announced to the party the guards constrain. The line that matters —
--    a refusal means a real collision, move the work to its own tree — stays.
--
-- Idempotent: matches on id, and the new text contains none of the removed
-- phrases, so a re-run is a no-op write of identical content.

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
    || 'spawned it. Seven invariants: PIN (main tree commits only to main) · '
    || 'BIND (worktree commits only to its bound branch) · SCOPE (envs, '
    || 'caches, databases, secrets never NEWLY added) · CONTENT (staged '
    || 'additions carry no real brand or person tokens) · ADOPT (no silent '
    || 'commit of agent-authored files you did not write) · PROTECT (no '
    || 'force-push or deletion of main) · PUBLISH (a push exposes no '
    || 'non-product path). A refused commit is a REAL collision — move the '
    || 'work to its own tree.'
WHERE id = 'ORCH-BRANCH-PIN';
