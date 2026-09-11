# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: redline element crops — the optional server-side screenshot of
#   design v1.1 §11, which degrades to None and never to an error.
# index:
#   def playwright_available | def selector_for | def crop_version
# AGENT_HEADER_END -->
"""Element crops for redline comments — optional, and honest about it.

A crop is the one item in the owner's return shape that has no cheap
mechanism. There is no in-browser element capture without a library, and
adding one to the served overlay would contradict the module's own scope rule,
so the capture happens SERVER-SIDE against the version's own bytes with the
``browser`` extra (``playwright>=1.58``) that this repo already declares and
already drives in ``media/illustrator/render.py``.

Three things this module refuses to do:

1. **Guess.** A crop is taken only when a selector built from the anchor
   matches EXACTLY ONE element. Two matches is no crop, not a coin flip — the
   same discipline the resolver's two-tier rule enforces, for the same reason:
   a confident wrong answer is worse than a missing one.
2. **Fail.** No extra, no browser, no bytes, or no single match — every one of
   those is a skip with a named reason, and ``redline_list`` reports
   ``screenshot: null``. Nothing upstream breaks.
3. **Render a deck.** A prism document's bytes are a DeckDoc, not a page; it is
   the SPA's own runtime that turns one into pixels. Cropping it would mean
   booting the SPA, so a prism version is skipped and says why.

The crop is the element's box expanded by :data:`PAD` on every side, because a
20-pixel bubble photographed at its own size tells the reader nothing about
where it sits.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Optional

from okuro.sense import redline as redline_svc
from okuro.sense import redline_anchor

logger = logging.getLogger("okuro.sense.redline_shots")

# Context around the element, in CSS pixels.
PAD = 24
# The page is rendered at this width before cropping. The mockups this module
# was built for are laid out for a desktop viewport.
VIEWPORT = {"width": 1440, "height": 1000}
# A page that never settles must not hold the crop run open.
LOAD_TIMEOUT_MS = 15000


def playwright_available() -> tuple[bool, Optional[str]]:
    """Whether the optional ``browser`` extra is importable, and why not."""
    try:
        import playwright.sync_api  # noqa: F401
    except Exception as exc:  # noqa: BLE001 — any import failure is "absent"
        return False, (
            "the optional 'browser' extra is not installed "
            f"(pip install -e '.[browser]'): {exc}"
        )
    return True, None


def selector_for(anchor: dict) -> Optional[str]:
    """A selector for the picked element, strongest identity first.

    Deliberately NOT the five-tier resolver: this runs inside a browser to
    obtain a RECT, and the resolver's verdict has already been recorded by
    ``redline_list``. What matters here is that the selector either names one
    element or is discarded, which the caller enforces by counting matches.
    """
    chain = anchor.get("chain") or []
    if not chain:
        return None
    first = chain[0]
    attrs = first.get("attrs") or {}
    for name in redline_anchor.IDENTITY_ATTRS:
        value = attrs.get(name)
        if value:
            if name == "id":
                return f'[id="{_escape(value)}"]'
            return f'[{name}="{_escape(value)}"]'
    attrquote = anchor.get("attrquote") or {}
    if attrquote.get("n") and attrquote.get("v"):
        return f'[{attrquote["n"]}="{_escape(str(attrquote["v"]))}"]'
    return redline_anchor.css_path(chain) or None


def _escape(value: str) -> str:
    return str(value).replace("\\", "\\\\").replace('"', '\\"')


def crop_version(
    document_id: str,
    *,
    version_id: Optional[str] = None,
    overwrite: bool = False,
) -> dict:
    """Crop every anchored comment on one version. Returns what happened.

    ``{"made": int, "skipped": int, "reason": str | None, "paths": [...]}``.
    ``reason`` is set whenever nothing could be made, so a caller can say so
    once instead of returning a silent zero.

    Only comments with no crop yet are photographed, so the common re-open
    launches no browser at all: a version's bytes are pinned by its content
    hash, which makes a crop of them permanently correct.
    """
    listing = redline_svc.list_comments(
        document_id, version_id=version_id, status="all", include_anchor=True
    )
    document = listing["document"]
    version = listing["version"]
    comments = listing["comments"]

    if document["kind"] == "prism":
        return _nothing(
            len(comments),
            "a prism document's bytes are a DeckDoc, not a page — only the "
            "SPA's deck runtime renders one, so there is nothing here to "
            "photograph",
        )
    if not version.get("renderable"):
        return _nothing(
            len(comments),
            f"v{version['seq']} kept its content hash and not its bytes, so "
            "there is nothing to render",
        )

    pending = [
        c for c in comments
        if c.get("anchor")
        and (overwrite or not c.get("screenshot"))
    ]
    if not pending:
        return {"made": 0, "skipped": 0, "reason": None, "paths": []}

    ok, why = playwright_available()
    if not ok:
        return _nothing(len(pending), why)

    entry = _entry_path(document, version)
    if entry is None or not entry.is_file():
        return _nothing(len(pending), f"no entry document on disk for v{version['seq']}")

    return _run(document, version, pending, entry)


def _entry_path(document: dict, version: dict) -> Optional[Path]:
    if document["kind"] == "file":
        return Path(document["ref"])
    root = version.get("root_path")
    if not root:
        return None
    return Path(root) / redline_svc.entry_name(document["kind"], document["ref"])


def _nothing(skipped: int, reason: Optional[str]) -> dict:
    return {"made": 0, "skipped": skipped, "reason": reason, "paths": []}


def _run(document: dict, version: dict, pending: list[dict], entry: Path) -> dict:
    from playwright.sync_api import sync_playwright

    made: list[str] = []
    skipped = 0
    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--no-sandbox"])
        try:
            page = browser.new_page(viewport=dict(VIEWPORT), device_scale_factor=1)
            page.goto(entry.as_uri(), wait_until="load", timeout=LOAD_TIMEOUT_MS)
            # A webfont landing after first paint moves every rect on the page,
            # so a crop taken before fonts settle can miss its own element.
            try:
                page.evaluate("() => document.fonts && document.fonts.ready")
            except Exception:  # noqa: BLE001 — a page without the API is fine
                pass
            for comment in pending:
                path = _crop_one(page, document, version, comment)
                if path is None:
                    skipped += 1
                else:
                    made.append(str(path))
        finally:
            browser.close()

    reason = None
    if not made:
        reason = (
            "no comment produced a single-match selector — nothing was cropped "
            "rather than cropping the wrong element"
        )
    return {"made": len(made), "skipped": skipped, "reason": reason, "paths": made}


def _crop_one(page: Any, document: dict, version: dict, comment: dict) -> Optional[Path]:
    selector = selector_for(comment.get("anchor") or {})
    if not selector:
        return None
    try:
        matches = page.query_selector_all(selector)
    except Exception as exc:  # noqa: BLE001 — an unparseable selector is a skip
        logger.debug("redline crop: selector %r failed: %s", selector, exc)
        return None
    if len(matches) != 1:
        # Exactly the case the resolver calls an orphan. No crop.
        return None
    box = matches[0].bounding_box()
    if not box or box["width"] <= 0 or box["height"] <= 0:
        # An element with no box is real and common here — the invisible toggle
        # that drives the whole page is `opacity:0; pointer-events:none`.
        return None

    page_height = page.evaluate(
        "() => Math.max(document.documentElement.scrollHeight, document.body"
        " ? document.body.scrollHeight : 0)"
    )
    clip = {
        "x": max(box["x"] - PAD, 0),
        "y": max(box["y"] - PAD, 0),
        "width": box["width"] + 2 * PAD,
        "height": box["height"] + 2 * PAD,
    }
    clip["width"] = min(clip["width"], VIEWPORT["width"] - clip["x"])
    clip["height"] = min(clip["height"], float(page_height) - clip["y"])
    if clip["width"] <= 0 or clip["height"] <= 0:
        return None

    out = redline_svc._shot_path(document["id"], version["seq"], comment["seq"])
    out.parent.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(out), clip=clip, full_page=True)
    return out
