import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowLeft, ArrowRight, ArrowDown, ArrowDownUp, ArrowUp, Check, ChevronDown, FileText, FolderPlus, Languages, Mic, MoreHorizontal, PanelLeft, Plus, Search, Share2, Shrink, Trash2, Network, PenTool } from "lucide-react";
import "./notes.css";
import {
  notesApi,
  type NoteDetail,
  type NoteFolder,
  type NoteSummary,
  type NoteSearchHit,
  type NoteSavePayload,
} from "@/lib/api";
import { NoteTree } from "@/components/notes/note-tree";
import { dmy } from "@/lib/note-dates";
import { useStreamingDictation } from "@/lib/dictation";
import { MarkdownContent } from "@/components/ui/markdown-content";
import { NoteEditor, type NoteEditorHandle } from "@/components/notes/note-editor";
import { WysiwygEditor, type WysiwygEditorHandle } from "@/components/notes/wysiwyg-editor";
import { NoteGraph } from "@/components/notes/note-graph";
import { ScribbleWindow } from "@/components/notes/scribble-block";
import { HandoverDialog } from "@/components/handover/handover-dialog";
import type { ContentIR } from "@/lib/handover-api";
import { registerHandoverSource, registerSnapshotContext } from "@/lib/handover-context";
import { useSearchParams } from "react-router";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { cn } from "@/lib/utils";

type Mode = "edit" | "wysiwyg" | "split";

// Sidebar sort. Persisted so the list order is a user choice, not an
// edit-driven reshuffle. Pinned notes always float to the top regardless.
type SortKey = "updated" | "created" | "title";
type SortDir = "asc" | "desc";
const SORT_STORAGE_KEY = "notes-sort";
const SORT_DIR_STORAGE_KEY = "notes-sort-dir";
const SORT_OPTIONS: { key: SortKey; label: string }[] = [
  { key: "updated", label: "Last updated" },
  { key: "created", label: "Date created" },
  { key: "title", label: "Title" },
];
function readSort(): SortKey {
  try {
    const v = localStorage.getItem(SORT_STORAGE_KEY);
    if (v === "updated" || v === "created" || v === "title") return v;
  } catch {
    /* ignore */
  }
  return "updated";
}
// Default direction is "desc": newest-first for dates, but that reads as Z→A for
// titles, so the title label flips wording based on the active direction.
function readSortDir(): SortDir {
  try {
    const v = localStorage.getItem(SORT_DIR_STORAGE_KEY);
    if (v === "asc" || v === "desc") return v;
  } catch {
    /* ignore */
  }
  return "desc";
}

// Editor content measure (readable line length). Persisted; applied via a
// data-measure attribute on the editor <main> (see notes.css). "wide" = full.
type Measure = "narrow" | "med" | "wide";
const MEASURE_STORAGE_KEY = "notes-measure";
const MEASURE_OPTIONS: { key: Measure; label: string }[] = [
  { key: "narrow", label: "Narrow" },
  { key: "med", label: "Medium" },
  { key: "wide", label: "Wide" },
];
function readMeasure(): Measure {
  try {
    const v = localStorage.getItem(MEASURE_STORAGE_KEY);
    if (v === "narrow" || v === "med" || v === "wide") return v;
  } catch {
    /* ignore */
  }
  return "med";
}

// Right backlinks/links rail — user-toggleable, persisted open by default.
const RAIL_STORAGE_KEY = "notes-rail-open";
function readRailOpen(): boolean {
  try {
    return localStorage.getItem(RAIL_STORAGE_KEY) !== "0";
  } catch {
    return true;
  }
}

// WYSIWYG formatting bar — user-toggleable, persisted shown by default.
const FORMATBAR_STORAGE_KEY = "notes-formatbar-open";
function readFormatBarOpen(): boolean {
  try {
    return localStorage.getItem(FORMATBAR_STORAGE_KEY) !== "0";
  } catch {
    return true;
  }
}

// Left folder/list rail — hidable; persisted open by default.
const SIDEBAR_STORAGE_KEY = "notes-sidebar-open";
function readSidebarOpen(): boolean {
  try {
    return localStorage.getItem(SIDEBAR_STORAGE_KEY) !== "0";
  } catch {
    return true;
  }
}
// Zen mode — hides both rails + the editor chrome, leaving just the note sheet.
const ZEN_STORAGE_KEY = "notes-zen";
function readZen(): boolean {
  try {
    return localStorage.getItem(ZEN_STORAGE_KEY) === "1";
  } catch {
    return false;
  }
}

/**
 * okuro·notes — Obsidian-inspired note surface. Markdown format, DB storage,
 * RAG-native (every save embeds into vec_note_chunks). List + CodeMirror editor
 * with [[ ]] autocomplete + semantic search + backlinks + voice dictation.
 */
function todayStr(): string {
  const d = new Date();
  const m = String(d.getMonth() + 1).padStart(2, "0");
  const day = String(d.getDate()).padStart(2, "0");
  return `${d.getFullYear()}-${m}-${day}`;
}

interface NoteTemplate {
  label: string;
  title: () => string;
  body: string;
}

const TEMPLATES: NoteTemplate[] = [
  { label: "Blank", title: () => "Untitled", body: "" },
  {
    label: "Daily note",
    title: () => todayStr(),
    body: "## Today\n\n## Notes\n\n## Tasks\n- [ ] ",
  },
  {
    label: "Meeting",
    title: () => "Meeting — ",
    body: "**Date:** \n**Attendees:** \n\n## Agenda\n\n## Decisions\n\n## Actions\n- [ ] ",
  },
  { label: "Idea", title: () => "Idea — ", body: "## Idea\n\n## Why it matters\n\n## Next\n" },
];

// First body line that carries text, sanitised, as the note title.
// Mirrors _derive_title / _sanitize_title_line in notes/storage.py so the label
// the user watches while typing is the one the backend persists on save. The
// pytest vectors in tests/notes/test_title_derivation.py and the vitest ones in
// notes.test.tsx are the same list on purpose — keep them in lockstep.
const TITLE_STRIP = /^\s*(?:#{1,6}\s+|>\s+|[-*+]\s+(?:\[[ xX]\]\s+)?|\d+[.)]\s+)/;
const HTML_COMMENT = /<!--[\s\S]*?-->/g;
// An opener with no closer on the same line — a note starting with an
// AGENT_HEADER block is the live case.
const HTML_COMMENT_OPEN = /<!--[\s\S]*$/;
const HTML_TAG = /<[^>]*>/g;
const MD_IMAGE = /!\[([^\]]*)\]\([^)]*\)/g;
const MD_LINK = /\[([^\]]*)\]\([^)]*\)/g;
const MD_WIKILINK = /!?\[\[([^\]\n]+?)\]\]/g;
// Emphasis markers must hug their text (CommonMark), so `2 * 3 * 4` keeps its
// stars while `*emphasis*` loses them. `_` additionally refuses to fire
// intra-word, which is what keeps snake_case identifiers intact.
const MD_EMPHASIS = [
  /\*\*(?!\s)(.+?)(?<!\s)\*\*/g,
  /__(?!\s)(.+?)(?<!\s)__/g,
  /\*(?!\s)(.+?)(?<!\s)\*/g,
  /(?<!\w)_(?!\s)(.+?)(?<!\s)_(?!\w)/g,
  /`(.+?)`/g,
];
// Unpaired markers left at either end — '*Written 2026-04-15' opens emphasis it
// never closes. '_' stays out: a leading underscore reads as an identifier.
const MD_DANGLING = /^[*`]+|[*`]+$/g;

