// <!-- AGENT_HEADER
// role: code
// purpose: Deck overview / index for okuro·prism, rendered as a MATRIX MAP —
//   rows = topics (facets, depth-first), columns = depth (Overview → Working →
//   Expert). A filled cell means that topic carries that depth; clicking any
//   cell jumps straight there. The index therefore teaches the 2D model it sits
//   inside: right = more expert, down = more topics. Shared by GridDeck (row 0 /
//   first slide) and both scroll decks (viewer + export) so there is one index.
// index: COLS | DeckIndex
// AGENT_HEADER_END -->
import { rungToBlocks, type Facet } from "@/lib/prism-api";

// The four depth columns = the slide-model ladder L1→L4, one per GridDeck slide:
// L1 cover · L2 key points · L3 detail · L4 full-doc. A filled cell means the
// topic carries that level; clicking it jumps to that slide.
import type { Rung } from "@/lib/prism-api";
import { parseApiDate } from "@/lib/format";
const COLS: { col: number; label: string; short: string; rung: Rung }[] = [
  { col: 0, label: "Cover", short: "L1", rung: "L1" },
  { col: 1, label: "Key Points", short: "L2", rung: "L2" },
  { col: 2, label: "Detail", short: "L3", rung: "L3" },
  { col: 3, label: "Full Doc", short: "L4", rung: "L4" },
];

function hasCol(facet: Facet, col: number): boolean {
  const rung = COLS[col]?.rung;
  if (col === 0) return true; // every topic has an L1 cover
  return !!rung && rungToBlocks(facet.rungs?.[rung]).length > 0;
}

/** Cover meta (brand + date + recipient) shown under the title on the first
 *  slide, turning the overview into the deck's header/cover. */
export interface DeckMeta {
  brand?: string;
  /** Raw SQLite timestamp ("YYYY-MM-DD HH:MM:SS" UTC) or any Date-parseable string. */
  date?: string;
  recipient?: string;
}

function fmtDate(s?: string): string | null {
  if (!s) return null;
  const d = parseApiDate(s.trim());
  return isNaN(d.getTime()) ? null : d.toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" });
}

/** Matrix-map index. `onSelect(facetId, col)` navigates to that cell; the host
 *  decides what a column means (2D → that slide; scroll → section / detail).
 *  `active` highlights the current position when the host tracks one. */
export function DeckIndex({
  facets,
  title,
  accent,
  onSelect,
  active,
  meta,
}: {
  facets: Facet[];
  title: string;
  accent: string;
  onSelect: (facetId: string, col: number) => void;
  active?: { facetId: string; col: number };
  meta?: DeckMeta;
}) {
  const rowGrid = "grid grid-cols-[1fr_repeat(4,2rem)] items-center gap-x-3";
  const date = fmtDate(meta?.date);
  const coverBits = [meta?.brand, meta?.recipient ? `for ${meta.recipient}` : null, date].filter(Boolean);
  return (
    <div className="w-full">
      <p className="text-xs font-medium uppercase tracking-[0.25em]" style={{ color: accent }}>Overview</p>
      <h2 className="mt-1 text-4xl font-semibold leading-tight md:text-5xl">{title}</h2>
      {coverBits.length > 0 && (
        <p className="mt-3 flex flex-wrap items-center gap-x-2 gap-y-1 text-sm opacity-60">
          {coverBits.map((b, i) => (
            <span key={i} className="flex items-center gap-2">
              {i > 0 && <span className="opacity-40">·</span>}
              {b}
            </span>
          ))}
        </p>
      )}
      <p className="mt-1 text-sm opacity-50">
        {facets.length} {facets.length === 1 ? "topic" : "topics"} · right = deeper, down = next · click any cell to jump
      </p>

      <div className="mt-6 border-t border-current/10">
        {/* column header */}
        <div className={`${rowGrid} py-2 text-[10px] uppercase tracking-wide opacity-40`}>
          <span>Topic</span>
          {COLS.map((c) => (
            <span key={c.col} className="text-center">{c.short}</span>
          ))}
        </div>

        {facets.map((f, i) => (
          <div key={f.id} className={`${rowGrid} border-t border-current/5 py-2`}>
            <button
              onClick={() => onSelect(f.id, 0)}
              className="flex min-w-0 items-baseline gap-3 text-left transition-opacity hover:opacity-100"
              title={f.title}
            >
              <span className="shrink-0 text-[11px] tabular-nums opacity-40">{String(i + 1).padStart(2, "0")}</span>
              <span className="truncate font-medium">{f.headline || f.title}</span>
            </button>
            {COLS.map((c) => {
              const present = hasCol(f, c.col);
              const isActive = active?.facetId === f.id && active.col === c.col;
              if (!present) {
                return (
                  <span key={c.col} className="flex justify-center" aria-hidden>
                    <span className="h-1 w-1 rounded-full bg-current opacity-15" />
                  </span>
                );
              }
              return (
                <button
                  key={c.col}
                  onClick={() => onSelect(f.id, c.col)}
                  aria-label={`${f.title} · ${c.label}`}
                  title={c.label}
                  className="mx-auto h-5 w-7 rounded transition-all"
                  style={
                    isActive
                      ? { background: accent, opacity: 1 }
                      : { background: accent, opacity: 0.28 }
                  }
                  onMouseEnter={(e) => { if (!isActive) e.currentTarget.style.opacity = "0.55"; }}
                  onMouseLeave={(e) => { if (!isActive) e.currentTarget.style.opacity = "0.28"; }}
                />
              );
            })}
          </div>
        ))}
      </div>
    </div>
  );
}
