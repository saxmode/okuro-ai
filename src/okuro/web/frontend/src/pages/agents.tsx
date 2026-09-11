import { Tabs, TabsList, TabsTrigger, TabsContent } from "@/components/ui/tabs";
import { SectionLabel } from "@/components/ui/section-label";
import { ComplianceScorecard } from "@/components/dashboard/compliance-scorecard";
import { ToolsCatalog } from "@/components/tools/tools-catalog";
import { DashboardPage } from "@/pages/dashboard";
import { RolesPage } from "@/pages/roles";
import { useUrlTab } from "@/hooks/use-url-tab";
import { PageHeader } from "@/components/shell/page-header";

/**
 * /agents — umbrella page for everything agent-shaped.
 * Tabs: Live · Roles · Tools.
 *
 * The DEV-only "Flows" tab is gone. It hosted a star-topology role-placement
 * editor (orchestrator + role cards, dnd-kit) that shared nothing with the
 * node-graph editor at /flow beyond the word "flow" — a name collision that
 * cost a session's worth of confusion. Its store (`/api/flows`) STAYS: the task
 * create dialog still reads it as a role-preset picker
 * (components/task/create-dialog.tsx). Those presets are now read-only —
 * nothing authors them any more.
 */
export function AgentsPage() {
  const tab = useUrlTab("live");

  return (
    <div className="page-shell space-y-8">
      <PageHeader
        title="Agents"
        subtitle="Live roster, role catalog, flow composition, tool inventory"
      />

      <Tabs value={tab.value} onValueChange={tab.onValueChange}>
        <TabsList>
          <TabsTrigger value="live">Live</TabsTrigger>
          <TabsTrigger value="roles">Roles</TabsTrigger>
          <TabsTrigger value="tools">Tools</TabsTrigger>
        </TabsList>

        <TabsContent value="live" className="mt-0 space-y-6">
          <SectionLabel className="mb-3 px-0">Running agents + telemetry</SectionLabel>
          <DashboardPage />
          <ComplianceScorecard />
        </TabsContent>

        <TabsContent value="roles" className="mt-0">
          <RolesPage />
        </TabsContent>

        <TabsContent value="tools" className="mt-0">
          <ToolsCatalog />
        </TabsContent>
      </Tabs>
    </div>
  );
}
