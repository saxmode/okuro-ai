# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Okuro — personal AI agent infrastructure.
# index: none
# AGENT_HEADER_END -->
"""Okuro — personal AI agent infrastructure."""

import os as _os
from pathlib import Path as _Path

_HF_DIR = _Path.home() / ".okuro" / "hf"
_os.environ.setdefault("HF_HOME", str(_HF_DIR))
_os.environ.setdefault("SENTENCE_TRANSFORMERS_HOME", str(_HF_DIR / "sentence-transformers"))

# Rewritten by `okuro release bump X.Y.Z` from pyproject.toml, the single
# source of truth; pinned equal by gate_version_consistency. Never hand-edit.
__version__ = "3.0.1"
