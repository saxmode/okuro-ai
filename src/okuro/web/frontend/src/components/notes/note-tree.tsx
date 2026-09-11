// <!-- AGENT_HEADER
// role: code
// purpose: Sidebar folder tree for okuro-notes. Renders folders (adjacency list
//   via parent_id) with their notes nested underneath, root notes at the top.
//   Organise by native drag-and-drop: drop a note (or a multi-selection) on a
//   folder to file it, a folder on a folder to re-parent, empty space to root.
//   Notes support multiselect — plain click opens, Shift+click range-selects,
//   Cmd/Ctrl+click toggles; a drag carries the whole selection. No DnD library.
// index: types | NoteTree | FolderRow | NoteRow
// AGENT_HEADER_END -->
import { useEffect, useMemo, useRef, useState } from "react";
import {
  ChevronDown,
  ChevronRight,
  FilePlus,
  FileText,
  FolderClosed,
  FolderOpen,
  FolderPlus,
  Trash2,
} from "lucide-react";

import type { NoteFolder, NoteSummary } from "@/lib/api";
import { parseTs } from "@/lib/note-dates";
import { cn } from "@/lib/utils";
import {
  ContextMenu,
  ContextMenuContent,
  ContextMenuItem,
  ContextMenuSeparator,
  ContextMenuTrigger,
} from "@/components/ui/context-menu";

// Which timestamp column NoteRow surfaces — mirrors the sidebar sort key so the
// visible date matches what the list is ordered by.
type DateField = "updated_at" | "created_at";

