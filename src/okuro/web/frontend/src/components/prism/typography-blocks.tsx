// <!-- AGENT_HEADER
// role: code
// purpose: okuro·prism typographic blocks — text-only slide layouts that give a
//   deck real variety instead of the same stacked paragraphs. Statement (one
//   assertion, full page, huge type — "hardcore typography") and Quote (pull-
//   quote + attribution). `display:"standalone"` renders full-bleed as its own
//   slide. Swiss/editorial type scale; dependency-free, theme-aware.
// index: Statement | Quote
// AGENT_HEADER_END -->
import type { Block } from "@/lib/prism-api";

type StatementData = Extract<Block, { type: "statement" }>;
type QuoteData = Extract<Block, { type: "quote" }>;

/** STATEMENT — one sentence, as the whole point. Standalone renders it huge and
 *  centered in the frame (the "hardcore typography" slide); inline is a strong
 *  lead-in. A kicker (eyebrow) frames it; a footnote grounds it. */
export function Statement({ data, accent }: { data: StatementData; accent: string }) {
  const text = (data.text || "").trim();
  if (!text) return null;
  const standalone = data.display === "standalone";
  const align = data.align === "center" || standalone ? "text-center items-center" : "text-left items-start";
  return (
    <div className={`flex flex-col gap-4 ${align} ${standalone ? "min-h-[62vh] justify-center px-4" : ""}`}>
      {data.kicker && (
        <span className="text-xs font-semibold uppercase tracking-[0.25em]" style={{ color: accent }}>
          {data.kicker}
        </span>
      )}
      <p
        className={`font-semibold leading-[1.08] tracking-tight ${
          standalone ? "text-4xl sm:text-5xl md:text-6xl" : "text-2xl sm:text-3xl"
        }`}
        style={{ maxWidth: "22ch" }}
      >
        {text}
      </p>
      {data.footnote && <p className="text-sm opacity-55">{data.footnote}</p>}
    </div>
  );
}

/** QUOTE — a stakeholder's voice as a pull-quote. The oversized opening mark and
 *  the attribution line give it weight; standalone centers it in the frame. */
export function Quote({ data, accent }: { data: QuoteData; accent: string }) {
  const quote = (data.quote || "").trim();
  if (!quote) return null;
  const standalone = data.display === "standalone";
  return (
    <figure className={`relative ${standalone ? "flex min-h-[55vh] flex-col justify-center px-4 text-center" : ""}`}>
      <span
        aria-hidden
        className="pointer-events-none select-none font-serif leading-none opacity-15"
        style={{ color: accent, fontSize: standalone ? "6rem" : "3.5rem" }}
      >
        &ldquo;
      </span>
      <blockquote
        className={`-mt-6 font-medium italic leading-snug ${standalone ? "text-3xl sm:text-4xl md:text-5xl" : "text-xl sm:text-2xl"}`}
        style={{ maxWidth: standalone ? "26ch" : undefined, marginInline: standalone ? "auto" : undefined }}
      >
        {quote}
      </blockquote>
      {(data.attribution || data.role) && (
        <figcaption className="mt-4 text-sm opacity-70">
          {data.attribution && <span className="font-semibold" style={{ color: accent }}>{data.attribution}</span>}
          {data.attribution && data.role && <span className="opacity-50"> · </span>}
          {data.role && <span className="opacity-70">{data.role}</span>}
        </figcaption>
      )}
    </figure>
  );
}
