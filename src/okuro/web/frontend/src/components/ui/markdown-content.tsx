import { lazy, Suspense, useMemo, useState } from "react";
import type { Components } from "react-markdown";
import { Check, Copy } from "lucide-react";
import { tokenizeApiSrc } from "@/lib/slides-api";

// Same-origin /api/ images (note attachments, inline drawing PNGs) are behind
// the global bearer, and an <img src> can't send a header — tokenize the URL so
// it authenticates via ?token=. data:/https: srcs pass through unchanged.
const MarkdownImg: Components["img"] = ({ src, alt }) => (
  <img
    src={tokenizeApiSrc(typeof src === "string" ? src : undefined)}
    alt={alt ?? ""}
    loading="lazy"
    className="my-3 max-w-full rounded border border-border"
  />
);

// Heavy react-markdown pipeline lives in its own dynamically-imported chunk
// so it stays off the entry/first-paint path. See markdown-render.tsx.
const MarkdownRender = lazy(() => import("./markdown-render"));

interface MarkdownContentProps {
  children: string;
  className?: string;
  /**
   * "compact" (default) is the dense inline rendering used in sidebars and
   * role popovers. "viewer" is the document-reading variant: larger body
   * text, clearer heading hierarchy, and more generous vertical rhythm.
   * Typography can be additionally scaled via the CSS variable
   * `--md-scale` on any ancestor.
   */
  variant?: "compact" | "viewer";
}

// Extract plain text from React children (strings, arrays, nested elements).
function childrenToText(children: React.ReactNode): string {
  if (children === null || children === undefined) return "";
  if (typeof children === "string") return children;
  if (typeof children === "number") return String(children);
  if (Array.isArray(children)) return children.map(childrenToText).join("");
  if (typeof children === "object" && "props" in (children as object)) {
    return childrenToText(
      (children as { props: { children?: React.ReactNode } }).props.children,
    );
  }
  return "";
}

// A fenced code block with a copy button. Text is always selectable
// (`select-text`) so it works even where an ancestor disables selection, and
// the copy button is the reliable path — clipboard-write needs no selection.
function CodeBlock({
  children,
  className,
}: {
  children: React.ReactNode;
  className: string;
}) {
  const [copied, setCopied] = useState(false);
  const copy = () => {
    void navigator.clipboard
      ?.writeText(childrenToText(children))
      .then(() => {
        setCopied(true);
        setTimeout(() => setCopied(false), 1500);
      })
      .catch(() => {});
  };
  return (
    <div className="group relative my-4">
      <button
        type="button"
        onClick={copy}
        aria-label="Copy code"
        title="Copy code"
        className="absolute right-2 top-2 z-10 inline-flex items-center gap-1 rounded border border-border bg-surface px-1.5 py-1 text-3xs text-fg-subtle opacity-0 transition-opacity hover:text-fg group-hover:opacity-100 focus:opacity-100"
      >
        {copied ? (
          <>
            <Check className="h-3 w-3" aria-hidden="true" /> Copied
          </>
        ) : (
          <>
            <Copy className="h-3 w-3" aria-hidden="true" /> Copy
          </>
        )}
      </button>
      <pre
        className={`${className} select-text`}
        style={{ fontSize: "calc(var(--font-size-sm, 0.875rem) * var(--md-scale, 1))" }}
      >
        {children}
      </pre>
    </div>
  );
}

// Slugify text for anchor ids. Mirrors the common github-style algorithm.
export function slugify(text: string): string {
  return text
    .toLowerCase()
    .trim()
    .replace(/[^\w\s-]/g, "")
    .replace(/\s+/g, "-")
    .replace(/-+/g, "-")
    .replace(/^-|-$/g, "");
}

