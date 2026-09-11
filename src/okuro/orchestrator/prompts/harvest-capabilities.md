<!-- AGENT_HEADER
role: doc
purpose: provides a framework for extracting reusable components from completed tasks
index:
  Capability Harvester
  Completed Task
  Artifacts Produced
  Code Changes
  Instructions
  Output Format
AGENT_HEADER_END -->
# Capability Harvester

You are analyzing a completed task to extract reusable building blocks (capabilities) that could power future projects.

## Completed Task

**Description:** {task_description}

## Artifacts Produced

{artifact_summaries}

## Code Changes

{code_diff_summary}

## Instructions

Analyze the task output above and extract **generic, reusable components** — not task-specific glue code.

For each capability you identify, provide:
- **id**: kebab-case identifier (e.g. `pixel-renderer-64x64`)
- **type**: exactly one of: `api-client`, `display-renderer`, `hardware-bridge`, `data-pipeline`, `web-ui`, `infra-pattern`, `ai-integration`, `data-source`
- **name**: human-readable name
- **description**: what it does (1-2 sentences)
- **interface**: function signature or API contract (e.g. `render(pixels: list[tuple[int,int,int]]) -> bytes`)
- **constraints**: list of limitations or requirements
- **reusable_for**: list of other things this could power — be creative and expansive
- **tags**: list of keywords for search
- **confidence**: 0.0–1.0 how reusable this is beyond the original task

Focus on:
- APIs, renderers, data pipelines, hardware bridges — things with clear interfaces
- Patterns that solve generic problems, not one-off task wiring
- Components that could be composed into new products or features

Skip:
- Configuration files, environment setup, boilerplate
- Task-specific orchestration or glue code
- Anything too tightly coupled to the original task context

If the task produced no reusable capabilities, return an empty list.

## Output Format

Output YAML only. No preamble, no explanation, no markdown fences.

```yaml
capabilities:
  - id: example-capability
    type: api-client
    name: Example Capability
    description: Does something reusable.
    interface: "do_thing(input: str) -> Result"
    constraints:
      - Requires network access
    reusable_for:
      - Building dashboards
      - Monitoring pipelines
    tags: [api, http, rest]
    confidence: 0.8
```
