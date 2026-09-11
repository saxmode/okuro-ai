import { useCallback, useEffect, useRef, useState } from "react";
import { Ban, Check, Eraser, Hand, Pencil, Trash2 } from "lucide-react";
import { notesApi } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

// Strokes live in an unbounded WORLD coordinate space; a viewport (pan + zoom)
// maps world → screen, so the canvas is effectively infinite and zoomable.
// screen = world * scale + offset.

const COLORS = ["#e6e6e6", "#111111", "#ff5c5c", "#ffcf5c", "#5cff9d", "#5cb2ff", "#c98cff"];
const SIZES = [3, 6, 12];
const DEFAULT_COLOR = "#e6e6e6";
const DEFAULT_SIZE = 6;
const BG = "#0a0a0a";
const MIN_SCALE = 0.1;
const MAX_SCALE = 8;

interface Point {
  x: number;
  y: number;
}
interface Stroke {
  color: string;
  size: number;
  erase: boolean;
  points: Point[];
}
interface Scene {
  strokes: Stroke[];
}
interface View {
  scale: number;
  ox: number;
  oy: number;
}

function emptyScene(): Scene {
  return { strokes: [] };
}

function strokesBBox(strokes: Stroke[]): { minX: number; minY: number; maxX: number; maxY: number } | null {
  let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity, pad = 0;
  for (const s of strokes) {
    pad = Math.max(pad, s.size);
    for (const p of s.points) {
      if (p.x < minX) minX = p.x;
      if (p.y < minY) minY = p.y;
      if (p.x > maxX) maxX = p.x;
      if (p.y > maxY) maxY = p.y;
    }
  }
  if (!isFinite(minX)) return null;
  pad += 12;
  return { minX: minX - pad, minY: minY - pad, maxX: maxX + pad, maxY: maxY + pad };
}

// Paint strokes onto a context already transformed to world space.
function paintStrokes(ctx: CanvasRenderingContext2D, strokes: Stroke[]) {
  ctx.lineJoin = "round";
  ctx.lineCap = "round";
  for (const s of strokes) {
    const p0 = s.points[0];
    if (!p0) continue;
    ctx.strokeStyle = s.erase ? BG : s.color;
    ctx.lineWidth = s.size;
    ctx.beginPath();
    ctx.moveTo(p0.x, p0.y);
    for (let i = 1; i < s.points.length; i++) {
      const p = s.points[i];
      if (p) ctx.lineTo(p.x, p.y);
    }
    if (s.points.length === 1) ctx.lineTo(p0.x + 0.01, p0.y + 0.01);
    ctx.stroke();
  }
}

// Render the drawn content, cropped to its bounding box, as a PNG data URL for
// the inline note embed.
function toPng(strokes: Stroke[]): string {
  const bb = strokesBBox(strokes);
  const c = document.createElement("canvas");
  const w = bb ? bb.maxX - bb.minX : 100;
  const h = bb ? bb.maxY - bb.minY : 100;
  const exportScale = Math.min(2, 2400 / Math.max(w, h, 1));
  c.width = Math.max(1, Math.round(w * exportScale));
  c.height = Math.max(1, Math.round(h * exportScale));
  const ctx = c.getContext("2d");
  if (!ctx) return "";
  ctx.fillStyle = BG;
  ctx.fillRect(0, 0, c.width, c.height);
  if (bb) {
    ctx.setTransform(exportScale, 0, 0, exportScale, -bb.minX * exportScale, -bb.minY * exportScale);
    paintStrokes(ctx, strokes);
  }
  return c.toDataURL("image/png");
}

interface Props {
  drawingId: string | null;
  noteId: string;
  onCreated: (id: string) => void;
  onClose: () => void;
  onDelete?: () => void;
  /** "screen" = full-screen modal over the note (embedded scribbles). "region"
   *  = fills its positioned parent, no close/delete chrome (drawing-note sheet). */
  fill?: "screen" | "region";
}

