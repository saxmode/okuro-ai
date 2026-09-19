import { useDashboardBrain, useDashboardTelemetry } from "@/hooks/use-dashboard";
import { CollapsiblePanel } from "@/components/dashboard/collapsible-panel";
import { SystemPanel } from "@/components/dashboard/system-panel";
import { SessionsPanel } from "@/components/dashboard/sessions-panel";
import { ProgressPanel } from "@/components/dashboard/progress-panel";
import { ThoughtsPanel } from "@/components/dashboard/thoughts-panel";
import { MemoryPanel } from "@/components/dashboard/memory-panel";
import { TelemetryPanel } from "@/components/dashboard/telemetry-panel";

/**
 * THE LIVE VIEW OF WORK/AGENTS — a VIEW, not a page, since the AGENTS pass.
 *
 * It was `DashboardPage` and was mounted inside a tab, which is how AGENTS
 * came to show three headings. A view gives up the two things only a page may
 * have: a `.page-shell` wrapper and a title. It also gives up `p-6` — the
 * shell's container already owns the padding ring, and a page gutter inside
 * the pane is the double-gutter the neutraliser exists to remove (it keys on
 * `.page-shell`, which this file deliberately no longer has).
 *
 * `DashboardPage` stays exported as an alias: `/dashboard` is a legacy
 * address that redirects to `/work/agents`, and three other leaves import
 * pieces of `components/dashboard/`.
 */
export function LiveView() {
  const { data: brain } = useDashboardBrain();
  const { data: telemetry } = useDashboardTelemetry();

  return (
    <div className="space-y-5">
      {/* System */}
      <section>
        <h2 className="mb-2 text-2xs font-medium case-label tracking-wider text-tertiary">
          System
        </h2>
        <div className="rounded border border-border p-3">
          <SystemPanel />
        </div>
      </section>

      {/* Telemetry heatmap */}
      <section>
        <h2 className="mb-2 text-2xs font-medium case-label tracking-wider text-tertiary">
          Telemetry
        </h2>
        <div className="rounded border border-border p-3">
          <TelemetryPanel data={telemetry} />
        </div>
      </section>

      {/* Brain sections — 2 column grid */}
      {/* R3 (8546865f) — TWO COLUMNS DO NOT FIT THE NARROW PANE. `grid-cols-2`
          was unconditional: at the 728.44px pane each column gets ~356px and
          the memory panel's rows need 382, so the view measured 772 in a 728
          pane — a real horizontal overflow of the container, not a truncation.
          Keyed to the pane now: one column at 728, two at 1262. */}
      <div className="grid grid-cols-1 gap-4 @4xl:grid-cols-2">
        {/* Sessions */}
        <section className="rounded border border-border">
          <CollapsiblePanel
            title="Sessions"
            count={brain?.sessions.length}
          >
            <SessionsPanel sessions={brain?.sessions ?? []} />
          </CollapsiblePanel>
        </section>

        {/* Progress */}
        <section className="rounded border border-border">
          <CollapsiblePanel
            title="Progress"
            count={brain?.progress.length}
          >
            <ProgressPanel entries={brain?.progress ?? []} />
          </CollapsiblePanel>
        </section>

        {/* Thoughts */}
        <section className="rounded border border-border">
          <CollapsiblePanel
            title="Thoughts"
            count={brain?.thoughts.length}
          >
            <ThoughtsPanel thoughts={brain?.thoughts ?? []} />
          </CollapsiblePanel>
        </section>

        {/* Memory */}
        <section className="rounded border border-border">
          <CollapsiblePanel
            title="Memory"
            count={brain?.memory.length}
          >
            <MemoryPanel memories={brain?.memory ?? []} />
          </CollapsiblePanel>
        </section>
      </div>

      {/* Projects */}
      {brain?.projects && brain.projects.length > 0 && (
        <section>
          <h2 className="mb-2 text-2xs font-medium case-label tracking-wider text-tertiary">
            Projects
          </h2>
          <div className="grid grid-cols-3 gap-2">
            {brain.projects.map((p) => (
              <div
                key={p.id}
                className="rounded border border-border p-2 text-2xs"
              >
                <span className="text-fg">{p.name}</span>
              </div>
            ))}
          </div>
        </section>
      )}
    </div>
  );
}

/** Legacy name, kept so nothing that imported the page breaks. */
export const DashboardPage = LiveView;
