# SPDX-License-Identifier: Apache-2.0
"""PRISM v4 W3 — depth-resolution authoring (engine 1 + engine 2).

Turns a mined TopicSource (claims + L4 master doc) into a resolution ladder of
typed ``SlidePlan``s (L1 hook -> L2 -> L3 densest, resolved DOWNWARD from the L4
master), translates each information unit into a component instance via the W1
manifest fit-scores + capacity, and proves the ladder with a blocking code gate
(coverage maps incl. L4-coverage) + accuracy accounting. The W2 solver then lays
each SlidePlan out; the viewer (W4) renders the DeckDoc fields.
"""
