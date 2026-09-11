// <!-- AGENT_HEADER
// role: code
// purpose: Node inspector for the workflow designer — every field of the
//   compiler's node contract, editable, with role constrained to the live
//   catalogue so an invalid role cannot be authored.
// AGENT_HEADER_END -->
import React from "react";
import { STEP } from "@/lib/nouns";

import { Switch } from "@/components/ui/switch";
import type { RoleInfo } from "@/types/api";

import {
  COMPLEXITIES,
  DEFAULT_COMPLEXITY,
  DEFAULT_RISK,
  RISKS,
  nodeIssues,
  type Complexity,
  type NodeData,
  type Risk,
} from "./contract";

interface Props {
  nodeId: string;
  data: NodeData;
  roles: RoleInfo[];
  /** Phases already on the canvas — offered as choices so an author reuses a
   *  phase instead of inventing 7 of them by accident. */
  knownPhases: number[];
  onChange: (patch: NodeData) => void;
}

const roleIds = (roles: RoleInfo[]) => roles.map((r) => r.id);

/** Every field the compiler reads, in the order an author fills them: WHEN it
 *  runs (phase), WHO runs it (role), WHAT they do (prompt), what counts as done
 *  (acceptance criteria), then the dials.
 *
 *  ``role`` is a picker over the live catalogue and never a text input. The
 *  compiler REFUSES an unknown role, and a free-text field would let an author
 *  save a workflow that can only fail at Compile — a mistake that is trivially
 *  preventable at the point of authoring.
 */
