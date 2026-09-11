# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Portable path resolution for managed corpora. Anchors the corpus
#   store on the per-user data dir (where okuro.db lives), mirroring
#   repos/paths.py so clones and corpora sit side by side.
# index: def corpora_root | def corpus_path | def corpus_id | def safe_segment
# AGENT_HEADER_END -->
"""Where materialized corpora live.

The store is ``<data-dir>/corpora/<workspace>/<name>``, resolved via the same
helper the DB uses — never a hardcoded machine path, because okuro ships to
many users.

``safe_segment`` is deliberately NOT ``slugify``: a corpus mirrors a source
hierarchy, and the on-disk path IS the breadcrumb an agent reads back out of a
cortex hit. Slugifying "Artikeltyp: Buch" to "artikeltyp-buch" throws away the
casing and spacing that make the path self-describing, so segments keep their
shape and only lose characters a filesystem cannot hold.
"""

from __future__ import annotations

import re
import unicodedata
from pathlib import Path

# Characters no mainstream filesystem accepts in a segment (Windows is the
# binding constraint — okuro ships there too), plus control chars.
_UNSAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

# Windows refuses these as base names regardless of extension.
_RESERVED = {
    "con", "prn", "aux", "nul",
    *(f"com{i}" for i in range(1, 10)),
    *(f"lpt{i}" for i in range(1, 10)),
}

# Budget per path SEGMENT. ext4 caps a name at 255 bytes; non-ASCII titles cost
# multiple bytes per char, so the limit is applied to the encoded form.
_MAX_SEGMENT_BYTES = 120


def corpora_root() -> Path:
    """Return ``<data-dir>/corpora`` and create it on demand."""
    from okuro.cli.db_helpers import default_db_path

    root = default_db_path().parent / "corpora"
    root.mkdir(parents=True, exist_ok=True)
    return root


def slugify(text: str) -> str:
    """Filesystem-safe slug for registry ids: lowercase, non-alnum → hyphen."""
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s or "corpus"


def safe_segment(text: str, fallback: str = "untitled") -> str:
    """Make one path segment safe while preserving human shape.

    Keeps spaces, case, and accents (NFC-normalised so the same title always
    produces the same bytes — macOS hands back NFD, which would otherwise make
    every sync see a "new" file). Collapses whitespace, strips characters the
    filesystem rejects, and truncates on a byte budget.
    """
    s = unicodedata.normalize("NFC", text or "")
    # Whitespace is collapsed BEFORE the unsafe-character pass. Tab/CR/LF fall
    # inside the \x00-\x1f control range, so stripping first turned a tab into
    # a hyphen while a space collapsed normally — same class of character,
    # two different outcomes. Caught by test_safe_segment_collapses_whitespace.
    s = re.sub(r"\s+", " ", s)
    s = _UNSAFE.sub("-", s).strip()
    # A trailing dot or space is silently dropped by Windows — strip it here so
    # the path we record matches the path that exists.
    s = s.rstrip(". ")
    if not s:
        return fallback
    if s.lower() in _RESERVED or s.lower().split(".")[0] in _RESERVED:
        s = f"{s}_"
    encoded = s.encode("utf-8")
    if len(encoded) > _MAX_SEGMENT_BYTES:
        # Cut on a character boundary, not a byte one.
        s = encoded[:_MAX_SEGMENT_BYTES].decode("utf-8", errors="ignore").rstrip()
    return s or fallback


def corpus_path(workspace: str, name: str) -> Path:
    """Resolve the corpus directory for ``workspace``/``name`` (not created)."""
    return corpora_root() / slugify(workspace) / slugify(name)


def corpus_id(workspace: str, name: str) -> str:
    """Stable registry id for a managed corpus: ``<workspace>__<name>``."""
    return f"{slugify(workspace)}__{slugify(name)}"
