import { useEffect, useLayoutEffect, useRef, useState } from "react";
import type { Deck, SlideElement } from "./scene";
import { DEFAULT_LINE_HEIGHT, boxDecor, captionScrim, captionScrimForRect, chartSurface, deckSize, hugs, imageAlt, isComposite } from "./scene";
import { moveElements, updateElement } from "./scene-ops";
import { layoutChildren } from "./flatten";
import { elementHeight } from "./measure";
import { renderRich } from "./rich-text";
import { ElementInner } from "./element-body";
import { tokenizeApiSrc } from "@/lib/slides-api";

/**
 * SlidesEditor — the editable canvas for ONE slide.
 *
 * Fit-contains the baseline canvas, renders elements as absolutely-positioned
 * divs, and supports select / shift-multi-select / move (group) / resize. A
 * single dragged/resized element SNAPS its edges + centre to other elements and
 * to the canvas centre/edges, drawing smart guides. Coordinates are baseline px.
 * Text wraps + hugs from the top so long copy is never cropped.
 */

interface DragState {
  mode: "move" | "resize";
  ids: string[];
  lastX: number;
  lastY: number;
  // single-element move/resize (enables snapping):
  rid?: string;
  ox?: number;
  oy?: number;
  ow?: number;
  oh?: number;
  startX?: number;
  startY?: number;
}

const SNAP_PX = 8; // screen px threshold (converted to baseline via scale)

/** Kinds whose rendered height is COMPUTED, never read from `el.h`: hug kinds
 *  (text + text-bearing primitives) measure their wrapped content, and a `frame`
 *  hugs its auto-layout children. Writing `h` for these is silently ignored by
 *  the render, so a vertical resize drag would feel broken. */
export function heightManaged(kind: SlideElement["kind"]): boolean {
  return hugs(kind) || kind === "frame";
}

/** The box the editor DRAWS for `el` — the SINGLE height authority, shared with
 *  `flattenElements` so the editable canvas cannot drift from Play/Present/export.
 *
 *  A `frame` is an authoring container: its authored `w/h` are a placeholder (the
 *  binder emits h:10 and children at 0,0; `layoutChildren` computes the real
 *  local offsets AND the hugged size). Drawing the frame at `el.h` while its
 *  children sit at their true offsets clipped every child into a ~10px sliver —
 *  the editor showed one line of text while thumbnails and Present rendered fine.
 */
export function renderBox(el: SlideElement, deckFont?: string): { w: number; h: number } {
  if (el.kind === "frame") {
    const { w, h } = layoutChildren(el, deckFont);
    return { w, h };
  }
  return { w: el.w, h: hugs(el.kind) ? elementHeight(el, deckFont) : el.h };
}

/** Geometry a resize handle writes for `kind`. Height-managed kinds (see
 *  `heightManaged`) resize WIDTH only; every other kind resizes in both axes. */
export function resizePatch(
  kind: SlideElement["kind"],
  w: number,
  h: number,
): Partial<SlideElement> {
  return heightManaged(kind) ? { w } : { w, h };
}

export function elementStyle(el: SlideElement, interactive: boolean): React.CSSProperties {
  const isText = hugs(el.kind);
  return {
    background: el.bg ?? "transparent",
    color: el.color ?? "#eafff0",
    fontSize: el.fontSize ?? 28,
    fontWeight: el.fontWeight ?? 400,
    fontFamily: el.font ?? undefined,
    letterSpacing: el.letterSpacing ? `${el.letterSpacing}px` : undefined,
    opacity: el.opacity ?? 1,
    borderRadius: el.radius ?? 0,
    textAlign: el.align ?? "left",
    lineHeight: el.lineHeight ?? DEFAULT_LINE_HEIGHT,
    display: "flex",
    flexDirection: "column",
    alignItems: el.align === "center" ? "center" : el.align === "right" ? "flex-end" : "flex-start",
    justifyContent: isText ? "flex-start" : "center",
    whiteSpace: "pre-wrap",
    wordBreak: "break-word",
    overflow: isText ? "visible" : "hidden",
    padding: el.pad ?? 0,
    cursor: interactive ? "move" : "default",
    ...boxDecor(el),
  };
}

interface Guides { v: number[]; h: number[] }

