# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Architectural-layer classifier — tags each file with a layer for the in_layer KG predicate.
# index:
#   imports
#   DEFAULT_LAYERS
#   DEFAULT_RULES
#   class LayerRule
#   class LayerClassifier
#   def classify_file
#   def classify_project
#   def llm_classify_batch
# AGENT_HEADER_END -->
"""Architectural-layer classifier.

Heuristic-first: per-rule regex on POSIX-style path + filename, applied in
priority order. Files unmatched by every rule fall through to ``unknown``.

LLM fallback (opt-in): unknowns are batched 20–30 at a time and asked for
a layer assignment via okuro.bridge.invoke. Off by default — runs only
when ``use_llm_fallback=True`` is passed AND the bridge has at least one
provider configured.

Per-project overrides: if ``.okuro/layers.yaml`` exists in the project
root, its rules are loaded AHEAD of the defaults so projects can rename,
add, or reorder layers without forking this module.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Iterator

log = logging.getLogger(__name__)


DEFAULT_LAYERS: tuple[str, ...] = (
    "ui",
    "api",
    "domain",
    "data",
    "infra",
    "test",
    "docs",
    "config",
)


@dataclass(frozen=True)
class LayerRule:
    layer: str
    pattern: str
    priority: int = 50

    def matches(self, posix_path: str) -> bool:
        return bool(re.search(self.pattern, posix_path, flags=re.IGNORECASE))


# Order matters: tests + docs first so they win over a UI-looking test file.
DEFAULT_RULES: tuple[LayerRule, ...] = (
    LayerRule("test", r"(^|/)(tests?|spec|__tests__|fixtures?)(/|$)", priority=100),
    LayerRule("test", r"(_test|test_|\.test|\.spec)\.[a-z]+$", priority=100),
    LayerRule("docs", r"(^|/)docs?(/|$)", priority=95),
    LayerRule("docs", r"\.(md|mdx|rst|txt)$", priority=20),
    LayerRule("config", r"(^|/)(\.config|config|conf|configs)(/|$)", priority=80),
    LayerRule("config", r"\.(ya?ml|toml|ini|cfg|env)$", priority=15),
    LayerRule("config", r"(^|/)(pyproject\.toml|package\.json|tsconfig\.json|Dockerfile|docker-compose\.ya?ml)$", priority=85),
    LayerRule("infra", r"(^|/)(deploy|infra|terraform|ansible|kubernetes|k8s|helm|charts|\.github|ci|\.circleci|nginx|caddy|systemd)(/|$)", priority=80),
    LayerRule("data", r"(^|/)(migrations?|schema|models?|orm|db|database)(/|$)", priority=75),
    LayerRule("data", r"\.sql$", priority=70),
    LayerRule("api", r"(^|/)(api|routes|routers|endpoints?|controllers?|handlers?)(/|$)", priority=70),
    LayerRule("api", r"(^|/)(graphql|grpc|rest|webhooks?)(/|$)", priority=65),
    LayerRule("ui", r"(^|/)(ui|frontend|web|views?|pages?|components?|templates?|styles?)(/|$)", priority=65),
    LayerRule("ui", r"\.(tsx|jsx|vue|svelte|css|scss|sass|html)$", priority=30),
    LayerRule("domain", r"(^|/)(domain|core|business|services?|usecases?|use_cases?|entities|aggregates?)(/|$)", priority=60),
)


@dataclass
class LayerClassifier:
    rules: list[LayerRule] = field(default_factory=lambda: list(DEFAULT_RULES))
    layers: tuple[str, ...] = DEFAULT_LAYERS

    def classify(self, relpath: str) -> str:
        """Return the highest-priority matching layer, or 'unknown'."""
        posix = relpath.replace("\\", "/")
        best: LayerRule | None = None
        for rule in self.rules:
            if rule.matches(posix):
                if best is None or rule.priority > best.priority:
                    best = rule
        return best.layer if best else "unknown"

    @classmethod
    def from_project(cls, root: Path) -> "LayerClassifier":
        """Load rules from ``<root>/.okuro/layers.yaml`` if present.

        YAML schema (all fields optional):
            layers: [ui, api, domain, data, infra, test, docs, config]
            rules:
              - layer: ui
                pattern: '(^|/)widgets(/|$)'
                priority: 70
        """
        cfg = root / ".okuro" / "layers.yaml"
        if not cfg.exists():
            return cls()
        try:
            import yaml
            data = yaml.safe_load(cfg.read_text()) or {}
        except Exception as exc:  # noqa: BLE001
            log.warning("layer config %s unreadable: %s", cfg, exc)
            return cls()

        layers = tuple(data.get("layers") or DEFAULT_LAYERS)
        extra = []
        for entry in data.get("rules") or []:
            try:
                extra.append(
                    LayerRule(
                        layer=entry["layer"],
                        pattern=entry["pattern"],
                        priority=int(entry.get("priority", 50)),
                    )
                )
            except (KeyError, TypeError, ValueError):
                continue
        # Custom rules come FIRST so they win ties of equal priority via
        # iteration order in classify().
        return cls(rules=extra + list(DEFAULT_RULES), layers=layers)


def classify_file(
    relpath: str,
    *,
    classifier: LayerClassifier | None = None,
) -> str:
    classifier = classifier or LayerClassifier()
    return classifier.classify(relpath)


def classify_project(
    root: Path,
    files: Iterable[str],
    *,
    classifier: LayerClassifier | None = None,
    use_llm_fallback: bool = False,
    llm_batch_size: int = 25,
) -> dict[str, str]:
    """Classify every file under ``root``. Returns {relpath: layer}.

    Unknowns may be sent to the LLM in batches when ``use_llm_fallback``
    is true AND a bridge provider is configured.
    """
    classifier = classifier or LayerClassifier.from_project(root)
    out: dict[str, str] = {}
    unknowns: list[str] = []
    for rel in files:
        layer = classifier.classify(rel)
        out[rel] = layer
        if layer == "unknown":
            unknowns.append(rel)

    if use_llm_fallback and unknowns:
        for batch in _chunks(unknowns, llm_batch_size):
            assignments = llm_classify_batch(batch, allowed=classifier.layers)
            for rel, layer in assignments.items():
                if layer in classifier.layers:
                    out[rel] = layer

    return out


def _chunks(seq: list[str], n: int) -> Iterator[list[str]]:
    for i in range(0, len(seq), n):
        yield seq[i : i + n]


def _resolve_invoke():
    """Return ``okuro.bridge.invoke`` callable, or None if unavailable.

    Indirected so tests can monkey-patch a single attribute on this module
    without caring about the re-export shape of okuro.bridge.
    """
    try:
        from okuro.bridge import invoke as bridge_invoke
        return bridge_invoke
    except ImportError:
        return None


_PROMPT = """You are tagging files in a codebase with one architectural layer each.

