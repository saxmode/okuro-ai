# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Centralized secret retrieval for Okuro Orchestrator.
# index: def get_key
# AGENT_HEADER_END -->
"""Centralized secret retrieval for Okuro Orchestrator."""

import logging

logger = logging.getLogger("okuro.orchestrator.secrets")

_cache: dict[str, str] = {}


def get_key(key_name: str) -> str:
    """Retrieve a secret from okuro.keyring.

    Args:
        key_name: Key path (e.g. 'supabase/service_role_key')

    Returns:
        Key value string.

    Raises:
        ValueError: If key not found.
    """
    if key_name in _cache:
        return _cache[key_name]

    try:
        from okuro.keyring.storage import KeyringStorage
        ks = KeyringStorage()  # resolves password via OS keyring / env var
        value = ks.get_key(key_name)
        if value:
            _cache[key_name] = value
            return value
    except Exception as e:
        logger.warning(f"Failed to retrieve key '{key_name}' from keyring: {e}")

    raise ValueError(f"Key '{key_name}' not found in okuro keyring")
