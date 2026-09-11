// <!-- AGENT_HEADER
// role: code
// purpose: /workflows landing — find, open or start a drawn workflow.
// AGENT_HEADER_END -->
import { Plus, Search, Workflow } from "lucide-react";
import React from "react";
import { useNavigate } from "react-router";

import { Button } from "@/components/ui/button";
import { workflowApi, type WorkflowSummary } from "@/lib/api";
import { parseApiDate } from "@/lib/format";

function relTime(iso: string): string {
  if (!iso) return "";
  const t = parseApiDate(iso).getTime();
  if (Number.isNaN(t)) return "";
  const s = Math.max(0, (Date.now() - t) / 1000);
  if (s < 60) return "just now";
  const m = s / 60;
  if (m < 60) return `${Math.floor(m)}m ago`;
  const h = m / 60;
  if (h < 24) return `${Math.floor(h)}h ago`;
  const d = h / 24;
  if (d < 30) return `${Math.floor(d)}d ago`;
  return `${Math.floor(d / 30)}mo ago`;
}

export function WorkflowGallery() {
  const navigate = useNavigate();
  const [items, setItems] = React.useState<WorkflowSummary[]>([]);
  const [search, setSearch] = React.useState("");
  const [loading, setLoading] = React.useState(true);

  React.useEffect(() => {
    let live = true;
    workflowApi
      .list()
      .then((r) => live && (setItems(r.workflows), setLoading(false)))
      .catch(() => live && setLoading(false));
    return () => {
      live = false;
    };
  }, []);

  const q = search.trim().toLowerCase();
  const shown = q
    ? items.filter(
        (w) => w.name.toLowerCase().includes(q) || w.description.toLowerCase().includes(q),
      )
    : items;

  return (
    <div className="wf-gallery h-full w-full overflow-y-auto p-6">
      <div className="mb-5 flex items-center gap-3">
        <Workflow className="h-5 w-5 text-accent" />
        <div className="flex-1">
          <h1 className="text-base font-semibold">Workflows</h1>
          <p className="text-xs text-fg-muted">
            Node graphs arranged by hand and compiled to an orchestrator plan —
            no decomposition, no LLM.
          </p>
        </div>
        <div className="relative">
          <Search className="pointer-events-none absolute left-2 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-fg-subtle" />
          <input
            className="wf-input pl-7"
            style={{ width: 220 }}
            placeholder="search"
            aria-label="Search workflows"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
          />
        </div>
        <Button onClick={() => navigate("/workflows?new=1")}>
          <Plus className="h-4 w-4" />
          New workflow
        </Button>
      </div>

      {loading ? (
        <div className="fd-placeholder">loading…</div>
      ) : shown.length === 0 ? (
        <div className="fd-placeholder">
          {items.length === 0
            ? "No workflows yet. A workflow is a drawing: one node per part, edges for dependencies."
            : "Nothing matches that search."}
        </div>
      ) : (
        <div className="wf-grid">
          {shown.map((w) => (
            <button
              key={w.id}
              className="wf-card"
              onClick={() => navigate(`/workflows?id=${encodeURIComponent(w.id)}`)}
            >
              <span className="wf-card-name">{w.name}</span>
              {w.description && <span className="wf-card-desc">{w.description}</span>}
              <span className="wf-card-meta">
                <span>{w.node_count} nodes</span>
                <span>{w.edge_count} edges</span>
                <span>{relTime(w.updated_at)}</span>
              </span>
            </button>
          ))}
        </div>
      )}
    </div>
  );
}