// Compact variant: existing dense inline rendering used in sidebars, role
// popovers, and anywhere the document is embedded in a chrome-heavy screen.
const compactComponents: Components = {
  h1: ({ children }) => {
    const id = slugify(childrenToText(children));
    return (
      <h1 id={id} className="mt-5 mb-3 type-title text-fg scroll-mt-4">
        {children}
      </h1>
    );
  },
  h2: ({ children }) => {
    const id = slugify(childrenToText(children));
    return (
      <h2 id={id} className="mt-4 mb-2 type-subtitle text-fg scroll-mt-4">
        {children}
      </h2>
    );
  },
  h3: ({ children }) => {
    const id = slugify(childrenToText(children));
    return (
      <h3
        id={id}
        className="mt-3 mb-1.5 text-sm font-semibold uppercase tracking-wider text-fg-muted scroll-mt-4"
      >
        {children}
      </h3>
    );
  },
  h4: ({ children }) => (
    <h4 className="mt-3 mb-1 text-sm font-semibold text-fg-muted">{children}</h4>
  ),
  h5: ({ children }) => (
    <h5 className="mt-2 mb-1 text-xs font-semibold uppercase tracking-wider text-tertiary">
      {children}
    </h5>
  ),
  h6: ({ children }) => (
    <h6 className="mt-2 mb-0.5 text-xs font-semibold text-tertiary">
      {children}
    </h6>
  ),
  p: ({ children }) => (
    <p className="my-2 type-body text-fg-muted">{children}</p>
  ),
  ul: ({ children }) => (
    <ul className="my-2 ml-5 list-disc space-y-1 type-body text-fg-muted">
      {children}
    </ul>
  ),
  ol: ({ children }) => (
    <ol className="my-2 ml-5 list-decimal space-y-1 type-body text-fg-muted">
      {children}
    </ol>
  ),
  li: ({ children }) => <li className="leading-relaxed">{children}</li>,
  strong: ({ children }) => <strong className="font-semibold text-fg">{children}</strong>,
  em: ({ children }) => <em className="italic">{children}</em>,
  a: ({ href, children }) => (
    <a
      href={href}
      target="_blank"
      rel="noopener noreferrer"
      className="text-accent underline-offset-2 hover:underline"
    >
      {children}
    </a>
  ),
  code: ({ className, children, ...rest }) => {
    const isBlock = /language-/.test(className ?? "");
    if (isBlock) {
      return (
        <code className={`${className ?? ""} text-xs`} {...rest}>
          {children}
        </code>
      );
    }
    return (
      <code className="rounded border border-border bg-surface px-1.5 py-0.5 font-mono text-xs text-fg">
        {children}
      </code>
    );
  },
  pre: ({ children }) => (
    <CodeBlock className="overflow-x-auto whitespace-pre-wrap break-words rounded border border-border bg-surface p-3 font-mono text-fg-muted leading-relaxed">
      {children}
    </CodeBlock>
  ),
  blockquote: ({ children }) => (
    <blockquote className="my-3 border-l-2 border-border pl-3 text-sm italic text-tertiary">
      {children}
    </blockquote>
  ),
  hr: () => <hr className="my-4 border-border" />,
  img: MarkdownImg,
  table: ({ children }) => (
    <div className="my-3 overflow-x-auto">
      <table className="w-full border-collapse text-xs">{children}</table>
    </div>
  ),
  thead: ({ children }) => <thead className="border-b border-border">{children}</thead>,
  tbody: ({ children }) => <tbody>{children}</tbody>,
  tr: ({ children }) => <tr className="border-b border-border-subtle">{children}</tr>,
  th: ({ children }) => (
    <th className="px-2 py-1.5 text-left font-semibold text-fg">{children}</th>
  ),
  td: ({ children }) => <td className="px-2 py-1.5 text-fg-muted">{children}</td>,
};

