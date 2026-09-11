import type { Deck, SlideElement } from "./scene";
import { DEFAULT_LINE_HEIGHT, boxDecorCssText, chartSurface, deckSize, hugs, isComposite } from "./scene";
import { flattenElements } from "./flatten";
import { chartSvg } from "./chart-svg";
import { dividerColor, elementBodyHtml } from "./element-body";
import { tokenizeApiSrc } from "@/lib/slides-api";

/** Export helpers — PDF via the browser print pipeline, PPTX via pptxgenjs. */

function esc(s: string): string {
  return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

// **bold** / *italic* → HTML (after escaping).
function inlineHtml(text: string): string {
  return esc(text)
    .replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>")
    .replace(/\*([^*\n]+)\*/g, "<em>$1</em>");
}

function elHtml(el: SlideElement, surface?: string): string {
  const opacity = el.opacity ?? 1;
  const common = `position:absolute;left:${el.x}px;top:${el.y}px;width:${el.w}px;height:${el.h}px;z-index:${el.z ?? 0};opacity:${opacity};`;
  if (el.kind === "image" && el.src) {
    return `<img src="${tokenizeApiSrc(el.src)}" style="${common}object-fit:cover;border-radius:${el.radius ?? 0}px;${boxDecorCssText(el)}"/>`;
  }
  const topAligned = hugs(el.kind);
  const justify = topAligned ? "flex-start" : "center";
  const alignItems = el.align === "center" ? "center" : el.align === "right" ? "flex-end" : "flex-start";
  const tracking = el.letterSpacing ? `letter-spacing:${el.letterSpacing}px;` : "";
  const style =
    common +
    `display:flex;flex-direction:column;align-items:${alignItems};justify-content:${justify};` +
    `background:${el.bg ?? "transparent"};color:${el.color ?? "#eafff0"};font-size:${el.fontSize ?? 28}px;` +
    `font-weight:${el.fontWeight ?? 400};border-radius:${el.radius ?? 0}px;text-align:${el.align ?? "left"};` +
    (el.font ? `font-family:${el.font};` : "") + tracking +
    `line-height:${el.lineHeight ?? DEFAULT_LINE_HEIGHT};white-space:pre-wrap;word-break:break-word;overflow:${topAligned ? "visible" : "hidden"};` +
    boxDecorCssText(el);
  const inner = isComposite(el.kind) ? elementBodyHtml(el, surface) : inlineHtml(el.text ?? "");
  return `<div style="${style}">${inner}</div>`;
}

/** Rasterize an SVG string to a PNG data URL at `scale`× — used to embed a
 *  hand-rolled chart into PPTX (which has no native inline-SVG shape). Browser
 *  Image→canvas, no extra dependency (mirrors the exportPng foreignObject path).
 *  The chart SVG is same-origin data, so the canvas never taints. */
async function svgToPngDataUrl(svg: string, w: number, h: number, scale = 2): Promise<string> {
  const url = `data:image/svg+xml;charset=utf-8,${encodeURIComponent(svg)}`;
  const img = new Image();
  img.decoding = "async";
  await new Promise<void>((resolve, reject) => {
    img.onload = () => resolve();
    img.onerror = () => reject(new Error("chart svg load failed"));
    img.src = url;
  });
  const canvas = document.createElement("canvas");
  canvas.width = Math.max(1, Math.round(w * scale));
  canvas.height = Math.max(1, Math.round(h * scale));
  const ctx = canvas.getContext("2d");
  if (!ctx) throw new Error("no 2d context");
  ctx.scale(scale, scale);
  ctx.drawImage(img, 0, 0, w, h);
  return canvas.toDataURL("image/png");
}

/** The chart SVG with explicit px dimensions (the render form uses width/height
 *  100% so it scales in the box; rasterizing needs a concrete size). */
function chartSvgSized(el: SlideElement, surface?: string): string {
  return chartSvg(el, surface).replace('width="100%" height="100%"', `width="${Math.max(1, el.w)}" height="${Math.max(1, el.h)}"`);
}

/** Open a print window with one full-page slide each; user prints / saves PDF. */
export function exportPdf(deck: Deck): void {
  const { w, h } = deckSize(deck);
  const pages = deck.slides
    .map((s) => {
      const flat = flattenElements(s.elements, deck.font);
      const els = flat
        .map((el) => elHtml(el, el.kind === "chart" ? chartSurface(el, flat, deck.background) : deck.background))
        .join("");
      return `<div class="slide" style="width:${w}px;height:${h}px;background:${deck.background ?? "#0b0f0c"};font-family:${deck.font ?? "Inter,system-ui,sans-serif"};position:relative;overflow:hidden;">${els}</div>`;
    })
    .join("");
  const html = `<!doctype html><html><head><meta charset="utf-8"><title>${esc(deck.title)}</title>
<style>@page{size:${w}px ${h}px;margin:0}html,body{margin:0;padding:0}.slide{page-break-after:always}</style>
</head><body onload="setTimeout(()=>window.print(),300)">${pages}</body></html>`;
  const win = window.open("", "_blank");
  if (!win) return;
  win.document.open();
  win.document.write(html);
  win.document.close();
}

/** Generate + download a .pptx via pptxgenjs. Text + boxes + images (best-effort). */
export async function exportPptx(deck: Deck): Promise<void> {
  const PptxGen = (await import("pptxgenjs")).default;
  const pptx = new PptxGen();
  pptx.defineLayout({ name: "OKURO", width: 13.333, height: 7.5 });
  pptx.layout = "OKURO";
  const { w: CW, h: CH } = deckSize(deck);
  const xin = (px: number) => (px / CW) * 13.333;
  const yin = (px: number) => (px / CH) * 7.5;
  const pt = (px: number) => Math.max(1, Math.round(px * (7.5 * 72) / CH));
  const hex = (c?: string) => (c && c.startsWith("#") ? c.slice(1) : undefined);

  for (const s of deck.slides) {
    const slide = pptx.addSlide();
    slide.background = { color: hex(deck.background) ?? "0B0F0C" };
    const flat = flattenElements(s.elements, deck.font);
    for (const el of flat) {
      try {
        if (el.kind === "image" && el.src) {
          slide.addImage({ path: tokenizeApiSrc(el.src), x: xin(el.x), y: yin(el.y), w: xin(el.w), h: yin(el.h) });
        } else if (el.kind === "chart") {
          // No native inline-SVG shape in PPTX → rasterize the same SVG we render
          // and embed it as an image, so the chart never vanishes on export.
          const data = await svgToPngDataUrl(chartSvgSized(el, chartSurface(el, flat, deck.background)), el.w, el.h);
          slide.addImage({ data, x: xin(el.x), y: yin(el.y), w: xin(el.w), h: yin(el.h) });
        } else if (el.kind === "table" && (el.columns?.length || el.rows?.length)) {
          // A native pptx table (editable in PowerPoint), not an image.
          const cols = Math.max(1, el.columns?.length || el.rows?.[0]?.length || 1);
          const fs = pt(el.fontSize ?? 22);
          const trows: { text: string; options?: Record<string, unknown> }[][] = [];
          if (el.header !== false && el.columns?.length) {
            trows.push(el.columns.map((c) => ({ text: String(c), options: { bold: true, fill: { color: "163A24" }, color: "EAFFF0" } })));
          }
          for (const row of el.rows ?? []) {
            trows.push(Array.from({ length: cols }, (_, i) => ({ text: String(row[i] ?? ""), options: { color: hex(el.color) ?? "EAFFF0" } })));
          }
          if (trows.length) {
            slide.addTable(trows, {
              x: xin(el.x), y: yin(el.y), w: xin(el.w),
              fontSize: fs, fontFace: "Arial", valign: "middle",
              border: { type: "solid", pt: 0.5, color: "2C3A30" },
              autoPage: false,
            });
          }
        } else if (el.kind === "list" && el.items?.length) {
          const runs = el.items.map((it) => ({
            text: it.replace(/\*\*/g, "").replace(/\*/g, ""),
            options: { bullet: el.ordered ? { type: "number" as const } : true, breakLine: true },
          }));
          slide.addText(runs, {
            x: xin(el.x), y: yin(el.y), w: xin(el.w), h: yin(el.h),
            fontSize: pt(el.fontSize ?? 28), color: hex(el.color) ?? "EAFFF0",
            bold: (el.fontWeight ?? 400) >= 600, align: el.align ?? "left",
            valign: "top", fontFace: "Arial", fit: "shrink",
            lineSpacingMultiple: el.lineHeight ?? DEFAULT_LINE_HEIGHT,
          });
        } else if (el.kind === "kpi") {
          const valueSize = el.fontSize ?? 72;
          const subSize = Math.max(14, Math.round(valueSize * 0.28));
          const dmap = { up: { c: "22C55E", a: "▲" }, down: { c: "EF4444", a: "▼" }, flat: { c: "9CA3AF", a: "▬" } } as const;
          const d = dmap[el.deltaDir ?? "flat"];
          const runs = [
            { text: el.value ?? "", options: { fontSize: pt(valueSize), bold: true, color: hex(el.color) ?? "EAFFF0", breakLine: true } },
            ...(el.label ? [{ text: el.label, options: { fontSize: pt(subSize), color: "B8C6BC", breakLine: !el.delta } }] : []),
            ...(el.delta ? [{ text: `${d.a} ${el.delta}`, options: { fontSize: pt(subSize), bold: true, color: d.c, breakLine: true } }] : []),
          ];
          slide.addText(runs, {
            x: xin(el.x), y: yin(el.y), w: xin(el.w), h: yin(el.h),
            align: el.align ?? "left", valign: "top", fontFace: "Arial", fit: "shrink",
          });
        } else if (el.kind === "divider") {
          const t = Math.max(1, el.thickness ?? 2);
          const vertical = el.orientation === "v";
          const rx = vertical ? el.x + el.w / 2 - t / 2 : el.x;
          const ry = vertical ? el.y : el.y + el.h / 2 - t / 2;
          slide.addShape("rect", {
            x: xin(rx), y: yin(ry),
            w: vertical ? xin(t) : xin(el.w), h: vertical ? yin(el.h) : yin(t),
            fill: { color: hex(dividerColor(el)) ?? "39463D" }, line: { type: "none" },
          });
        } else if (el.kind === "quote") {
          const fontSize = el.fontSize ?? 40;
          const attrSize = Math.max(14, Math.round(fontSize * 0.5));
          const runs = [
            { text: (el.text ?? "").replace(/\*\*/g, "").replace(/\*/g, ""), options: { fontSize: pt(fontSize), italic: true, color: hex(el.color) ?? "EAFFF0", breakLine: true } },
            ...(el.attribution ? [{ text: `— ${el.attribution}`, options: { fontSize: pt(attrSize), color: "B8C6BC", breakLine: true } }] : []),
          ];
          slide.addText(runs, {
            x: xin(el.x), y: yin(el.y), w: xin(el.w), h: yin(el.h),
            align: el.align ?? "left", valign: "top", fontFace: "Arial", fit: "shrink",
            lineSpacingMultiple: el.lineHeight ?? DEFAULT_LINE_HEIGHT,
          });
        } else if (el.kind === "text" && el.text) {
          const transparency = Math.round((1 - (el.opacity ?? 1)) * 100);
          slide.addText(el.text.replace(/\*\*/g, "").replace(/\*/g, ""), {
            x: xin(el.x), y: yin(el.y), w: xin(el.w), h: yin(el.h),
            fontSize: pt(el.fontSize ?? 28), color: hex(el.color) ?? "EAFFF0",
            bold: (el.fontWeight ?? 400) >= 600, align: el.align ?? "left",
            valign: "top", fontFace: "Arial", fit: "shrink",
            lineSpacingMultiple: el.lineHeight ?? DEFAULT_LINE_HEIGHT,
            charSpacing: el.letterSpacing ? Math.round((el.letterSpacing / CW) * 13.333 * 72) : undefined,
            ...(transparency > 0 ? { transparency } : {}),
          });
        } else if (el.kind === "video" || el.kind === "flow") {
          // No native embed for a video/iframe in a static pptx → draw a labelled
          // poster rect so the element never silently vanishes on export (#16).
          slide.addShape("rect", {
            x: xin(el.x), y: yin(el.y), w: xin(el.w), h: yin(el.h),
            fill: { color: hex(el.bg) ?? "10231A" },
            line: { color: "2C3A30", width: 1 },
            rectRadius: Math.min(0.2, (el.radius ?? 0) / 100),
          });
          const label = (el.alt || el.text || (el.kind === "video" ? "▶ Video" : "◇ Embed")).trim();
          slide.addText(label, {
            x: xin(el.x), y: yin(el.y), w: xin(el.w), h: yin(el.h),
            align: "center", valign: "middle", fontFace: "Arial", fit: "shrink",
            fontSize: pt(el.fontSize ?? 24), color: "B8C6BC",
          });
        } else {
          const transparency = Math.round((1 - (el.opacity ?? 1)) * 100);
          // Enrichment (best-effort in PPTX): gradient → its first stop as the
          // fill; border → line; shadow preset → outer shadow.
          const fillColor = el.gradient ? hex(el.gradient.from) : hex(el.bg);
          const line = el.border && el.border.width > 0
            ? { color: hex(el.border.color) ?? "000000", width: Math.max(1, Math.round(el.border.width * 0.75)) }
            : { type: "none" as const };
          const sh = { sm: { blur: 3, offset: 1, opacity: 0.3 }, md: { blur: 8, offset: 4, opacity: 0.35 }, lg: { blur: 16, offset: 8, opacity: 0.5 } };
          const shadow = el.shadow && el.shadow !== "none"
            ? { type: "outer" as const, color: "000000", angle: 90, ...sh[el.shadow] }
            : undefined;
          slide.addShape("rect", {
            x: xin(el.x), y: yin(el.y), w: xin(el.w), h: yin(el.h),
            fill: fillColor ? { color: fillColor, ...(transparency > 0 ? { transparency } : {}) } : { type: "none" },
            line, rectRadius: Math.min(0.2, (el.radius ?? 0) / 100),
            ...(shadow ? { shadow } : {}),
          });
        }
      } catch {
        /* skip un-exportable element */
      }
    }
  }
  await pptx.writeFile({ fileName: `${deck.title || "deck"}.pptx` });
}

/** Filesystem-safe slug for download filenames. */
function slug(s: string): string {
  return (s || "deck").trim().toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "") || "deck";
}

