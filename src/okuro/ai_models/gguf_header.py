# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: source
# purpose: Read ONLY the key/value metadata header of a GGUF file — never a
#          tensor — so KV-cache arithmetic can be exact instead of guessed.
# index:
#   def read_kv          (the parser; returns the scalar KV map)
#   def attention_dims   (the five numbers fitting.py actually wants)
# AGENT_HEADER_END -->
"""A pure-Python reader for the GGUF metadata header.

``ingest.read_gguf_metadata`` already reads GGUF, through the ``gguf`` package
and ``GGUFReader``. This module exists beside it for two reasons, neither of
them a duplication of that job:

**No dependency.** ``gguf`` is an optional extra. The fit function runs on
every unit in the inventory, including on a host that never installed it, and
a fit that silently degrades to a heuristic because an import failed is a
worse answer than one that says which basis it used.

**It stops at the tensors.** ``GGUFReader`` memory-maps the file and indexes
every tensor's info block. This reads the header, walks the KV section, and
returns — it never seeks past the metadata, never touches tensor data, and
caps how far it will read. That is what makes it safe to run across a cold
store of spinning disks, which is the same constraint that shaped P1's
stat-only walk.

Format (GGUF v2/v3, little-endian): magic ``GGUF``, ``uint32`` version,
``uint64`` tensor count, ``uint64`` KV count, then that many ``(key, type,
value)`` triples.
"""

from __future__ import annotations

import logging
import struct
from typing import Any, Optional

log = logging.getLogger("okuro.ai_models.gguf_header")

GGUF_MAGIC = b"GGUF"

#: Give up rather than read further. A model's KV metadata is kilobytes plus a
#: tokenizer vocabulary; 64 MiB is far past any real header and bounds the cost
#: of a corrupt or hostile file to something a cold store can absorb.
MAX_HEADER_BYTES = 64 * 1024 * 1024

# GGUF value type ids.
_UINT8, _INT8, _UINT16, _INT16, _UINT32, _INT32 = 0, 1, 2, 3, 4, 5
_FLOAT32, _BOOL, _STRING, _ARRAY, _UINT64, _INT64, _FLOAT64 = 6, 7, 8, 9, 10, 11, 12

_FIXED = {
    _UINT8: ("<B", 1), _INT8: ("<b", 1),
    _UINT16: ("<H", 2), _INT16: ("<h", 2),
    _UINT32: ("<I", 4), _INT32: ("<i", 4),
    _FLOAT32: ("<f", 4), _BOOL: ("<?", 1),
    _UINT64: ("<Q", 8), _INT64: ("<q", 8), _FLOAT64: ("<d", 8),
}

#: Arrays longer than this are skipped without materialising. A tokenizer
#: vocabulary is ~150k strings and nothing here wants it; keeping the elements
#: would turn a metadata read into a megabyte of garbage per unit.
_ARRAY_KEEP_MAX = 64


class _Cursor:
    """A bounded forward reader over the header."""

    def __init__(self, fh):
        self._fh = fh
        self.read_bytes = 0

    def take(self, n: int) -> bytes:
        if n < 0 or self.read_bytes + n > MAX_HEADER_BYTES:
            raise ValueError("gguf header exceeds the read cap")
        b = self._fh.read(n)
        if len(b) != n:
            raise ValueError("gguf header truncated")
        self.read_bytes += n
        return b

    def skip(self, n: int) -> None:
        if n < 0 or self.read_bytes + n > MAX_HEADER_BYTES:
            raise ValueError("gguf header exceeds the read cap")
        self._fh.seek(n, 1)
        self.read_bytes += n

    def scalar(self, vtype: int) -> Any:
        fmt, size = _FIXED[vtype]
        return struct.unpack(fmt, self.take(size))[0]

    def string(self) -> str:
        n = struct.unpack("<Q", self.take(8))[0]
        return self.take(n).decode("utf-8", "replace")

    def skip_string(self) -> None:
        n = struct.unpack("<Q", self.take(8))[0]
        self.skip(n)

    def value(self, vtype: int) -> Any:
        if vtype in _FIXED:
            return self.scalar(vtype)
        if vtype == _STRING:
            return self.string()
        if vtype == _ARRAY:
            elem = struct.unpack("<I", self.take(4))[0]
            count = struct.unpack("<Q", self.take(8))[0]
            if count <= _ARRAY_KEEP_MAX and elem in _FIXED:
                return [self.scalar(elem) for _ in range(count)]
            # Skip wholesale. A fixed-width element array is one seek; a string
            # array has to be walked, because only each element's own length
            # prefix says how long it is.
            if elem in _FIXED:
                self.skip(_FIXED[elem][1] * count)
            elif elem == _STRING:
                for _ in range(count):
                    self.skip_string()
            else:
                raise ValueError(f"unsupported gguf array element type {elem}")
            return None
        raise ValueError(f"unsupported gguf value type {vtype}")


