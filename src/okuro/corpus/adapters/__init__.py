# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Adapter registry — maps a source_type string to its adapter class and
#   builds one from the stored config blob. The single place a new source type
#   is wired in.
# index: def build_adapter | def adapter_types | def describe_types
# AGENT_HEADER_END -->
"""Adapter registry.

Adding a source is ONE entry here plus one module — no migration (config is a
JSON blob), no new table, no new MCP tool, no new search surface. That is the
whole point of the corpus layer: the Nth knowledge source costs an adapter,
not a subsystem.

Imports are lazy so that a broken or dependency-heavy adapter cannot break
`corpus_list` for the others.
"""

from __future__ import annotations

from typing import Any

# source_type → (module path, class name, required config keys)
_REGISTRY: dict[str, tuple[str, str, tuple[str, ...]]] = {
    "confluence": (
        "okuro.corpus.adapters.confluence",
        "ConfluenceAdapter",
        ("base_url", "space_key"),
    ),
    "local_folder": (
        "okuro.corpus.adapters.local_folder",
        "LocalFolderAdapter",
        ("path",),
    ),
}


def adapter_types() -> list[str]:
    """Registered source_type ids."""
    return sorted(_REGISTRY)


def build_adapter(source_type: str, config: dict[str, Any]):
    """Instantiate the adapter for ``source_type`` from a config dict.

    Unknown keys are dropped rather than passed through: config is persisted
    JSON that may outlive an adapter signature, and a stale key should not turn
    every future sync into a TypeError.
    """
    if source_type not in _REGISTRY:
        raise ValueError(
            f"unknown source_type '{source_type}' — registered: {', '.join(adapter_types())}"
        )
    module_path, class_name, required = _REGISTRY[source_type]
    missing = [k for k in required if not config.get(k)]
    if missing:
        raise ValueError(
            f"source_type '{source_type}' requires config key(s): {', '.join(missing)}"
        )

    import importlib
    import inspect

    cls = getattr(importlib.import_module(module_path), class_name)
    accepted = set(inspect.signature(cls.__init__).parameters) - {"self"}
    return cls(**{k: v for k, v in config.items() if k in accepted})


def detect_source(url: str) -> dict:
    """Derive {source_type, config} from a pasted url or path.

    This is what lets the UI ask for ONE field. Adapters are tried in
    _DETECT_ORDER and the first to claim the input wins; local_folder is last
    because it is the permissive one (it accepts anything that resolves to a
    directory), so a more specific adapter must get first refusal.

    Raises ValueError with the registered types when nothing matches — a silent
    fallback to the wrong adapter would surface much later as a confusing
    "space not found".
    """
    import importlib

    u = (url or "").strip()
    if not u:
        raise ValueError("empty url")
    for st in _DETECT_ORDER:
        module_path, class_name, _ = _REGISTRY[st]
        cls = getattr(importlib.import_module(module_path), class_name)
        from_url = getattr(cls, "from_url", None)
        if from_url is None:
            continue
        config = from_url(u)
        if config:
            return {"source_type": st, "config": config}
    raise ValueError(
        f"could not recognise '{u[:120]}' as any known source. "
        f"Registered: {', '.join(adapter_types())}. "
        f"Confluence expects a url containing /spaces/<KEY>; local_folder expects "
        f"a path to an existing directory."
    )


# Specific before permissive — see detect_source.
_DETECT_ORDER = ("confluence", "local_folder")


def describe_types() -> list[dict]:
    """Registry summary for tool descriptions and the /corpora UI."""
    out = []
    for st, (module_path, class_name, required) in sorted(_REGISTRY.items()):
        out.append({
            "source_type": st,
            "required_config": list(required),
            "implementation": f"{module_path}.{class_name}",
        })
    return out