// Viewer variant: document-reading scale. Base body = 1rem, short measure
// and generous vertical rhythm; headings use extreme size contrast so the
// document hierarchy is obvious at a glance (matches the focuser preset).
// All sizes scale with the CSS var `--md-scale` on any ancestor so a
// wrapper can respect font_scale from the user profile.
const viewerComponents: Components = {
  h1: ({ children }) => {
    const id = slugify(childrenToText(children));
    return (
      <h1
        id={id}
        className="mt-8 mb-4 scroll-mt-6 font-bold tracking-tight text-fg"
        style={{ fontSize: "calc(var(--font-size-display, 1.75rem) * var(--md-scale, 1))", lineHeight: 1.2 }}
      >
        {children}
      </h1>
    );
  },
  h2: ({ children }) => {
    const id = slugify(childrenToText(children));
    return (
      <h2
        id={id}
        className="mt-7 mb-3 scroll-mt-6 font-bold tracking-tight text-fg"
        style={{ fontSize: "calc(var(--font-size-2xl, 1.5rem) * var(--md-scale, 1))", lineHeight: 1.25 }}
      >
        {children}
      </h2>
    );
  },
  h3: ({ children }) => {
    const id = slugify(childrenToText(children));
    return (
      <h3
        id={id}
        className="mt-6 mb-2 scroll-mt-6 font-semibold text-fg"
        style={{ fontSize: "calc(var(--font-size-xl, 1.25rem) * var(--md-scale, 1))", lineHeight: 1.3 }}
      >
        {children}
      </h3>
    );
  },
  h4: ({ children }) => (
    <h4
      className="mt-5 mb-2 font-semibold text-fg"
      style={{ fontSize: "calc(var(--font-size-lg, 1.125rem) * var(--md-scale, 1))" }}
    >
      {children}
    </h4>
  ),
  h5: ({ children }) => (
    <h5
      className="mt-4 mb-1.5 font-semibold uppercase tracking-wider text-fg-muted"
      style={{ fontSize: "calc(var(--font-size-sm, 0.875rem) * var(--md-scale, 1))" }}
    >
      {children}
    </h5>
  ),
  h6: ({ children }) => (
    <h6
      className="mt-4 mb-1 font-semibold text-fg-muted"
      style={{ fontSize: "calc(var(--font-size-sm, 0.875rem) * var(--md-scale, 1))" }}
    >
      {children}
    </h6>
  ),
  p: ({ children }) => (
    <p
      className="my-4 text-fg"
      style={{
        fontSize: "calc(var(--font-size-base, 1rem) * var(--md-scale, 1))",
        lineHeight: 1.7,
      }}
    >
      {children}
    </p>
  ),
  ul: ({ children }) => (
    <ul
      className="my-4 ml-6 list-disc space-y-2 text-fg"
      style={{ fontSize: "calc(var(--font-size-base, 1rem) * var(--md-scale, 1))", lineHeight: 1.7 }}
    >
      {children}
    </ul>
  ),
  ol: ({ children }) => (
    <ol
      className="my-4 ml-6 list-decimal space-y-2 text-fg"
      style={{ fontSize: "calc(var(--font-size-base, 1rem) * var(--md-scale, 1))", lineHeight: 1.7 }}
    >
      {children}
    </ol>
  ),
  li: ({ children }) => <li className="leading-relaxed">{children}</li>,
  strong: ({ children }) => <strong className="font-semibold text-fg">{children}</strong>,
  em: ({ children }) => <em className="italic">{children}</em>,
  a: ({ href, children }) => (
    <a
      href={href}
      target="_blank"
      rel="noopener noreferrer"
      className="text-accent underline decoration-accent/40 underline-offset-4 transition-colors hover:decoration-accent"
    >
      {children}
    </a>
  ),
  code: ({ className, children, ...rest }) => {
    const isBlock = /language-/.test(className ?? "");
    if (isBlock) {
      return (
        <code
          className={className}
          style={{ fontSize: "calc(var(--font-size-sm, 0.875rem) * var(--md-scale, 1))" }}
          {...rest}
        >
          {children}
        </code>
      );
    }
    return (
      <code
        className="rounded border border-border bg-surface px-1.5 py-0.5 font-mono text-fg"
        style={{ fontSize: "calc(var(--font-size-sm, 0.875rem) * var(--md-scale, 1))" }}
      >
        {children}
      </code>
    );
  },
  pre: ({ children }) => (
    <CodeBlock className="overflow-x-auto whitespace-pre-wrap break-words rounded-md border border-border bg-surface p-4 font-mono leading-relaxed text-fg">
      {children}
    </CodeBlock>
  ),
  blockquote: ({ children }) => (
    <blockquote
      className="my-5 border-l-4 border-accent/60 pl-4 italic text-fg-muted"
      style={{ fontSize: "calc(var(--font-size-base, 1rem) * var(--md-scale, 1))", lineHeight: 1.7 }}
    >
      {children}
    </blockquote>
  ),
  hr: () => <hr className="my-8 border-border" />,
  img: MarkdownImg,
  table: ({ children }) => (
    <div
      className="my-5 overflow-x-auto"
      style={{ fontSize: "calc(var(--font-size-sm, 0.875rem) * var(--md-scale, 1))" }}
    >
      <table className="w-full border-collapse">{children}</table>
    </div>
  ),
  thead: ({ children }) => <thead className="border-b-2 border-border">{children}</thead>,
  tbody: ({ children }) => <tbody>{children}</tbody>,
  tr: ({ children }) => <tr className="border-b border-border-subtle">{children}</tr>,
  th: ({ children }) => (
    <th className="px-3 py-2 text-left font-semibold text-fg">{children}</th>
  ),
  td: ({ children }) => <td className="px-3 py-2 text-fg-muted">{children}</td>,
};

