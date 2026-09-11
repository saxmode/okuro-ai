# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: okuro-handover — general content-interchange between okuro tools.
#   A selection in any tool (notes span, flow subgraph, prism facet) is lifted
#   into one Content IR, then handed to any target tool whose intake contract is
#   satisfied. Public API re-exported here.
# index: import
# AGENT_HEADER_END -->
from okuro.handover.ir import ContentIR
from okuro.handover.registry import TARGETS, eligible, get_target, public_contract
from okuro.handover.service import (
    HandoverError,
    contract_for,
    handover,
    targets_for,
)

__all__ = [
    "ContentIR",
    "TARGETS",
    "eligible",
    "get_target",
    "public_contract",
    "HandoverError",
    "contract_for",
    "handover",
    "targets_for",
]
