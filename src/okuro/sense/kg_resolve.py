# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Fuzzy entity resolver — maps an approximate term to the canonical KG name-strings before the lexical kg_query runs.
# index: imports | def _normalize | def _pool | def _fuzzy_score | def resolve_entity
# AGENT_HEADER_END -->
"""Fuzzy-Entity-Resolver — the layer in front of ``kg_query``.

``kg_query`` matches ``WHERE subject = ?`` — exact string equality. Triples
store subject/object as raw strings (no FK to ``kg_entities``), so the graph
is only reachable by the *exact* string used at write time. ``"Ale"`` misses
``"Alex Reiter"``; a typo misses everything.

This module resolves an approximate ``term`` to the canonical entity
**name-strings** actually present in ``kg_entities`` — a *name-set*, not an
id, because triples key on the name string. Callers feed the resolved
name(s) back into the existing lexical query.

Cascade (cheap → expensive, short-circuits on a confident exact hit):

    0 exact       name == term                         score 1.00
    1 normalized  NFKC + casefold + whitespace-collapse score 0.99
    2 alias       properties.aliases contains term      score 0.97
    3 fuzzy       difflib ratio + token-subset boost    score < 0.97

Stdlib ``difflib`` only — no new dependency (``sense/rules.py`` already uses
it), no model, no network. Semantic resolution (embedding cosine over entity
names) is a deliberate future tier: entity names are 2-3 tokens where short-
string embeddings are noisy, and okuro's embed path is not cleanly reusable
here. ``resolve_entity`` is the seam — add a tier-4 pass before the return.
"""

from __future__ import annotations

import difflib
import json
import re
import unicodedata

_WS = re.compile(r"\s+")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _normalize(s: str) -> str:
    """NFKC + casefold + collapse whitespace. The tier-1 equality key."""
    return _WS.sub(" ", unicodedata.normalize("NFKC", s).casefold().strip())


# Codegraph entities — names are long file-path::symbol#Lx-y strings, ~70% of
# the table. They pollute name resolution with buried-substring false matches
# and are never a meaningful target for a human/concept lookup. Excluded from
# the default pool; still reachable by asking for the type explicitly.
_NOISE_TYPES = ("code_ref", "code_anchor")


def _pool(db, etype: str | None, project: str | None) -> list[dict]:
    """Candidate entities: (id, name, type, properties), optionally filtered.

    Full-table at okuro's scale (~14k rows) is a cheap C-side scan and this
    only runs on a lexical miss. If the pool grows past ~100k, pre-filter by
    token/first-letter before scoring — that's the optimization seam.
    """
    where = ["1=1"]
    params: list = []
    if etype:
        where.append("type = ?")
        params.append(etype)
    else:
        where.append(f"type NOT IN ({','.join('?' * len(_NOISE_TYPES))})")
        params.extend(_NOISE_TYPES)
    if project:
        where.append("(project = ? OR project IS NULL)")
        params.append(project)
    sql = f"SELECT id, name, type, properties FROM kg_entities WHERE {' AND '.join(where)}"
    return [dict(r) for r in db.fetchall(sql, tuple(params))]


def _fuzzy_score(nterm: str, nname: str) -> float:
    """Similarity in [0, 1) for two already-normalized strings.

    SequenceMatcher ratio, lifted when ``term``'s tokens are a subset of
    ``name``'s (``"alex"`` ⊂ ``"alex reiter"`` — the core recall case,
    which raw ratio underscores at ~0.6). Capped below 1.0 and below the
    normalized/alias tiers so exact-class matches always outrank fuzzy ones.
    """
    base = difflib.SequenceMatcher(None, nterm, nname).ratio()
    t_tok = set(nterm.split())
    n_tok = set(nname.split())
    if t_tok and t_tok <= n_tok:
        # full token containment — strong signal regardless of length gap
        base = max(base, 0.85 + 0.10 * (len(t_tok) / max(len(n_tok), 1)))
    elif t_tok and n_tok:
        # partial: term tokens that match a name token exactly, or (>=3 chars)
        # appear as a substring/prefix of one — catches "Ale"->"Alex".
        # Scored by term-recall × name-coverage so a match is only strong when
        # it explains MOST of the term AND most of the name. This kills two
        # false-tie classes: a term buried in a long path (low name-coverage)
        # and a short name matching only part of the term (low term-recall,
        # e.g. "frontend enginee" vs the bare "engineer" entity).
        matched = 0
        for t in t_tok:
            if t in n_tok or (len(t) >= 3 and any(t in nt for nt in n_tok)):
                matched += len(t)
        if matched:
            term_recall = matched / max(sum(len(t) for t in t_tok), 1)
            name_cover = matched / max(len(nname), 1)
            base = max(base, min(0.95, 0.50 + 0.45 * term_recall * name_cover))
    return min(base, 0.96)


# ---------------------------------------------------------------------------
# Public
# ---------------------------------------------------------------------------


def resolve_entity(term: str, type: str | None = None,
                   project: str | None = None, limit: int = 5,
                   threshold: float = 0.55) -> list[dict]:
    """Resolve ``term`` to ranked canonical entity candidates.

    Returns ``[{name, entity_id, type, score, tier, matched}]`` sorted by
    score desc. ``matched`` is the string that produced the hit (the entity
    name, or the alias for tier 2). Empty list when nothing clears
    ``threshold``. On an exact/normalized/alias hit the fuzzy tier is skipped
    — those candidates are returned alone.
    """
    if not term or not term.strip():
        return []
    from okuro.db import get_db
    db = get_db()

    nterm = _normalize(term)
    pool = _pool(db, type, project)

    exact: list[dict] = []
    fuzzy: list[dict] = []

    for e in pool:
        name = e["name"]
        nname = _normalize(name)

        # tier 0/1 — exact then normalized-exact
        if name == term:
            exact.append({**_cand(e, 1.00, "exact", name)})
            continue
        if nname == nterm:
            exact.append({**_cand(e, 0.99, "normalized", name)})
            continue

        # tier 2 — alias (properties.aliases list)
        alias_hit = _alias_match(e.get("properties"), term, nterm)
        if alias_hit is not None:
            exact.append({**_cand(e, 0.97, "alias", alias_hit)})
            continue

        # tier 3 — fuzzy (deferred; only materialized if no exact-class hit)
        score = _fuzzy_score(nterm, nname)
        if score >= threshold:
            fuzzy.append({**_cand(e, round(score, 4), "fuzzy", name)})

    if exact:
        exact.sort(key=lambda c: c["score"], reverse=True)
        return exact[:limit]

    fuzzy.sort(key=lambda c: c["score"], reverse=True)
    return fuzzy[:limit]


def _cand(entity: dict, score: float, tier: str, matched: str) -> dict:
    return {
        "name": entity["name"],
        "entity_id": entity["id"],
        "type": entity["type"],
        "score": score,
        "tier": tier,
        "matched": matched,
    }


def _alias_match(properties, term: str, nterm: str) -> str | None:
    """Return the alias string if ``properties.aliases`` contains ``term``."""
    if not properties:
        return None
    try:
        props = properties if isinstance(properties, dict) else json.loads(properties)
    except (ValueError, TypeError):
        return None
    aliases = props.get("aliases") or props.get("alias") or []
    if isinstance(aliases, str):
        aliases = [aliases]
    for a in aliases:
        if isinstance(a, str) and _normalize(a) == nterm:
            return a
    return None