/**
 * Renders markdown with GitHub-flavored extensions (tables, task lists,
 * strikethrough). Styled to match the app's design tokens.
 *
 * Two variants:
 *   - "compact" (default) — dense inline rendering for embedded contexts.
 *   - "viewer" — document-reading scale used by the artifact overlay.
 *     Respects `--md-scale` on any ancestor so callers can apply the
 *     user profile's font_scale.
 */
export function MarkdownContent({
  children,
  className,
  variant = "compact",
}: MarkdownContentProps) {
  const components = variant === "viewer" ? viewerComponents : compactComponents;
  return (
    <div className={className}>
      <Suspense
        fallback={
          <p className="whitespace-pre-wrap text-sm leading-relaxed text-fg-muted">
            {children}
          </p>
        }
      >
        <MarkdownRender components={components}>{children}</MarkdownRender>
      </Suspense>
    </div>
  );
}

export interface TocEntry {
  level: 1 | 2;
  text: string;
  id: string;
}

/**
 * Extracts h1 + h2 headings from a markdown source, skipping code fences.
 * Returns anchors compatible with the ids `MarkdownContent` assigns.
 */
export function extractToc(markdown: string): TocEntry[] {
  const entries: TocEntry[] = [];
  const lines = markdown.split("\n");
  let inFence = false;
  for (const raw of lines) {
    const line = raw.trim();
    if (line.startsWith("```") || line.startsWith("~~~")) {
      inFence = !inFence;
      continue;
    }
    if (inFence) continue;
    const m = /^(#{1,2})\s+(.+?)\s*#*\s*$/.exec(line);
    if (!m) continue;
    const level = m[1]!.length as 1 | 2;
    const text = m[2]!.trim();
    entries.push({ level, text, id: slugify(text) });
  }
  return entries;
}

interface MarkdownTocProps {
  source: string;
  onNavigate?: (id: string) => void;
  className?: string;
}

/**
 * Compact h1/h2 table-of-contents. Clicks scroll the matching heading into
 * view via `onNavigate` (caller controls the scroll surface) or the default
 * `scrollIntoView` fallback.
 */
export function MarkdownToc({ source, onNavigate, className }: MarkdownTocProps) {
  const entries = useMemo(() => extractToc(source), [source]);

  if (entries.length === 0) return null;

  return (
    <nav aria-label="Contents" className={className}>
      <div className="mb-2 text-2xs font-semibold uppercase tracking-wider text-tertiary">
        Contents
      </div>
      <ul className="space-y-0.5 text-xs">
        {entries.map((e, i) => (
          <li
            key={`${e.id}-${i}`}
            className={e.level === 2 ? "ml-3" : ""}
          >
            <button
              onClick={(ev) => {
                ev.preventDefault();
                if (onNavigate) onNavigate(e.id);
                else document.getElementById(e.id)?.scrollIntoView({
                  behavior: "smooth",
                  block: "start",
                });
              }}
              className={`block w-full truncate rounded px-2 py-1 text-left transition-colors hover:bg-surface ${
                e.level === 1 ? "text-fg" : "text-fg-muted"
              }`}
            >
              {e.text}
            </button>
          </li>
        ))}
      </ul>
    </nav>
  );
}
