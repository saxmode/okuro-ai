# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: M3 reviewer pipeline — deterministic checks → Critic (high-recall
#   findings) → Scorer (weighed verdict + rubric). Replaces the planned
#   single-LLM reviewer subtask under task.gates_enabled / phase.serialize.
#   Verdict events written to the M2 event log; load-bearing FAIL re-prompts
#   the producing subtask via the existing handle_failure path.
# index: run_review (pipeline.py) | generate_checks/run_checks (deterministic.py) |
#   run_critic (critic.py) | run_scorer (scorer.py)
# AGENT_HEADER_END -->
"""M3 reviewer pipeline package.

Public entrypoint: :func:`pipeline.run_review`. Engine calls it after
phase completion when the task opts in via ``task.gates_enabled`` or
``phase.serialize``.

Three stages, each gates the next:
    1. deterministic — pure functions over plan + ADRs + event log emit
       executable checks; failures short-circuit the LLM stages.
    2. critic        — FAIL-default, high-recall flaw finder. Reads the
       deterministic results + deliverables + active ADRs.
    3. scorer        — weighs findings (load-bearing vs cosmetic), emits
       PASS|CONDITIONAL|FAIL + per-dimension rubric. Cannot reduce a
       Critic FAIL on a load-bearing finding to PASS.

The Critic FAIL-default is intentional and load-bearing — documented in
``critic.yaml`` so future agents do not "fix" it to PASS-default. See
the Reflexion paper (arxiv 2303.11366) and the Agent-as-Judge survey
(arxiv 2508.02994) for the bias rationale; +8% inflated-score finding
in arxiv 2508.07805 is the specific failure mode this guards against.
"""

from okuro.orchestrator.reviewer.deterministic import (  # noqa: F401
    Check, CheckResult, generate_checks, run_checks, summarize,
)
from okuro.orchestrator.reviewer.pipeline import run_review  # noqa: F401
