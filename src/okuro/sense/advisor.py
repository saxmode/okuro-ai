# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Brain advisor — LLM-powered task briefing.
# index: def advise | def _gather_context
# AGENT_HEADER_END -->
"""Brain advisor — LLM-powered task briefing.

Ported from tm-launcher brain/advisor.py.
Key change: routes through okuro.bridge instead of raw tm-inference.
"""

SYSTEM_PROMPT = """\
You are a briefing officer for an AI agent about to start a task.

Given system context and a task description, produce a focused briefing:

1. **Situation** — What state is the project in?
2. **Key files** — Which files/dirs matter for this task?
3. **Gotchas** — Warnings, pitfalls, non-obvious constraints
4. **Approach** — Suggested first steps (be specific)
5. **Don't** — What to avoid

Rules:
- Max 300 words. No filler.
- Skip sections that don't apply.
- Reference specific paths, ports, tools — not generic advice."""


def advise(task_hint: str, budget: int = 3000, timeout: int = 60) -> str:
    """Generate an LLM-powered task briefing.

    Args:
        task_hint: What the agent is about to work on.
        budget: Token budget for raw context assembly.
        timeout: LLM call timeout in seconds.
    """
    import time

    # Step 1: Assemble raw context from available modules
    raw_context = _gather_context(task_hint, budget)

    # Step 2: Build prompt
    user_prompt = f"## Task\n{task_hint}\n\n## Raw System Context\n{raw_context}"

    # Step 3: Call LLM via okuro.bridge
    start = time.time()
    try:
        from okuro.bridge import invoke

        result = invoke(
            prompt=f"{SYSTEM_PROMPT}\n\n{user_prompt}",
            capability="brain-advise",
        )
        if isinstance(result, dict):
            if not result.get("success"):
                err = result.get("error", "unknown")
                return f"[advisor unavailable: {err}]\n\nFallback: raw context below.\n\n{raw_context}"
            briefing = result.get("output", "")
        else:
            briefing = str(result or "")
        duration = round(time.time() - start, 1)
        return f"# Task Briefing\n_Generated in {duration}s_\n\n{briefing}"

    except Exception as e:
        return f"[advisor unavailable: {e}]\n\nFallback: raw context below.\n\n{raw_context}"


def _gather_context(task_hint: str, budget: int) -> str:
    """Gather context for a task briefing via the shared brain retriever.

    Delegates to ``okuro.sense.grounding.gather_context`` (the same path
    podcast/slides/video use) so grounding is consistent and budget-aware.
    Keeps the advisor's historical source mix — profile, project, memory,
    thoughts (it briefs an agent on how to start, not on canonical reports)."""
    from okuro.sense.grounding import (
        SOURCE_MEMORY,
        SOURCE_PROFILE,
        SOURCE_PROJECT,
        SOURCE_THOUGHTS,
        gather_context,
    )

    context, _sources = gather_context(
        task_hint,
        budget=budget,
        sources=(SOURCE_PROFILE, SOURCE_PROJECT, SOURCE_MEMORY, SOURCE_THOUGHTS),
    )
    return context or "No context available."