/**
 * ScribbleWindow — an infinite, zoomable, pannable scribble surface tuned for
 * iPad + Apple Pencil: the PENCIL draws; FINGERS pan (one) and pinch-zoom (two)
 * and never draw, so a resting palm can't mark the page. Mouse draws;
 * ⌘/ctrl+wheel zooms, wheel pans; a Hand tool pans by drag. Autosaves the
 * stroke scene + a bbox-cropped PNG the note embeds inline.
 *
 * Input runs on TWO paths through one API-agnostic core — Touch Events where
 * they exist (the Pencil's real sample rate, and immune to Scribble eating
 * pointer events), Pointer Events for mouse/trackpad — because depending on a
 * single input API is what made strokes come out angular and half-recorded.
 */
export function ScribbleWindow({ drawingId, noteId, onCreated, onClose, onDelete, fill = "screen" }: Props) {
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const wrapRef = useRef<HTMLDivElement | null>(null);
  const sceneRef = useRef<Scene>(emptyScene());
  const viewRef = useRef<View>({ scale: 1, ox: 0, oy: 0 });
  const drawingRef = useRef<Stroke | null>(null);
  // Which input API owns the stroke in flight, and the pointer/touch id that
  // started it. The lock keeps the two APIs from recording one physical
  // stroke twice, and makes every "is this event mine?" test explicit.
  const srcRef = useRef<"pointer" | "touch" | null>(null);
  const gidRef = useRef<number | null>(null);
  // Active touch pointers for pan/pinch, and the current gesture anchor.
  const touchesRef = useRef<Map<number, Point>>(new Map());
  const gestureRef = useRef<{ mid: Point; dist: number } | null>(null);
  // Touch Events are PRIMARY wherever they exist. Apple's developer forums
  // (thread 776468) document pointermove having a LOWER sample rate than
  // touchmove for Apple Pencil on iPad Safari — pointer-driven strokes come
  // out as visible straight segments — and iPadOS Scribble can swallow
  // pointer events for the Pencil outright. Pointer Events stay as the
  // mouse/trackpad path; a mouse emits no touch events, so the two paths
  // cannot double-record.
  // maxTouchPoints, not just `ontouchstart`: iPadOS 13+ Safari (desktop-class
  // browsing) still FIRES touch events but no longer exposes ontouchstart on
  // window, so that check alone leaves this path dead on the one device it
  // exists for. Measured: Chromium with touch enabled behaves the same way.
  // A false positive is harmless — see the touchType escape hatch below.
  const touchPrimaryRef = useRef(
    typeof window !== "undefined" && ("ontouchstart" in window || (navigator.maxTouchPoints ?? 0) > 0),
  );
  const firstTouchRef = useRef(true);
  const idRef = useRef<string | null>(drawingId);
  const saveTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  // One drawingSave in flight at a time — see save() below.
  const savingRef = useRef(false);
  const rafRef = useRef<number | null>(null);
  const [ready, setReady] = useState(drawingId === null);
  const [status, setStatus] = useState("");
  const [zoomPct, setZoomPct] = useState(100);

  const [color, setColor] = useState<string>(DEFAULT_COLOR);
  const [size, setSize] = useState<number>(DEFAULT_SIZE);
  const [erase, setErase] = useState(false);
  const [hand, setHand] = useState(false);
  // Latest tool values for the imperative pointer handlers.
  const tool = useRef({ color, size, erase, hand });
  tool.current = { color, size, erase, hand };

  const dpr = () => Math.min(2, window.devicePixelRatio || 1);

  const draw = useCallback(() => {
    const canvas = canvasRef.current;
    const ctx = canvas?.getContext("2d");
    if (!canvas || !ctx) return;
    const d = dpr();
    const v = viewRef.current;
    ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.fillStyle = BG;
    ctx.fillRect(0, 0, canvas.width, canvas.height);
    ctx.setTransform(v.scale * d, 0, 0, v.scale * d, v.ox * d, v.oy * d);
    paintStrokes(ctx, sceneRef.current.strokes);
  }, []);

  const scheduleDraw = useCallback(() => {
    if (rafRef.current != null) return;
    rafRef.current = requestAnimationFrame(() => {
      rafRef.current = null;
      draw();
    });
  }, [draw]);

  // Paint ONE new segment on top of what is already on the canvas. Repainting
  // the WHOLE scene per animation frame while writing starves the event loop
  // (the Pencil samples at 240 Hz and the scene grows without bound), and the
  // symptom is late or missing points. A full repaint is needed only when the
  // view or the scene changes as a whole — pan, zoom, resize, clear, load.
  const paintSegment = useCallback((s: Stroke, a: Point, b: Point) => {
    const ctx = canvasRef.current?.getContext("2d");
    if (!ctx) return;
    const d = dpr();
    const v = viewRef.current;
    ctx.setTransform(v.scale * d, 0, 0, v.scale * d, v.ox * d, v.oy * d);
    ctx.lineJoin = "round";
    ctx.lineCap = "round";
    ctx.strokeStyle = s.erase ? BG : s.color;
    ctx.lineWidth = s.size;
    ctx.beginPath();
    ctx.moveTo(a.x, a.y);
    ctx.lineTo(b.x, b.y);
    ctx.stroke();
  }, []);

  const resize = useCallback(() => {
    const canvas = canvasRef.current;
    const wrap = wrapRef.current;
    if (!canvas || !wrap) return;
    const rect = wrap.getBoundingClientRect();
    const d = dpr();
    canvas.style.width = `${rect.width}px`;
    canvas.style.height = `${rect.height}px`;
    canvas.width = Math.max(1, Math.round(rect.width * d));
    canvas.height = Math.max(1, Math.round(rect.height * d));
    draw();
  }, [draw]);

  // Fit the view so existing strokes are centered + visible.
  const fitView = useCallback(() => {
    const wrap = wrapRef.current;
    const bb = strokesBBox(sceneRef.current.strokes);
    if (!wrap || !bb) {
      viewRef.current = { scale: 1, ox: 0, oy: 0 };
    } else {
      const rect = wrap.getBoundingClientRect();
      const bw = bb.maxX - bb.minX, bh = bb.maxY - bb.minY;
      const scale = Math.max(MIN_SCALE, Math.min(MAX_SCALE, Math.min(rect.width / bw, rect.height / bh) * 0.9));
      viewRef.current = {
        scale,
        ox: rect.width / 2 - ((bb.minX + bb.maxX) / 2) * scale,
        oy: rect.height / 2 - ((bb.minY + bb.maxY) / 2) * scale,
      };
    }
    setZoomPct(Math.round(viewRef.current.scale * 100));
    scheduleDraw();
  }, [scheduleDraw]);

  // Load an existing scene once.
  useEffect(() => {
    let alive = true;
    if (drawingId) {
      notesApi
        .drawingGet(drawingId)
        .then((r) => {
          if (!alive) return;
          const sc = r.drawing?.scene as Partial<Scene> | undefined;
          sceneRef.current = sc && Array.isArray(sc.strokes) ? { strokes: sc.strokes as Stroke[] } : emptyScene();
          setReady(true);
          resize();
          fitView();
        })
        .catch(() => {
          if (!alive) return;
          setReady(true);
          resize();
        });
    }
    return () => {
      alive = false;
    };
  }, [drawingId, resize, fitView]);

  useEffect(() => {
    if (!ready) return;
    resize();
    window.addEventListener("resize", resize);
    return () => window.removeEventListener("resize", resize);
  }, [ready, resize]);

  const save = useCallback(() => {
    if (saveTimer.current) clearTimeout(saveTimer.current);
    saveTimer.current = setTimeout(async () => {
      if (sceneRef.current.strokes.length === 0) return;
      // Serialize writes: idRef is only assigned after the await below, so a
      // second save during an in-flight drawingSave would still see null and
      // create a SECOND drawing. Re-arm instead — the retry runs once the
      // first completes, with idRef set. Same guard as notes autosave.
      if (savingRef.current) {
        save();
        return;
      }
      savingRef.current = true;
      setStatus("saving…");
      try {
        const res = await notesApi.drawingSave({
          id: idRef.current ?? undefined,
          note_id: noteId,
          scene: sceneRef.current as unknown as Record<string, unknown>,
          png_base64: toPng(sceneRef.current.strokes),
        });
        if (!idRef.current) {
          idRef.current = res.drawing.id;
          onCreated(res.drawing.id);
        }
        setStatus("saved");
      } catch {
        setStatus("save failed");
      } finally {
        savingRef.current = false;
      }
    }, 700);
  }, [noteId, onCreated]);

  // Screen (canvas-relative) → world coordinates.
  const toWorld = (clientX: number, clientY: number): Point => {
    const canvas = canvasRef.current!;
    const rect = canvas.getBoundingClientRect();
    const v = viewRef.current;
    return { x: (clientX - rect.left - v.ox) / v.scale, y: (clientY - rect.top - v.oy) / v.scale };
  };

  const midOf = (pts: Point[]): Point => ({
    x: pts.reduce((a, p) => a + p.x, 0) / pts.length,
    y: pts.reduce((a, p) => a + p.y, 0) / pts.length,
  });
  const distOf = (a: Point, b: Point) => Math.hypot(a.x - b.x, a.y - b.y);

  const zoomAround = (screen: Point, factor: number) => {
    const v = viewRef.current;
    const next = Math.max(MIN_SCALE, Math.min(MAX_SCALE, v.scale * factor));
    const wx = (screen.x - v.ox) / v.scale;
    const wy = (screen.y - v.oy) / v.scale;
    v.scale = next;
    v.ox = screen.x - wx * next;
    v.oy = screen.y - wy * next;
    setZoomPct(Math.round(next * 100));
    scheduleDraw();
  };

  const screenOf = (clientX: number, clientY: number): Point => {
    const r = canvasRef.current!.getBoundingClientRect();
    return { x: clientX - r.left, y: clientY - r.top };
  };

  // Safari treats the Apple Pencil like a mouse and will start a text
  // selection on drag — clear whatever it managed to highlight.
  const dropSelection = () => {
    const sel = window.getSelection?.();
    if (sel && sel.rangeCount) sel.removeAllRanges();
  };

  // ---- input core, API-agnostic -------------------------------------------
  // The Touch and the Pointer path both funnel through these, so the two can
  // never diverge in behaviour. This is the shape the file lacked: one input
  // API, one set of rules, and no way to recover when that API misbehaved.

  const beginDraw = (src: "pointer" | "touch", id: number, clientX: number, clientY: number) => {
    // TAKE OVER rather than early-return. A pen-up the browser never
    // delivered (Scribble, a system gesture, a pen lifted off-screen) would
    // otherwise leave a stroke in flight that swallows every later one.
    if (drawingRef.current) commitDraw();
    const t = tool.current;
    srcRef.current = src;
    gidRef.current = id;
    drawingRef.current = { color: t.color, size: t.size, erase: t.erase, points: [toWorld(clientX, clientY)] };
    sceneRef.current.strokes.push(drawingRef.current);
    scheduleDraw();
  };

  const extendDraw = (src: "pointer" | "touch", id: number, clientX: number, clientY: number) => {
    const stroke = drawingRef.current;
    if (!stroke || srcRef.current !== src || gidRef.current !== id) return;
    const p = toWorld(clientX, clientY);
    const prev = stroke.points[stroke.points.length - 1]!;
    // Sub-pixel jitter at 240 Hz is noise, not movement; a straight lineTo
    // between half-pixel-spaced points is visually identical to a curve.
    if (Math.hypot(p.x - prev.x, p.y - prev.y) * viewRef.current.scale < 0.5) return;
    stroke.points.push(p);
    paintSegment(stroke, prev, p);
  };

  const commitDraw = () => {
    if (!drawingRef.current) return;
    drawingRef.current = null;
    srcRef.current = null;
    gidRef.current = null;
    save();
  };

  const endDraw = (src: "pointer" | "touch", id: number) => {
    if (srcRef.current !== src || gidRef.current !== id) return;
    commitDraw();
  };

  // ---- pan / pinch, API-agnostic ------------------------------------------

  const navBegin = (id: number, screen: Point) => {
    touchesRef.current.set(id, screen);
    const pts = [...touchesRef.current.values()];
    gestureRef.current = pts.length >= 2 ? { mid: midOf(pts), dist: distOf(pts[0]!, pts[1]!) } : { mid: pts[0]!, dist: 0 };
  };

  const navMove = (id: number, screen: Point) => {
    if (!touchesRef.current.has(id)) return;
    touchesRef.current.set(id, screen);
    const g = gestureRef.current;
    if (!g) return;
    const pts = [...touchesRef.current.values()];
    const v = viewRef.current;
    if (pts.length >= 2) {
      const mid = midOf(pts), dist = distOf(pts[0]!, pts[1]!);
      if (g.dist > 0) zoomAround(mid, dist / g.dist);
      v.ox += mid.x - g.mid.x;
      v.oy += mid.y - g.mid.y;
      gestureRef.current = { mid, dist };
    } else {
      v.ox += pts[0]!.x - g.mid.x;
      v.oy += pts[0]!.y - g.mid.y;
      gestureRef.current = { mid: pts[0]!, dist: 0 };
    }
    scheduleDraw();
  };

  const navEnd = (id: number) => {
    touchesRef.current.delete(id);
    const pts = [...touchesRef.current.values()];
    gestureRef.current =
      pts.length >= 2 ? { mid: midOf(pts), dist: distOf(pts[0]!, pts[1]!) } : pts.length === 1 ? { mid: pts[0]!, dist: 0 } : null;
  };

  // Input is bound NATIVELY, not through React props, for three reasons:
  // React registers touchstart/touchmove/wheel as PASSIVE, so preventDefault()
  // inside a React handler is ignored (no Scribble suppression, no zoom
  // containment); pointerup/pointercancel must sit on WINDOW so a pen lifted
  // past the canvas edge still ends the stroke; and pointermove needs
  // getCoalescedEvents(), which only exists on the native event.
  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas || !ready) return;

    // True when the Pointer path owns this event. On a touch device only the
    // mouse comes through here — pen and finger belong to the Touch path.
    const pointerOwns = (e: PointerEvent) => !touchPrimaryRef.current || e.pointerType === "mouse";

    const onPointerDown = (e: PointerEvent) => {
      // Gate BEFORE preventDefault: on a touch device the Touch path owns this
      // gesture and cancels its own touchstart. Cancelling pointerdown here as
      // well can only risk suppressing the touch events that path needs.
      if (!pointerOwns(e)) return;
      e.preventDefault();
      dropSelection();
      // Fingers navigate; the pencil/mouse draws (unless the Hand tool is on).
      if (e.pointerType === "touch") {
        navBegin(e.pointerId, screenOf(e.clientX, e.clientY));
        return;
      }
      try {
        canvas.setPointerCapture?.(e.pointerId);
      } catch {
        /* pointer already released — ignore */
      }
      // Never start a stroke above the canvas (the header) — guards against a
      // pointer retargeted onto the canvas via stale capture.
      if (e.clientY < canvas.getBoundingClientRect().top) return;
      if (tool.current.hand) {
        gestureRef.current = { mid: screenOf(e.clientX, e.clientY), dist: 0 };
        return;
      }
      beginDraw("pointer", e.pointerId, e.clientX, e.clientY);
    };

    const onPointerMove = (e: PointerEvent) => {
      if (!pointerOwns(e)) return;
      if (e.pointerType === "touch") {
        navMove(e.pointerId, screenOf(e.clientX, e.clientY));
        return;
      }
      if (tool.current.hand && gestureRef.current) {
        const screen = screenOf(e.clientX, e.clientY);
        const v = viewRef.current;
        v.ox += screen.x - gestureRef.current.mid.x;
        v.oy += screen.y - gestureRef.current.mid.y;
        gestureRef.current = { mid: screen, dist: 0 };
        scheduleDraw();
        return;
      }
      // The browser coalesces pointermove to one event per frame;
      // getCoalescedEvents() hands back every sample behind it, so nothing the
      // device actually reported is thrown away.
      const coalesced = e.getCoalescedEvents?.() ?? [];
      const samples = coalesced.length ? coalesced : [e];
      for (const s of samples) extendDraw("pointer", e.pointerId, s.clientX, s.clientY);
    };

    // On window, not the canvas: a pen lifted past the canvas edge still ends
    // the stroke. The old canvas-bound pointerleave TRUNCATED a stroke the
    // moment the pen crossed the edge mid-write.
    const onPointerUp = (e: PointerEvent) => {
      if (!pointerOwns(e)) return;
      if (e.pointerType === "touch") {
        navEnd(e.pointerId);
        return;
      }
      if (tool.current.hand) {
        gestureRef.current = null;
        return;
      }
      endDraw("pointer", e.pointerId);
    };

    // ---- Touch path (primary on iPad; Scribble- and sample-rate-proof) ----

    const isStylus = (t: Touch) => (t as Touch & { touchType?: string }).touchType === "stylus";

    const onTouchStart = (e: TouchEvent) => {
      // Non-passive preventDefault: this is what suppresses Scribble and text
      // selection on the canvas. It is a no-op in a React onTouchStart.
      e.preventDefault();
      dropSelection();
      if (firstTouchRef.current) {
        firstTouchRef.current = false;
        // Only WebKit reports Touch.touchType. Without it a stylus is
        // indistinguishable from a finger here, so hand the device back to
        // Pointer Events, which do report pointerType. A self-healing escape
        // hatch — never a filter that can reject everything forever.
        if ((e.changedTouches[0] as (Touch & { touchType?: string }) | undefined)?.touchType === undefined) {
          touchPrimaryRef.current = false;
          return;
        }
      }
      if (!touchPrimaryRef.current) return;
      for (const t of e.changedTouches) {
        if (isStylus(t) && !tool.current.hand) beginDraw("touch", t.identifier, t.clientX, t.clientY);
        else navBegin(t.identifier, screenOf(t.clientX, t.clientY));
      }
    };

    const onTouchMove = (e: TouchEvent) => {
      e.preventDefault();
      if (!touchPrimaryRef.current) return;
      for (const t of e.changedTouches) {
        if (srcRef.current === "touch" && gidRef.current === t.identifier) extendDraw("touch", t.identifier, t.clientX, t.clientY);
        else navMove(t.identifier, screenOf(t.clientX, t.clientY));
      }
    };

    const onTouchEnd = (e: TouchEvent) => {
      e.preventDefault();
      if (!touchPrimaryRef.current) return;
      for (const t of e.changedTouches) {
        if (srcRef.current === "touch" && gidRef.current === t.identifier) endDraw("touch", t.identifier);
        else navEnd(t.identifier);
      }
    };

    const onWheel = (e: WheelEvent) => {
      // Non-passive: without this the page zooms behind the canvas.
      e.preventDefault();
      const screen = screenOf(e.clientX, e.clientY);
      if (e.ctrlKey || e.metaKey) {
        zoomAround(screen, Math.exp(-e.deltaY * 0.01));
      } else {
        const v = viewRef.current;
        v.ox -= e.deltaX;
        v.oy -= e.deltaY;
        scheduleDraw();
      }
    };

    const active = { passive: false } as const;
    canvas.addEventListener("pointerdown", onPointerDown, active);
    canvas.addEventListener("pointermove", onPointerMove, active);
    canvas.addEventListener("touchstart", onTouchStart, active);
    canvas.addEventListener("touchmove", onTouchMove, active);
    canvas.addEventListener("touchend", onTouchEnd, active);
    canvas.addEventListener("touchcancel", onTouchEnd, active);
    canvas.addEventListener("wheel", onWheel, active);
    window.addEventListener("pointerup", onPointerUp);
    window.addEventListener("pointercancel", onPointerUp);
    return () => {
      canvas.removeEventListener("pointerdown", onPointerDown);
      canvas.removeEventListener("pointermove", onPointerMove);
      canvas.removeEventListener("touchstart", onTouchStart);
      canvas.removeEventListener("touchmove", onTouchMove);
      canvas.removeEventListener("touchend", onTouchEnd);
      canvas.removeEventListener("touchcancel", onTouchEnd);
      canvas.removeEventListener("wheel", onWheel);
      window.removeEventListener("pointerup", onPointerUp);
      window.removeEventListener("pointercancel", onPointerUp);
    };
    // The handlers read tool/view/scene through refs, so a closure from an
    // earlier render is still correct — only `ready` needs to re-bind.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [ready]);

  const clear = () => {
    if (sceneRef.current.strokes.length && !window.confirm("Clear the whole canvas?")) return;
    sceneRef.current = emptyScene();
    scheduleDraw();
    save();
  };

  const swatch = (c: string) => (
    <button
      key={c}
      onClick={() => {
        setColor(c);
        setErase(false);
        setHand(false);
      }}
      title="Colour"
      className={cn(
        "h-5 w-5 rounded-full border border-border transition-transform hover:scale-110",
        color === c && !erase && "ring-2 ring-accent ring-offset-1 ring-offset-surface",
      )}
      style={{ background: c }}
    />
  );

  return (
    <div
      className={cn(
        "scribble-window flex select-none flex-col bg-surface/95 backdrop-blur-sm",
        fill === "region" ? "absolute inset-0 z-10" : "fixed inset-0 z-50",
      )}
      style={{ WebkitUserSelect: "none", WebkitTouchCallout: "none" }}
    >
      <div className="scribble-header flex items-center gap-3 border-b border-border px-4 py-2">
        <div className="flex items-center gap-1.5">{COLORS.map(swatch)}</div>
        <div className="mx-1 h-5 w-px bg-border" />
        <div className="flex items-center gap-1">
          {SIZES.map((s) => (
            <button
              key={s}
              onClick={() => {
                setSize(s);
                setErase(false);
                setHand(false);
              }}
              title={`Thickness ${s}`}
              className={cn(
                "flex h-7 w-7 items-center justify-center rounded hover:bg-surface-elevated",
                size === s && !erase && !hand && "bg-surface-elevated ring-1 ring-inset ring-accent",
              )}
            >
              <span className="rounded-full bg-fg" style={{ width: s + 2, height: s + 2 }} />
            </button>
          ))}
        </div>
        <div className="mx-1 h-5 w-px bg-border" />
        <Button
          size="icon"
          variant={erase ? "secondary" : "ghost"}
          onClick={() => {
            setErase((v) => !v);
            setHand(false);
          }}
          title="Eraser"
        >
          <Eraser className="h-4 w-4" />
        </Button>
        <Button
          size="icon"
          variant={hand ? "secondary" : "ghost"}
          onClick={() => {
            setHand((v) => !v);
            setErase(false);
          }}
          title="Pan (or drag with one finger)"
        >
          <Hand className="h-4 w-4" />
        </Button>
        <Button size="icon" variant="ghost" onClick={clear} title="Clear canvas">
          <Ban className="h-4 w-4" />
        </Button>
        <button
          onClick={fitView}
          title="Fit / reset view"
          className="rounded px-2 py-1 text-3xs tabular-nums text-tertiary hover:bg-surface-elevated"
        >
          {zoomPct}%
        </button>
        <div className="ml-auto flex items-center gap-3">
          <span className="hidden items-center gap-1 text-3xs text-tertiary md:inline-flex">
            <Pencil className="h-3 w-3" /> draws · fingers pan / pinch-zoom
          </span>
          <span className="text-3xs text-tertiary">{status}</span>
          {fill === "screen" && onDelete && (
            <Button
              size="icon"
              variant="ghost"
              onClick={onDelete}
              title="Delete scribble (remove from note)"
              className="hover:text-error"
            >
              <Trash2 className="h-4 w-4" />
            </Button>
          )}
          {fill === "screen" && (
            <Button size="icon" variant="ghost" onClick={onClose} title="Done">
              <Check className="h-4 w-4" />
            </Button>
          )}
        </div>
      </div>
      <div ref={wrapRef} className="min-h-0 flex-1 overflow-hidden">
        {/* All input is bound natively in the effect above — see why there. */}
        <canvas
          ref={canvasRef}
          className={cn("h-full w-full touch-none select-none", hand ? "cursor-grab" : erase ? "cursor-cell" : "cursor-crosshair")}
          style={{ background: BG, WebkitUserSelect: "none", WebkitTouchCallout: "none" }}
        />
      </div>
    </div>
  );
}
