# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Task-to-project resolver — maps task hints to project slugs.
# index: def resolve_project | def resolve_role
# AGENT_HEADER_END -->
"""Task-to-project resolver — maps task hints to project slugs."""

import json
import logging
import re

log = logging.getLogger("okuro.sense.bootstrap.resolver")

# Descriptions that carry no signal. Auto-registration stamps the same string
# onto most projects (57 of 78 active, measured 2026-07-19), so scoring them
# semantically produces a large exact-tie block that row order then resolves —
# an arbitrary project bound with full confidence.
_PLACEHOLDER_DESCRIPTIONS = frozenset({
    "auto-registered by agent",
    "auto-registered",
})

_STOPWORDS = frozenset({
    "the", "a", "an", "and", "or", "for", "to", "in", "of", "with", "is", "it",
    # 'by' and 'agent' come from the auto-registration boilerplate itself, so
    # any hint mentioning an agent scored 2 against every placeholder row.
    "by", "agent", "on", "at", "this", "that", "my", "our",
    # Ordinary English connective vocabulary. Measured 2026-09-16: the hint
    # "GPUs not detected after sudo apt upgrade ... NVIDIA driver/kernel
    # mismatch" bound the RETIRED project `tm-illustrator` on the overlap
    # {"after", "not", "—"} — three words that describe no subject matter.
    "not", "no", "after", "before", "when", "while", "then", "than", "as",
    "be", "am", "are", "was", "were", "been", "being", "has", "have", "had",
    "do", "does", "did", "will", "would", "can", "could", "should", "may",
    "from", "into", "over", "under", "about", "against", "between", "during",
    "without", "within", "through", "up", "down", "out", "off", "again",
    "all", "any", "both", "each", "more", "most", "other", "some", "such",
    "only", "own", "same", "so", "too", "very", "just", "also", "still",
    "here", "there", "these", "those", "they", "them", "their", "its",
    "what", "which", "who", "whom", "whose", "why", "how", "if", "but",
    "new", "old", "one", "two", "now", "via", "per", "use", "used", "using",
    # Short task verbs. At a 3-character floor these appear in half the hints
    # and in many descriptions, and they say nothing about WHICH project.
    "fix", "run", "add", "set", "get", "put", "see", "let", "way", "end",
    "top", "job", "make", "made", "need", "want", "help", "keep", "show",
    "take", "give", "find", "look", "call", "said", "say", "went", "goes",
})

# Tokens that carry subject matter. Punctuation-only tokens ("—", "->") are
# not evidence, whatever tier is asking. The floor is 3, not 4, because this
# domain's most distinctive words are three-letter acronyms — gpu, llm, api,
# svg, ssh — and the stopword list plus the document-frequency ceiling below
# are what actually hold back common short words.
_MIN_WORD_LEN = 3
_WORD_RE = re.compile(r"[a-z0-9][a-z0-9._/-]*")

# Hints and descriptions disagree on number: a hint says "GPUs not detected,
# driver mismatch", the description says "two NVIDIA cards, GPU broker,
# drivers, mismatches". Without folding plurals those share exactly one word
# and the tier declines a project that plainly matches.
_PLURAL_ES_STEMS = ("s", "x", "z", "ch", "sh")
# Endings that are not a plural -s. "us" is deliberately NOT here: it would
# leave "gpus" unfolded against a description saying "GPU", and gpu is this
# host's central noun. The cost is that "status" folds to "statu" — harmless,
# because both sides fold the same way.
_KEEPS_TRAILING_S = ("ss", "is", "as")

# A word appearing in this share of describable descriptions is vocabulary of
# the corpus, not of a project ("okuro", "deck", "agent"), so it cannot
# distinguish one project from another.
_MAX_DOC_FREQUENCY = 0.20

# Tier 4 must clear the same two bars tiers 1 and 5 already clear: enough
# evidence, and a clear winner. Without them it was first-row-wins over every
# project that shared two words with the hint.
_KEYWORD_MIN_WORDS = 2
_KEYWORD_MARGIN = 1

