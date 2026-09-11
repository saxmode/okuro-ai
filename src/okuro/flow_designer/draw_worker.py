# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Out-of-process flow-draw worker — spawns claude, streams JSONL to a sink.
# index: flow_draw_worker
# AGENT_HEADER_END -->
"""Runs the streaming flow generation in a SEPARATE process.

Reading the claude stream on the orchestrator (event loop or a thread) made
the first token take 38-105s: the busy orchestrator holds the GIL, starving
the reader. A separate process has its own GIL, so claude streams at full
speed (~2-3s TTFT). The worker appends each event as a JSONL line to
``sink_path``; the orchestrator's SSE handler tails the file.

Kept dependency-free (no okuro.web import) so the process-pool worker spawns
cheaply — ``layout`` is the one sibling it pulls in, and that module is stdlib
only.
"""

from __future__ import annotations

import json
import os
import subprocess

from okuro.flow_designer.layout import StreamLayout
# Re-exported, not redefined: okuro.flow_designer.ports is the ONE
# implementation, shared with the web draw routes. handover.transform imports
# both names from here, so the names stay bound at this path.
from okuro.flow_designer.ports import norm_edge, norm_node  # noqa: F401

__all__ = ["norm_node", "norm_edge", "flow_draw_worker"]


def flow_draw_worker(cmd, env, envelope, mcp_path, sink_path) -> None:
    """Spawn claude, parse its JSONL deltas, append {k,v} events to sink_path.

    Emits ``{"k":"meta"|"node"|"edge", "v":…}`` per object, ``{"k":"err","v":…}``
    on failure, and a final ``{"k":"done"}``."""
    def emit(o: dict) -> None:
        try:
            with open(sink_path, "a") as f:
                f.write(json.dumps(o) + "\n")
        except OSError:
            pass

    # The agent guesses coordinates while it writes, so its nodes land on top
    # of each other as soon as a title is longer than its guessed column. The
    # allocator keeps a sane position and finds room for the rest — per node,
    # because the stream must render one the moment it is generated and the
    # edge that would give it a layer has not been emitted yet.
    slots = StreamLayout()

    proc = None
    try:
        proc = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, env=env, text=True, bufsize=1,
        )
        proc.stdin.write(envelope + "\n")
        proc.stdin.flush()
        proc.stdin.close()
        buf = ""
        for raw in proc.stdout:  # blocking line reads — this process' own GIL
            try:
                ev = json.loads(raw)
            except Exception:
                continue
            ty = ev.get("type")
            if ty == "result":
                break
            if ty != "stream_event":
                continue
            e = ev.get("event", {})
            if e.get("type") != "content_block_delta":
                continue
            buf += e.get("delta", {}).get("text", "")
            while "\n" in buf:
                ln, buf = buf.split("\n", 1)
                ln = ln.strip().rstrip(",")
                if not ln or not ln.startswith("{"):
                    continue
                try:
                    obj = json.loads(ln)
                except Exception:
                    continue
                if isinstance(obj.get("meta"), dict):
                    emit({"k": "meta", "v": obj["meta"]})
                elif isinstance(obj.get("node"), dict):
                    emit({"k": "node", "v": slots.place(norm_node(obj["node"]))})
                elif isinstance(obj.get("edge"), dict):
                    emit({"k": "edge", "v": norm_edge(obj["edge"])})
    except Exception as exc:  # noqa: BLE001
        emit({"k": "err", "v": str(exc)})
    finally:
        try:
            if proc:
                proc.terminate()
                # Reap it — without wait() the claude child lingers as a
                # <defunct> zombie held by this pool worker, and a leaked
                # stdout fd can keep the next worker's read loop from ever
                # seeing EOF (the 0-byte-sink hang).
                try:
                    proc.wait(timeout=5)
                except Exception:
                    proc.kill()
        except Exception:
            pass
        try:
            os.remove(mcp_path)
        except OSError:
            pass
        emit({"k": "done"})
