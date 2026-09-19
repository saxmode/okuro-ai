import { useMemo, useRef, useState } from "react";

import { ComplianceScorecard } from "@/components/dashboard/compliance-scorecard";
import { RolesView } from "@/pages/roles";
import { LiveView } from "@/pages/dashboard";
import { SectionLabel } from "@/components/ui/section-label";
import { Segmented } from "@/components/ui/segmented";
import { ToolsCatalog } from "@/components/tools/tools-catalog";
import type { LeafViewProps } from "@/shell/views/registry";
import { sectionSlugs } from "@/shell/views/sections";
import { useSectionTitle } from "@/shell/components/PageTitle";

/**
 * WORK/AGENTS — everything agent-shaped: the live roster, the role catalogue,
 * the tool inventory.
 *
 * THIS LEAF USED TO MOUNT TWO OTHER PAGES INSIDE TABS, and that is why
 * `?tab=roles` showed THREE headings. Measured at 1366, kit `standard`:
 *
 *   shell .c-title   "Run the work"   y 111   32px
 *   agents.tsx h1    "AGENTS"         y 199   28px
 *   roles.tsx  h1    "ROLES"          y 360   28px
 *
 * y 111 -> 388 spent on three headings, two of them the same rank, before the
 * role list began — and `.page-shell` counted 2, NESTED, on that tab. Ruled
 * (spec Q1, option A): the two inner pages are demoted to VIEWS. They keep
 * their files and their behaviour and give up the two things only a page may
 * have — a `.page-shell` wrapper and a title. R1 then leaves exactly one.
 *
 * `?tab=` IS NOW `?view=`, and the three slugs were already declared in
 * `sections.ts` in the same order, so the rename is the shell reading what the
 * page always meant. `?tab=` keeps working without this file doing anything:
 * `routes.ts:605` reads `params.get("view") ?? params.get("tab")`.
 *
 * AND THE TABS ARE GONE, which is a behaviour fix rather than a swap. Radix
 * `Tabs` UNMOUNTS inactive content — measured as `.page-shell` going 1 -> 2 ->
 * 1 across a tab switch — so every switch away from ROLES threw away its
 * search box, its domain filter and its stale toggle. `ui/segmented` is the
 * sanctioned control and does not unmount, so the views stay mounted and keep
 * their state.
 *
 * The DEV-only "Flows" tab is gone. It hosted a star-topology role-placement
 * editor (orchestrator + role cards, dnd-kit) that shared nothing with the
 * node-graph editor at /flow beyond the word "flow" — a name collision that
 * cost a session's worth of confusion. Its store (`/api/flows`) STAYS: the task
 * create dialog still reads it as a role-preset picker
 * (components/task/create-dialog.tsx). Those presets are now read-only —
 * nothing authors them any more.
 */
const SLUGS = sectionSlugs("work", "agents");
/** Resolved FROM the declared list, never pinned to 0..2: a section inserted
 *  above would otherwise silently select a different view. */
const SLUG_OF = (index: number): string => SLUGS[index] ?? SLUGS[0] ?? "live";
const INDEX_OF = (slug: string): number => Math.max(0, SLUGS.indexOf(slug));

export function AgentsPage({ view: section, onSelectView, id }: Partial<LeafViewProps> = {}) {
  /* Local fallback for every context without the shell — `?embed=1`, a unit
     test — exactly as `pane-active`'s default is `true`. */
  const [localSection, setLocalSection] = useState(0);
  const current = section ?? localSection;
  const slug = SLUG_OF(current);

  const select = (next: string) => {
    const index = INDEX_OF(next);
    if (index === current) return;
    if (onSelectView) onSelectView(index);
    else setLocalSection(index);
  };

  /* MOUNT LAZILY, THEN NEVER UNMOUNT — which is the point of dropping Radix
     Tabs and the cure for its cost in one mechanism.
     · Radix unmounted the inactive view, so leaving ROLES threw away its
       search box, its domain filter and its stale toggle.
     · Mounting all three from the start would instead have the LIVE view
       polling `/api/dashboard/*` while you are reading TOOLS.
     A view therefore appears the first time it is selected and stays. Nothing
     polls a view you have never opened, and nothing loses state on a view you
     have. `pane-active` still gates everything by PANE on top of this. */
  /* MOUNT LAZILY, THEN NEVER UNMOUNT — which is the point of dropping Radix
     Tabs and the cure for its own cost, in one mechanism.
     · Radix UNMOUNTED the inactive view, so leaving ROLES threw away its
       search box, its domain filter and its stale toggle. Measured as
       `.page-shell` going 1 -> 2 -> 1 across a tab switch.
     · Mounting all three from the start would instead leave the LIVE view
       polling `/api/dashboard/*` while you read TOOLS.
     A view therefore appears the first time it is selected and stays. Nothing
     polls a view you never opened; nothing loses state on a view you have.
     `pane-active` still gates every interval by PANE on top of this.

     A REF, not state: this is "remember that I rendered", which must not
     itself cause a render. Mutating it during render is safe because the very
     next line reads it — there is no tear. */
  const seen = useRef<Set<string>>(new Set());
  seen.current.add(slug);

  /* ===================================================================
     THE VIEW SWITCHER IS ON THE PLATE NOW, NOT UNDER IT.
     ===================================================================
     the owner, 2026-09-17: *"the page Titles are not in the glass part. They
     are underneath it. Section titles need to be on top."* This control
     used to be the first interactive row of the pane, which put it below
     the blurred plate — the same defect `pages/tasks.tsx` records for its
     search box, in the leaf next door.

     NO `title` KEY. The shell already derives "Agents" from the IA and R1
     gives it that rank; a leaf overrides the title only to say something
     the shell cannot know, which is what TASKS's *"YOUR RECENT WORK"* is.
     This leaf has nothing to add, so it publishes actions alone and the
     derived title stands.

     MEMOISED HERE BECAUSE THE DEPENDENCIES LIVE HERE — `useSectionTitle`
     keys on the slot's identity and deliberately does not guess at a
     dependency list for state it cannot see. See `PageTitle.tsx`.
     `select` closes over `current` and `onSelectView`, so both are deps
     even though neither appears in the JSX. */
  const header = useMemo(
    () => ({
      actions: (
        <Segmented
          ariaLabel="Agents view"
          value={slug}
          onChange={(v) => select(v as string)}
          options={SLUGS.map((x) => ({
            label: x.charAt(0).toUpperCase() + x.slice(1),
            value: x,
          }))}
        />
      ),
    }),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [slug, current, onSelectView],
  );
  useSectionTitle(header);

  return (
    <div className="page-shell space-y-8">
      {/* R1 (86b8f1f0) — THE LEAF TITLE IS THE SHELL'S. `TopicBar` renders
          `<h1 class="c-title">Agents</h1>` above this pane. The subtitle stays
          as the first content line: it is the only place the screen says what
          the three views together are. */}
      <p className="type-small text-fg-muted">
        Live roster, role catalog, tool inventory.
      </p>

      {seen.current.has("live") && (
        <div hidden={slug !== "live"} className="space-y-6">
          <SectionLabel className="mb-3 px-0">Running agents + telemetry</SectionLabel>
          <LiveView />
          <ComplianceScorecard />
        </div>
      )}

      {seen.current.has("roles") && (
        <div hidden={slug !== "roles"}>
          <RolesView openRoleId={id ?? undefined} />
        </div>
      )}

      {seen.current.has("tools") && (
        <div hidden={slug !== "tools"}>
          <ToolsCatalog />
        </div>
      )}
    </div>
  );
}
