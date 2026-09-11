# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: okuro.ingress.adapters — one module per channel adapter
#   (telegram, slack, …). The supervisor imports adapters by channel
#   name; nothing else should import them directly.
# index: none
# AGENT_HEADER_END -->
"""Channel adapters. Imported by IngressSupervisor on enable."""
