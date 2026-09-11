import type { ReactNode } from "react";

/**
 * Minimal inline rich text for slide text elements: **bold** and *italic*.
 * Newlines are preserved by the element's white-space: pre-wrap. Keeps the IR
 * a plain string (portable to okuro-video) while giving real emphasis.
 */
export function renderRich(text: string | undefined): ReactNode {
  if (!text) return null;
  const parts = text.split(/(\*\*[^*\n]+\*\*|\*[^*\n]+\*)/g);
  return parts.map((p, i) => {
    if (/^\*\*[^*\n]+\*\*$/.test(p)) return <strong key={i}>{p.slice(2, -2)}</strong>;
    if (/^\*[^*\n]+\*$/.test(p)) return <em key={i}>{p.slice(1, -1)}</em>;
    return <span key={i}>{p}</span>;
  });
}
