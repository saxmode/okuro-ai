import { useDashboardBrain, useDashboardTelemetry } from "@/hooks/use-dashboard";
import { CollapsiblePanel } from "@/components/dashboard/collapsible-panel";
import { SessionsPanel } from "@/components/dashboard/sessions-panel";
import { ProgressPanel } from "@/components/dashboard/progress-panel";
import { ThoughtsPanel } from "@/components/dashboard/thoughts-panel";
import { TelemetryPanel } from "@/components/dashboard/telemetry-panel";

/**
 * Sidebar bottom panels — always-visible brain intelligence.
 * Shows below the PULSE canvas in the collapsible area.
 */
export function SidebarPanels() {
  const { data: brain } = useDashboardBrain();
  const { data: telemetry } = useDashboardTelemetry();

  return (
    <div>
      {/* All panels default closed — users expand what they need instead
          of being greeted by a wall of auto-loaded state. CollapsiblePanel
          persists per-title open state in localStorage, so this default
          only applies on first visit (or after clearing storage). */}
      <CollapsiblePanel title="Sessions" count={brain?.sessions.length} defaultOpen={false}>
        <SessionsPanel sessions={brain?.sessions ?? []} />
      </CollapsiblePanel>

      <CollapsiblePanel title="Telemetry" defaultOpen={false}>
        <TelemetryPanel data={telemetry} />
      </CollapsiblePanel>

      <CollapsiblePanel title="Progress" count={brain?.progress.length} defaultOpen={false}>
        <ProgressPanel entries={brain?.progress ?? []} />
      </CollapsiblePanel>

      <CollapsiblePanel title="Thoughts" count={brain?.thoughts.length} defaultOpen={false}>
        <ThoughtsPanel thoughts={brain?.thoughts ?? []} />
      </CollapsiblePanel>
    </div>
  );
}
