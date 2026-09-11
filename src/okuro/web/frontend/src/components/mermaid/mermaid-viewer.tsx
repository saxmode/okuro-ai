// <!-- AGENT_HEADER
// role: code
// purpose: Shared Mermaid viewer — one component behind the flow Mermaid tab
//   and prism diagram blocks. Lazy-loads mermaid (stays out of the initial
//   bundle), renders the SVG, and adds zoom + pan (wheel/drag, no deps) and a
//   scratch "edit source → live re-render" toggle. The editor is NOT persisted.
// index: MermaidViewer
// AGENT_HEADER_END -->
import { useCallback, useEffect, useRef, useState } from "react";
import type { PointerEvent as ReactPointerEvent } from "react";
import { Code2, Maximize2, Minus, Plus } from "lucide-react";

import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

const MIN_SCALE = 0.2;
const MAX_SCALE = 5;
const rid = () => Math.random().toString(36).slice(2);

interface Transform {
  s: number;
  tx: number;
  ty: number;
}

const clampScale = (s: number) => Math.max(MIN_SCALE, Math.min(MAX_SCALE, s));

/**
 * Zoom/pan-able Mermaid diagram with an optional scratch source editor.
 *
 * Zoom + pan are implemented by hand (strict CSP forbids external libs): a
 * `translate()/scale()` transform on a content wrapper. Wheel zooms toward the
 * cursor via a native non-passive listener so `preventDefault` actually stops
 * the page from scrolling (React's synthetic onWheel is passive). Drag pans.
 * "Edit source" re-renders the preview live (300ms debounce) but persists
 * nothing — the draft reseeds from `code` while the user hasn't edited.
 */
