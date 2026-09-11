# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Corpus adapter contract — the two-phase interface (enumerate → fetch)
#   every source implements, plus the ItemRef/CorpusItem records passed between
#   the phases and the lifecycle.
# index: class ItemRef | class CorpusItem | class CorpusAdapter | def frontmatter
# AGENT_HEADER_END -->
"""What a corpus source must provide.

The interface is deliberately TWO-PHASE:

    enumerate() -> list[ItemRef]     cheap; id + version token only
    fetch(ref)  -> CorpusItem        expensive; the actual body

Delta sync is the whole reason. A one-phase ``iter_documents()`` would force a
full body download on every sync just to discover that 361 of 363 pages are
unchanged. With a version token in the listing, a re-sync of an idle Confluence
space is ONE http request and zero page fetches.

The token is opaque to okuro — Confluence hands back a monotonic version
number, a folder hands back ``mtime:size``. The only contract is that it
changes when the content changes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Protocol


@dataclass(frozen=True)
class ItemRef:
    """A source item as seen from the cheap listing pass.

    Attributes:
        key:      stable source-side identity (page id, relative path). Survives
                  a rename — which is why it, not the title, keys the manifest.
        version:  opaque change token. Different string ⇒ re-fetch.
        title:    human title, used for the on-disk filename.
        segments: ancestor titles, root-first. Becomes the directory path, so a
                  cortex hit's file path reads back as the breadcrumb.
        url:      canonical source url for citation, if the source has one.
    """

    key: str
    version: str
    title: str
    segments: tuple[str, ...] = ()
    url: str | None = None


@dataclass
class CorpusItem:
    """A fetched item, normalized to markdown."""

    ref: ItemRef
    body: str
    metadata: dict = field(default_factory=dict)


class CorpusAdapter(Protocol):
    """Contract for a corpus source.

    ``source_type`` is the registry dispatch key and must match the name the
    adapter is registered under.

    ``materializes`` says whether okuro writes files for this source:
      True  — remote source; items are written under corpora/<ws>/<name>.
      False — the source is ALREADY a directory on disk (local_folder). Nothing
              is copied; the corpus path IS the user's folder. This distinction
              is what keeps remove_corpus from ever deleting a user's own files.
    """

    source_type: str
    materializes: bool

    def describe(self) -> dict:
        """Human/agent-readable summary of what this adapter points at."""
        ...

    def enumerate(self) -> Iterable[ItemRef]:
        """List every item with a version token. Cheap — no bodies."""
        ...

    def fetch(self, ref: ItemRef) -> CorpusItem:
        """Retrieve one item's content as markdown."""
        ...


def frontmatter(item: CorpusItem) -> str:
    """Render the YAML header prepended to every materialized item.

    Carries the facts a chunk needs to stay citable after retrieval — title,
    source url, version, breadcrumb. cortex indexes the file as markdown, so
    this block travels with the first chunk and gives an agent the provenance
    without a second lookup.

    Values are emitted as JSON scalars (json.dumps) rather than bare strings:
    a page titled ``Preis: 12`` or one holding a quote character produces
    invalid YAML unquoted, and Confluence titles are user-authored text.
    """
    import json

    ref = item.ref
    lines = ["---"]
    lines.append(f"title: {json.dumps(ref.title, ensure_ascii=False)}")
    if ref.url:
        lines.append(f"source_url: {json.dumps(ref.url, ensure_ascii=False)}")
    lines.append(f"source_key: {json.dumps(ref.key, ensure_ascii=False)}")
    lines.append(f"version: {json.dumps(ref.version, ensure_ascii=False)}")
    if ref.segments:
        crumb = " / ".join(ref.segments)
        lines.append(f"breadcrumb: {json.dumps(crumb, ensure_ascii=False)}")
    for k, v in sorted(item.metadata.items()):
        if v is None or isinstance(v, (dict, list)):
            continue
        lines.append(f"{k}: {json.dumps(v, ensure_ascii=False)}")
    lines.append("---")
    return "\n".join(lines)
