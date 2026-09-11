# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: The one libyaml-backed YAML loader for the orchestrator package.
# index:
#   FastLoader
#   def yload
#   def yload_all
# AGENT_HEADER_END -->
"""The one libyaml-backed YAML loader for the orchestrator package.

``yaml.safe_load`` hardcodes the pure-Python parser. On this corpus that is
measured 15x slower than the libyaml C parser (98 task.yaml, 508 KB: 245.1 ms
vs 16.0 ms) — and the orchestrator parses task.yaml / plan.yaml on nearly every
request path, so the tax is per-request and grows with the task count.

``CSafeLoader`` is libyaml's implementation of the SAME SafeLoader semantics;
``tests/orchestrator/test_yaml_loader_equivalence.py`` pins that equivalence on
representative documents and, opt-in, on the whole live corpus.

Why one module rather than the constant repeated per file: the idiom was copied
into state.py, watchdog.py and api/state_reader.py, and the fourth corpus
reader (the deleted ``_collect_all_suggestions``) was written without it and
cost 245 ms of every ``/api/suggestions`` call for months. Copies do not get
swept; a single import does. ``tests/orchestrator/test_yaml_loader_guard.py``
re-derives the offender set from the AST so a new bare call site cannot land
unnoticed.

The fallback keeps a libyaml-less build working (correct, just slow);
``test_libyaml_is_actually_present`` fails loudly on such a host so the
degradation is never silent.
"""

from __future__ import annotations

import yaml

FastLoader = getattr(yaml, "CSafeLoader", yaml.SafeLoader)


def yload(stream):
    """Fast, safe YAML load. Drop-in for ``yaml.safe_load``.

    Accepts a string or an open file object, like ``safe_load`` does.
    """
    return yaml.load(stream, Loader=FastLoader)


def yload_all(stream):
    """Fast, safe multi-document load. Drop-in for ``yaml.safe_load_all``."""
    return yaml.load_all(stream, Loader=FastLoader)
