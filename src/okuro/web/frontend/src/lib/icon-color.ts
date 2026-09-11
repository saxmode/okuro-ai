// <!-- AGENT_HEADER
// role: code
// purpose: Recolor an SVG string to currentColor so icons inherit the design-system fg.
// index: toCurrentColor
// AGENT_HEADER_END -->
// Ported from tm-icon-manager src/shared/svg-color.ts (applyColorOverride), targeting
// `currentColor` so the rendered icon takes the container's CSS color (a design token).
// Paint values that must be preserved (none/transparent/url()/var()) are left alone.

function keepPaint(value: string): boolean {
  const v = value.trim().toLowerCase();
  return (
    v === "none" ||
    v === "transparent" ||
    v.startsWith("url(") ||
    v.startsWith("var(")
  );
}

function rewriteStylePaint(styleValue: string, color: string): string {
  return styleValue
    .split(";")
    .map((entry) => entry.trim())
    .filter(Boolean)
    .map((entry) => {
      const sep = entry.indexOf(":");
      if (sep === -1) return entry;
      const prop = entry.slice(0, sep).trim();
      const val = entry.slice(sep + 1).trim();
      const lower = prop.toLowerCase();
      // `color` matters too: Streamline ships style="color:#000;fill:currentColor"
      // — fill:currentColor then resolves to BLACK. Rewriting color to
      // currentColor makes it inherit the container's design-token fg instead.
      if (lower !== "fill" && lower !== "stroke" && lower !== "color") return entry;
      if (keepPaint(val)) return `${prop}:${val}`;
      return `${prop}:${color}`;
    })
    .join(";");
}

function ensureRootFill(svg: string, color: string): string {
  return svg.replace(/<svg\b([^>]*)>/i, (match, attrs: string) => {
    const hasFill = /\bfill\s*=\s*["'][^"']*["']/i.test(attrs);
    const hasStyleFill = /\bstyle\s*=\s*["'][^"']*\bfill\s*:/i.test(attrs);
    if (hasFill || hasStyleFill) return match;
    if (match.endsWith("/>")) return `${match.slice(0, -2)} fill="${color}"/>`;
    return `${match.slice(0, -1)} fill="${color}">`;
  });
}

// Shape elements whose default fill is black. Streamline icons are filled
// paths with NO fill attribute, so they render black unless we set it. The
// root <svg fill> alone is NOT reliably inherited by children across browsers
// (observed: svg computes fg, child path computes initial black), so we set
// fill on each fill-less shape directly. Elements that already declare a fill
// (e.g. lucide's fill="none") are left untouched.
const SHAPE = /(<(?:path|circle|rect|ellipse|polygon|polyline|line|g)\b)([^>]*?)(\/?>)/gi;

function ensureShapeFills(svg: string, color: string): string {
  return svg.replace(SHAPE, (match, open: string, attrs: string, close: string) => {
    const hasFill = /\bfill\s*=\s*["'][^"']*["']/i.test(attrs);
    const hasStyleFill = /\bstyle\s*=\s*["'][^"']*\bfill\s*:/i.test(attrs);
    if (hasFill || hasStyleFill) return match;
    return `${open}${attrs} fill="${color}"${close}`;
  });
}

/**
 * Rewrite every concrete fill/stroke in an SVG to `currentColor` so the icon
 * renders in the container's CSS color (set to a design token). Returns the
 * original string unchanged when `svg` is empty.
 */
export function toCurrentColor(svg: string | null | undefined): string {
  if (!svg) return "";
  const color = "currentColor";
  let out = svg.replace(/currentColor/g, color); // no-op, keeps intent explicit
  out = out.replace(
    /(fill|stroke|color)\s*=\s*["']([^"']+)["']/gi,
    (_m, attr: string, value: string) =>
      keepPaint(value) ? `${attr}="${value}"` : `${attr}="${color}"`,
  );
  out = out.replace(/style\s*=\s*["']([^"']*)["']/gi, (_m, sv: string) => `style="${rewriteStylePaint(sv, color)}"`);
  // Stroke icons (lucide, tabler-outline, iconoir, phosphor-thin/light/regular)
  // declare fill="none" on the root <svg> and stroke their paths — the paths
  // intentionally have no per-path fill. Injecting fill there fills closed
  // outlines (squares/circles) solid. So skip both fill steps for stroke-based
  // icons; their stroke="currentColor" already inherits the fg.
  if (/<svg\b[^>]*\bfill\s*=\s*["']none["']/i.test(out)) {
    return out;
  }
  out = ensureRootFill(out, color);
  return ensureShapeFills(out, color);
}


/**
 * Combine multiple SVG strings into ONE valid SVG (icons tiled in a grid).
 *
 * A blank-line-joined blob of many <svg> elements is NOT pasteable into Figma
 * (it expects a single SVG). This wraps each icon's inner content in a scaled,
 * translated <g> inside one root <svg>, so Cmd+V in Figma imports it as one
 * vector group (ungroup to get the individual icons). For a single svg, just
 * return it unchanged.
 */
export function combineSvgs(svgs: string[], cell = 24): string {
  const list = svgs.filter(Boolean);
  if (list.length <= 1) return list[0] ?? "";
  const items = list.map((s) => {
    const vb = (s.match(/viewBox\s*=\s*["']([^"']+)["']/i)?.[1] ?? "0 0 24 24")
      .trim().split(/[\s,]+/).map(Number);
    const [minx, miny, vw, vh] = [vb[0] || 0, vb[1] || 0, vb[2] || 24, vb[3] || 24];
    const inner = s.replace(/^[\s\S]*?<svg[^>]*>/i, "").replace(/<\/svg>\s*$/i, "");
    return { minx, miny, vw, vh, inner };
  });
  const cols = Math.ceil(Math.sqrt(items.length));
  const rows = Math.ceil(items.length / cols);
  const body = items.map((it, i) => {
    const c = i % cols, r = Math.floor(i / cols);
    const sx = cell / it.vw, sy = cell / it.vh;
    const tx = c * cell - it.minx * sx, ty = r * cell - it.miny * sy;
    return `<g transform="translate(${tx} ${ty}) scale(${sx} ${sy})">${it.inner}</g>`;
  }).join("");
  return `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ${cols * cell} ${rows * cell}" ` +
    `width="${cols * cell}" height="${rows * cell}">${body}</svg>`;
}
