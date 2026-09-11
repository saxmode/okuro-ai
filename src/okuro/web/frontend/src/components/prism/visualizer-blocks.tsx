// <!-- AGENT_HEADER
// role: code
// purpose: okuro·prism visual embed blocks — Gallery (tiled images) and
//   Visualizer (the meta-module that carries ANY okuro visual output: a Studio-
//   generated image, an okuro-assets icon, or a podcast/audio brief). Payloads
//   are baked at attach time (data URIs / inline svg) — no live client fetch, no
//   auth surface. `display:"standalone"` renders full-bleed as its own slide.
// index: Gallery | Visualizer
// AGENT_HEADER_END -->
import type { Block } from "@/lib/prism-api";

type GalleryData = Extract<Block, { type: "gallery" }>;
type VisualizerData = Extract<Block, { type: "visualizer" }>;

/** GALLERY — visual proof by volume: a responsive tile grid of images, each
 *  with an optional caption. `columns` sets the target density. */
export function Gallery({ data }: { data: GalleryData; accent: string }) {
  const images = (data.images ?? []).filter((im) => im && im.src);
  if (!images.length) return null;
  const cols = Math.min(4, Math.max(1, data.columns ?? (images.length >= 3 ? 3 : images.length)));
  return (
    <div className="rounded-lg border border-current/10 p-3">
      <div className="grid gap-2" style={{ gridTemplateColumns: `repeat(${cols}, minmax(0, 1fr))` }}>
        {images.map((im, i) => (
          <figure key={i} className="overflow-hidden rounded-md border border-current/10">
            <img src={im.src} alt={im.caption || ""} className="h-full w-full object-cover" loading="lazy" />
            {im.caption && <figcaption className="px-2 py-1 text-[11px] opacity-60">{im.caption}</figcaption>}
          </figure>
        ))}
      </div>
      {data.caption && <p className="mt-2 text-xs opacity-60">{data.caption}</p>}
    </div>
  );
}

/** VISUALIZER — one module, any okuro visual output. `source` selects the
 *  renderer: studio (a generated image), icon (an okuro-assets glyph, inline
 *  svg or img), or podcast (an audio player + transcript). Graceful: renders
 *  nothing when its payload is missing (a stale/unbaked ref). */
export function Visualizer({ data, accent }: { data: VisualizerData; accent: string }) {
  const standalone = data.display === "standalone";
  const wrap = `rounded-lg border border-current/10 p-3 ${standalone ? "flex min-h-[60vh] flex-col items-center justify-center" : ""}`;

  if (data.source === "podcast") {
    if (!data.src) return null;
    return (
      <div className={wrap}>
        <div className="flex items-center gap-2 text-xs font-semibold uppercase tracking-wide opacity-70">
          <span aria-hidden>🎧</span> Audio brief
          {data.duration ? <span className="font-normal opacity-50">· {Math.round(data.duration)}s</span> : null}
        </div>
        <audio controls src={data.src} className="mt-2 w-full" style={{ accentColor: accent }} />
        {data.transcript && <p className="mt-2 max-h-40 overflow-y-auto text-sm opacity-70">{data.transcript}</p>}
        {data.caption && <p className="mt-2 text-xs opacity-60">{data.caption}</p>}
      </div>
    );
  }

  if (data.source === "illustrator") {
    if (!data.svg) return null;
    // tm-illustrator returns a wide "Architecture Noir" SVG with its own black bg.
    // Contain it in a bordered figure (not full-bleed) so it reads as a noir panel
    // on any theme, and scale the inline svg to the column width.
    return (
      <figure className={`overflow-hidden rounded-lg border border-current/10 bg-black ${standalone ? "flex min-h-[60vh] flex-col items-center justify-center p-4" : "p-2"}`}>
        <div
          className="w-full [&>svg]:h-auto [&>svg]:w-full"
          // trusted inline svg from the internal LAN illustrator service.
          dangerouslySetInnerHTML={{ __html: data.svg }}
        />
        {data.caption && <figcaption className="mt-2 text-center text-xs text-white/60">{data.caption}</figcaption>}
      </figure>
    );
  }

  if (data.source === "icon") {
    const size = standalone ? 160 : 64;
    return (
      <div className={wrap}>
        {data.svg ? (
          <div
            aria-label={data.alt || "icon"}
            style={{ width: size, height: size, color: accent }}
            // okuro-assets returns trusted inline svg markup (internal store).
            dangerouslySetInnerHTML={{ __html: data.svg }}
          />
        ) : data.src ? (
          <img src={data.src} alt={data.alt || "icon"} style={{ width: size, height: size }} />
        ) : null}
        {data.caption && <p className="mt-2 text-xs opacity-60">{data.caption}</p>}
      </div>
    );
  }

  // studio (default) — a generated image baked as a data URI.
  if (!data.src) return null;
  return (
    <figure className={wrap}>
      <img src={data.src} alt={data.alt || data.caption || ""} className={`rounded-md ${standalone ? "max-h-[70vh] w-auto" : "w-full"} object-contain`} loading="lazy" />
      {data.caption && <figcaption className="mt-2 text-xs opacity-60">{data.caption}</figcaption>}
    </figure>
  );
}
