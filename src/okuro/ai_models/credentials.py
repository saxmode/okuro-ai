# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Model-source credential resolution over the okuro keyring.
# index:
#   imports
#   _SOURCE_KEYS
#   def _storage
#   def resolve_token
#   def auth_header
#   def credential_status
# AGENT_HEADER_END -->
"""Resolve model-source API credentials (HuggingFace, Civitai) from the okuro
keyring — never .env, never hardcoded.

Discovery + acquisition degrade gracefully: a token is only *required* for
gated/private models or to lift anonymous rate limits, so ``resolve_token``
returns None (not an error) when absent. Canonical names are tried first, then
known aliases — stream E found duplicate entries (HF x2, Civitai x3) in the
live keyring; this reads through them without mutating (dedup/deletion is a
user-approved action, surfaced via ``credential_status``).
"""

from __future__ import annotations

from typing import Optional, Protocol

# source -> canonical keyring name + accepted aliases (canonical first)
_SOURCE_KEYS: dict[str, dict] = {
    "hf": {"canonical": "HF_TOKEN", "aliases": ["HUGGINGFACE_HUB_TOKEN"]},
    "huggingface": {"canonical": "HF_TOKEN", "aliases": ["HUGGINGFACE_HUB_TOKEN"]},
    "civitai": {
        "canonical": "CIVITAI_API_KEY",
        "aliases": ["CIVITAI_API_TOKEN", "CIVIT-RED-ACCESS"],
    },
}


class _KeyStore(Protocol):
    def get_key(self, name: str) -> Optional[str]: ...


def _storage() -> _KeyStore:
    from okuro.keyring import KeyringStorage

    return KeyringStorage()


def _names_for(source: str) -> list[str]:
    spec = _SOURCE_KEYS.get(source.lower())
    if not spec:
        return []
    return [spec["canonical"], *spec.get("aliases", [])]


def resolve_token(source: str, storage: Optional[_KeyStore] = None) -> Optional[str]:
    """First non-empty token for a source across canonical + aliases, else None."""
    store = storage or _storage()
    for name in _names_for(source):
        try:
            value = store.get_key(name)
        except Exception:
            value = None
        if value:
            return value
    return None


def auth_header(source: str, storage: Optional[_KeyStore] = None) -> dict[str, str]:
    """Bearer header for a source, or {} when no credential is present."""
    token = resolve_token(source, storage)
    return {"Authorization": f"Bearer {token}"} if token else {}


def credential_status(storage: Optional[_KeyStore] = None) -> dict[str, dict]:
    """Per-source credential state for the Models page (values never exposed).

    Reports which canonical/alias names are present, so the UI can show
    present/absent and flag duplicates worth canonicalizing — without ever
    deleting (user-approved action).
    """
    store = storage or _storage()
    out: dict[str, dict] = {}
    for source, spec in _SOURCE_KEYS.items():
        if source == "huggingface":
            continue  # alias of "hf"; report once
        canonical = spec["canonical"]
        present_names: list[str] = []
        for name in [canonical, *spec.get("aliases", [])]:
            try:
                if store.get_key(name):
                    present_names.append(name)
            except Exception:
                continue
        out[source] = {
            "present": bool(present_names),
            "canonical": canonical,
            "resolved_from": present_names[0] if present_names else None,
            "duplicates": [n for n in present_names if n != canonical],
        }
    return out