/** Split a CSS font-family value into its individual family names, unquoted. */
function familyNames(value?: string): string[] {
  if (!value) return [];
  return value
    .split(",")
    .map((p) => p.trim().replace(/^["']|["']$/g, "").trim())
    .filter(Boolean);
}

/** Every font family a deck actually paints — the deck default plus any
 *  per-element (and frame-child) override. Deduped, order-stable. Used to embed
 *  ONLY the faces the export needs. Pure — safe to unit-test. */
export function deckFontFamilies(deck: Deck): string[] {
  const fams = new Set<string>();
  for (const f of familyNames(deck.font)) fams.add(f);
  for (const s of deck.slides) {
    for (const el of s.elements) {
      for (const f of familyNames(el.font)) fams.add(f);
      for (const c of el.children ?? []) for (const f of familyNames(c.font)) fams.add(f);
    }
  }
  return [...fams];
}

/** Rewrite the FIRST `url(...)` in an @font-face `src` to a data: URI, dropping
 *  any sibling `url()`/`format()` fallbacks (the data URI is self-sufficient).
 *  Pure — the testable core of the embed. */
export function rewriteFontFaceSrc(cssText: string, dataUri: string): string {
  return cssText.replace(/src\s*:\s*[^;]+;/i, `src: url(${dataUri});`);
}

async function fetchAsDataUri(url: string): Promise<string> {
  const res = await fetch(url);
  const blob = await res.blob();
  return await new Promise<string>((resolve, reject) => {
    const fr = new FileReader();
    fr.onload = () => resolve(String(fr.result));
    fr.onerror = () => reject(new Error("font fetch failed"));
    fr.readAsDataURL(blob);
  });
}

// Bounds the worst case (Fontsource splits a variable family into many
// unicode-range subset faces; a per-weight static family adds more). An export
// only needs the faces the visible text uses, but we cannot know the glyphs
// ahead of time, so we embed matched faces up to this cap — enough for the
// latin + latin-ext subsets a deck realistically uses, without a multi-MB SVG.
const MAX_EMBED_FACES = 16;

/**
 * Build an inline `<style>` embedding the WOFF2 bytes (as data-URIs) of the
 * @font-face rules whose family the deck uses, so the PNG `<foreignObject>`
 * renders in the BRAND face instead of a fallback (#15). The foreignObject
 * render context does NOT inherit the document's already-loaded fonts, so the
 * faces must travel inside the SVG.
 *
 * Scans the document's OWN stylesheets (Fontsource self-hosts the WOFF2 under
 * the app origin → fetch never taints), rewrites each matched face's `src` to a
 * data: URI, and concatenates them. Browser-only; returns "" when there is no
 * document, no matching face, or nothing fetchable (graceful fallback to the
 * prior behaviour — a system font in the PNG).
 */
export async function embedFontFaceCss(families: string[]): Promise<string> {
  if (typeof document === "undefined" || families.length === 0) return "";
  const want = new Set(families.map((f) => f.toLowerCase()));
  const faces: string[] = [];
  const seenUrl = new Set<string>();

  const sheets = Array.from(document.styleSheets);
  for (const sheet of sheets) {
    if (faces.length >= MAX_EMBED_FACES) break;
    let rules: CSSRuleList | undefined;
    try {
      rules = sheet.cssRules;
    } catch {
      continue; // cross-origin sheet — cannot read; skip
    }
    for (const rule of Array.from(rules ?? [])) {
      if (faces.length >= MAX_EMBED_FACES) break;
      if (!(rule instanceof CSSFontFaceRule)) continue;
      const fam = rule.style
        .getPropertyValue("font-family")
        .trim()
        .replace(/^["']|["']$/g, "")
        .toLowerCase();
      if (!want.has(fam)) continue;
      const m = /url\(\s*["']?([^"')]+)["']?\s*\)/.exec(rule.style.getPropertyValue("src"));
      // Bind the capture once: with noUncheckedIndexedAccess a match group is
      // string | undefined, and re-indexing m[1] three times re-raised it each
      // time. One guarded const narrows it for all uses.
      const srcUrl = m?.[1];
      if (!srcUrl || seenUrl.has(srcUrl)) continue;
      seenUrl.add(srcUrl);
      try {
        const dataUri = await fetchAsDataUri(new URL(srcUrl, document.baseURI).href);
        faces.push(rewriteFontFaceSrc(rule.cssText, dataUri));
      } catch {
        /* a face we can't fetch → that weight falls back; keep going */
      }
    }
  }
  return faces.length ? `<style>${faces.join("\n")}</style>` : "";
}

/**
 * Export ONE slide to a PNG via the SVG `<foreignObject>` technique — no extra
 * dependency (html-to-image / dom-to-image deliberately avoided):
 *   1. Serialize the flattened slide's element HTML (the SAME `elHtml` used for
 *      PDF, so styling matches) into an `<svg><foreignObject>` at canvas size.
 *   2. Wrap that SVG in a data: URL, draw it onto a `<canvas>`.
 *   3. `canvas.toBlob('image/png')` → download.
 *
 * LIMITATION — taint: external image `href`s drawn through foreignObject taint
 * the canvas on cross-origin/non-CORS responses, making `toBlob` throw a
 * SecurityError. okuro asset images are same-origin (served under /api), so the
 * common case works; if a deck references a truly cross-origin image without
 * CORS, the PNG of that slide will fail and we fall back to a text/box-only
 * render so the export still succeeds. Text + box slides always export.
 */
export async function exportPng(deck: Deck, index: number, scale = 2): Promise<void> {
  const slide = deck.slides[index];
  if (!slide) return;
  const { w, h } = deckSize(deck);
  const bg = deck.background ?? "#0b0f0c";
  const font = deck.font ?? "Inter,system-ui,sans-serif";
  // Embed the brand webfont(s) so the foreignObject renders in the real face,
  // not a fallback (#15). Computed once — identical for both render attempts.
  const fontStyle = await embedFontFaceCss(deckFontFamilies(deck));

  const render = async (withImages: boolean): Promise<Blob> => {
    const flat = flattenElements(slide.elements, deck.font);
    const els = flat
      .filter((el) => withImages || !(el.kind === "image" && el.src))
      .map((el) => elHtml(el, el.kind === "chart" ? chartSurface(el, flat, deck.background) : deck.background))
      .join("");
    const body =
      `<div xmlns="http://www.w3.org/1999/xhtml" style="width:${w}px;height:${h}px;position:relative;` +
      `overflow:hidden;background:${bg};font-family:${font};box-sizing:border-box;">${fontStyle}${els}</div>`;
    const svg =
      `<svg xmlns="http://www.w3.org/2000/svg" width="${w}" height="${h}" viewBox="0 0 ${w} ${h}">` +
      `<foreignObject width="100%" height="100%">${body}</foreignObject></svg>`;
    const url = `data:image/svg+xml;charset=utf-8,${encodeURIComponent(svg)}`;

    const img = new Image();
    img.decoding = "async";
    await new Promise<void>((resolve, reject) => {
      img.onload = () => resolve();
      img.onerror = () => reject(new Error("svg image load failed"));
      img.src = url;
    });

    const canvas = document.createElement("canvas");
    canvas.width = Math.round(w * scale);
    canvas.height = Math.round(h * scale);
    const ctx = canvas.getContext("2d");
    if (!ctx) throw new Error("no 2d context");
    ctx.scale(scale, scale);
    ctx.drawImage(img, 0, 0, w, h);

    return await new Promise<Blob>((resolve, reject) => {
      canvas.toBlob((b) => (b ? resolve(b) : reject(new Error("toBlob failed"))), "image/png");
    });
  };

  let blob: Blob;
  try {
    blob = await render(true);
  } catch {
    // Likely a cross-origin image tainted the canvas — retry without images so
    // text/box slides still export. See LIMITATION above.
    blob = await render(false);
  }

  const href = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = href;
  a.download = `${slug(deck.title)}-${index + 1}.png`;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(href);
}