Allowed layers: {layers}

Pick the SINGLE best layer for each file. Reply with ONE JSON object on a
single line mapping each path to a layer. No prose, no markdown, no keys
outside the allowed layers.

Files:
{paths}
"""


def llm_classify_batch(
    paths: list[str],
    *,
    allowed: tuple[str, ...] = DEFAULT_LAYERS,
    provider: str | None = None,
    model: str | None = None,
) -> dict[str, str]:
    """Ask the bridge to classify a batch of unknown files.

    Returns {path: layer}. Silently returns ``{}`` when the bridge has no
    provider configured or the response is unparseable — callers should
    treat the absence of a key as ``unknown``.
    """
    if not paths:
        return {}
    invoke = _resolve_invoke()
    if invoke is None:
        return {}

    prompt = _PROMPT.format(
        layers=", ".join(allowed),
        paths="\n".join(f"- {p}" for p in paths),
    )
    result = invoke(
        prompt=prompt,
        capability="quick_classify",
        provider=provider,
        model=model,
        system_prompt="Reply with one line of JSON. No surrounding text.",
    )
    if not result.get("success"):
        log.debug("layer LLM fallback unavailable: %s", result.get("error"))
        return {}

    raw = (result.get("output") or "").strip()
    # Trim common markdown wrappers
    if raw.startswith("```"):
        raw = raw.strip("`")
        if raw.lower().startswith("json"):
            raw = raw[4:].lstrip()
    # Pull out the first {...} block defensively
    m = re.search(r"\{.*\}", raw, flags=re.DOTALL)
    if not m:
        return {}
    try:
        parsed = json.loads(m.group(0))
        if isinstance(parsed, dict):
            return {str(k): str(v) for k, v in parsed.items()}
    except json.JSONDecodeError:
        pass
    return {}


def write_layers_to_kg(
    assignments: dict[str, str],
    *,
    project: str | None,
) -> int:
    """Persist a layer map as ``in_layer`` triples. Returns count written."""
    from okuro.sense.kg_code import set_file_layer

    written = 0
    for relpath, layer in assignments.items():
        if layer == "unknown":
            continue
        set_file_layer(relpath, layer, project=project)
        written += 1
    return written
