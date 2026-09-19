// <!-- AGENT_HEADER
// role: code
// purpose: okuro-flow gallery — the /flow landing page. Visual tile grid (icon
//   + name + per-tile folder picker), a nestable folder rail with drag-to-assign
//   and counts, plus live search + sort. Replaces the old picker dropdown.
// AGENT_HEADER_END -->
import { ArrowDownAZ, Clock, Folder, FolderPlus, Pencil, Plus, Search, Trash2, Workflow } from "lucide-react";
import React from "react";
import { useNavigate } from "react-router";

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogTitle,
} from "@/components/ui/dialog";
import { flowDesignerApi, type FlowDesignerSummary, type FlowFolder } from "@/lib/api";

import { FlowThumbnail } from "./flow-thumbnail";
import { parseApiDate } from "@/lib/format";

const ACCENT = "var(--color-accent)";

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

// "all" | "ungrouped" | a folder id
type View = string;

export function FlowGallery() {
  const navigate = useNavigate();
  const [flows, setFlows] = React.useState<FlowDesignerSummary[]>([]);
  const [folders, setFolders] = React.useState<FlowFolder[]>([]);
  const [view, setView] = React.useState<View>("all");
  const [search, setSearch] = React.useState("");
  const [sort, setSort] = React.useState<"recent" | "name">("recent");
  const [loading, setLoading] = React.useState(true);
  const [dragId, setDragId] = React.useState<string | null>(null);
  // current drag-over drop target: a folder id, or "__ungrouped__"
  const [dropTarget, setDropTarget] = React.useState<string | null>(null);

  const refresh = React.useCallback(async () => {
    const [f, d] = await Promise.all([flowDesignerApi.list(), flowDesignerApi.folders()]);
    setFlows(f.flows);
    setFolders(d.folders);
    setLoading(false);
  }, []);

  React.useEffect(() => {
    refresh().catch(() => setLoading(false));
  }, [refresh]);

  const childFolders = React.useCallback(
    (parent: string | null) => folders.filter((f) => (f.parent_id || null) === parent),
    [folders],
  );
  const folderCount = React.useCallback((id: string) => flows.filter((f) => f.folder_id === id).length, [flows]);
  const folderName = React.useCallback((id: string | null) => folders.find((f) => f.id === id)?.name || "", [folders]);

  const visible = React.useMemo(() => {
    const q = search.trim().toLowerCase();
    let list = flows;
    if (q) list = flows.filter((f) => (f.name + " " + f.description).toLowerCase().includes(q));
    else if (view === "ungrouped") list = flows.filter((f) => !f.folder_id);
    else if (view !== "all") list = flows.filter((f) => f.folder_id === view);
    const sorted = [...list];
    if (sort === "name") sorted.sort((a, b) => a.name.localeCompare(b.name));
    else sorted.sort((a, b) => (b.updated_at || "").localeCompare(a.updated_at || ""));
    return sorted;
  }, [flows, view, search, sort]);

  const moveFlow = async (id: string, folderId: string | null) => {
    setFlows((fs) => fs.map((f) => (f.id === id ? { ...f, folder_id: folderId } : f)));
    try {
      await flowDesignerApi.move(id, folderId);
    } catch {
      refresh();
    }
  };
  const newFolder = async () => {
    const name = window.prompt("Folder name");
    if (!name) return;
    const parent = view !== "all" && view !== "ungrouped" ? view : null;
    const f = await flowDesignerApi.createFolder(name, parent);
    setFolders((fs) => [...fs, f]);
    setView(f.id);
  };
  const renameFolder = async (f: FlowFolder) => {
    const name = window.prompt("Rename folder", f.name);
    if (!name || name === f.name) return;
    const up = await flowDesignerApi.updateFolder(f.id, { name });
    setFolders((fs) => fs.map((x) => (x.id === f.id ? up : x)));
  };
  /**
   * R5 (372ccdb2), PERMANENT BRANCH — ONE modal for both deletions, and these
   * two were the last `window.confirm` calls in WORK.
   *
   * The editor's delete got its modal in this pass and these did not, which is
   * exactly the instance-scope miss DP11 names: the gallery deletes the SAME
   * documents through the SAME store. `graphdoc/store.py:274::delete_doc` runs
   * `DELETE FROM docs` AND `DELETE FROM history` in one transaction ("History
   * dies with its document"), so a flow cannot come back. A folder cannot
   * either — but its flows survive, and the sentence has to say which is
   * which, because that is the whole question the user is being asked.
   */
  const [pending, setPending] = React.useState<
    { kind: "folder"; id: string; name: string } | { kind: "flow"; id: string; name: string } | null
  >(null);

  const confirmDelete = async () => {
    const t = pending;
    setPending(null);
    if (!t) return;
    if (t.kind === "folder") {
      await flowDesignerApi.removeFolder(t.id);
      if (view === t.id) setView("all");
      refresh();
    } else {
      await flowDesignerApi.remove(t.id);
      setFlows((fs) => fs.filter((f) => f.id !== t.id));
    }
  };

  const deleteFolder = (f: FlowFolder) => setPending({ kind: "folder", id: f.id, name: f.name });
  const deleteFlow = (id: string, name: string) => setPending({ kind: "flow", id, name });

  const renderFolders = (parent: string | null, depth: number): React.ReactNode =>
    childFolders(parent).map((f) => (
      <React.Fragment key={f.id}>
        <div
          className={"fd-folder-row" + (view === f.id ? " sel" : "") + (dropTarget === f.id ? " drop" : "")}
          style={{ paddingLeft: 10 + depth * 14 }}
          onClick={() => setView(f.id)}
          onDragOver={(e) => {
            if (dragId) {
              e.preventDefault();
              setDropTarget(f.id);
            }
          }}
          onDragLeave={() => setDropTarget((t) => (t === f.id ? null : t))}
          onDrop={() => {
            if (dragId) moveFlow(dragId, f.id);
            setDropTarget(null);
          }}
        >
          <Folder size={14} className="fd-folder-ic" />
          <span className="fd-folder-name">{f.name}</span>
          <span className="fd-folder-count">{folderCount(f.id)}</span>
          <span className="fd-folder-acts">
            <button title="rename" onClick={(e) => { e.stopPropagation(); renameFolder(f); }}>
              <Pencil size={12} />
            </button>
            <button title="delete" onClick={(e) => { e.stopPropagation(); deleteFolder(f); }}>
              <Trash2 size={12} />
            </button>
          </span>
        </div>
        {renderFolders(f.id, depth + 1)}
      </React.Fragment>
    ));

  return (
    <div className="fd-gallery">
      <aside className="fd-gallery-rail">
        <div className="fd-rail-head">
          <span>Folders</span>
          <button title="new folder" onClick={newFolder}>
            <FolderPlus size={15} />
          </button>
        </div>
        <div className={"fd-folder-row" + (view === "all" ? " sel" : "")} onClick={() => setView("all")}>
          <Workflow size={14} className="fd-folder-ic" />
          <span className="fd-folder-name">All flows</span>
          <span className="fd-folder-count">{flows.length}</span>
        </div>
        <div
          className={"fd-folder-row" + (view === "ungrouped" ? " sel" : "") + (dropTarget === "__ungrouped__" ? " drop" : "")}
          onClick={() => setView("ungrouped")}
          onDragOver={(e) => {
            if (dragId) {
              e.preventDefault();
              setDropTarget("__ungrouped__");
            }
          }}
          onDragLeave={() => setDropTarget((t) => (t === "__ungrouped__" ? null : t))}
          onDrop={() => {
            if (dragId) moveFlow(dragId, null);
            setDropTarget(null);
          }}
        >
          <Folder size={14} className="fd-folder-ic" />
          <span className="fd-folder-name">Ungrouped</span>
          <span className="fd-folder-count">{flows.filter((f) => !f.folder_id).length}</span>
        </div>
        <div className="fd-rail-tree">{renderFolders(null, 0)}</div>
      </aside>

      <main className="fd-gallery-main">
        <div className="fd-gallery-toolbar">
          <div className="fd-search">
            <Search size={14} />
            <input placeholder="Search flows…" value={search} onChange={(e) => setSearch(e.target.value)} />
          </div>
          <button className="fd-sort" title="toggle sort" onClick={() => setSort((s) => (s === "recent" ? "name" : "recent"))}>
            {sort === "recent" ? <Clock size={13} /> : <ArrowDownAZ size={13} />}
            {sort === "recent" ? "Recent" : "Name"}
          </button>
          <div className="fd-gallery-spacer" />
          <Button size="sm" onClick={() => navigate("/work/flow?view=new")}>
            <Plus size={15} /> New flow
          </Button>
        </div>

        {loading ? (
          <p className="fd-gallery-empty">Loading…</p>
        ) : visible.length === 0 ? (
          <p className="fd-gallery-empty">
            {search ? "No flows match your search." : "No flows here yet. Create one with New flow."}
          </p>
        ) : (
          <div className="fd-flow-grid">
            {visible.map((f) => (
              <div
                key={f.id}
                className={"fd-flow-card" + (dragId === f.id ? " dragging" : "")}
                draggable
                onDragStart={() => setDragId(f.id)}
                onDragEnd={() => {
                  setDragId(null);
                  setDropTarget(null);
                }}
                onClick={() => navigate(`/work/flow/${encodeURIComponent(f.id)}`)}
                title={f.description || `Open ${f.name}`}
              >
                <FlowThumbnail id={f.id} accent={ACCENT} />
                <div className="fd-flow-body">
                  <div className="fd-flow-name">{f.name}</div>
                  <div className="fd-flow-sub">
                    Edited {relTime(f.updated_at)} · {f.node_count} nodes
                  </div>
                </div>
                <div className="fd-flow-foot" onClick={(e) => e.stopPropagation()}>
                  <Folder size={12} />
                  <select
                    className="fd-move-select"
                    value={f.folder_id || ""}
                    title="move to folder"
                    onChange={(e) => moveFlow(f.id, e.target.value || null)}
                  >
                    <option value="">Ungrouped</option>
                    {folders.map((fl) => (
                      <option key={fl.id} value={fl.id}>
                        {fl.name}
                      </option>
                    ))}
                  </select>
                </div>
                <button
                  className="fd-flow-del"
                  title="delete flow"
                  onClick={(e) => {
                    e.stopPropagation();
                    deleteFlow(f.id, f.name);
                  }}
                >
                  <Trash2 size={13} />
                </button>
                {f.folder_id && view === "all" && !search && <span className="fd-flow-badge">{folderName(f.folder_id)}</span>}
              </div>
            ))}
          </div>
        )}
      </main>

      {/* `ui/dialog` steers `open` through `usePaneModalOpen`, so this cannot
          survive a topic change and leave the app unclickable. */}
      <Dialog open={!!pending} onOpenChange={(o) => !o && setPending(null)}>
        <DialogContent className="max-w-md">
          {pending && (
            <>
              <DialogTitle>Delete “{pending.name}”?</DialogTitle>
              <DialogDescription>
                {pending.kind === "flow" ? (
                  <>
                    This removes the flow AND its version history in one
                    transaction — the store deletes both together on purpose,
                    so a later flow of the same name cannot inherit these
                    snapshots. There is nothing to restore afterwards.
                  </>
                ) : (
                  <>
                    This removes the folder. Its flows and subfolders are{" "}
                    <b>not</b> deleted — they move to the parent — but the
                    folder itself does not come back.
                  </>
                )}
              </DialogDescription>
              <div className="flex justify-end gap-2">
                <Button variant="outline" size="sm" onClick={() => setPending(null)}>
                  Keep it
                </Button>
                <Button variant="destructive" size="sm" onClick={confirmDelete}>
                  Delete permanently
                </Button>
              </div>
            </>
          )}
        </DialogContent>
      </Dialog>
    </div>
  );
}