// Python's html.unescape, without the innerHTML round-trip that would make this
// an XSS sink. DOMParser builds a detached document that never runs scripts or
// loads resources, and every remaining `<` is escaped first so the parser is
// doing entity decoding only — no tag semantics, which is what keeps this
// identical to the backend's decode-after-tag-strip order.
function decodeEntities(text: string): string {
  if (!text.includes("&")) return text;
  try {
    const doc = new DOMParser().parseFromString(
      text.replace(/</g, "&lt;"),
      "text/html",
    );
    return doc.documentElement.textContent ?? text;
  } catch {
    return text;
  }
}

// Reduce one body line to the plain text a title should show. Returns "" when
// the line carried no text of its own (a lone `<br />`, an unclosed `<!--`),
// which is the signal to try the next line rather than name a note after markup.
function sanitizeTitleLine(line: string): string {
  let text = line.replace(TITLE_STRIP, "");
  text = text.replace(HTML_COMMENT, "");
  text = text.replace(HTML_COMMENT_OPEN, "");
  text = text.replace(HTML_TAG, "");
  // After tag removal on purpose: an ESCAPED tag (&lt;b&gt;) is text the author
  // meant to show, and decoding first would let the next pass eat it as markup.
  text = decodeEntities(text);
  text = text.replace(MD_WIKILINK, (_m, inner: string) =>
    (inner.split("|").pop() ?? "").trim(),
  );
  text = text.replace(MD_IMAGE, "$1");
  text = text.replace(MD_LINK, "$1");
  for (const rx of MD_EMPHASIS) {
    let prev = "";
    while (prev !== text) {
      prev = text;
      text = text.replace(rx, "$1");
    }
  }
  text = text.replace(/\s+/g, " ").trim();
  return text.replace(MD_DANGLING, "").trim();
}

// Exported for the vector tests that hold it to parity with the backend.
export function deriveTitle(body: string): string {
  for (const line of body.split("\n")) {
    const s = sanitizeTitleLine(line);
    if (s) return s.slice(0, 120);
  }
  return "Untitled";
}

function renderBody(md: string): string {
  if (!md) return "*Nothing to preview.*";
  // Inline drawing embeds: ![[drawing:<id>]] -> the flattened PNG the backend serves.
  return md.replace(
    /!\[\[drawing:([a-f0-9-]+)\]\]/gi,
    (_m, id) => `![drawing](/api/notes/drawings/${id}/png)`,
  );
}

