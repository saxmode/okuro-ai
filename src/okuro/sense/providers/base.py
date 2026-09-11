# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Abstract base for provider adapters.
# index: class ProviderAdapter
# AGENT_HEADER_END -->
"""Abstract base for provider adapters."""

from abc import ABC, abstractmethod


class ProviderAdapter(ABC):
    """Each provider adapter configures a specific agent provider.

    Responsibilities:
    - Generate provider-specific instruction files
    - Install enforcement hooks where supported
    - Detect if the provider is available on this system
    - Produce protocol fragments for subagent prompts
    """

    name: str  # e.g. "claude", "codex", "gemini"

    @abstractmethod
    def generate_instructions(self) -> list[str]:
        """Generate provider-specific instruction files to canonical paths.

        Each adapter writes to its provider's standard config location
        (e.g., ~/.claude/, ~/.codex/, ~/.gemini/). No output_dir needed —
        paths are fixed per provider.

        Returns:
            List of absolute paths of files written.
        """

    @abstractmethod
    def install_hooks(self) -> list[str]:
        """Install enforcement hooks for this provider.

        Returns empty list if the provider has no hook system.
        """

    def enforcement_capability(self) -> dict:
        """Declare what this provider can ENFORCE, versus merely be told.

        Rule 4 of the tier model: adapters detect capability and degrade
        LOUDLY, never silently. The failure this prevents is the default one —
        when a provider changes or lacks a hook surface, nothing raises and
        nothing logs, so an absent gate is indistinguishable from a working
        one with nothing to deny. Three of the four known churn modes produce
        no error at all (openai/codex#21639, antigravity config-path churn,
        a matcher that stops matching); the fourth denies everything
        (manaflow-ai/cmux#5358).

        The default is the SAFE claim: no blocking capability. An adapter that
        can enforce must say so explicitly, and say where the claim came from —
        `verified` is a provenance string, not a boolean, because "we read it
        in a blog post" and "we read it out of the installed binary" are not
        the same evidence and should not render the same.

        Keys:
          blocking_pre_tool  can a hook refuse a tool call before it runs
          shell_tool         the tool name carrying shell commands, if any
          command_field      dotted path to the command text in the payload
          deny_contract      how a refusal is expressed
          verified           where the claim comes from, and when
          degradation        what is LOST when this provider cannot enforce;
                             None only when nothing is lost
        """
        return {
            "provider": self.name,
            "blocking_pre_tool": False,
            "shell_tool": None,
            "command_field": None,
            "deny_contract": None,
            "verified": "not probed",
            "degradation": (
                "no declared enforcement capability — treat every gate as "
                "advisory on this provider until an adapter overrides this"
            ),
        }

    def gate_bodies(self) -> dict[str, str]:
        """The gate scripts this provider would emit: {name: rendered source}.

        Rendered, not installed — this must work on a machine where the
        provider is absent, because the parity test that reads it runs in CI.

        Why it exists: `enforcement_capability()` says whether a provider CAN
        block, and that boolean cannot say WHICH rules it blocks on. Cursor
        answers True, emits a real gate, and carries no hunt detection; the
        record read as full coverage and nothing disagreed. Coverage is
        therefore measured from what an adapter actually emits, and this is the
        seam that exposes it.

        Default is {} — a provider with no gate, honestly reported as covering
        no rules.
        """
        return {}

    def gate_coverage(self) -> dict[str, bool]:
        """Which contract rules this provider's emitted gates enforce."""
        from ._gate_contract import gate_coverage

        return gate_coverage(self.gate_bodies())

    @abstractmethod
    def detect(self) -> bool:
        """Check if this provider is available on this system."""

    @abstractmethod
    def get_subagent_protocol(self, bootstrap_context: str) -> str:
        """Protocol fragment to inject when this provider runs as a subagent."""
