# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: okuro·prism compiler LLM stage helper — a thin, schema-validated wrapper
#   over okuro.bridge.invoke.invoke used by every LLM stage (mine/project/outline/
#   author/compose). Does exactly one thing well: invoke -> lenient JSON extract ->
#   jsonschema validate -> BOUNDED RE-ASK on failure (feed the validation error
#   back). No new client, no styling, no provider assumptions.
# index: LLMError | call_json | _validate
# AGENT_HEADER_END -->
"""Schema-validated LLM call with bounded re-ask.

Why a wrapper and not raw ``invoke``: ``response_format="json"`` is honoured only
by local-http providers (CLI providers ignore it), so structure can NEVER be
assumed from the transport. The single robust contract is: extract the JSON span,
validate it against the stage's schema, and on failure re-ask the SAME model with
the concrete validation error appended — up to ``max_retries`` times. This is the
"schema-validated LLM stage, bounded re-ask" the brief mandates, and it is
provider-agnostic (DP10).

Reuses the repo's JSON extractors (``prism.generate._extract_json_span`` /
``_lenient_json_loads``) so parsing behaviour matches the rest of prism.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)


class LLMError(RuntimeError):
    """A stage's model call failed irrecoverably (bridge down or never valid)."""


def _validate(data: Any, schema: dict[str, Any]) -> Optional[str]:
    """Return None if ``data`` satisfies ``schema``, else a short error string."""
    from jsonschema import Draft202012Validator

    errors = sorted(Draft202012Validator(schema).iter_errors(data), key=lambda e: e.path)
    if not errors:
        return None
    e = errors[0]
    loc = "/".join(str(p) for p in e.path) or "<root>"
    return f"at {loc}: {e.message}"


def call_json(
    *,
    system_prompt: str,
    user_prompt: str,
    schema: dict[str, Any],
    stage: str,
    provider: Optional[str] = None,
    capability: str = "standard",
    timeout: int = 300,
    thinking_tokens: Optional[int] = None,
    max_retries: int = 2,
    open_ch: str = "{",
    close_ch: str = "}",
    postprocess: Optional[Callable[[Any], Any]] = None,
    reask_example: Optional[Any] = None,
    invoke_fn: Optional[Callable[..., Any]] = None,
) -> Any:
    """Invoke a model and return JSON that validates against ``schema``.

    Bounded re-ask: on a parse or schema failure, re-invoke with the error text
    appended (``max_retries`` extra attempts). Raises ``LLMError`` if the bridge
    call itself fails (retrying won't help) or if no attempt ever validates.

    ``invoke_fn`` is injectable so schema-contract tests can drive the wrapper
    with a scripted model instead of the live bridge.
    """
    if invoke_fn is None:
        from okuro.bridge.invoke import invoke as invoke_fn  # type: ignore
    from okuro.prism.generate import _extract_json_span, _lenient_json_loads

    prompt = user_prompt
    last_err = "no attempt made"

    _prev = os.environ.get("MAX_THINKING_TOKENS")
    if thinking_tokens is not None:
        os.environ["MAX_THINKING_TOKENS"] = str(thinking_tokens)
    try:
        for attempt in range(max_retries + 1):
            res = invoke_fn(
                prompt=prompt, system_prompt=system_prompt, provider=provider,
                capability=capability, timeout=timeout, response_format="json",
            )
            if not (isinstance(res, dict) and res.get("success")):
                # Bridge/provider failure. A TRANSIENT one (socket drop, provider
                # hiccup) is fixed by simply calling again — measured live on
                # task-20260727-005417, where a one-shot raise here lost a whole
                # understand chunk and 45% of a book-length source. A PERMANENT
                # failure (no provider configured) just fails the remaining
                # attempts in seconds — retrying costs nothing. Re-invoke with the
                # ORIGINAL prompt: the model never saw anything, a re-ask would lie.
                last_err = (f"invoke failed: "
                            f"{(res or {}).get('error') if isinstance(res, dict) else res}")
                logger.warning("%s: attempt %d %s", stage, attempt + 1, last_err)
                prompt = user_prompt
                continue
            raw = res.get("output") or ""
            try:
                data = _lenient_json_loads(_extract_json_span(raw, open_ch, close_ch))
            except (ValueError, KeyError) as exc:
                last_err = f"not valid JSON ({exc})"
                logger.warning("%s: attempt %d parse failed: %s", stage, attempt + 1, last_err)
                prompt = _reask(user_prompt, last_err, reask_example)
                continue
            if postprocess is not None:
                data = postprocess(data)
            err = _validate(data, schema)
            if err is None:
                if attempt:
                    logger.info("%s: validated on re-ask attempt %d", stage, attempt + 1)
                return data
            last_err = err
            logger.warning("%s: attempt %d schema-invalid: %s", stage, attempt + 1, err)
            prompt = _reask(user_prompt, err, reask_example)
    finally:
        if thinking_tokens is not None:
            if _prev is None:
                os.environ.pop("MAX_THINKING_TOKENS", None)
            else:
                os.environ["MAX_THINKING_TOKENS"] = _prev

    raise LLMError(f"{stage}: no valid response after {max_retries + 1} attempts "
                   f"(last error: {last_err})")


def _reask(original: str, error: str, example: Optional[Any] = None) -> str:
    msg = (
        f"{original}\n\n---\nYour previous reply was rejected by the schema "
        f"validator at this exact path:\n  {error}\n"
    )
    if example is not None:
        msg += ("A minimal VALID example of the required structure (fill it with the "
                "real claim data, keep every key and non-empty array shown):\n"
                f"  {json.dumps(example, ensure_ascii=False)}\n")
    msg += "Return ONLY the corrected JSON object. No prose, no markdown fences."
    return msg


def dumps(obj: Any) -> str:
    """Stable JSON for embedding IR back into a prompt (sorted, compact)."""
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, default=str)
