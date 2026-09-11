# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: okuro·prism COMPILER — the kit-first pipeline (design v3 792f4fbf).
#   Source artifact id -> typed claims -> audience profile -> projection ->
#   outline -> authored master docs + tier map (+ code slicer) -> deck PagePlan
#   -> LLM slot-mapping (compose) -> filled-slot slide JSON the A4 renderer feeds
#   into the shipped kit templates. The LLM never styles, never emits geometry,
#   never invents layout — it only mines data, selects/ranks, authors prose, and
#   maps claims into named archetype slots. Everything visual is fixed in the kit.
# index: STAGES | PrismCompiler | compile | resume | retailor
# AGENT_HEADER_END -->
"""okuro·prism compiler package.

Eight stages (design v3 §4, brief A3):

1. mine     — source artifact id -> typed data claims (LLM, schema-validated,
              bounded re-ask). Entry is an okuro artifact id ONLY.
2. profile  — recipients (1-n) -> AudienceProfile. people graph -> web ->
              archetype. Person×brand ambiguity returns needs_disambiguation.
3. project  — claims × profile -> keep/cut/emphasize/jargon/depth/takeaway.
              SELECT/RANK only; claim DATA is immutable.
4. outline  — 4-7 topics, an arc, every kept claim in exactly one topic.
5. author   — ONE master doc per topic + tier 0-3 per claim; CODE slicer with
              hard asserts (superset chain, L3==master, one tier-0/topic,
              prose budgets L0<=12 / L1<=60 / L2<=150 words).
6. plan     — deck PagePlan over the topic×level grid using the 12 kit
              archetypes; code-checked adjacency / coverage / rhythm quotas.
7. compose  — LLM slot-mapping ONLY: claim -> archetype slot -> component via
              the kit shape matrix; ×2 A/B per slide with deterministic seeds.
8. cache    — content-hashed IRs per stage; resume --from <stage>; retailor =
              re-run profile onward on the cached mine output.

Pure-function stages (slicer, plan quotas, cache) have golden fixtures and no
LLM. LLM stages (mine, project, outline, author, compose) are schema-contract
tested and exercised by one recorded golden run.
"""

from __future__ import annotations

from okuro.prism.compiler.pipeline import (
    STAGES,
    PrismCompiler,
    compile_deck,
)

__all__ = ["STAGES", "PrismCompiler", "compile_deck"]