def read_kv(path: str) -> Optional[dict]:
    """The scalar key/value metadata of a GGUF file, or None.

    Returns None — never raises — when the file is not GGUF, is truncated, or
    uses a construct this reader does not know. A fit that falls back to the
    param heuristic is a correct answer with a weaker basis; an exception in a
    store walk is not.
    """
    try:
        with open(path, "rb") as fh:
            cur = _Cursor(fh)
            if cur.take(4) != GGUF_MAGIC:
                return None
            version = struct.unpack("<I", cur.take(4))[0]
            if version not in (1, 2, 3):
                log.debug("gguf %s: unsupported version %s", path, version)
                return None
            tensor_count = struct.unpack("<Q", cur.take(8))[0]
            kv_count = struct.unpack("<Q", cur.take(8))[0]
            if kv_count > 100_000:
                return None
            out: dict[str, Any] = {
                "_gguf_version": version,
                "_tensor_count": tensor_count,
            }
            for _ in range(kv_count):
                key = cur.string()
                vtype = struct.unpack("<I", cur.take(4))[0]
                val = cur.value(vtype)
                if val is not None:
                    out[key] = val
            return out
    except (OSError, ValueError, struct.error) as exc:
        log.debug("gguf header unreadable for %s: %s", path, exc)
        return None


def attention_dims(path: str) -> Optional[dict]:
    """``{arch, n_layers, n_kv_heads, head_dim, context_length, …}`` or None.

    ``head_dim`` follows the same derivation ``ingest.read_gguf_metadata``
    uses: the explicit ``attention.key_length`` when the architecture publishes
    it, else ``embedding_length / head_count``. A per-layer ``head_count_kv``
    list collapses to its MAXIMUM so the estimate can never under-count.
    """
    kv = read_kv(path)
    if not kv:
        return None
    arch = kv.get("general.architecture")
    if not arch:
        return None

    def g(suffix: str):
        return kv.get(f"{arch}.{suffix}")

    n_kv_heads = g("attention.head_count_kv")
    if isinstance(n_kv_heads, (list, tuple)) and n_kv_heads:
        n_kv_heads = max(int(x) for x in n_kv_heads)
    n_heads = g("attention.head_count")
    if isinstance(n_heads, (list, tuple)) and n_heads:
        n_heads = max(int(x) for x in n_heads)
    hidden = g("embedding_length")
    key_len = g("attention.key_length")

    head_dim = None
    if key_len:
        head_dim = int(key_len)
    elif hidden and n_heads:
        head_dim = int(hidden) // int(n_heads)

    out = {
        "arch": arch,
        "n_layers": int(g("block_count")) if g("block_count") else None,
        "n_kv_heads": int(n_kv_heads) if n_kv_heads else None,
        "head_dim": head_dim,
        "context_length": int(g("context_length")) if g("context_length") else None,
        "expert_count": int(g("expert_count")) if g("expert_count") else None,
        "expert_used_count": (int(g("expert_used_count"))
                              if g("expert_used_count") else None),
        "name": kv.get("general.name"),
        "size_label": kv.get("general.size_label"),
    }
    return out


__all__ = ["GGUF_MAGIC", "MAX_HEADER_BYTES", "attention_dims", "read_kv"]