export function NotesPage() {
  const qc = useQueryClient();
  const [activeId, setActiveId] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const [mode, setMode] = useState<Mode>("wysiwyg");
  const [showGraph, setShowGraph] = useState(false);
  const [drawOpen, setDrawOpen] = useState(false);
  // Which drawing the canvas is editing: null = a fresh one to insert at the
  // caret; an id = re-opening an embedded drawing the user clicked.
  const [drawEditId, setDrawEditId] = useState<string | null>(null);
  // Mobile master-detail: below `md` only one pane shows at a time. The list
  // rail is the "master"; opening/creating a note drills into the "editor".
  // Ignored at md+ where both panes are always visible via CSS.
  const [mobileView, setMobileView] = useState<"list" | "editor">("list");
  // Select a note AND drill to the editor pane on mobile.
  const openNote = useCallback((id: string | null) => {
    setActiveId(id);
    setMobileView("editor");
  }, []);

  // Local editor buffer — kept separate from the fetched note so typing is
  // never clobbered by a refetch; flushed to the backend by a debounced save.
  // No title field: the first body line IS the title (derived on save).
  const [body, setBody] = useState("");
  // Always-fresh mirror of body — scribble insert/remove run from window
  // callbacks whose closures can otherwise capture a stale body.
  const bodyRef = useRef("");
  bodyRef.current = body;
  const dirtyRef = useRef(false);
  const saveTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  // One save in flight at a time. Without this, every debounce tick that fires
  // before the first create responds re-enters the create branch and inserts
  // another row — N typing pauses during a slow create = N notes. Same guard
  // flow-designer already uses; see scheduleSave below.
  const savingRef = useRef(false);
  // activeId as a ref: the debounce callback closes over its scope, so reading
  // state directly would see a stale null even after the create resolved.
  // Mirrored on every render (like bodyRef) so every setActiveId site — note
  // switch, create success, delete, template — stays in sync without patching
  // each call site. createMut.onSuccess also sets it eagerly, before React
  // re-renders, so a debounce tick firing in that gap still sees the new id.
  const activeIdRef = useRef<string | null>(null);
  activeIdRef.current = activeId;
  const editorRef = useRef<NoteEditorHandle | null>(null);
  const wysiwygRef = useRef<WysiwygEditorHandle | null>(null);
  // Id of the scribble the window last created — lets "delete" target a
  // freshly-drawn scribble that has no drawEditId yet.
  const createdScribbleId = useRef<string | null>(null);

  // Folder context menu's "New note" — set right before newNote() resets the
  // buffer, read once the debounced save actually creates the row (empty
  // notes are never persisted; see scheduleSave), then cleared so the next
  // plain "New" defaults back to root.
  const pendingFolderIdRef = useRef<string | null>(null);

  // Hand-over: "Hand over →" is only live while the user has a live text
  // selection in the document (the editor surfaces are plain contenteditable /
  // CodeMirror DOM, so the native selection already carries what we need).
  const [hasSelection, setHasSelection] = useState(false);
  const [handoverContent, setHandoverContent] = useState<ContentIR | null>(null);
  useEffect(() => {
    const onSelectionChange = () => {
      setHasSelection(!!window.getSelection()?.toString().trim());
    };
    document.addEventListener("selectionchange", onSelectionChange);
    return () => document.removeEventListener("selectionchange", onSelectionChange);
  }, []);
  const openHandover = () => {
    // Prefer a live selection; with none, hand over the whole note.
    const selected = (window.getSelection()?.toString() || "").trim();
    const payload = selected || body.trim();
    if (!payload) return;
    const title = selected
      ? selected.split("\n").find((l) => l.trim())?.trim().slice(0, 120) || "Note selection"
      : deriveTitle(body);
    setHandoverContent({
      kind: "text",
      title,
      body_md: payload,
      project: activeData?.note?.project ?? undefined,
      source: { tool: "notes", id: activeId ?? undefined, label: deriveTitle(body) },
    });
  };

  // --- list (poll for cross-process / agent edits) ---
  const { data: listData } = useQuery({
    queryKey: ["notes", "list"],
    queryFn: () => notesApi.list(),
    refetchInterval: 4000,
  });
  const notes: NoteSummary[] = listData?.notes ?? [];

  // User-selected sidebar order. Pinned first, then the chosen key. Titles use
  // locale compare; dates compare the raw SQLite strings (lexicographic ==
  // chronological for a fixed "YYYY-MM-DD HH:MM:SS" format), newest first.
  const [sort, setSort] = useState<SortKey>(readSort);
  const [sortDir, setSortDir] = useState<SortDir>(readSortDir);
  const chooseSort = useCallback((k: SortKey) => {
    setSort(k);
    try {
      localStorage.setItem(SORT_STORAGE_KEY, k);
    } catch {
      /* ignore */
    }
  }, []);
  const toggleSortDir = useCallback(() => {
    setSortDir((d) => {
      const next: SortDir = d === "asc" ? "desc" : "asc";
      try {
        localStorage.setItem(SORT_DIR_STORAGE_KEY, next);
      } catch {
        /* ignore */
      }
      return next;
    });
  }, []);
  const sortedNotes = useMemo(() => {
    const arr = [...notes];
    // Base comparator is ascending; direction flips it. Titles A→Z, dates
    // oldest→newest at asc. Pinned notes always float, independent of direction.
    const dir = sortDir === "asc" ? 1 : -1;
    arr.sort((a, b) => {
      const pa = a.pinned ? 1 : 0;
      const pb = b.pinned ? 1 : 0;
      if (pa !== pb) return pb - pa;
      if (sort === "title") {
        return dir * (a.title || "Untitled").localeCompare(b.title || "Untitled");
      }
      const fa = (sort === "created" ? a.created_at : a.updated_at) || "";
      const fb = (sort === "created" ? b.created_at : b.updated_at) || "";
      return dir * fa.localeCompare(fb);
    });
    return arr;
  }, [notes, sort, sortDir]);
  const [measure, setMeasure] = useState<Measure>(readMeasure);
  const chooseMeasure = useCallback((m: Measure) => {
    setMeasure(m);
    try {
      localStorage.setItem(MEASURE_STORAGE_KEY, m);
    } catch {
      /* ignore */
    }
  }, []);
  const [railOpen, setRailOpen] = useState<boolean>(readRailOpen);
  const toggleRail = useCallback(() => {
    setRailOpen((o) => {
      const next = !o;
      try {
        localStorage.setItem(RAIL_STORAGE_KEY, next ? "1" : "0");
      } catch {
        /* ignore */
      }
      return next;
    });
  }, []);
  const [formatBarOpen, setFormatBarOpen] = useState<boolean>(readFormatBarOpen);
  const toggleFormatBar = useCallback(() => {
    setFormatBarOpen((o) => {
      const next = !o;
      try {
        localStorage.setItem(FORMATBAR_STORAGE_KEY, next ? "1" : "0");
      } catch {
        /* ignore */
      }
      return next;
    });
  }, []);
  const [sidebarOpen, setSidebarOpen] = useState<boolean>(readSidebarOpen);
  const toggleSidebar = useCallback(() => {
    setSidebarOpen((o) => {
      const next = !o;
      try {
        localStorage.setItem(SIDEBAR_STORAGE_KEY, next ? "1" : "0");
      } catch {
        /* ignore */
      }
      return next;
    });
  }, []);
  const [zen, setZen] = useState<boolean>(readZen);
  const toggleZen = useCallback(() => {
    setZen((z) => {
      const next = !z;
      try {
        localStorage.setItem(ZEN_STORAGE_KEY, next ? "1" : "0");
      } catch {
        /* ignore */
      }
      return next;
    });
  }, []);
  const dateField: "updated_at" | "created_at" =
    sort === "created" ? "created_at" : "updated_at";

  // --- folder tree (polled with the list so agent-side moves show up) ---
  const { data: foldersData } = useQuery({
    queryKey: ["notes", "folders"],
    queryFn: () => notesApi.folders(),
    refetchInterval: 4000,
  });
  const folders: NoteFolder[] = foldersData?.folders ?? [];

  // --- semantic search (only when a query is present) ---
  const { data: searchData } = useQuery({
    queryKey: ["notes", "search", query],
    queryFn: () => notesApi.search(query),
    enabled: query.trim().length > 0,
  });
  const hits: NoteSearchHit[] = searchData?.results ?? [];

  // --- active note detail ---
  const { data: activeData } = useQuery({
    queryKey: ["notes", "detail", activeId],
    queryFn: () => notesApi.get(activeId as string),
    enabled: !!activeId,
  });
  const { data: linksData } = useQuery({
    queryKey: ["notes", "links", activeId],
    queryFn: () => notesApi.backlinks(activeId as string),
    enabled: !!activeId,
    refetchInterval: 4000,
  });
  const { data: graphData } = useQuery({
    queryKey: ["notes", "graph"],
    queryFn: () => notesApi.graph(),
    enabled: showGraph,
  });

  // Load fetched note into the buffer when switching notes (not on every
  // refetch — only when the id changes or the buffer is clean).
  const loadedId = useRef<string | null>(null);
  useEffect(() => {
    const n: NoteDetail | undefined = activeData?.note;
    if (!n) return;
    if (loadedId.current !== n.id || !dirtyRef.current) {
      setBody(n.body);
      loadedId.current = n.id;
      dirtyRef.current = false;
    }
  }, [activeData]);

  // Declare this page as the ambient hand-over source (Cmd+K → "Hand over…"),
  // re-registered whenever the active note changes so the builder always sees
  // the current note's id/project. Same logic as the header's openHandover.
  useEffect(() => {
    return registerHandoverSource((): ContentIR | null => {
      const b = bodyRef.current;
      const selected = (window.getSelection()?.toString() || "").trim();
      const payload = selected || b.trim();
      if (!payload) return null;
      const title = selected
        ? selected.split("\n").find((l) => l.trim())?.trim().slice(0, 120) || "Note selection"
        : deriveTitle(b);
      return {
        kind: "text",
        title,
        body_md: payload,
        project: activeData?.note?.project ?? undefined,
        source: { tool: "notes", id: activeId ?? undefined, label: deriveTitle(b) },
      };
    });
  }, [activeId, activeData]);

  // L2 snapshot context — expose the loaded note record so a captured snapshot of
  // this page hands over the note's DATA (title, body, frontmatter, project), not
  // just pixels. Absent on the empty state; re-registered per active note.
  useEffect(() => {
    const note = activeData?.note;
    if (!note) return;
    return registerSnapshotContext(() => ({
      entity: { type: "note", id: note.id, title: deriveTitle(note.body) },
      data: note,
    }));
  }, [activeData]);

  const createMut = useMutation({
    mutationFn: (data: NoteSavePayload) => notesApi.create(data),
    onSuccess: (res, vars) => {
      setActiveId(res.note.id);
      activeIdRef.current = res.note.id;
      setMobileView("editor");
      loadedId.current = res.note.id;
      // Only mark clean if nothing was typed since this request was dispatched.
      // Clearing unconditionally lets the load effect below rewind the editor to
      // the server snapshot — which is how a note ends up holding one letter.
      if (bodyRef.current === vars.body) dirtyRef.current = false;
      pendingFolderIdRef.current = null;
      void qc.invalidateQueries({ queryKey: ["notes", "list"] });
    },
    onSettled: () => {
      savingRef.current = false;
    },
  });

  // Cmd+K "New note" convention: a `?new=1` intent creates a blank note, then
  // consumes the param so a refresh / back-nav doesn't re-create. Matches how
  // flow/prism honor ?new=1 — one shared entry-point convention across tools.
  const [searchParams, setSearchParams] = useSearchParams();
  const newHandledRef = useRef(false);
  useEffect(() => {
    if (newHandledRef.current || searchParams.get("new") !== "1") return;
    newHandledRef.current = true;
    createMut.mutate({ body: "" });
    const next = new URLSearchParams(searchParams);
    next.delete("new");
    setSearchParams(next, { replace: true });
  }, [searchParams, createMut, setSearchParams]);

  const updateMut = useMutation({
    mutationFn: (data: { id: string; body: string }) =>
      notesApi.update(data.id, { body: data.body }),
    onSuccess: (_res, vars) => {
      if (bodyRef.current === vars.body) dirtyRef.current = false;
      void qc.invalidateQueries({ queryKey: ["notes", "list"] });
      if (activeId) void qc.invalidateQueries({ queryKey: ["notes", "links", activeId] });
    },
    onSettled: () => {
      savingRef.current = false;
    },
  });

  // Rename. TITLE ONLY in the payload — including folder_id would send a filed
  // note back to the vault root (storage's _UNSET sentinel decides by key
  // presence). An empty title is not a no-op: it hands the note back to the
  // first-body-line rule, which is how a user undoes a name.
  const renameMut = useMutation({
    mutationFn: (v: { id: string; title: string }) =>
      notesApi.update(v.id, { title: v.title }),
    onSuccess: (_res, vars) => {
      void qc.invalidateQueries({ queryKey: ["notes", "list"] });
      void qc.invalidateQueries({ queryKey: ["notes", "detail", vars.id] });
    },
  });

  const removeMut = useMutation({
    mutationFn: (id: string) => notesApi.remove(id),
    onSuccess: () => {
      setActiveId(null);
      setMobileView("list");
      loadedId.current = null;
      setBody("");
      void qc.invalidateQueries({ queryKey: ["notes", "list"] });
    },
  });

  // Sidebar context menu's "Delete" — single note or a whole multi-selection.
  const bulkDeleteMut = useMutation({
    mutationFn: (ids: string[]) => Promise.all(ids.map((id) => notesApi.remove(id))),
    onSuccess: (_res, ids) => {
      if (activeId && ids.includes(activeId)) {
        setActiveId(null);
        setMobileView("list");
        loadedId.current = null;
        setBody("");
      }
      void qc.invalidateQueries({ queryKey: ["notes", "list"] });
    },
  });

  // --- folder tree mutations ---
  const invalidateTree = () => {
    void qc.invalidateQueries({ queryKey: ["notes", "folders"] });
    void qc.invalidateQueries({ queryKey: ["notes", "list"] });
  };
  // Id of a just-created folder, so the tree drops straight into rename mode
  // (no more piles of literal "New folder"s).
  const [newFolderId, setNewFolderId] = useState<string | null>(null);
  const folderCreateMut = useMutation({
    mutationFn: (parentId: string | null) => notesApi.folderCreate("New folder", parentId),
    onSuccess: (res) => {
      invalidateTree();
      setNewFolderId(res.folder?.id ?? null);
    },
  });
  const folderUpdateMut = useMutation({
    mutationFn: (v: { id: string; name?: string; parent_id?: string | null }) =>
      notesApi.folderUpdate(v.id, { name: v.name, parent_id: v.parent_id }),
    onSuccess: invalidateTree,
  });
  const folderDeleteMut = useMutation({
    mutationFn: (id: string) => notesApi.folderDelete(id),
    onSuccess: invalidateTree,
  });
  const moveNotesMut = useMutation({
    mutationFn: (v: { noteIds: string[]; folderId: string | null }) =>
      Promise.all(v.noteIds.map((id) => notesApi.setFolder(id, v.folderId))),
    onSuccess: invalidateTree,
  });

  // Debounced autosave. Body only — the backend derives the title from the
  // first line, so there is nothing else to persist.
  const scheduleSave = useCallback(
    (nextBody: string) => {
      dirtyRef.current = true;
      if (saveTimer.current) clearTimeout(saveTimer.current);
      saveTimer.current = setTimeout(() => {
        // Serialize writes: a second trigger re-arms the debounce so it runs
        // AFTER the first completes (by which point activeIdRef is set). This
        // is what prevents duplicate-create rows.
        if (savingRef.current) {
          scheduleSave(bodyRef.current);
          return;
        }
        const id = activeIdRef.current;
        if (id) {
          savingRef.current = true;
          updateMut.mutate({ id, body: nextBody });
        } else if (nextBody.trim()) {
          savingRef.current = true;
          const folderId = pendingFolderIdRef.current;
          createMut.mutate({ body: nextBody, folder_id: folderId ?? undefined });
        }
      }, 800);
    },
    [createMut, updateMut],
  );

  const onBody = (v: string) => {
    setBody(v);
    scheduleSave(v);
  };

  // Voice dictation — reuses the shared hook (raw PCM → WAV → /api/stt).
  const insertTranscript = useCallback(
    (text: string) => {
      const target = mode === "wysiwyg" ? wysiwygRef.current : editorRef.current;
      if (target) {
        target.insertAtCursor(text);
      } else {
        const sep = body && !body.endsWith("\n") ? " " : "";
        onBody(body + sep + text);
      }
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [body, mode],
  );
  // Streaming dictation: committed words flow into the note live; the unstable
  // tail shows in a floating pill so the user sees speech land as they talk.
  // Language is pinned per session — Whisper's small models are English-biased,
  // so 'de' picks a bigger model server-side and stops auto-detect from flipping
  // languages mid-stream.
  const [interim, setInterim] = useState("");
  const [dictLang, setDictLang] = useState<"auto" | "en" | "de">("auto");
  const { recording, toggle } = useStreamingDictation(
    insertTranscript,
    setInterim,
    undefined,
    dictLang === "auto" ? undefined : dictLang,
  );

  const newNote = () => {
    // Every plain "New" defaults back to root — only createNoteInFolder below
    // overrides this, and only for the note it creates.
    pendingFolderIdRef.current = null;
    setActiveId(null);
    setMobileView("editor");
    loadedId.current = null;
    setBody("");
    dirtyRef.current = false;
    setTimeout(() => (mode === "wysiwyg" ? wysiwygRef.current : editorRef.current)?.focus(), 0);
  };

  // Sidebar context menu's "New note" — scoped to wherever the user
  // right-clicked (a folder, or a note's own folder); null = root.
  const createNoteInFolder = (folderId: string | null) => {
    newNote();
    pendingFolderIdRef.current = folderId;
  };

  // --- drawing notes (the whole sheet is a canvas) ---
  // A note flagged frontmatter.kind === "drawing" renders as a full-page canvas
  // instead of the text editor. The drawing id is carried in the body embed
  // (added on first stroke) so re-opening loads the same drawing.
  const isDrawingNote = (activeData?.note?.frontmatter as { kind?: string } | undefined)?.kind === "drawing";
  const drawingNoteId = useMemo(() => {
    if (!isDrawingNote) return null;
    return body.match(/\/api\/notes\/drawings\/([a-f0-9-]+)\/png/i)?.[1] ?? null;
  }, [isDrawingNote, body]);
  const newDrawingNote = () => {
    setShowGraph(false);
    setDrawOpen(false);
    createMut.mutate({ body: "Drawing\n", frontmatter: { kind: "drawing" } });
  };
  // First stroke on a fresh drawing note → persist its id in the body.
  const onDrawingNoteCreated = (id: string) => {
    onBody(`Drawing\n\n![scribble](/api/notes/drawings/${id}/png)\n`);
  };

  // --- inline drawings ---
  // Embed a fresh scribble at the caret (or append when no editor is focused),
  // as a plain inline image so it renders in both the WYSIWYG editor and preview.
  const insertDrawing = useCallback(
    (id: string) => {
      // Append to the note body rather than poking the editor doc: inserting at
      // the ProseMirror selection can overwrite a *selected* scribble (e.g. one
      // the user just clicked), collapsing every scribble into one. The WYSIWYG
      // re-parses the new body and renders it as an image node.
      const embed = `![scribble](/api/notes/drawings/${id}/png)`;
      const base = bodyRef.current.replace(/\n+$/, "");
      onBody(base ? `${base}\n\n${embed}\n` : `${embed}\n`);
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [],
  );
  // Toolbar Draw button: open a fresh canvas (insert-on-first-save), or close it.
  const openDrawNew = () => {
    setShowGraph(false);
    if (drawOpen) {
      setDrawOpen(false);
      setDrawEditId(null);
      return;
    }
    setDrawEditId(null);
    setDrawOpen(true);
  };
  // Remove a scribble's embed from the note body (works in every mode — the
  // WYSIWYG re-syncs from the body). Matches the image markdown for this id,
  // with or without a trailing ?token=… on the src.
  const removeScribble = (id: string) => {
    const re = new RegExp(`!\\[[^\\]]*\\]\\(/api/notes/drawings/${id}/png[^)]*\\)\\n?`, "g");
    onBody(bodyRef.current.replace(re, ""));
  };

  // The PNG lives at a stable URL, so an in-place edit would show the cached
  // old raster. Force the displayed <img> to re-fetch (display-only bust — the
  // saved markdown src stays clean).
  const bustScribbleCache = (id: string) => {
    requestAnimationFrame(() => {
      document
        .querySelectorAll<HTMLImageElement>(`img[src*="/api/notes/drawings/${id}/png"]`)
        .forEach((img) => {
          const u = new URL(img.src, window.location.origin);
          u.searchParams.set("_r", String(Date.now()));
          img.src = u.toString();
        });
    });
  };

  // Clicking an embedded drawing image re-opens that drawing maximized to edit.
  const onEditorClick = (e: React.MouseEvent) => {
    const el = (e.target as HTMLElement)?.closest?.("img") as HTMLImageElement | null;
    if (!el) return;
    const m = el.src.match(/\/api\/notes\/drawings\/([a-f0-9-]+)\/png/i);
    const id = m?.[1];
    if (!id) return;
    e.preventDefault();
    setShowGraph(false);
    setDrawEditId(id);
    setDrawOpen(true);
  };

  // Templates + daily note. The template's title becomes the note's first line
  // (an H1) so the first-line-is-title rule holds. "Daily note" is get-or-create
  // on today's date so re-opening the day lands on the same note.
  const openTemplate = (t: NoteTemplate) => {
    setShowGraph(false);
    setDrawOpen(false);
    if (t.label === "Blank") {
      newNote();
      return;
    }
    const heading = t.title();
    if (t.label === "Daily note") {
      const existing = notes.find((n) => n.title === heading);
      if (existing) {
        openNote(existing.id);
        return;
      }
    }
    createMut.mutate({ body: `# ${heading}\n\n${t.body}` });
  };

  const links = linksData;

  const derivedTitle = useMemo(() => deriveTitle(body), [body]);
  // A note somebody named shows THAT name; one nobody named keeps tracking the
  // first body line as it is typed (migration 141's title_explicit).
  const titleIsExplicit = !!activeData?.note.title_explicit;
  const displayTitle = titleIsExplicit
    ? activeData?.note.title || "Untitled"
    : derivedTitle;

  // Inline rename in the editor chrome — double-click the title.
  const [renamingTitle, setRenamingTitle] = useState(false);
  const [titleDraft, setTitleDraft] = useState("");
  // What the box opened with, so Enter on an untouched title stays a no-op
  // instead of pinning a derived name the user never chose.
  const titleSeed = useRef("");
  const startTitleRename = () => {
    if (!activeIdRef.current) return;
    const seed = displayTitle === "Untitled" ? "" : displayTitle;
    titleSeed.current = seed;
    setTitleDraft(seed);
    setRenamingTitle(true);
  };
  const commitTitleRename = () => {
    setRenamingTitle(false);
    const id = activeIdRef.current;
    if (!id) return;
    const next = titleDraft.trim();
    if (next === titleSeed.current) return;
    renameMut.mutate({ id, title: next });
  };

  const created = dmy(activeData?.note.created_at);
  const edited = dmy(activeData?.note.updated_at);
  const titles = useMemo(() => notes.map((n) => n.title).filter(Boolean), [notes]);

  const wordCount = useMemo(
    () => (body.trim() ? body.trim().split(/\s+/).length : 0),
    [body],
  );

  return (
    <div className="flex h-full min-h-0">
      {/* --- list rail (folders + notes) — hidable, and hidden in zen --- */}
      <aside
        className={cn(
          "w-full shrink-0 flex-col border-r border-border bg-surface md:w-72",
          !zen && sidebarOpen
            ? // Mobile: hide the master list once a note is open (drill-in).
              mobileView === "editor"
              ? "hidden md:flex"
              : "flex"
            : "hidden",
        )}
      >
        <div className="flex items-center gap-2 p-3">
          <div className="relative flex-1">
            <Search className="pointer-events-none absolute left-2 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-tertiary" />
            <Input
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder="Search notes…"
              className="h-8 pl-7 text-sm"
            />
          </div>
          <Button
            size="icon"
            variant="ghost"
            onClick={() => folderCreateMut.mutate(null)}
            title="New folder"
          >
            <FolderPlus className="h-4 w-4" />
          </Button>
          <DropdownMenu>
            <DropdownMenuTrigger asChild>
              <Button
                size="icon"
                variant="ghost"
                title={`Sort: ${SORT_OPTIONS.find((o) => o.key === sort)?.label} (${sortDir})`}
              >
                <ArrowDownUp className="h-4 w-4" />
              </Button>
            </DropdownMenuTrigger>
            <DropdownMenuContent align="end">
              {SORT_OPTIONS.map((o) => (
                <DropdownMenuItem key={o.key} onClick={() => chooseSort(o.key)}>
                  <Check
                    className={cn("mr-2 h-3.5 w-3.5", sort === o.key ? "opacity-100" : "opacity-0")}
                  />
                  {o.label}
                </DropdownMenuItem>
              ))}
              <DropdownMenuItem
                onSelect={(e) => {
                  e.preventDefault();
                  toggleSortDir();
                }}
                className="mt-1 border-t border-border-subtle pt-1.5"
              >
                {sortDir === "asc" ? (
                  <ArrowUp className="mr-2 h-3.5 w-3.5" />
                ) : (
                  <ArrowDown className="mr-2 h-3.5 w-3.5" />
                )}
                {sort === "title"
                  ? sortDir === "asc" ? "A → Z" : "Z → A"
                  : sortDir === "asc" ? "Oldest first" : "Newest first"}
              </DropdownMenuItem>
            </DropdownMenuContent>
          </DropdownMenu>
          <DropdownMenu>
            <DropdownMenuTrigger asChild>
              <Button size="icon" variant="ghost" title="New note / template">
                <Plus className="h-4 w-4" />
              </Button>
            </DropdownMenuTrigger>
            <DropdownMenuContent align="end">
              {TEMPLATES.map((t) => (
                <DropdownMenuItem key={t.label} onClick={() => openTemplate(t)}>
                  {t.label}
                </DropdownMenuItem>
              ))}
              <DropdownMenuItem
                onClick={newDrawingNote}
                className="mt-1 border-t border-border-subtle pt-1.5"
              >
                <PenTool className="mr-2 h-3.5 w-3.5" /> Drawing
              </DropdownMenuItem>
            </DropdownMenuContent>
          </DropdownMenu>
        </div>
        {query.trim() ? (
          // Search is a flat, ranked list — the tree only shows when browsing.
          <div className="min-h-0 flex-1 overflow-y-auto">
            {hits.length === 0 ? (
              <p className="px-3 py-6 text-center text-xs text-tertiary">No matches</p>
            ) : (
              hits.map((n) => (
                <button
                  key={n.id}
                  onClick={() => openNote(n.id)}
                  className={cn(
                    "flex w-full items-center gap-2 border-b border-border-subtle px-3 py-2 text-left transition-colors hover:bg-surface-elevated",
                    activeId === n.id && "bg-surface-elevated",
                  )}
                >
                  <FileText className="h-3.5 w-3.5 shrink-0 text-tertiary" />
                  <span className="flex-1 truncate text-sm text-fg">{n.title || "Untitled"}</span>
                  {n.similarity != null && (
                    <span className="text-3xs tabular-nums text-tertiary">
                      {Math.round(n.similarity * 100)}%
                    </span>
                  )}
                </button>
              ))
            )}
          </div>
        ) : (
          <NoteTree
            notes={sortedNotes}
            folders={folders}
            dateField={dateField}
            activeId={activeId}
            onSelect={openNote}
            onMoveNotes={(noteIds, folderId) => moveNotesMut.mutate({ noteIds, folderId })}
            onMoveFolder={(folderId, parentId) =>
              folderUpdateMut.mutate({ id: folderId, parent_id: parentId })
            }
            onCreateFolder={(parentId) => folderCreateMut.mutate(parentId)}
            onRenameFolder={(id, name) => folderUpdateMut.mutate({ id, name })}
            onRenameNote={(id, title) => renameMut.mutate({ id, title })}
            onDeleteFolder={(id) => folderDeleteMut.mutate(id)}
            onCreateNote={createNoteInFolder}
            onDeleteNotes={(ids) => bulkDeleteMut.mutate(ids)}
            newFolderId={newFolderId}
            onFolderRenameConsumed={() => setNewFolderId(null)}
          />
        )}
      </aside>

      {/* --- editor --- */}
      <main
        data-measure={measure}
        className={cn(
          "flex min-w-0 flex-1 flex-col",
          // Mobile: hide the editor while browsing the master list.
          mobileView === "list" && "hidden md:flex",
        )}
      >
        <div className={cn("flex items-center gap-2 border-b border-border px-4 py-2", zen && "hidden")}>
          {/* Mobile-only: drill back out to the folder/note list. */}
          <Button
            size="icon"
            variant="ghost"
            className="md:hidden"
            onClick={() => setMobileView("list")}
            title="Back to notes"
          >
            <ArrowLeft className="h-4 w-4" />
          </Button>
          {/* Panels — one control for folders / backlinks / zen (desktop). */}
          <DropdownMenu>
            <DropdownMenuTrigger asChild>
              <Button size="icon" variant="ghost" className="hidden md:inline-flex" title="Panels">
                <PanelLeft className="h-4 w-4" />
              </Button>
            </DropdownMenuTrigger>
            <DropdownMenuContent align="start">
              <DropdownMenuItem onSelect={(e) => { e.preventDefault(); toggleSidebar(); }}>
                <Check className={cn("mr-2 h-3.5 w-3.5", sidebarOpen ? "opacity-100" : "opacity-0")} /> Folders
              </DropdownMenuItem>
              <DropdownMenuItem onSelect={(e) => { e.preventDefault(); toggleRail(); }}>
                <Check className={cn("mr-2 h-3.5 w-3.5", railOpen ? "opacity-100" : "opacity-0")} /> Backlinks
              </DropdownMenuItem>
              <DropdownMenuItem onSelect={(e) => { e.preventDefault(); toggleZen(); }}>
                <Check className={cn("mr-2 h-3.5 w-3.5", zen ? "opacity-100" : "opacity-0")} /> Zen mode
              </DropdownMenuItem>
              <DropdownMenuItem onSelect={(e) => { e.preventDefault(); toggleFormatBar(); }}>
                <Check className={cn("mr-2 h-3.5 w-3.5", formatBarOpen ? "opacity-100" : "opacity-0")} /> Formatting bar
              </DropdownMenuItem>
            </DropdownMenuContent>
          </DropdownMenu>
          {/* Title tracks the first body line until someone renames it here —
              double-click to edit in place, Enter commits, Esc cancels, and an
              emptied box hands it back to the first-line rule. Dates sit beneath
              it; an unsaved note has no timestamps yet, so the line is omitted. */}
          <div className="flex min-w-0 flex-1 flex-col justify-center">
            {renamingTitle ? (
              <input
                autoFocus
                data-testid="title-rename"
                onFocus={(e) => e.currentTarget.select()}
                value={titleDraft}
                onChange={(e) => setTitleDraft(e.target.value)}
                onBlur={commitTitleRename}
                onKeyDown={(e) => {
                  if (e.key === "Enter") commitTitleRename();
                  if (e.key === "Escape") setRenamingTitle(false);
                }}
                placeholder={derivedTitle === "Untitled" ? "Untitled" : derivedTitle}
                className="min-w-0 rounded border border-border bg-surface px-1 text-lg font-semibold leading-tight text-fg outline-none focus:border-accent"
              />
            ) : (
              <span
                data-testid="note-title"
                className="min-w-0 cursor-text truncate text-lg font-semibold leading-tight text-fg"
                title={
                  titleIsExplicit
                    ? "Double-click to rename"
                    : "Title = first line of the note. Double-click to rename."
                }
                onDoubleClick={startTitleRename}
              >
                {displayTitle === "Untitled" ? (
                  <span className="text-tertiary">Untitled</span>
                ) : (
                  displayTitle
                )}
              </span>
            )}
            {created && (
              <span className="truncate text-xs leading-tight text-tertiary">
                Created: {created}
                {edited && edited !== created && <> / Edited: {edited}</>}
              </span>
            )}
          </div>
          <div className="flex items-center gap-1.5">
            {/* Primary action — always visible (works with folders hidden / zen). */}
            <DropdownMenu>
              <DropdownMenuTrigger asChild>
                <Button variant="secondary" className="h-8 gap-1 px-2.5" title="New note">
                  <Plus className="h-4 w-4" />
                  <span className="text-sm font-medium">New</span>
                  <ChevronDown className="h-3 w-3 opacity-60" />
                </Button>
              </DropdownMenuTrigger>
              <DropdownMenuContent align="start">
                {TEMPLATES.map((t) => (
                  <DropdownMenuItem key={t.label} onClick={() => openTemplate(t)}>
                    {t.label}
                  </DropdownMenuItem>
                ))}
                <DropdownMenuItem
                  onClick={newDrawingNote}
                  className="mt-1 border-t border-border-subtle pt-1.5"
                >
                  <PenTool className="mr-2 h-3.5 w-3.5" /> Drawing
                </DropdownMenuItem>
              </DropdownMenuContent>
            </DropdownMenu>

            {/* Editing mode — one segmented control, plain labels (no glyph). */}
            <div className="flex items-center rounded-md border border-border p-0.5">
              {(
                [
                  ["edit", "Source"],
                  ["split", "Split"],
                  ["wysiwyg", "Live"],
                ] as [Mode, string][]
              ).map(([m, label]) => (
                <button
                  key={m}
                  onClick={() => setMode(m)}
                  className={cn(
                    "rounded px-2.5 py-1 text-xs font-medium transition-colors",
                    mode === m ? "bg-surface-elevated text-fg" : "text-tertiary hover:text-fg",
                  )}
                >
                  {label}
                </button>
              ))}
            </div>

            {/* Voice. */}
            <Button
              size="icon"
              variant={recording ? "destructive" : "ghost"}
              onClick={toggle}
              title={recording ? "Stop dictation" : "Dictate"}
            >
              <Mic className="h-4 w-4" />
            </Button>
            <DropdownMenu>
              <DropdownMenuTrigger asChild>
                <Button size="icon" variant="ghost" disabled={recording} title={`Dictation language: ${dictLang.toUpperCase()}`}>
                  <Languages className="h-4 w-4" />
                </Button>
              </DropdownMenuTrigger>
              <DropdownMenuContent align="end">
                {(["auto", "en", "de"] as const).map((l) => (
                  <DropdownMenuItem key={l} onClick={() => setDictLang(l)}>
                    <Check className={cn("mr-2 h-3.5 w-3.5", dictLang === l ? "opacity-100" : "opacity-0")} />
                    {l.toUpperCase()}
                  </DropdownMenuItem>
                ))}
              </DropdownMenuContent>
            </DropdownMenu>

            {/* Everything low-frequency or destructive → overflow. */}
            <DropdownMenu>
              <DropdownMenuTrigger asChild>
                <Button size="icon" variant="ghost" title="More">
                  <MoreHorizontal className="h-4 w-4" />
                </Button>
              </DropdownMenuTrigger>
              <DropdownMenuContent align="end">
                <DropdownMenuItem onClick={() => setShowGraph((v) => !v)}>
                  <Network className="mr-2 h-3.5 w-3.5" /> Graph view
                </DropdownMenuItem>
                <DropdownMenuItem onClick={openDrawNew} disabled={!activeId}>
                  <PenTool className="mr-2 h-3.5 w-3.5" /> Insert drawing
                </DropdownMenuItem>
                <DropdownMenuItem onClick={openHandover} disabled={!body.trim()}>
                  <Share2 className="mr-2 h-3.5 w-3.5" /> {hasSelection ? "Hand over selection" : "Hand over note"}
                </DropdownMenuItem>
                <div className="mt-1 border-t border-border-subtle pt-1">
                  <div className="px-2 py-1 text-3xs uppercase tracking-wider text-tertiary">Text width</div>
                  {MEASURE_OPTIONS.map((o) => (
                    <DropdownMenuItem key={o.key} onClick={() => chooseMeasure(o.key)}>
                      <Check className={cn("mr-2 h-3.5 w-3.5", measure === o.key ? "opacity-100" : "opacity-0")} />
                      {o.label}
                    </DropdownMenuItem>
                  ))}
                </div>
                {activeId && (
                  <DropdownMenuItem
                    onClick={() => {
                      if (window.confirm("Delete this note?")) removeMut.mutate(activeId);
                    }}
                    className="mt-1 border-t border-border-subtle pt-1.5 text-error focus:text-error"
                  >
                    <Trash2 className="mr-2 h-3.5 w-3.5" /> Delete note
                  </DropdownMenuItem>
                )}
              </DropdownMenuContent>
            </DropdownMenu>
          </div>
        </div>

        {/* Editor region. The drawing canvas overlays this area (editor stays
            mounted underneath so its caret survives) and a click on any embedded
            drawing image re-opens it to edit. */}
        <div className="relative flex min-h-0 flex-1 flex-col" onClick={onEditorClick}>
          {/* Zen: the chrome is hidden, so a slim floating cluster keeps the
              essentials (drawing + exit) reachable. */}
          {zen && (
            <div className="absolute right-3 top-3 z-30 flex items-center gap-1 rounded-lg border border-border bg-surface/80 p-1 backdrop-blur">
              {!isDrawingNote && (
                <Button
                  size="icon"
                  variant="ghost"
                  onClick={openDrawNew}
                  disabled={!activeId}
                  title="Insert drawing"
                >
                  <PenTool className="h-4 w-4" />
                </Button>
              )}
              <Button size="icon" variant="ghost" onClick={toggleZen} title="Exit zen">
                <Shrink className="h-4 w-4" />
              </Button>
            </div>
          )}
          {isDrawingNote && activeId ? (
            // The whole sheet is a canvas — a full-page drawing note.
            <ScribbleWindow
              key={activeId}
              fill="region"
              drawingId={drawingNoteId}
              noteId={activeId}
              onCreated={onDrawingNoteCreated}
              onClose={() => {}}
            />
          ) : showGraph ? (
            <div className="min-h-0 flex-1">
              <NoteGraph
                nodes={graphData?.nodes ?? []}
                edges={graphData?.edges ?? []}
                activeId={activeId}
                onSelect={(id) => {
                  openNote(id);
                  setShowGraph(false);
                }}
              />
            </div>
          ) : mode === "wysiwyg" ? (
            <WysiwygEditor
              ref={wysiwygRef}
              value={body}
              onChange={onBody}
              noteId={activeId}
              showToolbar={formatBarOpen}
              className="min-h-0 flex-1 bg-surface"
            />
          ) : (
            <div className="flex min-h-0 flex-1">
              <NoteEditor
                ref={editorRef}
                value={body}
                onChange={onBody}
                linkOptions={titles}
                noteId={activeId}
                className={cn(
                  "min-h-0 flex-1 overflow-hidden bg-surface",
                  mode === "split" && "border-r border-border",
                )}
              />
              {mode === "split" && (
                <div className="note-prose min-h-0 flex-1 overflow-y-auto p-4">
                  <MarkdownContent variant="viewer">{renderBody(body)}</MarkdownContent>
                </div>
              )}
            </div>
          )}
        </div>

        <div className="flex items-center justify-between border-t border-border px-4 py-1.5 text-3xs text-tertiary">
          <span>{wordCount} words</span>
          {recording && (
            <span className="flex min-w-0 flex-1 items-center gap-1.5 truncate px-3">
              <span className="inline-block h-1.5 w-1.5 shrink-0 animate-pulse rounded-full bg-error" />
              <span className="truncate italic text-secondary">
                {interim || "listening…"}
              </span>
            </span>
          )}
          <span>
            {updateMut.isPending || createMut.isPending
              ? "saving…"
              : dirtyRef.current
                ? "unsaved"
                : activeId
                  ? "saved"
                  : "new"}
          </span>
        </div>
      </main>

      {/* --- backlinks rail (detail/links; hidden on small screens + toggleable) --- */}
      <aside
        className={cn(
          "w-60 shrink-0 flex-col gap-4 border-l border-border bg-surface p-3",
          railOpen && !zen ? "hidden lg:flex" : "hidden",
        )}
      >
        <div>
          <h3 className="mb-2 text-3xs font-semibold uppercase tracking-wider text-tertiary">
            Backlinks
          </h3>
          {!links || links.backlinks.length === 0 ? (
            <p className="text-xs text-tertiary">None</p>
          ) : (
            links.backlinks.map((b) => (
              <button
                key={b.id}
                onClick={() => openNote(b.id)}
                className="flex w-full items-center gap-1 py-1 text-left text-xs text-accent hover:underline"
              >
                <ArrowLeft className="h-3 w-3 shrink-0" />
                <span className="truncate">{b.title}</span>
              </button>
            ))
          )}
        </div>
        <div>
          <h3 className="mb-2 text-3xs font-semibold uppercase tracking-wider text-tertiary">
            Links
          </h3>
          {!links || links.outgoing.length === 0 ? (
            <p className="text-xs text-tertiary">None</p>
          ) : (
            links.outgoing.map((o, i) => (
              <div
                key={i}
                className={cn(
                  "flex items-center gap-1 py-1 text-xs",
                  o.target_type === "unresolved" ? "text-tertiary italic" : "text-fg-muted",
                )}
                title={o.target_type}
              >
                <ArrowRight className="h-3 w-3 shrink-0" />
                <span className="truncate">
                  {o.link_text}
                  {o.target_type === "unresolved" && " (new)"}
                </span>
              </div>
            ))
          )}
        </div>
      </aside>

      {drawOpen && activeId && (
        <ScribbleWindow
          drawingId={drawEditId}
          noteId={activeId}
          onCreated={(id) => {
            // Just record the id — the embed is inserted on close so it carries
            // the finished drawing, not a mid-stroke snapshot.
            createdScribbleId.current = id;
          }}
          onClose={() => {
            const newId = createdScribbleId.current;
            if (!drawEditId && newId) insertDrawing(newId);
            else if (drawEditId) bustScribbleCache(drawEditId);
            setDrawOpen(false);
            setDrawEditId(null);
            createdScribbleId.current = null;
          }}
          onDelete={() => {
            const id = drawEditId ?? createdScribbleId.current;
            if (id) removeScribble(id);
            setDrawOpen(false);
            setDrawEditId(null);
            createdScribbleId.current = null;
          }}
        />
      )}

      <HandoverDialog
        open={!!handoverContent}
        onOpenChange={(o) => !o && setHandoverContent(null)}
        content={handoverContent}
      />
    </div>
  );
}
