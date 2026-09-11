// @ts-nocheck
// <!-- AGENT_HEADER
// role: code
// purpose: okuro-flow — floating, draggable touch toolbar (Figma/FigJam-style).
//   Small icon bar for the main selection actions, built for iPad use where
//   keyboard shortcuts aren't available. Drag the grip to re-dock bottom ↔ left;
//   the chosen dock persists to localStorage. Selection-gated buttons disable
//   when nothing is selected.
// AGENT_HEADER_END -->
import {
  ArrowDownToLine,
  ArrowLeftToLine,
  ArrowRightFromLine,
  ArrowRightToLine,
  ArrowUpToLine,
  CopyPlus,
  GripVertical,
  Plus,
  Redo2,
  RotateCw,
  Settings2,
  Share2,
  SquareDashedMousePointer,
  Trash2,
  Undo2,
} from "lucide-react";
import React from "react";

const DOCK_KEY = "fd-tb-dock";

const PLACE = [
  ["left", ArrowLeftToLine],
  ["top", ArrowUpToLine],
  ["bottom", ArrowDownToLine],
  ["right", ArrowRightToLine],
];

// actions: { newNode, duplicate, remove, addInput, addOutput, undo, redo }
// hasSel: at least one node selected (gates the selection-dependent buttons).
// multiSelect / onToggleMultiSelect: tablet box-select mode toggle.
// portSelected/portSide/onPlacePort: long-pressed port → side-placement icons.
// align/onAlign: selected node's text alignment ({h,v}) → 3×3 grid.
export function FloatingToolbar({ actions, hasSel, settingsOpen, onOpenSettings, multiSelect, onToggleMultiSelect, portSelected, portSide, onPlacePort, align, onAlign }) {
  const [dock, setDock] = React.useState(() => {
    try {
      return localStorage.getItem(DOCK_KEY) === "left" ? "left" : "bottom";
    } catch (e) {
      return "bottom";
    }
  });
  // {x,y} viewport coords while a drag is in flight; null when docked.
  const [drag, setDrag] = React.useState(null);
  const dragRef = React.useRef(null);

  const onPointerDown = (e) => {
    e.preventDefault();
    e.currentTarget.setPointerCapture?.(e.pointerId);
    dragRef.current = { moved: false };
    setDrag({ x: e.clientX, y: e.clientY });
  };
  const onPointerMove = (e) => {
    if (!dragRef.current) return;
    dragRef.current.moved = true;
    setDrag({ x: e.clientX, y: e.clientY });
  };
  const onPointerUp = (e) => {
    if (!dragRef.current) return;
    const moved = dragRef.current.moved;
    dragRef.current = null;
    if (moved) {
      // left third of the viewport → left dock, otherwise bottom dock.
      const next = e.clientX < window.innerWidth * 0.25 ? "left" : "bottom";
      setDock(next);
      try {
        localStorage.setItem(DOCK_KEY, next);
      } catch (err) {
        /* private mode / quota — keep the in-memory dock */
      }
    }
    setDrag(null);
  };

  const items = [
    { key: "new", icon: Plus, label: "New node", on: actions.newNode },
    {
      key: "settings",
      icon: Settings2,
      label: settingsOpen ? "Close node settings" : "Node settings",
      on: onOpenSettings,
      need: true,
      toggle: true,
      active: settingsOpen,
    },
    { key: "dup", icon: CopyPlus, label: "Duplicate", on: actions.duplicate, need: true },
    { key: "del", icon: Trash2, label: "Delete", on: actions.remove, need: true, danger: true },
    { key: "s1", sep: true },
    { key: "in", icon: ArrowRightToLine, label: "Add input", on: actions.addInput, need: true },
    { key: "out", icon: ArrowRightFromLine, label: "Add output", on: actions.addOutput, need: true },
    { key: "rot", icon: RotateCw, label: "Rotate ports (clockwise)", on: actions.rotate, need: true },
    { key: "s2", sep: true },
    {
      key: "msel",
      icon: SquareDashedMousePointer,
      label: multiSelect ? "Multi-select: on (drag = box-select)" : "Multi-select: off (drag = pan)",
      on: onToggleMultiSelect,
      toggle: true,
      active: multiSelect,
    },
    { key: "s3", sep: true },
    { key: "handover", icon: Share2, label: "Hand over selection", on: actions.handover, need: true },
    { key: "s4", sep: true },
    { key: "undo", icon: Undo2, label: "Back", on: actions.undo },
    { key: "redo", icon: Redo2, label: "Forward", on: actions.redo },
  ];

  const style = drag
    ? { position: "fixed", left: drag.x, top: drag.y, transform: "translate(-50%, -50%)", bottom: "auto", right: "auto" }
    : undefined;

  return (
    <div
      className={"fd-tb fd-tb-" + dock + (drag ? " fd-tb-dragging" : "")}
      style={style}
      role="toolbar"
      aria-label="Flow tools"
    >
      <button
        type="button"
        className="fd-tb-grip"
        title="Drag to move (bottom ↔ left)"
        aria-label="Move toolbar"
        onPointerDown={onPointerDown}
        onPointerMove={onPointerMove}
        onPointerUp={onPointerUp}
        onPointerCancel={onPointerUp}
      >
        <GripVertical size={16} />
      </button>
      {items.map((b) =>
        b.sep ? (
          <span key={b.key} className="fd-tb-sep" />
        ) : (
          <button
            key={b.key}
            type="button"
            className={"fd-tb-btn" + (b.danger ? " fd-tb-danger" : "") + (b.active ? " fd-tb-on" : "")}
            title={b.label}
            aria-label={b.label}
            aria-pressed={b.toggle ? !!b.active : undefined}
            disabled={b.need && !hasSel}
            onClick={b.on}
          >
            <b.icon size={18} />
          </button>
        ),
      )}
      {portSelected && (
        <>
          <span className="fd-tb-sep" />
          {PLACE.map(([s, Ic]) => (
            <button
              key={"place-" + s}
              type="button"
              className={"fd-tb-btn" + (portSide === s ? " fd-tb-on" : "")}
              title={"Move port to " + s}
              aria-label={"Move port to " + s}
              aria-pressed={portSide === s}
              onClick={() => onPlacePort(s)}
            >
              <Ic size={18} />
            </button>
          ))}
        </>
      )}
      {align && (
        <>
          <span className="fd-tb-sep" />
          <span className="fd-tb-align" role="group" aria-label="text alignment">
            {["top", "center", "bottom"].map((v) =>
              ["left", "center", "right"].map((h) => (
                <button
                  key={v + "-" + h}
                  type="button"
                  className={"fd-tb-ag" + (align.h === h && align.v === v ? " sel" : "")}
                  data-h={h}
                  data-v={v}
                  title={"Align " + v + " " + h}
                  aria-label={"Align text " + v + " " + h}
                  onClick={() => onAlign(h, v)}
                />
              )),
            )}
          </span>
        </>
      )}
    </div>
  );
}
