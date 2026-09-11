# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Local model invocation via HTTP API (e.g. llama-cpp, vLLM).
# index: imports | def resolve_endpoint | def invoke_local | def check_health | def _api_post
# AGENT_HEADER_END -->
"""Local model invocation via HTTP API (e.g. llama-cpp, vLLM)."""

import json
import time
import urllib.request
import urllib.error


def resolve_endpoint(model: str, default: str) -> str:
    """Endpoint to call for ``model`` — prefer a live registered engine.

    The engine runner publishes ``model_id → endpoint`` to the durable
    registry (``okuro.inference.engine_registry``) when it launches a bundle.
    If ``model`` matches a running engine, target that (a dynamically-launched
    server may bind a different port than the static config endpoint);
    otherwise fall back to ``default``. The registry is optional infrastructure
    — any failure (no db, import error) degrades to the static endpoint.
    """
    if not model:
        return default
    try:
        from okuro.inference.engine_registry import EngineRegistry

        return EngineRegistry().resolve(model) or default
    except Exception:
        return default


def invoke_local(
    endpoint: str,
    model: str,
    prompt: str,
    system_prompt: str | None = None,
    timeout: int = 120,
    temperature: float = 0.7,
    response_format: str = "text",
    json_schema: dict | None = None,
    tools: list | None = None,
    stop: list | None = None,
    sampling: dict | None = None,
    images: list[str] | None = None,
) -> dict:
    """Invoke a local model via OpenAI-compatible HTTP API.

    Args:
        endpoint: Base URL (e.g. http://127.0.0.1:13207)
        model: Model name or alias
        prompt: Prompt text
        system_prompt: Optional system prompt
        timeout: Timeout in seconds
        temperature: Sampling temperature (was hardcoded 0.7)
        response_format: "text" | "json_object" | "json_schema". Enables
            server-side constrained decoding so JSON-parsing call sites get
            guaranteed-parseable output instead of prompt-coaxed text.
        json_schema: OpenAI json_schema payload, honored when
            response_format == "json_schema".
        tools: OpenAI tool specs for function-calling. None = no tools.
        stop: Stop sequences (e.g. a model's EOS token from its prompting
            block). None/empty = server default.
        sampling: Extra OpenAI sampling params (top_p, top_k, min_p) — the
            model-conditioned optimizer's tuned values. Unknown keys ignored.
        images: Validated absolute image paths (see okuro.bridge.vision). When
            given, the user message becomes an OpenAI multimodal content array
            instead of a plain string. The SERVED model must be a VLM — a text
            model will either reject the parts or ignore them and answer
            confidently about nothing, which is why routing never sends the
            "vision" capability here.

    Returns:
        Result dict with success, output, duration, error, tool_calls, and
        (when a JSON response_format was requested and parseable) parsed.
    """
    start_time = time.time()

    try:
        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        if images:
            from .vision import to_content_parts

            messages.append({"role": "user", "content": to_content_parts(prompt, images)})
        else:
            messages.append({"role": "user", "content": prompt})

        payload: dict = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
        }
        if stop:
            payload["stop"] = stop
        if sampling:
            for k in ("top_p", "top_k", "min_p"):
                if sampling.get(k) is not None:
                    payload[k] = sampling[k]
        if response_format == "json_object":
            payload["response_format"] = {"type": "json_object"}
        elif response_format == "json_schema" and json_schema:
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": json_schema,
            }
        if tools:
            payload["tools"] = tools

        result = _api_post(
            f"{endpoint}/v1/chat/completions",
            payload,
            timeout=timeout,
        )

        if "error" in result:
            return {
                "success": False,
                "output": "",
                "duration": round(time.time() - start_time, 2),
                "error": f"Completion failed: {result['error']}",
                "tool_calls": [],
            }

        output = ""
        tool_calls: list = []
        choices = result.get("choices", [])
        if choices:
            message = choices[0].get("message", {}) or {}
            output = message.get("content", "") or ""
            tool_calls = message.get("tool_calls", []) or []

        out: dict = {
            "success": True,
            "output": output,
            "duration": round(time.time() - start_time, 2),
            "error": None,
            "tool_calls": tool_calls,
        }

        # Surface parsed JSON when a JSON format was requested and the body
        # is parseable — callers can use result["parsed"] instead of
        # re-parsing result["output"]. Never raises on malformed output.
        if response_format in ("json_object", "json_schema") and output:
            try:
                out["parsed"] = json.loads(output)
            except (ValueError, TypeError):
                out["parsed"] = None

        return out

    except Exception as e:
        return {
            "success": False,
            "output": "",
            "duration": round(time.time() - start_time, 2),
            "error": str(e),
            "tool_calls": [],
        }


def check_health(endpoint: str) -> dict:
    """Check local inference daemon health."""
    try:
        req = urllib.request.Request(f"{endpoint}/health", method="GET")
        with urllib.request.urlopen(req, timeout=2) as resp:
            data = json.loads(resp.read())
            return {"healthy": True, "data": data}
    except Exception as e:
        return {"healthy": False, "error": str(e)}


def check_http_ok(url: str, timeout: int = 2) -> dict:
    """Poll an engine readiness URL: healthy iff it answers HTTP 2xx.

    Different engines expose readiness differently — vLLM/llama-server have
    ``/health``, llama-cpp-python only answers ``/v1/models`` once the model
    is loaded. The engine runner composes ``endpoint + health_path`` and calls
    this, so one probe covers every engine.
    """
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return {"healthy": 200 <= resp.status < 300}
    except Exception as e:
        return {"healthy": False, "error": str(e)}


def stream_chat(
    endpoint: str,
    model: str,
    messages: list,
    *,
    temperature: float = 0.7,
    timeout: int = 300,
):
    """Stream an OpenAI-compatible chat completion, yielding content deltas.

    POSTs ``stream: true`` to ``/v1/chat/completions`` and parses the SSE
    ``data:`` frames, yielding each ``choices[0].delta.content`` token as it
    arrives. ``messages`` is the full conversation ([{role, content}, ...]) so
    the caller gets multi-turn chat. Raises on connection/HTTP failure; the
    caller decides how to surface a mid-stream error.
    """
    payload = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "stream": True,
    }
    req = urllib.request.Request(
        f"{endpoint}/v1/chat/completions",
        data=json.dumps(payload).encode(),
        method="POST",
        headers={"Content-Type": "application/json", "Accept": "text/event-stream"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        for raw in resp:
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                return
            try:
                obj = json.loads(data)
            except ValueError:
                continue
            delta = (obj.get("choices") or [{}])[0].get("delta", {}).get("content")
            if delta:
                yield delta


def _api_post(url: str, data: dict, timeout: int = 30) -> dict:
    """POST JSON to URL, return parsed response."""
    body = json.dumps(data).encode()
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        try:
            error_body = json.loads(e.read())
            return {"error": error_body.get("detail", str(e))}
        except Exception:
            return {"error": str(e)}
    except Exception as e:
        return {"error": str(e)}
