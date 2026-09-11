import { useDashboardBrain, useDashboardTelemetry } from "@/hooks/use-dashboard";
import { CollapsiblePanel } from "@/components/dashboard/collapsible-panel";
import { SystemPanel } from "@/components/dashboard/system-panel";
import { SessionsPanel } from "@/components/dashboard/sessions-panel";
import { ProgressPanel } from "@/components/dashboard/progress-panel";
import { ThoughtsPanel } from "@/components/dashboard/thoughts-panel";
import { MemoryPanel } from "@/components/dashboard/memory-panel";
import { TelemetryPanel } from "@/components/dashboard/telemetry-panel";

export function DashboardPage() {
  const { data: brain } = useDashboardBrain();
  const { data: telemetry } = useDashboardTelemetry();

  return (
    <div className="p-6 space-y-5">
      {/* System */}
      <section>
        <h2 className="mb-2 text-2xs font-medium uppercase tracking-wider text-tertiary">
          System
        </h2>
        <div className="rounded border border-border p-3">
          <SystemPanel />
        </div>
      </section>

      {/* Telemetry heatmap */}
      <section>
        <h2 className="mb-2 text-2xs font-medium uppercase tracking-wider text-tertiary">
          Telemetry
        </h2>
        <div className="rounded border border-border p-3">
          <TelemetryPanel data={telemetry} />
        </div>
      </section>

      {/* Brain sections — 2 column grid */}
      <div className="grid grid-cols-2 gap-4">
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
          <h2 className="mb-2 text-2xs font-medium uppercase tracking-wider text-tertiary">
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