// Compact relative age for the list ("now" / "5m" / "3h" / "2d"), falling back to
// a short calendar date once a note is a week old.
function relTime(s?: string): string {
  const d = parseTs(s);
  if (!d) return "";
  const sec = Math.round((Date.now() - d.getTime()) / 1000);
  if (sec < 45) return "now";
  const min = Math.round(sec / 60);
  if (min < 60) return `${min}m`;
  const hr = Math.round(min / 60);
  if (hr < 24) return `${hr}h`;
  const day = Math.round(hr / 24);
  if (day < 7) return `${day}d`;
  return d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

function noteDeleteLabel(targetCount: number): string {
  return targetCount > 1 ? `Delete ${targetCount} notes` : "Delete note";
}

function fullTs(s?: string): string {
  const d = parseTs(s);
  return d ? d.toLocaleString() : "—";
}

// Hover tooltip always carries BOTH timestamps, whichever one is shown inline.
function noteDatesTitle(note: NoteSummary): string {
  return `Created ${fullTs(note.created_at)}\nUpdated ${fullTs(note.updated_at)}`;
}

const EXPAND_KEY = "notes-tree-expanded";
// Pixels of extra left padding per nesting level. Combined with the fixed
// chevron column, every level steps clearly right — a real file tree.
const INDENT = 16;
// Drag payload: "note:<id>" | "notes:<id,id,…>" | "folder:<id>".
const DND_MIME = "application/x-okuro-note-item";
// Double-click means rename on every row, so a row's primary action (open the
// note, expand the folder) waits one double-click window and is dropped when a
// second click lands. Without the hold, renaming always fires the row action
// first — and on a folder that is a visible expand/collapse flicker, since a
// double-click toggles it twice. Modifier-clicks skip the hold: nobody
// shift-double-clicks, and selection should stay instant.
const DBLCLICK_MS = 200;

/** The only fields the click handler reads. Naming them lets a held click
 *  replay from a plain object rather than a synthetic event. */
type ClickMods = Pick<React.MouseEvent, "shiftKey" | "metaKey" | "ctrlKey">;
const PLAIN_CLICK: ClickMods = { shiftKey: false, metaKey: false, ctrlKey: false };

interface Props {
  notes: NoteSummary[];
  folders: NoteFolder[];
  dateField: DateField;
  activeId: string | null;
  onSelect: (id: string) => void;
  onMoveNotes: (noteIds: string[], folderId: string | null) => void;
  onMoveFolder: (folderId: string, parentId: string | null) => void;
  onCreateFolder: (parentId: string | null) => void;
  onRenameFolder: (id: string, name: string) => void;
  /** Rename a note. "" hands the title back to the first-body-line rule. */
  onRenameNote: (id: string, title: string) => void;
  onDeleteFolder: (id: string) => void;
  /** Create a note inside the given folder (null = root) — the context menu's
   *  "New note" action, scoped to wherever the user right-clicked. */
  onCreateNote: (folderId: string | null) => void;
  /** Delete one or more notes — the context menu's "Delete" action. */
  onDeleteNotes: (noteIds: string[]) => void;
  /** Folder just created — the matching row opens straight into rename. */
  newFolderId?: string | null;
  onFolderRenameConsumed?: () => void;
}

function readExpanded(): Set<string> {
  try {
    const raw = localStorage.getItem(EXPAND_KEY);
    if (raw) return new Set(JSON.parse(raw) as string[]);
  } catch {
    /* ignore */
  }
  return new Set();
}

export function NoteTree({
  notes,
  folders,
  dateField,
  activeId,
  onSelect,
  onMoveNotes,
  onMoveFolder,
  onCreateFolder,
  onRenameFolder,
  onRenameNote,
  onDeleteFolder,
  onCreateNote,
  onDeleteNotes,
  newFolderId,
  onFolderRenameConsumed,
}: Props) {
  const [expanded, setExpanded] = useState<Set<string>>(readExpanded);
  // folder id currently under a drag hover (highlight); "" = the root zone.
  const [dropTarget, setDropTarget] = useState<string | null>(null);
  // Multiselect: the set of highlighted note ids + the range anchor.
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [anchorId, setAnchorId] = useState<string | null>(null);

  const foldersByParent = useMemo(() => {
    const m = new Map<string | null, NoteFolder[]>();
    for (const f of folders) {
      const key = f.parent_id ?? null;
      (m.get(key) ?? m.set(key, []).get(key)!).push(f);
    }
    return m;
  }, [folders]);

  const notesByFolder = useMemo(() => {
    const m = new Map<string | null, NoteSummary[]>();
    for (const n of notes) {
      const key = n.folder_id ?? null;
      (m.get(key) ?? m.set(key, []).get(key)!).push(n);
    }
    return m;
  }, [notes]);

  // Note ids in visible render order — the basis for Shift range-selection.
  const visibleNoteIds = useMemo(() => {
    const out: string[] = [];
    const walk = (fid: string) => {
      for (const cf of foldersByParent.get(fid) ?? []) {
        if (expanded.has(cf.id)) walk(cf.id);
      }
      for (const n of notesByFolder.get(fid) ?? []) out.push(n.id);
    };
    for (const f of foldersByParent.get(null) ?? []) {
      if (expanded.has(f.id)) walk(f.id);
    }
    for (const n of notesByFolder.get(null) ?? []) out.push(n.id);
    return out;
  }, [foldersByParent, notesByFolder, expanded]);

  const toggle = (id: string) => {
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      try {
        localStorage.setItem(EXPAND_KEY, JSON.stringify([...next]));
      } catch {
        /* ignore */
      }
      return next;
    });
  };

  // Resolve a click on a note into a selection change (+ open on a plain click).
  const onNoteClick = (id: string, e: ClickMods) => {
    if (e.shiftKey && anchorId) {
      const a = visibleNoteIds.indexOf(anchorId);
      const b = visibleNoteIds.indexOf(id);
      if (a !== -1 && b !== -1) {
        const [lo, hi] = a < b ? [a, b] : [b, a];
        setSelected(new Set(visibleNoteIds.slice(lo, hi + 1)));
      }
      onSelect(id);
      return;
    }
    if (e.metaKey || e.ctrlKey) {
      setSelected((prev) => {
        const next = new Set(prev);
        if (next.has(id)) next.delete(id);
        else next.add(id);
        return next;
      });
      setAnchorId(id);
      onSelect(id);
      return;
    }
    setSelected(new Set([id]));
    setAnchorId(id);
    onSelect(id);
  };

  // What a note drag carries: the whole selection if the note is part of a
  // multi-selection, else just itself.
  const dragPayload = (id: string) =>
    selected.has(id) && selected.size > 1
      ? `notes:${[...selected].join(",")}`
      : `note:${id}`;

  // Context menu "Delete" target: the whole selection when the right-clicked
  // note is part of a multi-selection, else just that note.
  const deleteTargets = (id: string): string[] =>
    selected.has(id) && selected.size > 1 ? [...selected] : [id];

  // Right-clicking a note that isn't already selected replaces the selection
  // with just that note (standard file-manager behavior) — a note already
  // part of a multi-selection keeps the whole selection intact.
  const selectForContextMenu = (id: string) => {
    if (!selected.has(id)) {
      setSelected(new Set([id]));
      setAnchorId(id);
      onSelect(id);
    }
  };

  // Apply a drop onto folderId (null = root).
  const applyDrop = (payload: string, folderId: string | null) => {
    setDropTarget(null);
    const sep = payload.indexOf(":");
    if (sep < 0) return;
    const kind = payload.slice(0, sep);
    const rest = payload.slice(sep + 1);
    if (kind === "note") {
      onMoveNotes([rest], folderId);
      setSelected(new Set());
    } else if (kind === "notes") {
      onMoveNotes(rest.split(","), folderId);
      setSelected(new Set());
    } else if (kind === "folder" && rest !== folderId) {
      onMoveFolder(rest, folderId);
    }
  };

  const rootFolders = foldersByParent.get(null) ?? [];
  const rootNotes = notesByFolder.get(null) ?? [];

  return (
    <div
      className={cn(
        "min-h-0 flex-1 overflow-y-auto pb-8",
        dropTarget === "" && "bg-accent-subtle",
      )}
      onDragOver={(e) => {
        if (e.dataTransfer.types.includes(DND_MIME)) {
          e.preventDefault();
          setDropTarget("");
        }
      }}
      onDragLeave={(e) => {
        if (e.currentTarget === e.target) setDropTarget(null);
      }}
      onDrop={(e) => {
        e.preventDefault();
        applyDrop(e.dataTransfer.getData(DND_MIME), null);
      }}
    >
      {rootFolders.length === 0 && rootNotes.length === 0 ? (
        <p className="px-3 py-6 text-center text-xs text-tertiary">No notes yet</p>
      ) : (
        <>
          {rootFolders.map((f) => (
            <FolderRow
              key={f.id}
              folder={f}
              depth={0}
              dateField={dateField}
              foldersByParent={foldersByParent}
              notesByFolder={notesByFolder}
              expanded={expanded}
              dropTarget={dropTarget}
              activeId={activeId}
              selected={selected}
              onToggle={toggle}
              onNoteClick={onNoteClick}
              dragPayload={dragPayload}
              onCreateFolder={onCreateFolder}
              onRenameFolder={onRenameFolder}
              onRenameNote={onRenameNote}
              onDeleteFolder={onDeleteFolder}
              onCreateNote={onCreateNote}
              newFolderId={newFolderId}
              onFolderRenameConsumed={onFolderRenameConsumed}
              onSetDropTarget={setDropTarget}
              onApplyDrop={applyDrop}
              deleteTargets={deleteTargets}
              onDeleteNotes={onDeleteNotes}
              onContextSelect={selectForContextMenu}
            />
          ))}
          {rootNotes.map((n) => (
            <NoteRow
              key={n.id}
              note={n}
              depth={0}
              dateField={dateField}
              active={activeId === n.id}
              selected={selected.has(n.id)}
              onNoteClick={onNoteClick}
              dragPayload={dragPayload}
              onRename={onRenameNote}
              onContextSelect={selectForContextMenu}
              onDelete={() => onDeleteNotes(deleteTargets(n.id))}
              deleteLabel={noteDeleteLabel(deleteTargets(n.id).length)}
              onCreateNoteHere={() => onCreateNote(n.folder_id ?? null)}
              onCreateFolderHere={() => onCreateFolder(n.folder_id ?? null)}
            />
          ))}
        </>
      )}
    </div>
  );
}

