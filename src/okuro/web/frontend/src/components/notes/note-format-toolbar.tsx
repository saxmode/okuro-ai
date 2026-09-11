import { useRef, useState, type RefObject } from "react";
import {
  Bold,
  Braces,
  Brackets,
  Code,
  GripVertical,
  Heading1,
  Heading2,
  Heading3,
  Italic,
  Link as LinkIcon,
  List,
  ListOrdered,
  Minus,
  Quote,
  Redo2,
  Strikethrough,
  Undo2,
  type LucideIcon,
} from "lucide-react";

import "./note-toolbar.css";

/** Formatting actions the bar can emit. The editor owns the mapping to
 *  concrete Milkdown commands — the bar stays presentational. */
export type ToolbarCmd =
  | "bold"
  | "italic"
  | "strike"
  | "code"
  | "h1"
  | "h2"
  | "h3"
  | "bullet"
  | "ordered"
  | "quote"
  | "codeblock"
  | "link"
  | "wikilink"
  | "hr"
  | "undo"
  | "redo";

type Dock = "top" | "bottom" | "left" | "right";

const DOCK_KEY = "notes-tb-dock";

interface Item {
  cmd?: ToolbarCmd;
  icon?: LucideIcon;
  label?: string;
  sep?: boolean;
}

const ITEMS: Item[] = [
  { cmd: "bold", icon: Bold, label: "Bold" },
  { cmd: "italic", icon: Italic, label: "Italic" },
  { cmd: "strike", icon: Strikethrough, label: "Strikethrough" },
  { cmd: "code", icon: Code, label: "Inline code" },
  { sep: true },
  { cmd: "h1", icon: Heading1, label: "Heading 1" },
  { cmd: "h2", icon: Heading2, label: "Heading 2" },
  { cmd: "h3", icon: Heading3, label: "Heading 3" },
  { sep: true },
  { cmd: "bullet", icon: List, label: "Bullet list" },
  { cmd: "ordered", icon: ListOrdered, label: "Numbered list" },
  { cmd: "quote", icon: Quote, label: "Quote" },
  { cmd: "codeblock", icon: Braces, label: "Code block" },
  { sep: true },
  { cmd: "link", icon: LinkIcon, label: "Link" },
  { cmd: "wikilink", icon: Brackets, label: "Wikilink [[ ]]" },
  { cmd: "hr", icon: Minus, label: "Divider" },
  { sep: true },
  { cmd: "undo", icon: Undo2, label: "Undo" },
  { cmd: "redo", icon: Redo2, label: "Redo" },
];

function readDock(): Dock {
  try {
    const v = localStorage.getItem(DOCK_KEY);
    if (v === "top" || v === "bottom" || v === "left" || v === "right") return v;
  } catch {
    /* private mode / quota */
  }
  return "bottom";
}

interface Props {
  onCmd: (cmd: ToolbarCmd) => void;
  disabled?: boolean;
  /** The position:relative editor wrapper the bar floats inside and snaps to. */
  containerRef: RefObject<HTMLElement | null>;
}

/** Floating, draggable formatting bar. Drag the grip and drop near any edge
 *  to re-dock top / bottom / left / right; the chosen dock persists. Mirrors
 *  okuro-flow's floating toolbar, extended to all four edges. */
export function NoteFormatToolbar({ onCmd, disabled, containerRef }: Props) {
  const [dock, setDock] = useState<Dock>(readDock);
  // {x,y} viewport coords while a drag is in flight; null when docked.
  const [drag, setDrag] = useState<{ x: number; y: number } | null>(null);
  // Edge the pointer is currently closest to — drives the LIVE orientation
  // preview so the bar turns vertical near the left/right edge before dropping.
  const [previewDock, setPreviewDock] = useState<Dock | null>(null);
  const movedRef = useRef(false);

  // Whichever editor edge the pointer is closest to.
  const nearestDock = (x: number, y: number): Dock => {
    const rect = containerRef.current?.getBoundingClientRect();
    if (!rect) return dock;
    const dl = x - rect.left;
    const dr = rect.right - x;
    const dt = y - rect.top;
    const db = rect.bottom - y;
    const min = Math.min(dl, dr, dt, db);
    return min === dl ? "left" : min === dr ? "right" : min === dt ? "top" : "bottom";
  };

  const onPointerDown = (e: React.PointerEvent) => {
    e.preventDefault();
    try {
      e.currentTarget.setPointerCapture?.(e.pointerId);
    } catch {
      /* pointer already released — ignore */
    }
    movedRef.current = false;
    setDrag({ x: e.clientX, y: e.clientY });
  };
  const onPointerMove = (e: React.PointerEvent) => {
    if (!drag) return;
    movedRef.current = true;
    setDrag({ x: e.clientX, y: e.clientY });
    setPreviewDock(nearestDock(e.clientX, e.clientY));
  };
  const onPointerUp = (e: React.PointerEvent) => {
    if (!drag) return;
    if (movedRef.current) {
      const next = nearestDock(e.clientX, e.clientY);
      setDock(next);
      try {
        localStorage.setItem(DOCK_KEY, next);
      } catch {
        /* keep in-memory dock */
      }
    }
    setDrag(null);
    setPreviewDock(null);
  };

  const style: React.CSSProperties | undefined = drag
    ? {
        position: "fixed",
        left: drag.x,
        top: drag.y,
        transform: "translate(-50%, -50%)",
        bottom: "auto",
        right: "auto",
      }
    : undefined;

  return (
    <div
      className={"nft nft-" + (drag ? (previewDock ?? dock) : dock) + (drag ? " nft-dragging" : "")}
      style={style}
      role="toolbar"
      aria-label="Formatting"
    >
      <button
        type="button"
        className="nft-grip"
        title="Drag to move (any edge)"
        aria-label="Move toolbar"
        onPointerDown={onPointerDown}
        onPointerMove={onPointerMove}
        onPointerUp={onPointerUp}
        onPointerCancel={onPointerUp}
      >
        <GripVertical size={16} />
      </button>
      {ITEMS.map((it, i) =>
        it.sep ? (
          <span key={`s${i}`} className="nft-sep" />
        ) : (
          <button
            key={it.cmd}
            type="button"
            title={it.label}
            aria-label={it.label}
            disabled={disabled}
            // Keep focus in the editor so the command acts on the live selection.
            onMouseDown={(e) => e.preventDefault()}
            onClick={() => onCmd(it.cmd as ToolbarCmd)}
            className="nft-btn"
          >
            {it.icon ? <it.icon size={16} /> : null}
          </button>
        ),
      )}
    </div>
  );
}