export function SlidesEditor({
  deck,
  slideIndex,
  selectedIds,
  onSelect,
  onChange,
}: {
  deck: Deck;
  slideIndex: number;
  selectedIds: string[];
  onSelect: (ids: string[]) => void;
  onChange: (deck: Deck) => void;
}) {
  const wrapRef = useRef<HTMLDivElement>(null);
  const [scale, setScale] = useState(0.5);
  const [guides, setGuides] = useState<Guides>({ v: [], h: [] });
  const [editingId, setEditingId] = useState<string | null>(null);
  const [draftText, setDraftText] = useState("");
  const editRef = useRef<HTMLTextAreaElement>(null);
  const drag = useRef<DragState | null>(null);
  const sel = new Set(selectedIds);
  const { w: canvasW, h: canvasH } = deckSize(deck);

  // Focus + place caret at end when inline text editing begins.
  useEffect(() => {
    if (!editingId) return;
    const ta = editRef.current;
    if (!ta) return;
    ta.focus();
    ta.setSelectionRange(ta.value.length, ta.value.length);
  }, [editingId]);

  useLayoutEffect(() => {
    const el = wrapRef.current;
    if (!el) return;
    const measure = () => {
      const s = Math.min(el.clientWidth / canvasW, el.clientHeight / canvasH);
      setScale(s > 0 ? s : 0.5);
    };
    measure();
    const ro = new ResizeObserver(measure);
    ro.observe(el);
    return () => ro.disconnect();
  }, [canvasW, canvasH]);

  const slide = deck.slides[slideIndex];
  const elements = slide?.elements ?? [];

  // Snap targets: canvas edges/centre + every OTHER element's edges/centres.
  // Uses the RENDERED box (renderBox) — snapping to a frame's authored h:10 would
  // put its "bottom" guide 10px below its top instead of under its real content.
  const snapTargets = (excludeId: string) => {
    const xs = [0, canvasW / 2, canvasW];
    const ys = [0, canvasH / 2, canvasH];
    for (const e of elements) {
      if (e.id === excludeId) continue;
      const { w, h } = renderBox(e, deck.font);
      xs.push(e.x, e.x + w / 2, e.x + w);
      ys.push(e.y, e.y + h / 2, e.y + h);
    }
    return { xs, ys };
  };

  // Inline text editing: double-click a text element → edit in place.
  const beginEdit = (el: SlideElement) => {
    if (el.kind !== "text") return;
    onSelect([el.id]);
    setDraftText(el.text ?? "");
    setEditingId(el.id);
  };
  const commitEdit = () => {
    if (!editingId || !slide) { setEditingId(null); return; }
    onChange(updateElement(deck, slide.id, editingId, { text: draftText }));
    setEditingId(null);
  };
  const cancelEdit = () => setEditingId(null);

  // Select an element (top-level OR frame child) by id, honoring shift-multi.
  const selectById = (id: string, shift: boolean) => {
    if (shift) onSelect(sel.has(id) ? selectedIds.filter((i) => i !== id) : [...selectedIds, id]);
    else onSelect([id]);
  };

  // Body for a text element: the inline-edit textarea while it's being edited,
  // else its rich text. Shared by top-level elements AND frame children so both
  // are click-to-edit with identical behavior (one `editRef`; only the element
  // whose id === editingId mounts it).
  const editableBody = (el: SlideElement) =>
    editingId === el.id ? (
      <textarea
        ref={editRef}
        value={draftText}
        onChange={(e) => setDraftText(e.target.value)}
        onPointerDown={(e) => e.stopPropagation()}
        onBlur={commitEdit}
        onKeyDown={(e) => {
          e.stopPropagation();
          if (e.key === "Escape") { e.preventDefault(); cancelEdit(); }
          else if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) { e.preventDefault(); commitEdit(); }
        }}
        className="h-full w-full resize-none border-0 bg-transparent p-0 outline-none"
        style={{
          color: el.color ?? "#eafff0",
          fontSize: el.fontSize ?? 28,
          fontWeight: el.fontWeight ?? 400,
          fontFamily: el.font ?? deck.font,
          lineHeight: el.lineHeight ?? DEFAULT_LINE_HEIGHT,
          letterSpacing: el.letterSpacing ? `${el.letterSpacing}px` : undefined,
          textAlign: el.align ?? "left",
          caretColor: "#8ff0a4",
        }}
      />
    ) : (
      renderRich(el.text)
    );

  const onElementDown = (e: React.PointerEvent, el: SlideElement) => {
    if (editingId === el.id) return; // editing this element → don't start a drag
    e.stopPropagation();
    let ids: string[];
    if (e.shiftKey) ids = sel.has(el.id) ? selectedIds.filter((i) => i !== el.id) : [...selectedIds, el.id];
    else ids = sel.has(el.id) ? selectedIds : [el.id];
    onSelect(ids);
    (e.target as HTMLElement).setPointerCapture?.(e.pointerId);
    drag.current = {
      mode: "move",
      ids,
      lastX: e.clientX,
      lastY: e.clientY,
      rid: ids.length === 1 ? el.id : undefined,
      ox: el.x,
      oy: el.y,
      startX: e.clientX,
      startY: e.clientY,
    };
  };

  const onResizeDown = (e: React.PointerEvent, el: SlideElement) => {
    e.stopPropagation();
    onSelect([el.id]);
    (e.target as HTMLElement).setPointerCapture?.(e.pointerId);
    drag.current = { mode: "resize", ids: [el.id], lastX: e.clientX, lastY: e.clientY, rid: el.id, ow: el.w, oh: el.h, ox: el.x, oy: el.y, startX: e.clientX, startY: e.clientY };
  };

  const onPointerMove = (e: React.PointerEvent) => {
    const d = drag.current;
    if (!d || !slide) return;
    const tol = SNAP_PX / scale;

    if (d.mode === "move") {
      if (d.rid && d.ox !== undefined && d.oy !== undefined) {
        // single element → absolute position + snapping + guides
        const el = elements.find((x) => x.id === d.rid);
        if (!el) return;
        let nx = Math.round(d.ox + (e.clientX - (d.startX ?? e.clientX)) / scale);
        let ny = Math.round(d.oy + (e.clientY - (d.startY ?? e.clientY)) / scale);
        const { xs, ys } = snapTargets(el.id);
        const gv: number[] = [];
        const gh: number[] = [];
        // x: try left, centre, right
        for (const [edge, val] of [["l", nx], ["c", nx + el.w / 2], ["r", nx + el.w]] as const) {
          const hit = xs.find((t) => Math.abs(val - t) <= tol);
          if (hit !== undefined) { nx += hit - val; gv.push(hit); break; }
          void edge;
        }
        for (const [edge, val] of [["t", ny], ["m", ny + el.h / 2], ["b", ny + el.h]] as const) {
          const hit = ys.find((t) => Math.abs(val - t) <= tol);
          if (hit !== undefined) { ny += hit - val; gh.push(hit); break; }
          void edge;
        }
        setGuides({ v: gv, h: gh });
        onChange(updateElement(deck, slide.id, el.id, { x: nx, y: ny }));
      } else {
        // group → incremental, no snap
        const dx = (e.clientX - d.lastX) / scale;
        const dy = (e.clientY - d.lastY) / scale;
        d.lastX = e.clientX;
        d.lastY = e.clientY;
        onChange(moveElements(deck, slide.id, d.ids, dx, dy));
      }
    } else if (d.rid) {
      const el = elements.find((x) => x.id === d.rid);
      if (!el) return;
      const hugKind = heightManaged(el.kind);
      let w = Math.max(20, Math.round((d.ow ?? 0) + (e.clientX - (d.startX ?? e.clientX)) / scale));
      const { xs, ys } = snapTargets(el.id);
      const gv: number[] = [];
      const gh: number[] = [];
      const rightHit = xs.find((t) => Math.abs((d.ox ?? 0) + w - t) <= tol);
      if (rightHit !== undefined) { w = rightHit - (d.ox ?? 0); gv.push(rightHit); }
      // Hug kinds hug their measured height, so a vertical drag is a no-op → width only.
      let h = el.h;
      if (!hugKind) {
        h = Math.max(20, Math.round((d.oh ?? 0) + (e.clientY - (d.startY ?? e.clientY)) / scale));
        const botHit = ys.find((t) => Math.abs((d.oy ?? 0) + h - t) <= tol);
        if (botHit !== undefined) { h = botHit - (d.oy ?? 0); gh.push(botHit); }
      }
      setGuides({ v: gv, h: gh });
      onChange(updateElement(deck, slide.id, el.id, resizePatch(el.kind, w, h)));
    }
  };

  const endDrag = () => {
    drag.current = null;
    setGuides({ v: [], h: [] });
  };

  return (
    <div
      ref={wrapRef}
      className="relative flex h-full w-full items-center justify-center overflow-hidden"
      onPointerDown={() => onSelect([])}
    >
      <div
        className="relative origin-center overflow-hidden rounded-lg shadow-2xl"
        style={{ width: canvasW * scale, height: canvasH * scale, background: deck.background ?? "#0b0f0c" }}
      >
        <div
          className="absolute left-0 top-0 origin-top-left"
          style={{ width: canvasW, height: canvasH, transform: `scale(${scale})`, fontFamily: deck.font }}
          onPointerMove={onPointerMove}
          onPointerUp={endDrag}
        >
          {elements.map((el) => {
            const selected = sel.has(el.id);
            const onlyOne = selected && selectedIds.length === 1;
            // Legibility plate for a caption over an image (see scene.captionScrim).
            const scrim = captionScrim(el, elements);
            // One layout pass per frame: its hugged box AND its placed children
            // come from the same `layoutChildren` call (single height authority).
            const lay = el.kind === "frame" ? layoutChildren(el, deck.font) : null;
            const box = lay ? { w: lay.w, h: lay.h } : renderBox(el, deck.font);
            return (
              <div
                key={el.id}
                onPointerDown={(e) => onElementDown(e, el)}
                onDoubleClick={(e) => { if (el.kind === "text") { e.stopPropagation(); beginEdit(el); } }}
                className="absolute select-none"
                style={{
                  left: el.x,
                  top: el.y,
                  width: box.w,
                  height: box.h,
                  zIndex: el.z ?? 0,
                  outline: selected ? "2px solid #8ff0a4" : "1px dashed rgba(143,240,164,0.25)",
                  ...elementStyle(el, true),
                  ...(scrim ? { background: scrim, borderRadius: el.radius || 6 } : {}),
                }}
              >
                {lay ? (
                  lay.children.map((c) => {
                    // A frame child is drawn in the frame's LOCAL coords, but its
                    // scrim overlap is tested in ABSOLUTE coords against the
                    // slide's images; the child renders above the frame, so use
                    // the frame's z as its effective depth.
                    const cRect = { x: el.x + c.x, y: el.y + c.y, w: c.w, h: c.h };
                    const cScrim =
                      c.kind === "text" && c.text?.trim() && (!c.bg || c.bg === "transparent")
                        ? captionScrimForRect(cRect, c.color, el.z ?? 0, elements.filter((e) => e.id !== el.id))
                        : null;
                    // Frame children are the elements that hold ALL generated
                    // text. They are selectable and double-click inline-editable
                    // (their stable ids are preserved by layoutChildren, so edits
                    // route through the recursive updateElement + still morph).
                    const cSelected = sel.has(c.id);
                    return (
                      <div
                        key={c.id}
                        onPointerDown={(e) => {
                          if (editingId === c.id) return;
                          e.stopPropagation(); // select the child, don't drag the frame
                          selectById(c.id, e.shiftKey);
                        }}
                        onDoubleClick={(e) => { if (c.kind === "text") { e.stopPropagation(); beginEdit(c); } }}
                        className="absolute"
                        style={{
                          left: c.x, top: c.y, width: c.w, height: c.h, zIndex: c.z ?? 0,
                          ...elementStyle(c, false),
                          outline: cSelected ? "2px solid #8ff0a4" : undefined,
                          cursor: c.kind === "text" ? "text" : "pointer",
                          ...(cScrim ? { background: cScrim, borderRadius: c.radius || 6 } : {}),
                        }}
                      >
                        {c.kind === "image" && c.src ? (
                          <img src={tokenizeApiSrc(c.src)} alt={imageAlt(c)} className="h-full w-full object-cover" draggable={false} />
                        ) : isComposite(c.kind) ? (
                          <ElementInner el={c} surface={deck.background} />
                        ) : (
                          editableBody(c)
                        )}
                      </div>
                    );
                  })
                ) : el.kind === "image" && el.src ? (
                  <img src={tokenizeApiSrc(el.src)} alt={imageAlt(el)} className="h-full w-full object-cover" draggable={false} />
                ) : el.kind === "video" && el.src ? (
                  <video src={tokenizeApiSrc(el.src)} muted className="h-full w-full object-cover" style={{ pointerEvents: "none" }} />
                ) : el.kind === "flow" ? (
                  <div className="flex h-full w-full items-center justify-center text-center text-sm text-accent" style={{ pointerEvents: "none" }}>
                    ⚙ okuro·flow
                    <br />
                    {el.flowId}
                  </div>
                ) : isComposite(el.kind) ? (
                  <ElementInner
                    el={el}
                    surface={el.kind === "chart" ? chartSurface(el, elements, deck.background) : deck.background}
                  />
                ) : (
                  editableBody(el)
                )}
                {onlyOne && (
                  <div
                    onPointerDown={(e) => onResizeDown(e, el)}
                    className="absolute"
                    style={
                      heightManaged(el.kind)
                        ? { right: -6, top: "50%", marginTop: -7, width: 14, height: 14, background: "#8ff0a4", borderRadius: 3, cursor: "ew-resize" }
                        : { right: -6, bottom: -6, width: 14, height: 14, background: "#8ff0a4", borderRadius: 3, cursor: "nwse-resize" }
                    }
                  />
                )}
              </div>
            );
          })}
          {/* smart guides */}
          {guides.v.map((x, i) => (
            <div key={`v${i}`} className="pointer-events-none absolute" style={{ left: x, top: 0, width: 1, height: canvasH, background: "#ff4d6d" }} />
          ))}
          {guides.h.map((y, i) => (
            <div key={`h${i}`} className="pointer-events-none absolute" style={{ left: 0, top: y, width: canvasW, height: 1, background: "#ff4d6d" }} />
          ))}
        </div>
      </div>
    </div>
  );
}