export function WorkflowInspector({ nodeId, data, roles, knownPhases, onChange }: Props) {
  const issues = nodeIssues(data, roleIds(roles));
  const set = (patch: NodeData) => onChange({ ...data, ...patch });

  const criteria = data.acceptance_criteria ?? [];
  const outputs = data.outputs ?? [];
  const fanOn = !!data.fan_out;

  // Grouped by domain so a 90-role catalogue is navigable; ids stay the values
  // because the id is what the compiler matches against.
  const byDomain = React.useMemo(() => {
    const groups = new Map<string, RoleInfo[]>();
    for (const r of [...roles].sort((a, b) => a.id.localeCompare(b.id))) {
      const key = r.domain || "other";
      if (!groups.has(key)) groups.set(key, []);
      groups.get(key)!.push(r);
    }
    return [...groups.entries()].sort((a, b) => a[0].localeCompare(b[0]));
  }, [roles]);

  // A role that is set but absent from the catalogue (hand-edited graph, or a
  // role deleted since) must stay selectable, or opening the inspector would
  // silently rewrite it to the first role in the list.
  const roleMissing = !!data.role && roles.length > 0 && !roleIds(roles).includes(String(data.role));

  return (
    <div data-testid="wf-inspector" data-node-id={nodeId}>
      {issues.length > 0 && (
        <div className="wf-issues" role="alert" data-testid="wf-issues">
          <ul>
            {issues.map((m) => (
              <li key={m}>{m}</li>
            ))}
          </ul>
        </div>
      )}

      <div className="wf-field">
        <label className="wf-label" htmlFor="wf-phase">
          {STEP}
        </label>
        <div className="wf-row">
          <input
            id="wf-phase"
            className={"wf-input" + (Number(data.phase) >= 1 ? "" : " invalid")}
            type="number"
            min={1}
            step={1}
            value={data.phase ?? ""}
            onChange={(e) =>
              set({ phase: e.target.value === "" ? undefined : Number(e.target.value) })
            }
          />
          <input
            className="wf-input"
            placeholder="step name (optional)"
            aria-label="Step name"
            value={String(data.phase_name ?? "")}
            onChange={(e) => set({ phase_name: e.target.value })}
          />
        </div>
        <div className="wf-hint">
          Steps run in order; nodes in the same step run together. The step is
          explicit — drawing an edge does not move a node into a later step.
          {knownPhases.length > 1 ? ` On this canvas: ${knownPhases.join(", ")}.` : ""}
        </div>
      </div>

      <div className="wf-field">
        <label className="wf-label" htmlFor="wf-role">
          Role
        </label>
        <select
          id="wf-role"
          className={"wf-select" + (data.role ? "" : " invalid")}
          value={String(data.role ?? "")}
          onChange={(e) => set({ role: e.target.value })}
        >
          <option value="">— pick a role —</option>
          {roleMissing && (
            <option value={String(data.role)}>{String(data.role)} (not in catalogue)</option>
          )}
          {byDomain.map(([domain, list]) => (
            <optgroup key={domain} label={domain}>
              {list.map((r) => (
                <option key={r.id} value={r.id}>
                  {r.id}
                </option>
              ))}
            </optgroup>
          ))}
        </select>
        {roles.length === 0 && (
          <div className="wf-hint">Role catalogue unavailable — Compile will check it.</div>
        )}
      </div>

      <div className="wf-field">
        <label className="wf-label" htmlFor="wf-prompt">
          Prompt
        </label>
        <textarea
          id="wf-prompt"
          className="wf-textarea"
          placeholder="The brief this subagent receives…"
          value={String(data.prompt ?? "")}
          onChange={(e) => set({ prompt: e.target.value })}
        />
        <div className="wf-hint">
          {"{placeholders}"} are filled at compile time. A fan-out node also gets{" "}
          {"{item.field}"} from each item.
        </div>
      </div>

      <div className="wf-field">
        <span className="wf-label">Acceptance criteria</span>
        <ListEditor
          value={criteria}
          placeholder="what must be true when this is done"
          emptyLabel="No criteria — the reviewer has nothing specific to check."
          addLabel="+ criterion"
          onChange={(v) => set({ acceptance_criteria: v })}
        />
      </div>

      <div className="wf-field">
        <div className="wf-toggle">
          <label className="wf-label" htmlFor="wf-fanout">
            Fan out
          </label>
          <Switch
            id="wf-fanout"
            checked={fanOn}
            onCheckedChange={(on) => set({ fan_out: on ? { over: String(data.fan_out?.over ?? "") } : undefined })}
          />
        </div>
        {fanOn && (
          <>
            <input
              className={"wf-input" + (String(data.fan_out?.over ?? "").trim() ? "" : " invalid")}
              placeholder="list name, e.g. topics"
              aria-label="Fan out over"
              value={String(data.fan_out?.over ?? "")}
              onChange={(e) => set({ fan_out: { over: e.target.value } })}
            />
            <div className="wf-hint" data-testid="wf-fanout-hint">
              ONE PART PER ITEM. This node becomes N parts at runtime — one
              for every item in{" "}
              <strong>{String(data.fan_out?.over ?? "the list").trim() || "the list"}</strong> —
              and everything downstream waits for all of them.
            </div>
          </>
        )}
      </div>

      <div className="wf-field">
        <div className="wf-row">
          <div>
            <label className="wf-label" htmlFor="wf-risk">
              Risk
            </label>
            <select
              id="wf-risk"
              className="wf-select"
              value={String(data.risk ?? DEFAULT_RISK)}
              onChange={(e) => set({ risk: e.target.value as Risk })}
            >
              {RISKS.map((r) => (
                <option key={r} value={r}>
                  {r}
                </option>
              ))}
            </select>
          </div>
          <div>
            {/* This field IS the model tier — `complexity` is the plan's name
                for it, and its values are the canonical tier vocabulary
                (config.CANONICAL_TIERS). Labelled "Tier" because that is what
                choosing it actually does: config.resolve_unit_tier reads it to
                pick the model this node runs on. */}
            <label className="wf-label" htmlFor="wf-complexity">
              Tier <span className="wf-hint-inline">(model)</span>
            </label>
            <select
              id="wf-complexity"
              className="wf-select"
              value={String(data.complexity ?? DEFAULT_COMPLEXITY)}
              onChange={(e) => set({ complexity: e.target.value as Complexity })}
            >
              {COMPLEXITIES.map((c) => (
                <option key={c} value={c}>
                  {c}
                </option>
              ))}
            </select>
          </div>
        </div>
      </div>

      <div className="wf-field">
        <label className="wf-label" htmlFor="wf-artifact">
          Artifact name
        </label>
        <input
          id="wf-artifact"
          className="wf-input"
          placeholder="optional — names the deliverable"
          value={String(data.artifact_name ?? "")}
          onChange={(e) => set({ artifact_name: e.target.value })}
        />
      </div>

      <div className="wf-field">
        <span className="wf-label">Outputs</span>
        <ListEditor
          value={outputs}
          placeholder="file this part writes"
          emptyLabel="No declared outputs."
          addLabel="+ output"
          onChange={(v) => set({ outputs: v })}
        />
      </div>

      <div className="wf-field">
        <div className="wf-toggle">
          <label className="wf-label" htmlFor="wf-serialize">
            Serialize step
          </label>
          <Switch
            id="wf-serialize"
            checked={!!data.serialize}
            onCheckedChange={(on) => set({ serialize: on || undefined })}
          />
        </div>
        <div className="wf-hint">
          Step-level, not node-level: any node setting this makes the WHOLE
          step run one part at a time.
        </div>
      </div>
    </div>
  );
}

interface ListEditorProps {
  value: string[];
  placeholder: string;
  emptyLabel: string;
  addLabel: string;
  onChange: (next: string[]) => void;
}

/** A list of short strings. Rows are kept even while blank — trimming happens on
 *  save (``normalizeSubtaskData``), so typing does not fight the editor. */
function ListEditor({ value, placeholder, emptyLabel, addLabel, onChange }: ListEditorProps) {
  return (
    <div>
      {value.length === 0 && <div className="wf-list-empty">{emptyLabel}</div>}
      {value.map((item, i) => (
        <div className="wf-list-row" key={i}>
          <input
            className="wf-input"
            placeholder={placeholder}
            aria-label={`${addLabel.replace("+ ", "")} ${i + 1}`}
            value={item}
            onChange={(e) => onChange(value.map((v, j) => (j === i ? e.target.value : v)))}
          />
          <button
            type="button"
            className="wf-list-del"
            aria-label={`Remove ${addLabel.replace("+ ", "")} ${i + 1}`}
            onClick={() => onChange(value.filter((_, j) => j !== i))}
          >
            ×
          </button>
        </div>
      ))}
      <button type="button" className="fd-btn" onClick={() => onChange([...value, ""])}>
        {addLabel}
      </button>
    </div>
  );
}