interface FolderRowProps {
  folder: NoteFolder;
  depth: number;
  dateField: DateField;
  foldersByParent: Map<string | null, NoteFolder[]>;
  notesByFolder: Map<string | null, NoteSummary[]>;
  expanded: Set<string>;
  dropTarget: string | null;
  activeId: string | null;
  selected: Set<string>;
  onToggle: (id: string) => void;
  onNoteClick: (id: string, e: ClickMods) => void;
  dragPayload: (id: string) => string;
  onCreateFolder: (parentId: string | null) => void;
  onRenameFolder: (id: string, name: string) => void;
  /** Rename a note. "" hands the title back to the first-body-line rule. */
  onRenameNote: (id: string, title: string) => void;
  onDeleteFolder: (id: string) => void;
  onCreateNote: (folderId: string | null) => void;
  onSetDropTarget: (id: string | null) => void;
  onApplyDrop: (payload: string, folderId: string | null) => void;
  deleteTargets: (id: string) => string[];
  onDeleteNotes: (noteIds: string[]) => void;
  onContextSelect: (id: string) => void;
  newFolderId?: string | null;
  onFolderRenameConsumed?: () => void;
}

function FolderRow({
  folder,
  depth,
  dateField,
  foldersByParent,
  notesByFolder,
  expanded,
  dropTarget,
  activeId,
  selected,
  onToggle,
  onNoteClick,
  dragPayload,
  onCreateFolder,
  onRenameFolder,
  onRenameNote,
  onDeleteFolder,
  onCreateNote,
  onSetDropTarget,
  onApplyDrop,
  deleteTargets,
  onDeleteNotes,
  onContextSelect,
  newFolderId,
  onFolderRenameConsumed,
}: FolderRowProps) {
  const [renaming, setRenaming] = useState(false);
  const [draft, setDraft] = useState(folder.name);
  // Expand is held for one double-click window — see DBLCLICK_MS. Without it a
  // double-click toggles the folder open and shut before the rename box opens.
  const clickTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  useEffect(
    () => () => {
      if (clickTimer.current) clearTimeout(clickTimer.current);
    },
    [],
  );
  // A folder just created lands directly in rename mode with its name selected.
  useEffect(() => {
    if (newFolderId && folder.id === newFolderId) {
      setDraft(folder.name);
      setRenaming(true);
      onFolderRenameConsumed?.();
    }
  }, [newFolderId, folder.id, folder.name, onFolderRenameConsumed]);

  const isOpen = expanded.has(folder.id);
  const childFolders = foldersByParent.get(folder.id) ?? [];
  const childNotes = notesByFolder.get(folder.id) ?? [];
  const count = childFolders.length + childNotes.length;

  const commitRename = () => {
    setRenaming(false);
    const name = draft.trim();
    if (name && name !== folder.name) onRenameFolder(folder.id, name);
    else setDraft(folder.name);
  };

  const startRename = () => {
    if (clickTimer.current) {
      clearTimeout(clickTimer.current);
      clickTimer.current = null;
    }
    setDraft(folder.name);
    setRenaming(true);
  };

  return (
    <>
      <ContextMenu>
        <ContextMenuTrigger asChild>
          <div
            draggable={!renaming}
            onDragStart={(e) => {
              e.dataTransfer.setData(DND_MIME, `folder:${folder.id}`);
              e.dataTransfer.effectAllowed = "move";
            }}
            onDragOver={(e) => {
              if (e.dataTransfer.types.includes(DND_MIME)) {
                e.preventDefault();
                e.stopPropagation();
                onSetDropTarget(folder.id);
              }
            }}
            onDrop={(e) => {
              e.preventDefault();
              e.stopPropagation();
              onApplyDrop(e.dataTransfer.getData(DND_MIME), folder.id);
            }}
            style={{ paddingLeft: depth * INDENT + 6 }}
            className={cn(
              "group flex items-center gap-1.5 border-b border-border-subtle py-1.5 pr-2 text-left transition-colors hover:bg-surface-elevated",
              dropTarget === folder.id && "bg-accent-subtle ring-1 ring-inset ring-accent",
            )}
          >
            {/* fixed chevron column so folder + note icons line up as tree columns */}
            <span className="flex h-4 w-4 shrink-0 items-center justify-center">
              {count > 0 && (
                <button
                  onClick={() => onToggle(folder.id)}
                  className="text-tertiary hover:text-fg"
                  title={isOpen ? "Collapse" : "Expand"}
                >
                  {isOpen ? (
                    <ChevronDown className="h-3.5 w-3.5" />
                  ) : (
                    <ChevronRight className="h-3.5 w-3.5" />
                  )}
                </button>
              )}
            </span>
            {isOpen ? (
              <FolderOpen className="h-3.5 w-3.5 shrink-0 text-accent" />
            ) : (
              <FolderClosed className="h-3.5 w-3.5 shrink-0 text-tertiary" />
            )}
            {renaming ? (
              <input
                autoFocus
                onFocus={(e) => e.currentTarget.select()}
                value={draft}
                onChange={(e) => setDraft(e.target.value)}
                onBlur={commitRename}
                onKeyDown={(e) => {
                  if (e.key === "Enter") commitRename();
                  if (e.key === "Escape") {
                    setDraft(folder.name);
                    setRenaming(false);
                  }
                }}
                className="min-w-0 flex-1 rounded border border-border bg-surface px-1 text-sm text-fg outline-none focus:border-accent"
              />
            ) : (
              <button
                onClick={() => {
                  if (clickTimer.current) clearTimeout(clickTimer.current);
                  clickTimer.current = setTimeout(() => {
                    clickTimer.current = null;
                    onToggle(folder.id);
                  }, DBLCLICK_MS);
                }}
                onDoubleClick={startRename}
                className="min-w-0 flex-1 truncate text-left text-sm text-fg"
                title="Double-click to rename"
              >
                {folder.name}
              </button>
            )}
            {count > 0 && !renaming && (
              <span className="text-3xs tabular-nums text-tertiary group-hover:hidden">
                {count}
              </span>
            )}
            <button
              onClick={() => {
                if (!isOpen) onToggle(folder.id);
                onCreateFolder(folder.id);
              }}
              title="New subfolder"
              className="hidden shrink-0 items-center rounded p-0.5 text-tertiary hover:bg-surface hover:text-fg group-hover:inline-flex"
            >
              <FolderPlus className="h-3 w-3" />
            </button>
            <button
              onClick={() => onDeleteFolder(folder.id)}
              title="Delete folder (notes fall back to root)"
              className="hidden shrink-0 items-center rounded p-0.5 text-tertiary hover:bg-surface hover:text-error group-hover:inline-flex"
            >
              <Trash2 className="h-3 w-3" />
            </button>
          </div>
        </ContextMenuTrigger>
        <ContextMenuContent>
          <ContextMenuItem onClick={() => onCreateNote(folder.id)}>
            <FilePlus className="mr-2 h-3.5 w-3.5" /> New note
          </ContextMenuItem>
          <ContextMenuItem
            onClick={() => {
              if (!isOpen) onToggle(folder.id);
              onCreateFolder(folder.id);
            }}
          >
            <FolderPlus className="mr-2 h-3.5 w-3.5" /> New subfolder
          </ContextMenuItem>
          <ContextMenuSeparator />
          <ContextMenuItem variant="destructive" onClick={() => onDeleteFolder(folder.id)}>
            <Trash2 className="mr-2 h-3.5 w-3.5" /> Delete folder
          </ContextMenuItem>
        </ContextMenuContent>
      </ContextMenu>

      {isOpen && (
        <>
          {childFolders.map((cf) => (
            <FolderRow
              key={cf.id}
              folder={cf}
              depth={depth + 1}
              dateField={dateField}
              foldersByParent={foldersByParent}
              notesByFolder={notesByFolder}
              expanded={expanded}
              dropTarget={dropTarget}
              activeId={activeId}
              selected={selected}
              onToggle={onToggle}
              onNoteClick={onNoteClick}
              dragPayload={dragPayload}
              onCreateFolder={onCreateFolder}
              onRenameFolder={onRenameFolder}
              onRenameNote={onRenameNote}
              onDeleteFolder={onDeleteFolder}
              onCreateNote={onCreateNote}
              deleteTargets={deleteTargets}
              onDeleteNotes={onDeleteNotes}
              onContextSelect={onContextSelect}
              newFolderId={newFolderId}
              onFolderRenameConsumed={onFolderRenameConsumed}
              onSetDropTarget={onSetDropTarget}
              onApplyDrop={onApplyDrop}
            />
          ))}
          {childNotes.map((n) => (
            <NoteRow
              key={n.id}
              note={n}
              depth={depth + 1}
              dateField={dateField}
              active={activeId === n.id}
              selected={selected.has(n.id)}
              onNoteClick={onNoteClick}
              dragPayload={dragPayload}
              onRename={onRenameNote}
              onContextSelect={onContextSelect}
              onDelete={() => onDeleteNotes(deleteTargets(n.id))}
              deleteLabel={noteDeleteLabel(deleteTargets(n.id).length)}
              onCreateNoteHere={() => onCreateNote(n.folder_id ?? null)}
              onCreateFolderHere={() => onCreateFolder(n.folder_id ?? null)}
            />
          ))}
        </>
      )}
    </>
  );
}

