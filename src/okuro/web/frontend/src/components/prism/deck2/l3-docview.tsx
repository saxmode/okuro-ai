// <!-- AGENT_HEADER
// role: code
// purpose: okuro·prism deck v2 — L3 doc-view. The one place the kit abandons
//   scale-to-fit: prose REFLOWS (container queries, 72ch measure, ultra 2-col)
//   with a sticky TOC scrollspy. Mirrors the kit reflow.html markup exactly
//   (.doc-view > .l3-doc > .l3-shell > .l3-toc + .l3-main > .prose). Embedded
//   lower-level modules render as figures (kit .zoom-stage.autobox); cross-topic
//   links jump to another column. Kit CSS is the styling authority.
// AGENT_HEADER_END -->
import { useEffect, useRef, useState } from "react";
import type { DocView } from "./deck-types";
import { SlideView } from "./archetypes";
import { revealApi, zoomStageApi } from "./kit-assets";
import { MarkdownLite } from "./md-lite";

interface L3DocViewProps {
  doc: DocView;
  /** Jump to another topic's L3 (cross-topic link). */
  onNavigateTopic?: (topicId: string) => void;
}

export function L3DocView({ doc, onNavigateTopic }: L3DocViewProps) {
  const mainRef = useRef<HTMLElement>(null);
  const [activeId, setActiveId] = useState<string>(doc.sections[0]?.id ?? "");

  // TOC scrollspy — highlight the section nearest the top of the scroll box.
  useEffect(() => {
    const main = mainRef.current;
    if (!main) return;
    const heads = Array.from(main.querySelectorAll<HTMLElement>("[data-doc-head]"));
    if (!heads.length) return;
    if (typeof IntersectionObserver === "undefined") return;
    const io = new IntersectionObserver(
      (entries) => {
        const visible = entries
          .filter((e) => e.isIntersecting)
          .sort((a, b) => a.boundingClientRect.top - b.boundingClientRect.top);
        if (visible[0]) setActiveId(visible[0].target.id);
      },
      { root: null, rootMargin: "-10% 0px -70% 0px", threshold: 0.01 },
    );
    heads.forEach((h) => io.observe(h));
    return () => io.disconnect();
  }, [doc]);

  // Reveal embedded figures + scale each figure stage to its column width
  // (they rescale with the reflow via a ResizeObserver).
  useEffect(() => {
    const main = mainRef.current;
    if (!main) return;
    revealApi()?.revealAll(main);
    const stages = Array.from(main.querySelectorAll<HTMLElement>(".zoom-stage.autobox"));
    const api = zoomStageApi();
    const applyAll = () => stages.forEach((s) => api?.apply(s));
    applyAll();
    if (typeof ResizeObserver === "undefined") return;
    const ro = new ResizeObserver(applyAll);
    stages.forEach((s) => ro.observe(s));
    return () => ro.disconnect();
  }, [doc]);

  const jump = (id: string) => {
    const main = mainRef.current;
    const el = main?.querySelector<HTMLElement>(`#${CSS.escape(id)}`);
    el?.scrollIntoView({ behavior: "smooth", block: "start" });
    setActiveId(id);
  };

  return (
    <div className="doc-view" data-width={doc.width ?? "full"}>
      <div className="l3-doc">
        <div className="l3-shell">
          <aside className="l3-toc detail-toc">
            <div className="toc-eyebrow">On this page</div>
            <ul>
              {doc.sections.map((s) => (
                <li key={s.id}>
                  <a
                    href={`#${s.id}`}
                    className={`${s.level === 2 ? "toc-h3 " : ""}${activeId === s.id ? "active" : ""}`.trim()}
                    onClick={(e) => {
                      e.preventDefault();
                      jump(s.id);
                    }}
                  >
                    {s.heading}
                  </a>
                </li>
              ))}
            </ul>
          </aside>
          <main className="l3-main" ref={mainRef}>
            <article className="prose">
              {doc.sections.map((s) => (
                <section key={s.id}>
                  {s.level === 2 ? (
                    <h2 id={s.id} data-doc-head>
                      {s.heading}
                    </h2>
                  ) : (
                    <h1 id={s.id} data-doc-head>
                      {s.heading}
                    </h1>
                  )}
                  {s.md ? <MarkdownLite md={s.md} /> : null}
                  {s.figure ? (
                    <figure className="reveal" style={{ margin: "24px 0" }}>
                      <div className="zoom-stage autobox">
                        <div className="zoom-canvas" data-doc-figure>
                          <SlideView slide={s.figure.slide} />
                        </div>
                      </div>
                      {s.figure.caption ? (
                        <figcaption className="tiny muted" style={{ marginTop: 8 }}>
                          {s.figure.caption}
                        </figcaption>
                      ) : null}
                    </figure>
                  ) : null}
                  {s.crossLinks?.length ? (
                    <p className="tiny">
                      {s.crossLinks.map((cl, i) => (
                        <a
                          key={i}
                          href={`#topic-${cl.topicId}`}
                          onClick={(e) => {
                            e.preventDefault();
                            onNavigateTopic?.(cl.topicId);
                          }}
                        >
                          → {cl.label}
                          {i < s.crossLinks!.length - 1 ? " · " : ""}
                        </a>
                      ))}
                    </p>
                  ) : null}
                </section>
              ))}
            </article>
          </main>
        </div>
      </div>
    </div>
  );
}
