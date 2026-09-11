// <!-- AGENT_HEADER
// role: code
// purpose: okuro·prism deck v2 — SHADOW-SAFE, self-contained markdown renderer
//   for L3 doc-view prose. Emits standard React elements (h1–h4, p, ul/ol/li,
//   blockquote, pre>code, strong, em, a, code, hr) styled ENTIRELY by the kit
//   `.prose` rules (components.css). No external CDN/library fetch → CSP-safe;
//   no dependency (kept faithful to the "no markdown runtime in the shadow"
//   ethos, now with a tiny in-tree parser). Renders as React nodes, never
//   dangerouslySetInnerHTML, so text is auto-escaped (no XSS). Feature set:
//   headings, ordered/unordered lists, bold/em, inline code, fenced code blocks,
//   links, blockquote, thematic break. Reduced-motion safe (no animation).
// index: MarkdownLite | parseBlocks | renderInline
// AGENT_HEADER_END -->
import type { ReactNode } from "react";

/** Inline-token patterns, matched in priority order (code wins over emphasis so
 *  `**x**` inside a code span stays literal). Each capture group carries the
 *  inner text to recurse into — except code, which is literal. */
const INLINE = [
  { kind: "code", re: /`([^`]+)`/ },
  { kind: "link", re: /\[([^\]]+)\]\(([^)\s]+)\)/ },
  { kind: "bold", re: /\*\*([^*]+?)\*\*|__([^_]+?)__/ },
  { kind: "em", re: /\*([^*]+?)\*|_([^_]+?)_/ },
] as const;

/** Render inline markdown (bold/em/code/links) inside a block as React nodes. */
function renderInline(text: string, keyBase: string): ReactNode[] {
  const out: ReactNode[] = [];
  let rest = text;
  let k = 0;
  // Repeatedly find the EARLIEST-starting inline token across all patterns.
  while (rest.length) {
    let best: { kind: string; idx: number; m: RegExpMatchArray } | null = null;
    for (const { kind, re } of INLINE) {
      const m = rest.match(re);
      if (m && m.index !== undefined && (best === null || m.index < best.idx)) {
        best = { kind, idx: m.index, m };
      }
    }
    if (!best) {
      out.push(rest);
      break;
    }
    if (best.idx > 0) out.push(rest.slice(0, best.idx));
    const key = `${keyBase}-${k++}`;
    const m = best.m;
    if (best.kind === "code") {
      out.push(<code key={key}>{m[1] ?? ""}</code>);
    } else if (best.kind === "link") {
      // Plain <a> — no react-router; external links open in a new tab.
      const href = m[2] ?? "";
      const external = /^https?:\/\//i.test(href);
      out.push(
        <a key={key} href={href} {...(external ? { target: "_blank", rel: "noreferrer" } : {})}>
          {renderInline(m[1] ?? "", key)}
        </a>,
      );
    } else if (best.kind === "bold") {
      out.push(<strong key={key}>{renderInline(m[1] ?? m[2] ?? "", key)}</strong>);
    } else {
      out.push(<em key={key}>{renderInline(m[1] ?? m[2] ?? "", key)}</em>);
    }
    rest = rest.slice(best.idx + m[0].length);
  }
  return out;
}

const H_RE = /^(#{1,6})\s+(.*)$/;
const HR_RE = /^\s*(?:-{3,}|\*{3,}|_{3,})\s*$/;
const UL_RE = /^\s*[-*+]\s+(.*)$/;
const OL_RE = /^\s*\d+[.)]\s+(.*)$/;
const BQ_RE = /^\s*>\s?(.*)$/;
const FENCE_RE = /^\s*```/;

/** Parse a markdown body into block-level React elements. Flat lists only
 *  (deck L3 prose does not nest); tolerant of blank-line spacing. */
