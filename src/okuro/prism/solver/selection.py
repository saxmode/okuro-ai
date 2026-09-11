# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: PRISM v4 W2 — per-deck grid-family SELECTION (answer 7). The solver
#   CONSUMES a family selection as an input parameter; the selection intelligence
#   proper is W3/W5 pipeline territory (content character + audience mood come
#   from the audience engine). This module supplies a documented, deterministic
#   DEFAULT heuristic so the solver is runnable and the entry-gate params are
#   actually consumed — it is a placeholder the pipeline overrides, not the final
#   selector.
# index:
#   select_family(content_character, audience_mood) / CONTENT_BIAS / MOOD_BIAS
# AGENT_HEADER_END -->
"""Per-deck grid-family selection (the answer-7 input to the solver).

DEFECT 1 (W2 pre-check) requires the solver to consume a per-deck family
selection AND to state where it is supplied. It is supplied here — a deterministic
scoring over two content-side signals the audience engine already produces:

  content_character in {analytical, narrative, conceptual, minimal, editorial}
  audience_mood     in {board, executive, technical, creative, reading}

Mapping rationale (answer 7 'content character + audience mood', grounded in the
family research): analytical/board -> swiss (objective modular grid); minimal/
executive -> japanese (Ma, premium calm); conceptual/creative -> bauhaus (kinetic
contrast); narrative/reading -> editorial (column/measure). This is intentionally
simple and OVERRIDABLE — W3/W5 owns the real selector; the solver only honours
whatever family name it is handed.
"""
from __future__ import annotations

from okuro.prism.solver.families import family_names

# content character -> per-family affinity.
CONTENT_BIAS: dict[str, dict[str, float]] = {
    "analytical": {"swiss": 1.0, "editorial": 0.4, "bauhaus": 0.2, "japanese": 0.3},
    "narrative":  {"editorial": 1.0, "swiss": 0.3, "japanese": 0.4, "bauhaus": 0.2},
    "conceptual": {"bauhaus": 1.0, "swiss": 0.4, "editorial": 0.3, "japanese": 0.4},
    "minimal":    {"japanese": 1.0, "swiss": 0.5, "editorial": 0.3, "bauhaus": 0.1},
    "editorial":  {"editorial": 1.0, "swiss": 0.4, "japanese": 0.3, "bauhaus": 0.2},
}

# audience mood -> per-family affinity.
MOOD_BIAS: dict[str, dict[str, float]] = {
    "board":     {"swiss": 0.8, "japanese": 0.7, "editorial": 0.4, "bauhaus": 0.2},
    "executive": {"japanese": 0.9, "swiss": 0.6, "editorial": 0.4, "bauhaus": 0.2},
    "technical": {"swiss": 1.0, "editorial": 0.4, "bauhaus": 0.3, "japanese": 0.3},
    "creative":  {"bauhaus": 1.0, "japanese": 0.5, "editorial": 0.4, "swiss": 0.3},
    "reading":   {"editorial": 1.0, "swiss": 0.4, "japanese": 0.4, "bauhaus": 0.2},
}

_DEFAULT = "swiss"


def select_family(
    content_character: str = "analytical", audience_mood: str = "technical"
) -> str:
    """Deterministic default family selection. Ties broken by family_names() order
    (alphabetical) for reproducibility."""
    names = family_names()
    cb = CONTENT_BIAS.get(content_character, {})
    mb = MOOD_BIAS.get(audience_mood, {})
    if not cb and not mb:
        return _DEFAULT
    scores = {n: cb.get(n, 0.0) + mb.get(n, 0.0) for n in names}
    best = max(names, key=lambda n: (scores[n], -names.index(n)))
    return best


__all__ = ["select_family", "CONTENT_BIAS", "MOOD_BIAS"]
