# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Confluence storage-format (XHTML + ac:/ri: macros) → markdown, using
#   only the stdlib. Feeds the confluence adapter; kept separate so it is
#   testable without any network.
# index: class _StorageToMarkdown | def storage_to_markdown | def html_to_text
# AGENT_HEADER_END -->
"""Convert Confluence storage format to markdown.

Stdlib only — deliberately. markdownify/html2text would each be a new runtime
dependency for one adapter, and neither understands Confluence's ``ac:`` macro
vocabulary, so the macro handling below would have to be written either way.
bs4/lxml happen to be importable in this venv but are NOT declared in
pyproject dependencies, so importing them here would be an undeclared dep that
breaks on a clean install.

What storage format adds on top of XHTML, and what we do with it:

  ac:layout / ac:layout-section / ac:layout-cell   unwrapped (pure page
        furniture — preserving it would emit empty structure into every chunk)
  ac:structured-macro ac:name="code"               fenced block, language taken
        from the ``language`` ac:parameter
  ac:structured-macro ac:name="info|note|tip|      blockquote with a bold label;
        warning|panel"                             the distinction matters to a
        reader ("warning" is not body text) but not enough to invent syntax
  ac:structured-macro (anything else)              rich-text body is kept, the
        macro chrome is dropped. An unknown macro is usually a container, and
        dropping its CONTENT loses real text.
  ac:image / ri:attachment                         ``![](filename)``
  ac:link / ri:page                                ``[label](Page Title)``
  ac:task-list / ac:task                           ``- [ ]`` / ``- [x]``

Everything else falls through to ordinary XHTML handling.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser

# Macros whose body is prose worth keeping, mapped to the blockquote label.
_ADMONITIONS = {
    "info": "Info",
    "note": "Note",
    "tip": "Tip",
    "warning": "Warning",
    "panel": "Panel",
    "expand": "Details",
}

# Macros that render no useful text — navigation/automation furniture that
# would otherwise inject noise into every chunk of the page.
_DROP_MACROS = {
    "toc", "children", "pagetree", "recently-updated", "contentbylabel",
    "livesearch", "navmap", "labels-list", "detailssummary", "excerpt-include",
}

_HEADINGS = {"h1": "#", "h2": "##", "h3": "###", "h4": "####", "h5": "#####", "h6": "######"}


class _StorageToMarkdown(HTMLParser):
    """Streaming converter with a small block/inline state stack."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self._list_stack: list[dict] = []      # {'type': 'ul'|'ol', 'n': int}
        self._table: list[list[str]] | None = None
        self._row: list[str] | None = None
        self._cell: list[str] | None = None    # buffer while inside td/th
        self._header_row = False
        self._pre_depth = 0
        self._code_lang = ""
        self._in_param: str | None = None
        self._param_buf = ""
        self._macro_stack: list[str] = []
        self._skip_depth = 0                   # >0 ⇒ inside a dropped macro
        self._pending_task_checked = False
        self._anchor: list[str] | None = None  # buffer while inside <a>/ac:link
        self._anchor_href = ""
        self._wikilink_title = ""              # ri:page target inside an ac:link
        self._just_opened_li = False           # suppress the <p> break inside <li>

    # ── sinks ────────────────────────────────────────────────────────
    def _emit(self, text: str) -> None:
        """Write text to whichever buffer is currently active.

        Order matters: an anchor can occur inside a table cell, so the anchor
        buffer must win. Without this the link text lands in the cell and the
        assembled ``[text](href)`` comes out inside-out.
        """
        if self._skip_depth:
            return
        if self._anchor is not None:
            self._anchor.append(text)
        elif self._cell is not None:
            self._cell.append(text)
        else:
            self.out.append(text)

    def _block(self) -> None:
        """Close the current block with a blank line (idempotent)."""
        if self._cell is not None or self._skip_depth:
            return
        while self.out and self.out[-1] == "\n":
            self.out.pop()
        if self.out:
            self.out.append("\n\n")

    # ── tags ─────────────────────────────────────────────────────────
    def handle_starttag(self, tag: str, attrs_list) -> None:
        attrs = dict(attrs_list)

        if tag == "ac:structured-macro":
            name = (attrs.get("ac:name") or "").lower()
            self._macro_stack.append(name)
            if name in _DROP_MACROS:
                self._skip_depth += 1
            elif name in _ADMONITIONS:
                self._block()
                self._emit(f"> **{_ADMONITIONS[name]}**\n> ")
            return

        if self._skip_depth:
            return

        if tag == "ac:parameter":
            self._in_param = (attrs.get("ac:name") or "").lower()
            self._param_buf = ""
            return

        if tag == "ac:plain-text-body":
            lang = self._code_lang if self._macro_stack[-1:] == ["code"] else ""
            self._block()
            self._emit(f"```{lang}\n")
            self._pre_depth += 1
            return

        if tag in ("ac:image",):
            return  # the ri: child carries the filename

        if tag in ("ri:attachment", "ri:url"):
            src = attrs.get("ri:filename") or attrs.get("ri:value") or ""
            if src:
                self._emit(f"![]({src})")
            return

        if tag == "ri:page":
            # Target of a cross-page link. Emitted on </ac:link>, not here, so
            # the link BODY (which follows) can become the alias instead of
            # being duplicated after the target.
            self._wikilink_title = attrs.get("ri:content-title") or ""
            return

        if tag == "ac:task":
            self._pending_task_checked = False
            return
        if tag == "ac:task-body":
            mark = "x" if self._pending_task_checked else " "
            self._block()
            self._emit(f"- [{mark}] ")
            return

        if tag == "ac:link":
            self._wikilink_title = ""
            self._anchor = []
            return

        if tag in ("ac:layout", "ac:layout-section", "ac:layout-cell",
                   "ac:rich-text-body", "ac:link-body", "ac:task-list",
                   "ac:task-id", "ac:adf-extension", "ac:adf-node", "ac:adf-content"):
            return  # structural wrappers — unwrap, keep children

        if tag in _HEADINGS:
            self._block()
            self._emit(_HEADINGS[tag] + " ")
            return

        if tag == "p":
            # Storage format wraps every list-item body in <p>. Letting that <p>
            # open a block would wipe the bullet just emitted by <li> (_block
            # pops trailing newlines and the marker with them) — which silently
            # turned every numbered step into a bare "1." with its text orphaned
            # in the next paragraph.
            if self._just_opened_li:
                self._just_opened_li = False
                return
            self._block()
            return

        if tag == "br":
            self._emit("\n" if self._cell is None else " ")
            return

        if tag == "hr":
            self._block()
            self._emit("---")
            self._block()
            return

        if tag in ("ul", "ol"):
            self._list_stack.append({"type": tag, "n": 0})
            self._block()
            return

        if tag == "li":
            if not self._list_stack:
                self._list_stack.append({"type": "ul", "n": 0})
            ctx = self._list_stack[-1]
            ctx["n"] += 1
            indent = "  " * (len(self._list_stack) - 1)
            bullet = f"{ctx['n']}." if ctx["type"] == "ol" else "-"
            # A nested list opens inside its parent <li>, so the newline must be
            # emitted here rather than by _block(), which would swallow the
            # indent that makes the nesting readable.
            self.out.append(f"\n{indent}{bullet} ") if self._cell is None else self._cell.append(" ")
            self._just_opened_li = True
            return

        if tag in ("strong", "b"):
            self._emit("**")
            return
        if tag in ("em", "i"):
            self._emit("*")
            return
        if tag == "code" and self._pre_depth == 0:
            self._emit("`")
            return
        if tag == "pre":
            self._block()
            self._emit("```\n")
            self._pre_depth += 1
            return

        if tag == "blockquote":
            self._block()
            self._emit("> ")
            return

        if tag == "a":
            self._anchor = []
            self._anchor_href = attrs.get("href") or ""
            return

        if tag == "table":
            self._table = []
            return
        if tag == "tr":
            self._row = []
            self._header_row = False
            return
        if tag in ("td", "th"):
            self._cell = []
            if tag == "th":
                self._header_row = True
            return

    def handle_endtag(self, tag: str) -> None:
        if tag == "ac:structured-macro":
            name = self._macro_stack.pop() if self._macro_stack else ""
            if name in _DROP_MACROS:
                self._skip_depth = max(0, self._skip_depth - 1)
            elif name in _ADMONITIONS:
                self._block()
            self._code_lang = ""
            return

        if self._skip_depth:
            return

        if tag == "ac:parameter":
            if self._in_param == "language":
                self._code_lang = self._param_buf.strip()
            self._in_param = None
            self._param_buf = ""
            return

        if tag == "ac:plain-text-body":
            self._pre_depth = max(0, self._pre_depth - 1)
            self._emit("\n```")
            self._block()
            return

        if tag in _HEADINGS:
            self._block()
            return

        if tag == "p":
            # Inside a list, a paragraph break is a soft break with continuation
            # indent — a blank line would terminate the list and restart the
            # numbering on the next item.
            if self._list_stack and self._cell is None:
                indent = "  " * len(self._list_stack)
                self.out.append(f"\n{indent}")
                return
            self._block()
            return

        if tag in ("ul", "ol"):
            if self._list_stack:
                self._list_stack.pop()
            if not self._list_stack:
                self._block()
            return

        if tag in ("strong", "b"):
            self._emit("**")
            return
        if tag in ("em", "i"):
            self._emit("*")
            return
        if tag == "code" and self._pre_depth == 0:
            self._emit("`")
            return
        if tag == "pre":
            self._pre_depth = max(0, self._pre_depth - 1)
            self._emit("\n```")
            self._block()
            return

        if tag == "blockquote":
            self._block()
            return

        if tag == "a":
            text = "".join(self._anchor or []).strip()
            href = self._anchor_href
            self._anchor = None
            self._anchor_href = ""
            if text and href and text != href:
                self._emit(f"[{text}]({href})")
            elif href:
                # Bare url — ``[url](url)`` is noise that doubles the token cost
                # of every link for no added meaning.
                self._emit(href)
            elif text:
                self._emit(text)
            return

        if tag == "ac:link":
            text = "".join(self._anchor or []).strip()
            title = self._wikilink_title
            self._anchor = None
            self._wikilink_title = ""
            # Wiki-link syntax, matching okuro's own [[name]] convention: the
            # TARGET stays machine-readable after chunking, so an agent can
            # follow a cross-reference to another page in the same corpus.
            if title and text and text != title:
                self._emit(f"[[{title}|{text}]]")
            elif title:
                self._emit(f"[[{title}]]")
            elif text:
                self._emit(text)
            return

        if tag in ("td", "th"):
            if self._cell is not None and self._row is not None:
                text = re.sub(r"\s+", " ", "".join(self._cell)).strip()
                # A literal pipe would break the markdown row it lands in.
                self._row.append(text.replace("|", "\\|"))
            self._cell = None
            return

        if tag == "tr":
            if self._row is not None and self._table is not None:
                self._table.append(self._row)
            self._row = None
            return

        if tag == "table":
            self._flush_table()
            return

    def handle_startendtag(self, tag: str, attrs) -> None:
        self.handle_starttag(tag, attrs)
        if tag not in ("br", "hr", "ri:attachment", "ri:page", "ri:url", "ac:image"):
            self.handle_endtag(tag)

    def handle_data(self, data: str) -> None:
        if self._in_param is not None:
            self._param_buf += data
            return
        if self._skip_depth:
            return
        if self._pre_depth:
            self._emit(data)
            return
        # Outside <pre>, runs of whitespace are not meaningful; collapsing them
        # keeps chunk budgets honest (storage format is heavily indented XML).
        text = re.sub(r"[ \t\r\n]+", " ", data)
        if text.strip() or (self.out and not self.out[-1].endswith((" ", "\n"))):
            self._emit(text)

    def unknown_decl(self, data: str) -> None:
        """CDATA — how storage format carries code-macro bodies verbatim."""
        if data.startswith("CDATA["):
            self._emit(data[6:])

    # ── tables ───────────────────────────────────────────────────────
    def _flush_table(self) -> None:
        rows = [r for r in (self._table or []) if any(c for c in r)]
        self._table = None
        if not rows:
            return
        width = max(len(r) for r in rows)
        rows = [r + [""] * (width - len(r)) for r in rows]
        self._block()
        head, body = (rows[0], rows[1:]) if self._header_row or len(rows) > 1 else (rows[0], [])
        self.out.append("| " + " | ".join(head) + " |\n")
        self.out.append("|" + "|".join([" --- "] * width) + "|\n")
        for r in body:
            self.out.append("| " + " | ".join(r) + " |\n")
        self._block()

    def result(self) -> str:
        text = "".join(self.out)
        text = re.sub(r"[ \t]+\n", "\n", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()


def storage_to_markdown(storage: str) -> str:
    """Convert one Confluence storage-format body to markdown.

    Never raises on malformed input: a page that fails to parse still returns
    its text content, because an un-indexed page is worse than an ugly one.
    """
    if not storage:
        return ""
    parser = _StorageToMarkdown()
    try:
        parser.feed(storage)
        parser.close()
        return parser.result()
    except Exception:  # noqa: BLE001 — degrade to plain text, never lose the page
        return html_to_text(storage)


def html_to_text(html: str) -> str:
    """Last-resort tag strip, used when the structured parse fails."""
    text = re.sub(r"<!\[CDATA\[(.*?)\]\]>", r"\1", html, flags=re.S)
    text = re.sub(r"<[^>]+>", " ", text)
    from html import unescape

    return re.sub(r"\s+", " ", unescape(text)).strip()