# Retirement markers. A retired row is kept for history — its description
# describes something that no longer exists, so a FUZZY tier must never bind
# it. An explicit slug or name mention (tier 1) still does: naming a retired
# project is a deliberate request for its archive.
#
# The marker must OPEN the description, because that is how a retirement is
# actually written ("RETIRED 2026-09-05 — …", "okuro Project Manager
# (SUPERSEDED → okuro-kanban)"). Scanning further in, or admitting the word
# "deleted", misread a LIVE project: okuro-design-systems says "the ONLY
# design layer since v0 was deleted 2026-09-06" and was silently dropped from
# both fuzzy tiers, which is the same class of confident-wrong-answer this
# filter exists to prevent.
_RETIRED_MARKERS = ("retired", "superseded", "archival only")
_RETIRED_WINDOW = 60

# Project descriptions are the documents; the task hint is the query. Without
# an explicit instruction this inherits embed_query()'s default, which asks for
# an expert ROLE description — the wrong retrieval task entirely.
_PROJECT_QUERY_INSTRUCTION = (
    "Given a task description, retrieve the project it belongs to."
)

# Slugs that are ordinary English words. A bare mention is not evidence of
# project intent — "update the design system tokens" is not work on the
# `system` project. These bind only when the hint also carries an explicit
# project cue. Derived from the live table (2026-07-19): of 12 single-word
# slugs, these three are common nouns; the rest are distinctive names and bind
# normally.
_GENERIC_SLUGS = frozenset({"system", "personal", "explorer"})
# A leading slug segment shorter than this is too weak to bind on ("tm", "acme"
# would over-match). 6 keeps "carrierbird", "meridian", "orchestrator".
_MIN_SEGMENT_LEN = 6
_PROJECT_CUES = ("project", "repo", "repository", "codebase")

# Semantic floor raised from 0.5 after measuring a false bind: the hint
# "fix a CSS grid bug in the carrierbird gallery" scored another project at 0.512
# — over the old floor — for a project unrelated to the task, because only
# 3 of 78 active projects carry a real description. The tier can therefore
# only ever return one of three answers, so it must demand a strong signal
# before binding anything at all.
_SEMANTIC_FLOOR = 0.65
# The top match must beat the runner-up by this much. Without it, first-over-
# threshold won and near-ties were decided by row order.
_SEMANTIC_MARGIN = 0.05
# Below this many describable projects the semantic corpus is not
# representative of the project set; log when it decides anything.
_SEMANTIC_MIN_CORPUS = 10


def _is_placeholder_description(desc: str) -> bool:
    return desc.strip().lower() in _PLACEHOLDER_DESCRIPTIONS


def _is_retired_description(desc: str) -> bool:
    """True when the description OPENS by marking the project as history.

    Only the head is inspected: a live project may well MENTION a retired
    sibling ("supersedes the old engine") further down without being retired
    itself.
    """
    return any(m in desc[:_RETIRED_WINDOW].lower() for m in _RETIRED_MARKERS)


def _singular(word: str) -> str:
    """Fold a plural onto its singular. Deliberately crude, applied to BOTH sides.

    Only regular -s/-es forms; irregulars are left alone. Both the hint and the
    description pass through it, so the two sides agree whatever it does — a
    wrong fold costs nothing as long as it is the same wrong fold on both.
    """
    if len(word) < 4 or not word.endswith("s") or word.endswith(_KEEPS_TRAILING_S):
        return word
    if word.endswith("es"):
        stem = word[:-2]
        if stem.endswith(_PLURAL_ES_STEMS):
            return stem
    return word[:-1]


def _content_words(text: str) -> set[str]:
    """Subject-matter tokens: long enough, not punctuation, not a stopword."""
    words = set()
    for raw in _WORD_RE.findall(text.lower()):
        if len(raw) < _MIN_WORD_LEN or raw in _STOPWORDS:
            continue
        w = _singular(raw)
        if w not in _STOPWORDS:
            words.add(w)
    return words


def _describable(rows: list) -> list:
    """Rows a fuzzy tier may score: a real description, and still current."""
    return [
        r for r in rows
        if r.get("description")
        and not _is_placeholder_description(r["description"])
        and not _is_retired_description(r["description"])
    ]


def _mentions(needle: str, haystack: str) -> bool:
    """True when `needle` appears in `haystack` as a whole token.

    Word-boundary anchored, treating '-' as part of a token so 'acme' does not
    match inside 'acme-end2end' and 'system' does not match inside 'systemd'.
    """
    return re.search(rf"(?<![\w-]){re.escape(needle)}(?![\w-])", haystack) is not None


