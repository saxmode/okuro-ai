# SPDX-License-Identifier: Apache-2.0
"""okuro design engine.

A clean-room build of the system the owner describes in note OKURO-DS and the
session around it. The verbatim source is artifact
5712acd7-8bc6-4288-85d5-9e3a0ab9d4e1 ("THE BASE"); the P0 answers that amend it
are memories 91c5d24c and 84cb1682.

    "Figma export is not relevant now. The engine is."

Nothing in this package imports `okuro.design_systems`, and nothing in it was
derived from a Figma export. Every behaviour traces to a quote; the fixture
suite in tests/design_engine/test_fixtures.py carries the quotes.

The shape:

    schema   -- what a brand AUTHORS
    colour   -- OKLab, the 0.675 polarity test, compositing, shadow physics
    fonts    -- weight descriptors and the numbers read off the faces
    scale    -- the whole sizing engine: value = base x factor
    sizing   -- the text and component tables, downscale, SCALES
    resolve  -- the ground descent and the full derived palette
    emit     -- the whole system as one static sheet
    kits     -- the authored brands the engine ships with

Everything else in the system is a consumer of `resolve`.
"""

from .colour import POLARITY_THRESHOLD, WARN_BAND, Flag, polarity, separates
from .kits import Kit, okuro_ds
from .resolve import (
    ALTERNATE_STEPS,
    AlphaStep,
    Ground,
    Ladder,
    Palette,
    Request,
    Resolver,
    ShadowAnswer,
    States,
)
from .schema import Brand, BrandColours, Neutrals, Signals, lint

__all__ = [
    "ALTERNATE_STEPS",
    "AlphaStep",
    "Brand",
    "BrandColours",
    "Flag",
    "Kit",
    "Ground",
    "Ladder",
    "Neutrals",
    "POLARITY_THRESHOLD",
    "Palette",
    "Request",
    "Resolver",
    "ShadowAnswer",
    "Signals",
    "States",
    "WARN_BAND",
    "lint",
    "okuro_ds",
    "polarity",
    "separates",
]