function parseBlocks(md: string): ReactNode[] {
  const lines = md.replace(/\r\n?/g, "\n").split("\n");
  // noUncheckedIndexedAccess-safe accessor: every loop below is length-guarded,
  // so a missing index can only be an empty line — coalesce to "".
  const at = (n: number): string => lines[n] ?? "";
  const blocks: ReactNode[] = [];
  let i = 0;
  let b = 0;

  while (i < lines.length) {
    const line = at(i);

    // blank line → skip (block separator)
    if (!line.trim()) {
      i++;
      continue;
    }

    // fenced code block ``` … ```
    if (FENCE_RE.test(line)) {
      i++;
      const code: string[] = [];
      while (i < lines.length && !FENCE_RE.test(at(i))) {
        code.push(at(i));
        i++;
      }
      if (i < lines.length) i++; // consume closing fence
      blocks.push(
        <pre key={`b${b++}`}>
          <code>{code.join("\n")}</code>
        </pre>,
      );
      continue;
    }

    // thematic break
    if (HR_RE.test(line)) {
      blocks.push(<hr key={`b${b++}`} />);
      i++;
      continue;
    }

    // heading (# … ######) — kit styles h1–h4; clamp deeper levels to h4
    const hm = line.match(H_RE);
    if (hm) {
      const level = Math.min((hm[1] ?? "#").length, 4);
      const Tag = `h${level}` as "h1" | "h2" | "h3" | "h4";
      blocks.push(<Tag key={`b${b++}`}>{renderInline(hm[2] ?? "", `b${b}`)}</Tag>);
      i++;
      continue;
    }

    // blockquote (consecutive > lines)
    if (BQ_RE.test(line)) {
      const quoted: string[] = [];
      while (i < lines.length && BQ_RE.test(at(i))) {
        quoted.push(at(i).match(BQ_RE)?.[1] ?? "");
        i++;
      }
      blocks.push(
        <blockquote key={`b${b++}`}>
          <p>{renderInline(quoted.join(" ").trim(), `b${b}`)}</p>
        </blockquote>,
      );
      continue;
    }

    // unordered list
    if (UL_RE.test(line)) {
      const items: string[] = [];
      while (i < lines.length && UL_RE.test(at(i))) {
        items.push(at(i).match(UL_RE)?.[1] ?? "");
        i++;
      }
      blocks.push(
        <ul key={`b${b++}`}>
          {items.map((it, j) => (
            <li key={j}>{renderInline(it, `b${b}-${j}`)}</li>
          ))}
        </ul>,
      );
      continue;
    }

    // ordered list
    if (OL_RE.test(line)) {
      const items: string[] = [];
      while (i < lines.length && OL_RE.test(at(i))) {
        items.push(at(i).match(OL_RE)?.[1] ?? "");
        i++;
      }
      blocks.push(
        <ol key={`b${b++}`}>
          {items.map((it, j) => (
            <li key={j}>{renderInline(it, `b${b}-${j}`)}</li>
          ))}
        </ol>,
      );
      continue;
    }

    // paragraph — gather consecutive plain lines until a blank or a block start
    const para: string[] = [];
    while (
      i < lines.length &&
      at(i).trim() &&
      !FENCE_RE.test(at(i)) &&
      !HR_RE.test(at(i)) &&
      !H_RE.test(at(i)) &&
      !BQ_RE.test(at(i)) &&
      !UL_RE.test(at(i)) &&
      !OL_RE.test(at(i))
    ) {
      para.push(at(i));
      i++;
    }
    blocks.push(<p key={`b${b++}`}>{renderInline(para.join(" ").trim(), `b${b}`)}</p>);
  }

  return blocks;
}

/** Render a markdown body into kit-`.prose`-styled React elements. Drop-in for
 *  the previous plain-paragraph split at L3 (l3-docview.tsx). */
export function MarkdownLite({ md }: { md: string }): ReactNode {
  if (!md?.trim()) return null;
  return <>{parseBlocks(md)}</>;
}
