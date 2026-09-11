# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: enable `python -m okuro.voice` — delegate to the voice CLI group.
# index: content
# AGENT_HEADER_END -->
"""Module entry point so `python -m okuro.voice` works.

The installed `okuro voice` entrypoint and `python -m okuro voice` both
route through okuro.cli.cmd_voice already; this mirrors that for the
package-direct invocation path.
"""

from okuro.cli.cmd_voice import voice

if __name__ == "__main__":
    voice()
