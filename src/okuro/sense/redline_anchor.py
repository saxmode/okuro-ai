# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: redline anchor v2 — build it from served bytes, resolve it with the
#   two-tier agreement rule, and locate it as line:col in the source.
# index:
#   constants | class Node | def parse_document
#   def build_anchor | def validate_anchor
#   def css_path | def human_path
#   def resolve | def src_for_anchor
# AGENT_HEADER_END -->
"""Anchor v2 — what a redline comment points at, and whether it still points.

Three jobs, all over the SERVED BYTES of one version (never across versions —
0 of 5 anchors survived the real mockups-v2 -> v3 regeneration on any tier,
which is why a comment belongs to a version and the agent is the resolver):

1. **Build.** The full ancestor chain to ``BODY``: tag, id, classes,
   nth-of-type, and a text fingerprint that is ``None`` below 8 collapsed
   characters. The 8-char floor is the F5 guard and applies at EVERY rung, not
   only the picked element — an ``exact=""`` TextQuoteSelector matched 131
   elements on the real page.

2. **Resolve.** Five independent tiers, and ``state = "exact"`` only when at
   least two of them each return exactly one element AND it is the same
   element. Otherwise ``orphan``. There is no third state claiming the element
   merely relocated: a ``tag:nth-of-type`` path was measured surviving a
   sibling insert and confidently returning the WRONG row, and a tier that
   answers wrongly is worse than one that says gone.
   ``box`` is never a tier — it counts toward nothing.

3. **Locate.** ``{line, col}`` of the start tag in the served bytes, because a
   line number alone points at ~3,000 characters on this class of file (one
   line of the real mockup carries every bubble on the page).

The browser overlay (Wave 2) builds the same shape from the live DOM. This
module is the server-side half: it is what makes an anchor testable without a
browser, and what gives ``redline_list`` a ``line``/``col`` an agent in another
directory can open.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from html.parser import HTMLParser
from typing import Iterable, Optional

ANCHOR_VERSION = 2

# Recorded on every rung, in this order, when present. Classes are recorded as
# evidence for a human reading the excerpt and are NEVER used to build a
# selector — a Tailwind class list is the most churn-prone attribute on a page.
STABLE_ATTRS = (
    "id", "data-redline-anchor", "data-testid", "name",
    "title", "for", "href", "aria-label",
)
# T1 uses only the four that assert identity rather than describe.
IDENTITY_ATTRS = ("id", "data-redline-anchor", "data-testid", "name")
# T4, in preference order. This is the tier that saves a text-free element.
ATTRQUOTE_ATTRS = ("title", "aria-label", "for", "href", "data-testid", "name")

TEXT_FLOOR = 8       # collapsed text under this many chars is not a fingerprint
TEXT_CAP = 120
ATTR_VALUE_CAP = 120
MAX_ATTRS_PER_RUNG = 8
OUTER_HTML_CAP = 2000
QUOTE_CONTEXT = 32   # Hypothesis's number
CHAIN_CAP = 24

_VOID_TAGS = frozenset({
    "area", "base", "br", "col", "embed", "hr", "img", "input", "link",
    "meta", "param", "source", "track", "wbr",
})


class RedlineAnchorError(ValueError):
    """An anchor that cannot be trusted to mean anything."""


def _collapse(text: str) -> str:
    return " ".join(text.split())


def _fingerprint(text: str) -> Optional[str]:
    """The text rule, applied identically at every rung.

    ``None`` below the floor so the quote tier is never emitted empty, and so a
    two-character number like ``95`` cannot become a selector that matched 3
    elements unchanged and 6 after a mutation.
    """
    collapsed = _collapse(text)
    if len(collapsed) < TEXT_FLOOR:
        return None
    return collapsed[:TEXT_CAP]


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


@dataclass(eq=False)
class Node:
    """One element in the parsed document, with its position in the source.

    ``eq=False`` on purpose: nodes are compared by IDENTITY everywhere in this
    module (the agreement rule asks "is it the same element", not "does it look
    the same"), and a structural __eq__ over a parent pointer recurses.
    """

    tag: str                                   # lowercase, as written
    attrs: dict[str, str] = field(default_factory=dict)
    parent: Optional["Node"] = field(default=None, repr=False)
    children: list["Node"] = field(default_factory=list, repr=False)
    line: int = 0
    col: int = 0                               # 1-based
    start_offset: int = 0
    end_offset: int = 0
    nth: int = 1                               # nth-of-type among siblings
    _runs: list[str] = field(default_factory=list, repr=False)  # own direct text runs

    # -- reading ------------------------------------------------------------

    @property
    def upper(self) -> str:
        return self.tag.upper()

    @property
    def classes(self) -> list[str]:
        return (self.attrs.get("class") or "").split()

    def descendants(self) -> Iterable["Node"]:
        for child in self.children:
            yield child
            yield from child.descendants()

    def ancestors(self) -> Iterable["Node"]:
        node = self.parent
        while node is not None:
            yield node
            node = node.parent

    def text_content(self) -> str:
        """DOM ``textContent``: this element's runs plus every descendant's."""
        out: list[str] = []
        self._collect_text(out)
        return "".join(out)

    def _collect_text(self, out: list[str]) -> None:
        for item in self._ordered:
            if isinstance(item, str):
                out.append(item)
            else:
                item._collect_text(out)

    def fingerprint(self) -> Optional[str]:
        return _fingerprint(self.text_content())

    def outer_html(self, source: str) -> str:
        return source[self.start_offset:self.end_offset]


# ``_ordered`` keeps text runs and child elements interleaved in document
# order, which is what textContent needs. Declared outside the dataclass field
# list so the two never disagree about ordering.
Node._ordered = property(lambda self: self.__dict__.setdefault("_ordered_list", []))


class _DocumentParser(HTMLParser):
    """Build a Node tree that remembers where every tag started.

    ``html.parser`` performs no implied-tag insertion, so a ``<table>`` written
    without ``<tbody>`` yields a tree that differs from the browser DOM. That
    divergence is handled where it matters — :func:`src_for_anchor` falls back
    to a literal search of the served bytes and returns ``None`` rather than a
    guessed line.
    """

    def __init__(self, source: str) -> None:
        super().__init__(convert_charrefs=True)
        self.source = source
        self._line_starts = [0]
        for match in re.finditer(r"\n", source):
            self._line_starts.append(match.end())
        self.roots: list[Node] = []
        self._stack: list[Node] = []

    # -- offsets ------------------------------------------------------------

    def _offset(self) -> int:
        line, col = self.getpos()
        return self._line_starts[line - 1] + col

    def _tag_end(self, start: int) -> int:
        idx = self.source.find(">", start)
        return len(self.source) if idx < 0 else idx + 1

    # -- tree ---------------------------------------------------------------

    def _append(self, node: Node) -> None:
        if self._stack:
            parent = self._stack[-1]
            node.parent = parent
            parent.children.append(node)
            parent._ordered.append(node)
        else:
            self.roots.append(node)

    def handle_starttag(self, tag, attrs):  # noqa: D102
        line, col = self.getpos()
        start = self._offset()
        node = Node(
            tag=tag.lower(),
            attrs={k.lower(): (v if v is not None else "") for k, v in attrs},
            line=line,
            col=col + 1,          # getpos offset is 0-based; a column is not
            start_offset=start,
            end_offset=self._tag_end(start),
        )
        self._append(node)
        if tag.lower() not in _VOID_TAGS:
            self._stack.append(node)

    def handle_startendtag(self, tag, attrs):  # noqa: D102
        line, col = self.getpos()
        start = self._offset()
        node = Node(
            tag=tag.lower(),
            attrs={k.lower(): (v if v is not None else "") for k, v in attrs},
            line=line,
            col=col + 1,
            start_offset=start,
            end_offset=self._tag_end(start),
        )
        self._append(node)

    def handle_endtag(self, tag):  # noqa: D102
        tag = tag.lower()
        for depth in range(len(self._stack) - 1, -1, -1):
            if self._stack[depth].tag == tag:
                closing = self._stack[depth]
                closing.end_offset = self._tag_end(self._offset())
                del self._stack[depth:]
                return
        # An end tag with no open element is ignored, exactly as a browser does.

    def handle_data(self, data):  # noqa: D102
        if not self._stack:
            return
        node = self._stack[-1]
        node._runs.append(data)
        node._ordered.append(data)


@dataclass
class Document:
    """A parsed document plus the source it came from."""

    source: str
    roots: list[Node]

    def elements(self) -> Iterable[Node]:
        for root in self.roots:
            yield root
            yield from root.descendants()

    @property
    def body(self) -> Optional[Node]:
        for node in self.elements():
            if node.tag == "body":
                return node
        return None

    def resolution_root(self) -> Optional[Node]:
        return self.body or (self.roots[0] if self.roots else None)


def parse_document(source: str) -> Document:
    """Parse HTML into a Node tree that keeps source positions."""
    parser = _DocumentParser(source)
    parser.feed(source)
    parser.close()
    doc = Document(source=source, roots=parser.roots)
    _number_siblings(doc.roots)
    return doc


def _number_siblings(nodes: list[Node]) -> None:
    counters: dict[str, int] = {}
    for node in nodes:
        counters[node.tag] = counters.get(node.tag, 0) + 1
        node.nth = counters[node.tag]
        _number_siblings(node.children)


# ---------------------------------------------------------------------------
# Building
# ---------------------------------------------------------------------------


def _rung(node: Node) -> dict:
    attrs: dict[str, str] = {}
    for name in STABLE_ATTRS:
        if name in node.attrs and len(attrs) < MAX_ATTRS_PER_RUNG:
            attrs[name] = node.attrs[name][:ATTR_VALUE_CAP]
    return {
        "tag": node.upper,
        "id": node.attrs.get("id") or None,
        "classes": node.classes,
        "nth": node.nth,
        "attrs": attrs,
        "text": node.fingerprint(),
    }


def _quote_for(doc: Document, node: Node, exact: Optional[str]) -> Optional[dict]:
    """A W3C TextQuoteSelector, or None when the element carries no text.

    Never emitted empty: an ``exact`` of ``""`` matches every element in the
    document and its context check passes trivially.
    """
    if not exact:
        return None
    text, _owners = _document_text(doc)
    idx = text.find(exact)
    if idx < 0:
        return {"exact": exact, "prefix": "", "suffix": ""}
    return {
        "exact": exact,
        "prefix": text[max(0, idx - QUOTE_CONTEXT):idx],
        "suffix": text[idx + len(exact):idx + len(exact) + QUOTE_CONTEXT],
    }


def _attrquote_for(node: Node) -> Optional[dict]:
    for name in ATTRQUOTE_ATTRS:
        value = node.attrs.get(name)
        if value:
            return {"n": name, "v": value[:ATTR_VALUE_CAP]}
    return None


def build_anchor(
    doc: Document,
    node: Node,
    *,
    point: Optional[list[float]] = None,
    box: Optional[list[float]] = None,
    doc_size: Optional[list[int]] = None,
    hosts: Optional[list[str]] = None,
    with_src: bool = True,
    captured_at: Optional[str] = None,
) -> dict:
    """Build anchor v2 for ``node``, from ``doc``'s bytes.

    The browser overlay builds the same JSON from the live DOM; this path
    exists so an anchor can be built, stored and resolved without one — which
    is what makes the two-tier rule testable at all.
    """
    chain: list[dict] = []
    cursor: Optional[Node] = node
    while cursor is not None and len(chain) < CHAIN_CAP:
        chain.append(_rung(cursor))
        if cursor.tag == "body":
            break
        cursor = cursor.parent

    return {
        "v": ANCHOR_VERSION,
        "captured_at": captured_at or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "point": list(point) if point else [0.5, 0.5],
        "hosts": list(hosts or []),
        "chain": chain,
        "quote": _quote_for(doc, node, chain[0]["text"]),
        "attrquote": _attrquote_for(node),
        "box": list(box) if box else None,
        "doc": list(doc_size) if doc_size else None,
        "src": ({"line": node.line, "col": node.col} if with_src else None),
        "outer_html": node.outer_html(doc.source)[:OUTER_HTML_CAP],
    }


def validate_anchor(anchor: dict) -> dict:
    """Refuse an anchor that cannot mean anything. Returns it unchanged."""
    if not isinstance(anchor, dict):
        raise RedlineAnchorError("anchor must be an object")
    if anchor.get("v") != ANCHOR_VERSION:
        raise RedlineAnchorError(
            f"anchor v{anchor.get('v')!r} is not v{ANCHOR_VERSION} — "
            "bump the version rather than reinterpreting the old shape"
        )
    chain = anchor.get("chain")
    if not isinstance(chain, list) or not chain:
        raise RedlineAnchorError("anchor.chain must hold at least the picked element")
    for rung in chain:
        if not isinstance(rung, dict) or not rung.get("tag"):
            raise RedlineAnchorError("every chain rung needs a tag")
        text = rung.get("text")
        if text is not None and len(text) < TEXT_FLOOR:
            raise RedlineAnchorError(
                f"chain text {text!r} is under the {TEXT_FLOOR}-character floor — "
                "it must be null, not a short string"
            )
    quote = anchor.get("quote")
    if quote is not None and not (quote.get("exact") or "").strip():
        raise RedlineAnchorError("a quote tier with an empty exact matches everything")
    if quote is not None and chain[0].get("text") is None:
        raise RedlineAnchorError("quote must be null when the picked element has no text")
    return anchor


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------


def css_path(chain: list[dict]) -> str:
    """T2's selector: ``tag:nth-of-type(n)`` per rung, from BODY down.

    Truncated at the deepest rung carrying an id — that rung becomes ``#id``.
    Classes are excluded on purpose (see the module docstring).
    """
    rungs = list(reversed(chain))          # BODY first
    start = 0
    for idx in range(len(rungs) - 1, -1, -1):
        if rungs[idx].get("id"):
            start = idx
            break
    parts: list[str] = []
    for idx in range(start, len(rungs)):
        rung = rungs[idx]
        tag = rung["tag"].lower()
        if idx == start and rung.get("id"):
            parts.append(f"#{rung['id']}")
        else:
            parts.append(f"{tag}:nth-of-type({rung.get('nth', 1)})")
    return " > ".join(parts)


def human_path(chain: list[dict]) -> str:
    """``body › div.board › section.col › label.row › span.bub`` — for reading."""
    parts: list[str] = []
    for rung in reversed(chain):
        tag = rung["tag"].lower()
        if rung.get("id"):
            parts.append(f"{tag}#{rung['id']}")
        elif rung.get("classes"):
            parts.append(f"{tag}.{rung['classes'][0]}")
        else:
            parts.append(tag)
    return " › ".join(parts)


# ---------------------------------------------------------------------------
# Resolution — five tiers, two must agree
# ---------------------------------------------------------------------------


def _document_text(doc: Document) -> tuple[str, list[Node]]:
    """Collapsed document text plus the owning element of every character."""
    cached = getattr(doc, "_text_cache", None)
    if cached is not None:
        return cached
    chars: list[str] = []
    owners: list[Node] = []
    prev_space = True

    def walk(node: Node) -> None:
        nonlocal prev_space
        for item in node._ordered:
            if isinstance(item, str):
                for ch in item:
                    if ch.isspace():
                        if prev_space:
                            continue
                        chars.append(" ")
                        owners.append(node)
                        prev_space = True
                    else:
                        chars.append(ch)
                        owners.append(node)
                        prev_space = False
            else:
                walk(item)

    for root in doc.roots:
        walk(root)
    while chars and chars[-1] == " ":
        chars.pop()
        owners.pop()
    result = ("".join(chars), owners)
    doc._text_cache = result
    return result


def _lca(nodes: list[Node]) -> Optional[Node]:
    if not nodes:
        return None
    chains = []
    for node in nodes:
        chain = [node] + list(node.ancestors())
        chains.append(list(reversed(chain)))
    common: Optional[Node] = None
    for level in zip(*chains):
        first = level[0]
        if all(item is first for item in level):
            common = first
        else:
            break
    return common


def _tier_attr(doc: Document, anchor: dict) -> Optional[list[Node]]:
    wanted = {
        name: value
        for name, value in (anchor["chain"][0].get("attrs") or {}).items()
        if name in IDENTITY_ATTRS and value
    }
    if not wanted:
        return None
    return [
        node for node in doc.elements()
        if all(node.attrs.get(name) == value for name, value in wanted.items())
    ]


def _tier_css(doc: Document, anchor: dict) -> Optional[list[Node]]:
    rungs = list(reversed(anchor["chain"]))
    start = 0
    for idx in range(len(rungs) - 1, -1, -1):
        if rungs[idx].get("id"):
            start = idx
            break

    if rungs[start].get("id"):
        found = [n for n in doc.elements() if n.attrs.get("id") == rungs[start]["id"]]
        if len(found) != 1:
            return []
        cursor = found[0]
    else:
        root = doc.resolution_root()
        if root is None:
            return []
        # The first rung is the resolution root itself (BODY), matched by tag.
        if root.tag != rungs[start]["tag"].lower():
            return []
        cursor = root

    for rung in rungs[start + 1:]:
        tag = rung["tag"].lower()
        nth = int(rung.get("nth") or 1)
        siblings = [c for c in cursor.children if c.tag == tag]
        if len(siblings) < nth:
            return []
        cursor = siblings[nth - 1]
    return [cursor]


def _tier_quote(doc: Document, anchor: dict) -> Optional[list[Node]]:
    quote = anchor.get("quote")
    if not quote or not (quote.get("exact") or ""):
        return None
    text, owners = _document_text(doc)
    exact = quote["exact"]
    prefix = quote.get("prefix") or ""
    suffix = quote.get("suffix") or ""
    hits: list[Node] = []
    for match in re.finditer(re.escape(exact), text):
        start, end = match.span()
        if prefix and not text[max(0, start - len(prefix)):start].endswith(prefix):
            continue
        if suffix and not text[end:end + len(suffix)].startswith(suffix):
            continue
        owner = _lca(owners[start:end])
        if owner is not None and owner not in hits:
            hits.append(owner)
    return hits


def _tier_attrquote(doc: Document, anchor: dict) -> Optional[list[Node]]:
    attrquote = anchor.get("attrquote")
    if not attrquote or not attrquote.get("n"):
        return None
    name, value = attrquote["n"], attrquote.get("v")
    return [n for n in doc.elements() if n.attrs.get(name) == value]


def _tier_ancestor(doc: Document, anchor: dict) -> Optional[list[Node]]:
    chain = anchor["chain"]
    upper_rungs = chain[1:]
    if not any(r.get("text") for r in upper_rungs):
        return None
    own = chain[0]
    hits: list[Node] = []
    for node in doc.elements():
        if node.upper != own["tag"]:
            continue
        if own.get("text") is not None and node.fingerprint() != own["text"]:
            continue
        ancestors = list(node.ancestors())
        if len(ancestors) < len(upper_rungs):
            continue
        ok = True
        for rung, ancestor in zip(upper_rungs, ancestors):
            if ancestor.upper != rung["tag"]:
                ok = False
                break
            if rung.get("text") is not None and ancestor.fingerprint() != rung["text"]:
                ok = False
                break
        if ok:
            hits.append(node)
    return hits


_TIERS = (
    ("attr", _tier_attr),
    ("css", _tier_css),
    ("quote", _tier_quote),
    ("attrquote", _tier_attrquote),
    ("ancestor", _tier_ancestor),
)


def resolve(source: str | Document, anchor: dict) -> dict:
    """Place one anchor inside ONE version's bytes.

    ``state`` is ``exact`` if and only if at least two tiers each resolved to
    exactly one element and it is the same element — otherwise ``orphan``.
    Two states, and there is no third for "it relocated": a confident wrong
    answer is worse than "gone". A nth-of-type path was measured resolving to
    the row BELOW the anchored one after a single sibling insert.

    ``box`` never participates. It was measured hitting the captured element
    3 of 5 times unchanged and 0 of 5 across a regeneration, and it can never
    return an ``opacity:0; pointer-events:none`` toggle. Its only job is
    drawing a dimmed ghost when the state is ``orphan``.
    """
    doc = source if isinstance(source, Document) else parse_document(source)
    fired: dict[str, Optional[list[Node]]] = {}
    for name, fn in _TIERS:
        try:
            fired[name] = fn(doc, anchor)
        except Exception:  # noqa: BLE001 — a broken tier is a silent tier
            fired[name] = []

    singles = {name: hits[0] for name, hits in fired.items() if hits is not None and len(hits) == 1}
    winner: Optional[Node] = None
    agreeing: list[str] = []
    for name, node in singles.items():
        same = [other for other, node2 in singles.items() if node2 is node]
        if len(same) >= 2:
            winner = node
            agreeing = same
            break

    state = "exact" if winner is not None else "orphan"
    return {
        "state": state,
        "agreeing_tiers": agreeing,
        "tiers": {
            name: (None if hits is None else len(hits))
            for name, hits in fired.items()
        },
        "identified_by": _identified_by(agreeing, anchor),
        "element": winner,
    }


def _identified_by(agreeing: list[str], anchor: dict) -> str:
    """WHY this pointer can be trusted — the honest label, not a score."""
    if "attr" in agreeing:
        attrs = anchor["chain"][0].get("attrs") or {}
        for name in IDENTITY_ATTRS:
            if attrs.get(name):
                return name
        return "id"
    if "attrquote" in agreeing:
        return (anchor.get("attrquote") or {}).get("n") or "position"
    if "quote" in agreeing:
        return "text"
    return "position"


# ---------------------------------------------------------------------------
# line:col
# ---------------------------------------------------------------------------


def src_for_anchor(source: str, anchor: dict) -> Optional[dict]:
    """``{line, col}`` of the element's start tag, or ``None``. Never a guess.

    Two paths, in order:

    1. Walk the parser tree with T2's path. Exact when the parser tree and the
       browser DOM agree.
    2. When that walk fails — ``html.parser`` inserts no implied ``<tbody>``,
       so a table written without one parses differently than it renders —
       search the served bytes literally for the first 200 characters of
       ``outer_html`` and derive line/col from the byte offset.

    When both fail the answer is ``None``. A guessed line on a file whose one
    line carries 3,000 characters is worse than no line at all.
    """
    doc = parse_document(source)
    hits = _tier_css(doc, anchor)
    if hits and len(hits) == 1:
        node = hits[0]
        return {"line": node.line, "col": node.col}

    needle = (anchor.get("outer_html") or "")[:200]
    if needle:
        idx = source.find(needle)
        if idx >= 0:
            line = source.count("\n", 0, idx) + 1
            line_start = source.rfind("\n", 0, idx) + 1
            return {"line": line, "col": idx - line_start + 1}
    return None