def resolve_project(task_hint: str) -> str | None:
    """Resolve a task hint to a project slug.

    Resolution order:
    1. Exact slug match
    2. URL match
    3. Path match
    4. Keyword match in description
    5. Semantic match (embedding similarity) — only if 1-4 fail
    """
    if not task_hint:
        return None

    from okuro.db import get_db

    task_lower = task_hint.lower()
    db = get_db()

    rows = db.fetchall(
        "SELECT id, name, path, url, description, roles "
        "FROM projects WHERE active = 1"
    )
    if not rows:
        return None

    # 1. Exact slug or name match — anchored, longest-match-wins.
    #
    # This was an unanchored `in`, first-match-wins, over an unordered query.
    # It bound 'system' from inside "systemd", 'sumi' from inside "assuming",
    # and sent "work on acme-end2end today" to `acme` — i.e. it ignored a
    # project the user had named explicitly. 35 active slug pairs are
    # substrings of one another (okuro/okuro-orchestrator, meridian/meridian-ui,
    # northwind/northwind-api, …), so the winner was decided by physical row order.
    #
    # The damage was not an empty packet: every downstream section rendered
    # correctly FOR THE WRONG PROJECT, with no score and no disclosure. A
    # confidently-presented wrong charter is worse than no charter.
    has_cue = any(cue in task_lower for cue in _PROJECT_CUES)
    matches: list[tuple[int, str]] = []
    for r in rows:
        if r["id"] in _GENERIC_SLUGS and not has_cue:
            # Ordinary English word, no explicit project cue — not evidence.
            continue
        for needle in (r["id"], (r["name"] or "").lower()):
            if needle and _mentions(needle, task_lower):
                matches.append((len(needle), r["id"]))
        # Leading-segment match: people name a project by its distinctive head,
        # not its full slug — "the carrierbird gallery" means
        # `carrierbird-postal`. Anchoring on the WHOLE slug fixed
        # systemd->system but lost this, so a hyphenated project could only be
        # bound by typing every segment. Only the FIRST segment counts, and
        # only when it is distinctive: a trailing segment ("post", "ui", "v2")
        # is far too weak, and a generic head would reopen the original bug.
        head = (r["id"] or "").split("-")[0]
        if (
            head
            and head != r["id"]
            and len(head) >= _MIN_SEGMENT_LEN
            and head not in _GENERIC_SLUGS
            and _mentions(head, task_lower)
        ):
            matches.append((len(head), r["id"]))
    if matches:
        # Longest matched token wins: 'acme-end2end' beats 'acme'.
        return max(matches)[1]

    # 2. URL match
    for r in rows:
        if r.get("url") and r["url"] in task_lower:
            return r["id"]

    # 3. Path match
    for r in rows:
        if r.get("path") and r["path"].lower() in task_lower:
            return r["id"]

    # 4. Keyword match in description — scored, not first-row-wins.
    #
    # This tier used to return the first row sharing two non-stopword tokens
    # with the hint, over an unordered query. Measured 2026-09-16: a GPU/driver
    # hint matched four projects on {"after", "not", "—"} and bound the first,
    # `tm-illustrator` — a project RETIRED eleven days earlier. The packet
    # rendered a deleted tool's memory and todos with full confidence.
    #
    # Three bars now, the same ones tiers 1 and 5 already enforce: only words
    # that carry subject matter count, a word common across the corpus counts
    # for nothing, and the winner must beat the runner-up.
    describable = _describable(rows)
    task_words = _content_words(task_lower)
    if task_words and describable:
        doc_words = [_content_words(r["description"]) for r in describable]
        df: dict[str, int] = {}
        for words in doc_words:
            for w in words:
                df[w] = df.get(w, 0) + 1
        ceiling = max(1, int(len(describable) * _MAX_DOC_FREQUENCY))

        keyword_scored: list[tuple[int, str]] = []
        for r, words in zip(describable, doc_words):
            overlap = {w for w in (words & task_words) if df[w] <= ceiling}
            if len(overlap) >= _KEYWORD_MIN_WORDS:
                keyword_scored.append((len(overlap), r["id"]))

        if keyword_scored:
            keyword_scored.sort(reverse=True)
            runner_up = keyword_scored[1][0] if len(keyword_scored) > 1 else 0
            if keyword_scored[0][0] - runner_up >= _KEYWORD_MARGIN:
                return keyword_scored[0][1]
            log.debug(
                "resolve_project: keyword tie (%d vs %d) — falling through",
                keyword_scored[0][0], runner_up,
            )

    # 5. Semantic match (expensive)
    #
    # Three defects fixed here, all of which produced confident wrong answers:
    #   a) placeholder descriptions were scored. 57 of 78 active projects share
    #      the literal description "Auto-registered by agent", which sits at
    #      cos ~0.52 against any agent-flavoured hint — above the 0.5 threshold.
    #      With 57 exact ties, row order picked the winner.
    #   b) the query was embedded with embed_query()'s DEFAULT instruction,
    #      which is role-retrieval ("retrieve the description of the expert
    #      role best suited to perform it") — a category error against project
    #      descriptions. Now passes an explicit project-matching instruction.
    #   c) first-over-threshold won. Now the top score must clear the runner-up
    #      by a margin, so a tie returns None instead of an arbitrary row.
    try:
        from okuro.embed.client import embed_one, embed_query

        # task_hint is the QUERY; the project descriptions below are the
        # documents. Asymmetric model — the query side must be wrapped.
        task_embedding = embed_query(task_hint, instruction=_PROJECT_QUERY_INSTRUCTION)
        norm_a = sum(a * a for a in task_embedding) ** 0.5

        #   d) retired projects were scored like live ones. Same filter as
        #      tier 4 now — an archival row describes what was deleted.
        scored: list[tuple[float, str]] = []
        for r in describable:
            desc = r["description"]
            desc_embedding = embed_one(desc)
            dot = sum(a * b for a, b in zip(task_embedding, desc_embedding))
            norm_b = sum(b * b for b in desc_embedding) ** 0.5
            similarity = dot / (norm_a * norm_b) if norm_a and norm_b else 0
            if similarity > _SEMANTIC_FLOOR:
                scored.append((similarity, r["id"]))

        if len(describable) < _SEMANTIC_MIN_CORPUS:
            log.info(
                "resolve_project: semantic corpus is %d of %d active projects — "
                "most carry no real description, so this tier can only return a "
                "small fixed subset. Treat its binds with suspicion.",
                len(describable), len(rows),
            )

        if scored:
            scored.sort(reverse=True)
            runner_up = scored[1][0] if len(scored) > 1 else 0.0
            if scored[0][0] - runner_up >= _SEMANTIC_MARGIN:
                return scored[0][1]
            # Ambiguous — an unscoped packet beats a confidently wrong one.
            log.debug(
                "resolve_project: semantic tie (%.3f vs %.3f) — returning None",
                scored[0][0], runner_up,
            )
    except Exception:
        log.warning("resolve_project: semantic tier failed", exc_info=True)

    return None


