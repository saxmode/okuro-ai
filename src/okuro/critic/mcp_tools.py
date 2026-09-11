# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Critic module tools — the uninformed-critic pass, provider-agnostic.
# index: imports | def _text | def get_tools | async def handle_tool
# AGENT_HEADER_END -->
"""Critic module tools.

One tool, ``critique``. It hands work to a fresh model that owns none of
it and returns structured findings — always including a REQUIRED verdict
on whether the proposal fixes the instance or the class.

Accepts either raw ``subject`` text or an ``artifact_id``, so a plan
already written to the brain can be reviewed without the caller pasting it
back through the context window.
"""

import asyncio
import json
import logging

from mcp.types import Tool, TextContent

logger = logging.getLogger(__name__)

# Serialised for the same reason bridge_invoke is: this spawns a nested CLI
# subprocess, and concurrent nested CLIs collide on the parent's process
# group and can SIGTERM the caller. One in-flight at a time.
_CRITIQUE_LOCK = asyncio.Lock()

# A critique is a judgement call, and the subject is often long. Truncating
# it would review the opening and silently ignore the rest — the head-keep
# failure this codebase has three separate instances of. So the cap is
# large and, when it bites, it is REPORTED rather than applied quietly.
_MAX_SUBJECT_CHARS = 60_000


def _text(data) -> list[TextContent]:
    if isinstance(data, str):
        return [TextContent(type="text", text=data)]
    return [TextContent(type="text", text=json.dumps(data, indent=2, default=str))]


def get_tools() -> list[Tool]:
    return [
        Tool(
            name="critique",
            description=(
                "Hand a plan, diff, decision or recommendation to a fresh model that "
                "owns none of it, framed as 'a friend sent me this — tell me where he "
                "is wrong'. Returns ranked findings AND a required verdict on whether "
                "the proposal fixes the INSTANCE or the CLASS. Use it before "
                "committing to a plan, before shipping a fix you reasoned your way "
                "into, and whenever you are about to recommend one option over "
                "others. Self-review does not catch scope errors; this does."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "subject": {
                        "type": "string",
                        "description": (
                            "The work to review, verbatim. Pass the thing itself, not a "
                            "summary — a summary is your framing, and your framing is "
                            "what is under review. Omit if using artifact_id."
                        ),
                    },
                    "artifact_id": {
                        "type": "string",
                        "description": (
                            "Review an artifact already in the brain instead of pasting "
                            "its text. Takes precedence over subject."
                        ),
                    },
                    "kind": {
                        "type": "string",
                        "description": "plan | diff | decision | recommendation | code",
                        "default": "proposal",
                    },
                    "context": {
                        "type": "string",
                        "description": (
                            "Constraints the reviewer could not infer. Keep it factual — "
                            "anything persuasive here defeats the purpose."
                        ),
                    },
                    "capability": {
                        "type": "string",
                        "description": (
                            "Bridge routing capability. Defaults to 'quality' — this is a "
                            "judgement task and the fast tier measurably misses things."
                        ),
                        "default": "quality",
                    },
                    "provider": {"type": "string", "description": "Force a provider."},
                },
                "required": [],
            },
        ),
    ]


async def handle_tool(name: str, arguments: dict) -> list[TextContent]:
    if name != "critique":
        return _text({"error": f"unknown tool: {name}"})

    subject = (arguments.get("subject") or "").strip()
    artifact_id = (arguments.get("artifact_id") or "").strip()

    if artifact_id:
        try:
            from okuro.sense.artifacts import artifact_get
            art = await asyncio.to_thread(artifact_get, artifact_id)
            if not art:
                return _text({"ok": False, "error": f"artifact not found: {artifact_id}"})
            subject = art.get("body") or ""
            if art.get("title"):
                subject = f"# {art['title']}\n\n{subject}"
        except Exception as exc:  # pragma: no cover - defensive
            logger.exception("critique: artifact fetch failed")
            return _text({"ok": False, "error": f"artifact fetch failed: {exc}"})

    if not subject.strip():
        return _text({"ok": False,
                      "error": "nothing to critique — pass subject or artifact_id"})

    # Never truncate silently. A review of the first N chars that reports
    # itself as a review of the whole is the same defect class as the
    # notes-extractor body cap.
    truncated = False
    if len(subject) > _MAX_SUBJECT_CHARS:
        subject = subject[:_MAX_SUBJECT_CHARS]
        truncated = True

    from okuro.critic import critique

    async with _CRITIQUE_LOCK:
        result = await asyncio.to_thread(
            critique,
            subject=subject,
            kind=arguments.get("kind") or "proposal",
            context=arguments.get("context") or "",
            capability=arguments.get("capability") or "quality",
            provider=arguments.get("provider"),
        )

    if truncated:
        result["truncated"] = True
        result["truncation_note"] = (
            f"subject exceeded {_MAX_SUBJECT_CHARS} chars and was cut — the tail was "
            f"NOT reviewed. Split it and critique the parts."
        )
    return _text(result)
