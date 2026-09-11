# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: okuro package module-execution entry — enables `python -m okuro ...`.
# index: imports | __main__
# AGENT_HEADER_END -->
"""Enable ``python -m okuro`` as an equivalent to the ``okuro`` console script.

Both invocations dispatch the same Click group from ``okuro.cli.main``.
This keeps module-execution (`python -m okuro voice --help`) working in
environments where the ``okuro`` script is not on ``$PATH`` (CI, sub-venvs,
`uv run -m`, test harnesses).
"""

from okuro.cli.main import cli

if __name__ == "__main__":
    cli()
