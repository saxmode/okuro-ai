import { useQuery } from "@tanstack/react-query";

import { ABOUT_UNKNOWN, fetchAbout } from "@/lib/about-api";

/**
 * /about — what okuro is, plus project/license/security facts. Product
 * copy by design: this page ships in every build, so it describes the
 * project rather than naming whoever happens to run this instance. The
 * INSTANCE owner is still never named here — that is data, and it lives in
 * the bootstrap banner's `identity.author` config key.
 *
 * THE PROJECT'S AUTHOR IS A DIFFERENT FACT AND IT IS NAMED (2026-09-19).
 * Apache-2.0 attribution is the same in every install, so it belongs in
 * product copy — but it is FETCHED, never written here. pyproject's
 * `[project] authors` is its one home; the release owner gate permits the
 * name in LICENSE, NOTICE and pyproject.toml and refuses it everywhere
 * else, so a literal on this page would be both a second home that drifts
 * and a blocking gate finding. See `@/lib/about-api`.
 */
export function AboutPage() {
  // Credit is decoration on a static page: a failed or slow fetch must never
  // hold the prose back, so the query falls back to the credit-less shape and
  // the section below simply does not render. No spinner, no layout shift —
  // the aside's other three blocks are already the page at first paint.
  const { data: about = ABOUT_UNKNOWN } = useQuery({
    queryKey: ["about"],
    queryFn: fetchAbout,
    staleTime: Infinity,
  });

  return (
    <div className="page-shell space-y-10">
      {/* R1 (86b8f1f0) — THE LEAF TITLE IS THE SHELL'S. `TopicBar` renders
          `<h1 class="c-title">About</h1>` above this pane, so the page's own
          PageHeader h1 is gone. The subtitle stays as the block's lead: it
          says what the page is FOR, which the one-word leaf label cannot.

          NO CONTROLS TO RELOCATE — unlike MODELS, this header's `right` slot
          was empty (the leaf has zero actions of its own), so the whole
          `PageHeader` import goes with it.

          `text-fg-muted`, not the `text-tertiary` PageHeader used: that token
          is the standing AA failure (kit todo c581c9b2) and the aside below
          still feeds it, so this line at least does not add to the count. */}
      <p className="type-small text-fg-muted">Who built okuro and why</p>

      {/* R3 (8546865f) — THE WIDTH RULE KEYS TO THE PANE, NOT THE WINDOW.
          `md:` was the literal here, and under the engine's 8px root `md:` is
          48rem = 384px against the WINDOW, so it was true in every state the
          shell can produce — the two-column layout could never stack, whatever
          the pane did. `@2xl:` is the same scale read through `.pane`, which
          `shell.css` declares as a named query container at class scope.

          WHY `@2xl` AND NOT A HIGHER RUNG: zero visual delta today, measured.
          `--container-2xl` is 84rem = 672px at this root, and the six pane
          widths the shell produces are 751.63 · 943.63 · 1003.63 · 1285.63 ·
          1477.63 · 1537.63 — every one above the rung, so the layout renders
          exactly as it does now while the MECHANISM becomes pane-aware. A
          narrower pane (`?embed=1`, a future panel width) now stacks instead
          of squeezing the prose column against the fixed 360px aside. Whether
          751.63 SHOULD stack is a readability call, not a mechanism one, and
          it is carried as a question rather than decided here. Measured grid
          tracks, dark and light identical:

            pane 751.63  ->  345.63px 360px     <- the narrowest prose column
            pane 943.63  ->  537.63px 360px
            pane 1003.63 ->  597.63px 360px
            pane 1285.63 ->  879.63px 360px
            pane 1477.63 -> 1071.63px 360px
            pane 1537.63 -> 1131.63px 360px  */}
      <div className="grid grid-cols-1 gap-10 @2xl:grid-cols-[minmax(0,1fr)_minmax(0,360px)]">
        <section className="space-y-4 text-sm leading-relaxed text-fg-muted">
          <p>
            Okuro is personal AI agent infrastructure — the layer that wires
            CLI-based AI assistants (Claude Code, Codex, Gemini, Cursor) into
            a shared memory, tool, and context stack. It grew out of a
            multi-year effort to make agent-native computing practical on a
            single Linux/macOS workstation instead of hiding behind a SaaS.
          </p>

          <p>
            The premise is that context should be durable and should compound.
            Every session writes back what it learned, so the next one starts
            informed instead of being re-briefed — and that memory is
            provider-agnostic, so switching assistants costs you nothing you
            had already taught the system.
          </p>

          <p>
            It is built for low-clutter, focus-friendly work: structured output
            over prose, options over open questions, and a UI that tries to
            answer before it explains. Those are accessibility choices as much
            as aesthetic ones, and they apply to the agent protocol as much as
            to the screens.
          </p>
        </section>

        <aside className="space-y-6 text-xs text-tertiary">
          <section className="space-y-1.5">
            <h2 className="case-label text-2xs tracking-widest">Project</h2>
            <p>
              Source:{" "}
              <a
                href="https://github.com/saxmode/okuro-ai"
                target="_blank"
                rel="noopener noreferrer"
                className="text-accent hover:text-accent-hover"
              >
                github.com/saxmode/okuro-ai
              </a>
            </p>
          </section>

          {(about.author || about.author_email) && (
            <section className="space-y-1.5">
              <h2 className="case-label text-2xs tracking-widest">Author</h2>
              <p>
                {about.author}
                {about.author && about.author_email ? " — " : null}
                {about.author_email && (
                  <a
                    href={`mailto:${about.author_email}`}
                    className="text-accent hover:text-accent-hover"
                  >
                    {about.author_email}
                  </a>
                )}
              </p>
            </section>
          )}

          <section className="space-y-1.5">
            <h2 className="case-label text-2xs tracking-widest">License</h2>
            <p>
              Released under the Apache License 2.0 — use, modify, and redistribute
              freely with attribution. Full text in{" "}
              <a
                href="https://github.com/saxmode/okuro-ai/blob/main/LICENSE"
                target="_blank"
                rel="noopener noreferrer"
                className="text-accent hover:text-accent-hover"
              >
                LICENSE
              </a>
              .
            </p>
          </section>

          <section className="space-y-1.5">
            <h2 className="case-label text-2xs tracking-widest">Security</h2>
            <p>
              Okuro is a local, single-user tool — no telemetry, no cloud
              sync, no remote server component. Secrets live in the local
              keyring; all data stays in <span className="font-mono">~/.okuro</span>.
              Report vulnerabilities privately via{" "}
              <a
                href="https://github.com/saxmode/okuro-ai/blob/main/SECURITY.md"
                target="_blank"
                rel="noopener noreferrer"
                className="text-accent hover:text-accent-hover"
              >
                SECURITY.md
              </a>
              .
            </p>
          </section>
        </aside>
      </div>
    </div>
  );
}