interface NoteRowProps {
  note: NoteSummary;
  depth: number;
  dateField: DateField;
  active: boolean;
  selected: boolean;
  onNoteClick: (id: string, e: ClickMods) => void;
  dragPayload: (id: string) => string;
  /** Double-click rename. "" hands the title back to the first-body-line rule. */
  onRename: (id: string, title: string) => void;
  /** Right-click select-before-open, mirroring file-manager behavior. */
  onContextSelect: (id: string) => void;
  /** Context menu "Delete" — pre-bound to the right target set (this note,
   *  or the whole multi-selection when this note is part of one). */
  onDelete: () => void;
  /** "Delete note" vs "Delete N notes" — decided by the caller, which knows
   *  the actual target-set size (this row only knows its own selected flag). */
  deleteLabel: string;
  /** Context menu "New note" — pre-bound to this note's folder. */
  onCreateNoteHere: () => void;
  /** Context menu "New folder" — pre-bound to this note's folder as parent. */
  onCreateFolderHere: () => void;
}

function NoteRow({
  note,
  depth,
  dateField,
  active,
  selected,
  onNoteClick,
  dragPayload,
  onRename,
  onContextSelect,
  onDelete,
  deleteLabel,
  onCreateNoteHere,
  onCreateFolderHere,
}: NoteRowProps) {
  const [renaming, setRenaming] = useState(false);
  const [draft, setDraft] = useState("");
  // What the box opened with, so committing an untouched title stays a no-op
  // instead of pinning a derived name the user never chose.
  const seed = useRef("");
  // Opening the note is held for one double-click window — see DBLCLICK_MS.
  const clickTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  useEffect(
    () => () => {
      if (clickTimer.current) clearTimeout(clickTimer.current);
    },
    [],
  );

  const startRename = () => {
    if (clickTimer.current) {
      clearTimeout(clickTimer.current);
      clickTimer.current = null;
    }
    const s = note.title === "Untitled" ? "" : note.title;
    seed.current = s;
    setDraft(s);
    setRenaming(true);
  };

  const commitRename = () => {
    setRenaming(false);
    const next = draft.trim();
    if (next === seed.current) return;
    onRename(note.id, next);
  };

  const rowClass = cn(
    "flex w-full select-none items-center gap-1.5 border-b border-border-subtle py-1.5 pr-3 text-left transition-colors hover:bg-surface-elevated",
    active && !selected && "bg-surface-elevated shadow-[inset_2px_0_0_0_var(--color-accent)]",
    selected && "bg-accent-subtle ring-1 ring-inset ring-accent/50",
  );
  const rowStyle = { paddingLeft: depth * INDENT + 6 };

  return (
    <ContextMenu
      onOpenChange={(open) => {
        if (open) onContextSelect(note.id);
      }}
    >
      <ContextMenuTrigger asChild>
        {renaming ? (
          // A div, not the row button — an <input> inside a <button> is invalid
          // nesting and the button swallows the clicks meant for the field.
          <div className={rowClass} style={rowStyle}>
            <span className="h-4 w-4 shrink-0" />
            <FileText className="h-3.5 w-3.5 shrink-0 text-tertiary" />
            <input
              autoFocus
              data-testid="note-rename"
              onFocus={(e) => e.currentTarget.select()}
              value={draft}
              onChange={(e) => setDraft(e.target.value)}
              onBlur={commitRename}
              onKeyDown={(e) => {
                if (e.key === "Enter") commitRename();
                if (e.key === "Escape") setRenaming(false);
              }}
              className="min-w-0 flex-1 rounded border border-border bg-surface px-1 text-sm text-fg outline-none focus:border-accent"
            />
          </div>
        ) : (
          <button
            draggable
            onDragStart={(e) => {
              e.dataTransfer.setData(DND_MIME, dragPayload(note.id));
              e.dataTransfer.effectAllowed = "move";
            }}
            onClick={(e) => {
              // Modifier-clicks are selection, never a rename — run them now.
              if (e.shiftKey || e.metaKey || e.ctrlKey) {
                onNoteClick(note.id, e);
                return;
              }
              if (clickTimer.current) clearTimeout(clickTimer.current);
              clickTimer.current = setTimeout(() => {
                clickTimer.current = null;
                onNoteClick(note.id, PLAIN_CLICK);
              }, DBLCLICK_MS);
            }}
            onDoubleClick={startRename}
            style={rowStyle}
            title={`${noteDatesTitle(note)}\nDouble-click to rename`}
            className={rowClass}
          >
            {/* empty chevron column — keeps the file icon in the same column as folder icons */}
            <span className="h-4 w-4 shrink-0" />
            <FileText className="h-3.5 w-3.5 shrink-0 text-tertiary" />
            <span className="flex-1 truncate text-left text-sm text-fg">{note.title || "Untitled"}</span>
            {/* Age matches the active sort key; hover reveals both full timestamps. */}
            <span className="shrink-0 text-3xs tabular-nums text-tertiary">
              {relTime(note[dateField])}
            </span>
          </button>
        )}
      </ContextMenuTrigger>
      <ContextMenuContent>
        <ContextMenuItem onClick={onCreateNoteHere}>
          <FilePlus className="mr-2 h-3.5 w-3.5" /> New note
        </ContextMenuItem>
        <ContextMenuItem onClick={onCreateFolderHere}>
          <FolderPlus className="mr-2 h-3.5 w-3.5" /> New folder
        </ContextMenuItem>
        <ContextMenuSeparator />
        <ContextMenuItem variant="destructive" onClick={onDelete}>
          <Trash2 className="mr-2 h-3.5 w-3.5" /> {deleteLabel}
        </ContextMenuItem>
      </ContextMenuContent>
    </ContextMenu>
  );
}
