# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Bootstrap assembler — builds agent context packets in two halves.
# index: none
# AGENT_HEADER_END -->
"""Bootstrap assembler — builds agent context packets.

``assemble`` = the CORE half (session-invariant).
``assemble_project`` = the PROJECT half for a resolved slug.
``assemble_full`` = both, for in-process consumers only — see the assembler
module docstring for why that is never the MCP tool surface.
"""

from .assembler import assemble, assemble_project, assemble_full

__all__ = ["assemble", "assemble_project", "assemble_full"]