def resolve_role(task_hint: str, project_roles: list[str] | None) -> str | None:
    """Resolve the best matching role for a task.

    Args:
        task_hint: Task description
        project_roles: Available roles for the matched project
    """
    if not task_hint:
        return project_roles[0] if project_roles and len(project_roles) == 1 else None

    # Try okuro.roles semantic matching first
    try:
        from okuro.roles.resolver import match_role

        result = match_role(task_hint)
        if result.get("match_type") == "matched":
            matched_id = result["id"]
            if project_roles and matched_id not in project_roles:
                pass  # Fall through to keyword matching
            else:
                return matched_id
    except Exception:
        pass

    if not project_roles:
        return None

    if len(project_roles) == 1:
        return project_roles[0]

    task_lower = task_hint.lower()

    role_keywords = {
        "frontend": ["dashboard", "css", "ui", "frontend", "page", "component", "layout", "design", "tailwind", "html"],
        "data-pipeline": ["pipeline", "data", "etl", "scrape", "ingest", "transform"],
        "devops": ["deploy", "docker", "server", "ci", "cd", "infrastructure", "systemd", "nginx", "caddy"],
        "backend": ["api", "endpoint", "backend", "database", "query", "auth"],
        "mcp-developer": ["mcp", "tool", "server", "protocol"],
        "orchestrator": ["orchestrate", "workflow", "agent", "dispatch"],
        "role-designer": ["role", "prompt", "system prompt"],
    }

    best_role = None
    best_score = 0

    for role in project_roles:
        keywords = role_keywords.get(role, [role])
        score = sum(1 for kw in keywords if kw in task_lower)
        if score > best_score:
            best_score = score
            best_role = role

    return best_role
