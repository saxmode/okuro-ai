import { PageHeader } from "@/components/shell/page-header";

/**
 * /about — what okuro is, plus project/license/security facts. Product
 * copy by design: this page ships in every build, so it describes the
 * project rather than naming whoever happens to run this instance.
 * Authorship credit is data — see the bootstrap banner's
 * `identity.author` config key.
 */
export function AboutPage() {
  return (
    <div className="page-shell space-y-10">
      <PageHeader title="About" subtitle="Who built okuro and why" />

      <div className="grid grid-cols-1 gap-10 md:grid-cols-[minmax(0,1fr)_minmax(0,360px)]">
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
            <h2 className="text-2xs uppercase tracking-widest">Project</h2>
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

          <section className="space-y-1.5">
            <h2 className="text-2xs uppercase tracking-widest">License</h2>
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
            <h2 className="text-2xs uppercase tracking-widest">Security</h2>
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
