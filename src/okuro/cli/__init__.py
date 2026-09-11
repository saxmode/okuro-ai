# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: okuro.cli — command-line interface.
# index: none
# AGENT_HEADER_END -->
"""okuro.cli — command-line interface."""

__all__ = ["cli"]


# Lazy export (PEP 562): importing the *package* must NOT eagerly import
# ``okuro.cli.main``. The Okuro.app bundle launches the CLI via
# ``python -m okuro.cli.main``; if the package __init__ pre-imports main,
# runpy then re-executes an already-imported module and prints
# "RuntimeWarning: 'okuro.cli.main' found in sys.modules ... prior to
# execution ... may result in unpredictable behaviour". Deferring the import
# keeps ``from okuro.cli import cli`` working while silencing it at the source.
def __getattr__(name):
    if name == "cli":
        from .main import cli
        return cli
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