export function MermaidViewer({
  code,
  accent,
  className,
}: {
  code: string;
  accent?: string;
  className?: string;
}) {
  const wrapRef = useRef<HTMLDivElement>(null);
  const contentRef = useRef<HTMLDivElement>(null);
  const [err, setErr] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(code);
  const [dirty, setDirty] = useState(false);
  const [renderCode, setRenderCode] = useState(code);
  const [t, setT] = useState<Transform>({ s: 1, tx: 0, ty: 0 });
  const didFit = useRef(false);
  const drag = useRef<{ x: number; y: number; tx: number; ty: number } | null>(null);

  // Reseed the scratch draft from the prop whenever the user hasn't touched it,
  // so upstream changes to `code` flow through until the first local edit.
  useEffect(() => {
    if (!dirty) setDraft(code);
  }, [code, dirty]);

  // Debounce the active source (live edits, else the prop) into the render source.
  useEffect(() => {
    const src = dirty ? draft : code;
    const id = setTimeout(() => setRenderCode(src), 300);
    return () => clearTimeout(id);
  }, [draft, code, dirty]);

  // Fit the diagram to the viewport. Uses the SVG viewBox (transform-independent)
  // so it works whether called at identity or mid-zoom.
  const fit = useCallback(() => {
    const wrap = wrapRef.current;
    const content = contentRef.current;
    if (!wrap || !content) return;
    const svg = content.querySelector<SVGSVGElement>("svg");
    const cw = wrap.clientWidth;
    const ch = wrap.clientHeight;
    if (!svg || !cw || !ch) {
      setT({ s: 1, tx: 0, ty: 0 });
      return;
    }
    const vb = svg.viewBox?.baseVal;
    const bw = vb && vb.width ? vb.width : svg.getBoundingClientRect().width;
    const bh = vb && vb.height ? vb.height : svg.getBoundingClientRect().height;
    if (!bw || !bh) {
      setT({ s: 1, tx: 0, ty: 0 });
      return;
    }
    const s = clampScale(Math.min(cw / bw, ch / bh));
    setT({ s, tx: (cw - bw * s) / 2, ty: (ch - bh * s) / 2 });
  }, []);

  // Render mermaid → inject SVG. Lazy import keeps mermaid in its own chunk.
  useEffect(() => {
    let cancelled = false;
    setErr(null);
    (async () => {
      try {
        const mermaid = (await import("mermaid")).default;
        mermaid.initialize({
          startOnLoad: false,
          theme: "dark",
          securityLevel: "loose",
          flowchart: { htmlLabels: true },
          // Brand law: monospace only. Without this, mermaid's dark theme leaks
          // its trebuchet-ms default onto every node label (a mono-brand violation).
          themeVariables: {
            fontFamily: "var(--font-mono, 'JetBrains Mono', ui-monospace, 'SF Mono', 'Cascadia Code', 'Roboto Mono', Menlo, Monaco, Consolas, 'Liberation Mono', 'Courier New', monospace)",
            ...(accent ? { lineColor: accent } : {}),
          },
        });
        const { svg } = await mermaid.render("mmv-" + rid(), renderCode);
        if (cancelled || !contentRef.current) return;
        contentRef.current.innerHTML = svg;
        // Pin the SVG to its intrinsic size so our transform scale is predictable
        // (mermaid otherwise caps it with an inline max-width).
        const el = contentRef.current.querySelector<SVGSVGElement>("svg");
        const vb = el?.viewBox?.baseVal;
        if (el && vb && vb.width) {
          el.style.maxWidth = "none";
          el.style.width = `${vb.width}px`;
          el.style.height = `${vb.height}px`;
        }
        // Auto-fit only the first successful render; later renders (live edits,
        // accent changes) keep the user's current zoom/pan.
        if (!didFit.current) {
          didFit.current = true;
          requestAnimationFrame(fit);
        }
      } catch (e) {
        if (!cancelled) setErr(String((e as Error)?.message || e));
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [renderCode, accent, fit]);

  // Wheel zoom toward the cursor. Native non-passive listener → preventDefault
  // stops the page scrolling under the diagram.
  useEffect(() => {
    const wrap = wrapRef.current;
    if (!wrap) return;
    const onWheel = (e: WheelEvent) => {
      e.preventDefault();
      const rect = wrap.getBoundingClientRect();
      const mx = e.clientX - rect.left;
      const my = e.clientY - rect.top;
      setT((p) => {
        const s = clampScale(p.s * (e.deltaY < 0 ? 1.1 : 1 / 1.1));
        if (s === p.s) return p;
        const cx = (mx - p.tx) / p.s;
        const cy = (my - p.ty) / p.s;
        return { s, tx: mx - cx * s, ty: my - cy * s };
      });
    };
    wrap.addEventListener("wheel", onWheel, { passive: false });
    return () => wrap.removeEventListener("wheel", onWheel);
  }, []);

  const onPointerDown = (e: ReactPointerEvent<HTMLDivElement>) => {
    if (e.button !== 0) return;
    drag.current = { x: e.clientX, y: e.clientY, tx: t.tx, ty: t.ty };
    e.currentTarget.setPointerCapture?.(e.pointerId);
  };
  const onPointerMove = (e: ReactPointerEvent<HTMLDivElement>) => {
    const d = drag.current;
    if (!d) return;
    const dx = e.clientX - d.x;
    const dy = e.clientY - d.y;
    setT((p) => ({ ...p, tx: d.tx + dx, ty: d.ty + dy }));
  };
  const onPointerUp = (e: ReactPointerEvent<HTMLDivElement>) => {
    drag.current = null;
    e.currentTarget.releasePointerCapture?.(e.pointerId);
  };

  // +/- buttons zoom toward the viewport centre.
  const zoomBy = (factor: number) =>
    setT((p) => {
      const wrap = wrapRef.current;
      const cw = wrap ? wrap.clientWidth / 2 : 0;
      const ch = wrap ? wrap.clientHeight / 2 : 0;
      const s = clampScale(p.s * factor);
      if (s === p.s) return p;
      const cx = (cw - p.tx) / p.s;
      const cy = (ch - p.ty) / p.s;
      return { s, tx: cw - cx * s, ty: ch - cy * s };
    });

  const copy = () => {
    try {
      navigator.clipboard.writeText(dirty ? draft : code);
      setCopied(true);
      setTimeout(() => setCopied(false), 1200);
    } catch {
      /* clipboard blocked — no-op */
    }
  };

  return (
    <div className={cn("flex h-full min-h-0 flex-col", className)}>
      <div className="flex flex-wrap items-center gap-1.5 border-b border-border p-1.5">
        <Button variant="outline" size="sm" onClick={copy} title="copy Mermaid source">
          {copied ? "copied ✓" : "Copy source"}
        </Button>
        <Button
          variant={editing ? "secondary" : "outline"}
          size="sm"
          onClick={() => setEditing((v) => !v)}
          title="edit the source and re-render live (scratch, not saved)"
        >
          <Code2 className="h-4 w-4" />
          Edit source
        </Button>
        <div className="ml-auto flex items-center gap-1">
          <Button variant="outline" size="icon-sm" onClick={() => zoomBy(1 / 1.2)} title="zoom out">
            <Minus className="h-4 w-4" />
          </Button>
          <span className="w-10 text-center text-xs tabular-nums text-tertiary">
            {Math.round(t.s * 100)}%
          </span>
          <Button variant="outline" size="icon-sm" onClick={() => zoomBy(1.2)} title="zoom in">
            <Plus className="h-4 w-4" />
          </Button>
          <Button variant="outline" size="icon-sm" onClick={fit} title="fit to view">
            <Maximize2 className="h-4 w-4" />
          </Button>
        </div>
      </div>

      {err ? (
        <pre className="min-h-0 flex-1 overflow-auto whitespace-pre-wrap p-3 text-xs text-error">
          Mermaid render error:{"\n"}
          {err}
          {"\n\n"}
          {dirty ? draft : code}
        </pre>
      ) : (
        <div className="flex min-h-0 flex-1 flex-col">
          {editing && (
            <textarea
              value={draft}
              onChange={(e) => {
                setDraft(e.target.value);
                setDirty(true);
              }}
              spellCheck={false}
              className="h-40 w-full shrink-0 resize-y border-b border-border bg-surface px-3 py-2 font-mono text-xs text-fg outline-none"
              placeholder="mermaid source…"
            />
          )}
          <div
            ref={wrapRef}
            onPointerDown={onPointerDown}
            onPointerMove={onPointerMove}
            onPointerUp={onPointerUp}
            onPointerLeave={onPointerUp}
            className="relative min-h-0 flex-1 cursor-grab touch-none select-none overflow-hidden bg-black/10 active:cursor-grabbing"
          >
            <div
              ref={contentRef}
              style={{
                transform: `translate(${t.tx}px, ${t.ty}px) scale(${t.s})`,
                transformOrigin: "0 0",
              }}
              className="absolute left-0 top-0 origin-top-left"
            />
          </div>
        </div>
      )}
    </div>
  );
}

export default MermaidViewer;
