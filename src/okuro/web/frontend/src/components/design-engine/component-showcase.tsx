/**
 * THE WHOLE VENDORED SET, FUNCTIONAL, ON THE CANVAS.
 *
 *     "Before we wire anything, i want to see the full okuro ui component set
 *      in the demo page of the engine. functionally."   (the owner, 2026-08-19)
 *
 * WHAT THIS IS AND WHAT `kit-showcase.tsx` IS. That file is ten primitives
 * inside the DOCUMENT scene, where the claim being made is "the adapter reaches
 * okuro's real vocabulary" and the document around them is the subject. This
 * file is the COMPONENTS scene: every one of the 33 files in `components/ui/`,
 * grouped, each one live — a dropdown that opens, a dialog that traps focus, a
 * slider… — so the question "does my brand survive contact with the app" has an
 * answer that is looked at rather than argued.
 *
 * NOTHING HERE IS STYLED. Not one colour, not one radius, not one font size.
 * Every specimen is the component with its default props, and everything it
 * wears arrives through the 149 consumer names the adapter emits inside the
 * ground block this frame resolves to. A single hard-coded colour in this file
 * would let the page show a value the engine never produced, which is the one
 * failure mode a design-system demo cannot survive. Layout — the gaps between
 * specimens — is px and is deliberately NOT a system value: it is the page's own
 * rhythm, the same discipline the frame's seven hand-built blocks keep.
 *
 * EVERY OVERLAY PORTALS INTO THIS FRAME. `Select`, `Dialog`, `Sheet`,
 * `DropdownMenu`, `ContextMenu`, `Tooltip`, `DetailModal`, `SidePanel` and the
 * command palette all take `container`, and all of them stamp `data-ground`
 * through `useFrameStamp`. Without the container an overlay opened inside the
 * canvas mounts on the APP's body, one document up, and renders in whatever the
 * app is wearing — which is exactly the portal defect register rule 14 exists to
 * close, so demonstrating the closure is part of the point. Two of those nine
 * did not have the prop and now do; see `command-palette.tsx` and
 * `side-panel.tsx`.
 *
 * THE STATE IS LOCAL AND NOTHING IS SAVED. A specimen is an example, not a
 * setting: no store, no query, no mutation. `useState` per group and nothing
 * leaves this file.
 *
 * WHAT THE RUNG CAN AND CANNOT MOVE, stated because the showcase is where it
 * becomes visible. Tailwind's `text-*` utilities are BUILD-TIME LITERALS —
 * `@theme inline` pastes the value instead of publishing the variable — so the
 * shipped bundle contains `.text-sm{font-size:1.75rem}` and no runtime property
 * can resize a `Button`. What does follow the rung is the app's own `.type-*`
 * roles, which compile to `font-size: var(--type-body-size, …)`. Each group
 * therefore carries one `[data-rung-sample]` caption in `.type-body`, and that
 * caption is the honest answer to "does this scene obey the rung": it does, for
 * everything the app binds at runtime, and the gate asserts exactly that rather
 * than a claim the bundle cannot support.
 */

import * as React from "react";
import { Inbox, Plus, Settings, Trash2 } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import {
  Button,
  BUTTON_SIZES,
  BUTTON_VARIANTS,
} from "@/components/ui/button";
import {
  Card,
  CardAction,
  CardContent,
  CardDescription,
  CardFooter,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { CommandPalette } from "@/components/ui/command-palette";
import {
  ContextMenu,
  ContextMenuCheckboxItem,
  ContextMenuContent,
  ContextMenuItem,
  ContextMenuLabel,
  ContextMenuRadioGroup,
  ContextMenuRadioItem,
  ContextMenuSeparator,
  ContextMenuShortcut,
  ContextMenuTrigger,
} from "@/components/ui/context-menu";
import {
  DetailField,
  DetailFields,
  DetailModal,
  DetailProse,
  DetailSection,
} from "@/components/ui/detail-modal";
import {
  Dialog,
  DialogClose,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog";
import {
  DropdownMenu,
  DropdownMenuCheckboxItem,
  DropdownMenuContent,
  DropdownMenuGroup,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuShortcut,
  DropdownMenuSub,
  DropdownMenuSubContent,
  DropdownMenuSubTrigger,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { EmptyState } from "@/components/ui/empty-state";
import { FacetBar } from "@/components/ui/facet-bar";
import {
  FieldLabel,
  StringListEditor,
  SubsectionHeading,
} from "@/components/ui/form-primitives";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { LoadingSkeleton } from "@/components/ui/loading-skeleton";
import { MarkdownContent } from "@/components/ui/markdown-content";
import MarkdownRender from "@/components/ui/markdown-render";
import { MetricCard } from "@/components/ui/metric-card";
import { Progress } from "@/components/ui/progress";
import { Row } from "@/components/ui/row";
import { ScrollArea } from "@/components/ui/scroll-area";
import { SectionLabel } from "@/components/ui/section-label";
import { Segmented } from "@/components/ui/segmented";
import {
  Select,
  SelectContent,
  SelectGroup,
  SelectItem,
  SelectLabel,
  SelectSeparator,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Separator } from "@/components/ui/separator";
import {
  Sheet,
  SheetClose,
  SheetContent,
  SheetDescription,
  SheetFooter,
  SheetHeader,
  SheetTitle,
  SheetTrigger,
} from "@/components/ui/sheet";
import { MobilePanelTrigger, SidePanel } from "@/components/ui/side-panel";
import { StatusBadge } from "@/components/ui/status-badge";
import { Switch } from "@/components/ui/switch";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Textarea } from "@/components/ui/textarea";
import {
  Timeline,
  TimelineBadge,
  TimelineBadges,
  TimelineBody,
  TimelineEntry,
  TimelineTitle,
} from "@/components/ui/timeline";
import { TOAST_CLASSNAMES, TOAST_TONES, toast } from "@/components/ui/toast";
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip";

/* --------------------------------------------------------------- the census

   THE INVENTORY IS DATA, so the gate counts the vendored directory against THIS
   and not against a hand-kept list in a test file. A new file in
   `components/ui/` that nobody adds here fails `test_showcase.py`, which is the
   only way "the complete set" stays true after today.

   `file` is the filename without its extension; `group` is the section it
   renders in. Order inside a group is the order the specimens appear. */

export type ShowcaseGroup =
  | "forms"
  | "overlays"
  | "navigation"
  | "feedback"
  | "data";

export const GROUP_LABELS: { id: ShowcaseGroup; label: string; caption: string }[] = [
  {
    id: "forms",
    label: "Forms and input",
    caption:
      "Everything that takes a value. These carry the brand's border weights, its radius ladder and its focus ring — the three places a system is judged fastest.",
  },
  {
    id: "overlays",
    label: "Overlays",
    caption:
      "Everything that leaves the tree. Each one portals into this frame and carries its ground with it, so an overlay opened on a dark panel stays dark instead of falling back to the page root.",
  },
  {
    id: "navigation",
    label: "Navigation and structure",
    caption:
      "The parts that divide a page rather than fill it. Separators and rails are rungs of the alternate ladder, so they move with the ground and never with a hard-coded grey.",
  },
  {
    id: "feedback",
    label: "Feedback and status",
    caption:
      "The four authored signals, wearing the ink the threshold picked for them. A signal is a ground like any other; nothing here asks what kind of colour it is.",
  },
  {
    id: "data",
    label: "Data display",
    caption:
      "Surfaces that hold content. Elevation is a shadow on light grounds and a border on dark ones — one rule, two outcomes, and the ground decides which.",
  },
];

const GROUP_CONTRACTS: Record<
  ShowcaseGroup,
  { inherits: string; owns: string; proof: string }
> = {
  forms: {
    inherits: "border · focus · radius · component rung",
    owns: "value · validation · disabled state",
    proof: "focus, invalid and selection states",
  },
  overlays: {
    inherits: "ground · foreground · effects · motion",
    owns: "focus trap · dismissal · placement",
    proof: "every portal remains inside this ground",
  },
  navigation: {
    inherits: "alternate ladder · borders · radius",
    owns: "current item · orientation · overflow",
    proof: "selected, hover and scroll states",
  },
  feedback: {
    inherits: "signals · foreground · motion",
    owns: "semantic tone · progress · timing",
    proof: "all status roles on the active ground",
  },
  data: {
    inherits: "surface · shadow/border · type roles",
    owns: "content structure · density · loading",
    proof: "mixed content and elevation outcomes",
  },
};

/** Every file in `components/ui/`, and the group it is shown in. */
export const SHOWCASE_INVENTORY: { file: string; group: ShowcaseGroup }[] = [
  // forms
  { file: "button", group: "forms" },
  { file: "input", group: "forms" },
  { file: "textarea", group: "forms" },
  { file: "label", group: "forms" },
  { file: "select", group: "forms" },
  { file: "switch", group: "forms" },
  { file: "segmented", group: "forms" },
  { file: "form-primitives", group: "forms" },
  { file: "facet-bar", group: "forms" },
  // overlays
  { file: "dialog", group: "overlays" },
  { file: "sheet", group: "overlays" },
  { file: "side-panel", group: "overlays" },
  { file: "detail-modal", group: "overlays" },
  { file: "dropdown-menu", group: "overlays" },
  { file: "context-menu", group: "overlays" },
  { file: "tooltip", group: "overlays" },
  { file: "command-palette", group: "overlays" },
  // navigation
  { file: "tabs", group: "navigation" },
  { file: "scroll-area", group: "navigation" },
  { file: "separator", group: "navigation" },
  { file: "section-label", group: "navigation" },
  { file: "row", group: "navigation" },
  // feedback
  { file: "toast", group: "feedback" },
  { file: "progress", group: "feedback" },
  { file: "badge", group: "feedback" },
  { file: "status-badge", group: "feedback" },
  { file: "empty-state", group: "feedback" },
  { file: "loading-skeleton", group: "feedback" },
  { file: "timeline", group: "feedback" },
  // data
  { file: "card", group: "data" },
  { file: "metric-card", group: "data" },
  { file: "markdown-content", group: "data" },
  { file: "markdown-render", group: "data" },
];

/* ------------------------------------------------------------- the scaffold */

/**
 * One specimen: a name, a live component, and nothing between them.
 *
 * `data-specimen` is what the gate locates, and it is the FILENAME rather than a
 * display name so that "every file is shown" is a set comparison against the
 * directory listing instead of a judgement call.
 */
function Specimen({
  file,
  hint,
  children,
}: {
  file: string;
  hint?: string;
  children: React.ReactNode;
}) {
  const wide = new Set([
    "button",
    "form-primitives",
    "facet-bar",
    "dialog",
    "sheet",
    "side-panel",
    "detail-modal",
    "command-palette",
    "timeline",
    "card",
    "markdown-content",
    "markdown-render",
  ]).has(file);

  return (
    <article
      data-specimen={file}
      data-specimen-width={wide ? "wide" : "regular"}
      className="ds-transparent ds-bordered ds-radius-m ds-padding"
      data-slot="transparent"
      data-render="transparent"
      style={{
        display: "grid",
        alignContent: "start",
        gap: 20,
        minHeight: wide ? 0 : 176,
        gridColumn: wide ? "1 / -1" : undefined,
        borderStyle: "solid",
      }}
    >
      <div style={{ display: "grid", gap: 4 }}>
        <span
          className="ds-n8 ds-leads"
          data-specimen-name
          data-slot="foreground"
          data-render="foreground"
          style={{ fontFamily: "var(--font-mono)", letterSpacing: "0.04em" }}
        >
          {file}
        </span>
        {hint && (
          <span
            className="ds-n8"
            data-slot="foreground"
            data-render="foreground"
            style={{ opacity: 0.66, maxWidth: "62ch" }}
          >
            {hint}
          </span>
        )}
      </div>
      <div
        data-specimen-stage
        style={{ display: "flex", flexWrap: "wrap", gap: 12, alignItems: "flex-start" }}
      >
        {children}
      </div>
    </article>
  );
}

/**
 * One group: heading, caption, a rung sample, and its specimens.
 *
 * 48px between groups and 24px inside one — the 2:1 the layout research
 * measured across Geist, Radix Themes and Spectrum, and the ratio the page's own
 * chrome contract already keeps. It is a gap rather than a margin so no
 * neighbour can collapse it away.
 */
function Group({
  id,
  children,
}: {
  id: ShowcaseGroup;
  children: React.ReactNode;
}) {
  const meta = GROUP_LABELS.find((g) => g.id === id);
  const contract = GROUP_CONTRACTS[id];
  return (
    <section
      data-showcase-group={id}
      style={{
        display: "grid",
        gridTemplateColumns: "minmax(240px, 0.62fr) minmax(0, 1.38fr)",
        gap: 48,
        padding: "72px 0",
        borderTop: "1px solid color-mix(in srgb, currentColor 16%, transparent)",
      }}
    >
      <div style={{ display: "grid", alignContent: "start", gap: 12 }}>
        <span
          className="ds-n8 ds-leads"
          data-slot="foreground"
          data-render="foreground"
          style={{ opacity: 0.5, letterSpacing: "0.1em" }}
        >
          {String(GROUP_LABELS.findIndex((g) => g.id === id) + 1).padStart(2, "0")} / 05
        </span>
        <h3
          className="ds-h3 ds-headings"
          data-slot="foreground"
          data-render="foreground"
          style={{ margin: 0 }}
        >
          {meta?.label ?? id}
        </h3>
        <p
          className="ds-n5 ds-paragraphs"
          data-slot="foreground"
          data-render="foreground"
          style={{ margin: 0, maxWidth: "44ch" }}
        >
          {meta?.caption}
        </p>
        <dl className="showcase-contract">
          <div><dt>Inherits</dt><dd>{contract.inherits}</dd></div>
          <div><dt>Owns</dt><dd>{contract.owns}</dd></div>
          <div><dt>Proof here</dt><dd>{contract.proof}</dd></div>
        </dl>
        {/* THE ONE THING THE RUNG CAN ACTUALLY MOVE. `.type-body` compiles to
            `font-size: var(--type-body-size, …)`, read at the use site, so it
            follows `[data-rung]` on the frame's root. The primitives beside it
            cannot — see the file header. */}
        <p
          className="type-body"
          data-rung-sample={id}
          data-slot="foreground"
          data-render="foreground"
          style={{ margin: 0, opacity: 0.7 }}
        >
          This line is the app's own body role, so it re-sizes with the rung.
        </p>
      </div>
      <div
        data-showcase-specimens
        style={{
          display: "grid",
          gridTemplateColumns: "repeat(2, minmax(0, 1fr))",
          gap: 16,
          alignContent: "start",
        }}
      >
        {children}
      </div>
    </section>
  );
}

/* ------------------------------------------------------------------ groups */

function FormsGroup() {
  const [name, setName] = React.useState("meridian");
  const [notes, setNotes] = React.useState(
    "One colour and one typeface is the whole authored input.",
  );
  const [rung, setRung] = React.useState("L");
  const [twoColours, setTwoColours] = React.useState(true);
  const [appearance, setAppearance] = React.useState("light");
  const [aliases, setAliases] = React.useState<string[]>(["meridian", "mrd"]);
  const [facets, setFacets] = React.useState<Partial<Record<"kind" | "state", string>>>({});
  const container = useFrameBody();

  /* THE VALIDATION IS REAL, and it is the component's own: `aria-invalid` is
     what `input.tsx` styles off, so an empty name paints the error ring the
     engine emitted rather than one this file drew. */
  const nameInvalid = name.trim() === "";

  return (
    <Group id="forms">
      <Specimen file="button" hint="five variants, four sizes">
        <Button>Grow a system</Button>
        <Button variant="secondary">Secondary</Button>
        <Button variant="outline">Outline</Button>
        <Button variant="ghost">Ghost</Button>
        <Button variant="destructive">Destructive</Button>
        <Button size="sm">Small</Button>
        <Button size="lg">Large</Button>
        <Button size="icon" aria-label="settings">
          <Settings />
        </Button>
        <Button disabled>Disabled</Button>
      </Specimen>

      <Specimen file="label" hint="paired with every field below">
        <div style={{ display: "grid", gap: 6, minWidth: 240 }}>
          <Label htmlFor="cs-name">Brand name</Label>
          <Input
            id="cs-name"
            value={name}
            aria-invalid={nameInvalid}
            onChange={(e) => setName(e.target.value)}
          />
          {nameInvalid && (
            <span
              role="alert"
              data-field-error
              className="ds-n8"
              style={{ color: "var(--color-status-error)" }}
            >
              A brand needs a name.
            </span>
          )}
        </div>
      </Specimen>

      <Specimen file="input" hint="default, disabled, and the error ring above">
        <Input placeholder="Search kits…" style={{ maxWidth: 240 }} />
        <Input disabled defaultValue="locked" style={{ maxWidth: 160 }} />
      </Specimen>

      <Specimen file="textarea">
        <Textarea
          value={notes}
          onChange={(e) => setNotes(e.target.value)}
          rows={3}
          style={{ maxWidth: 420 }}
        />
      </Specimen>

      <Specimen file="select" hint="the overlay opens inside this frame">
        <Select value={rung} onValueChange={setRung}>
          <SelectTrigger aria-label="default rung" style={{ minWidth: 180 }}>
            <SelectValue />
          </SelectTrigger>
          <SelectContent container={container}>
            <SelectGroup>
              <SelectLabel>Larger than default</SelectLabel>
              <SelectItem value="XXL">XXL</SelectItem>
              <SelectItem value="XL">XL</SelectItem>
            </SelectGroup>
            <SelectSeparator />
            <SelectGroup>
              <SelectLabel>Default and below</SelectLabel>
              <SelectItem value="L">L</SelectItem>
              <SelectItem value="M">M</SelectItem>
              <SelectItem value="S">S</SelectItem>
            </SelectGroup>
          </SelectContent>
        </Select>
      </Specimen>

      <Specimen file="switch" hint="two sizes">
        <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
          <Switch
            id="cs-two"
            checked={twoColours}
            onCheckedChange={setTwoColours}
          />
          <Label htmlFor="cs-two">Two independent brand colours</Label>
        </div>
        <Switch size="sm" aria-label="small switch" defaultChecked />
      </Specimen>

      <Specimen file="segmented" hint="pills and ghost">
        <Segmented
          ariaLabel="showcase appearance"
          value={appearance}
          onChange={setAppearance}
          options={[
            { value: "light", label: "Light" },
            { value: "dark", label: "Dark" },
          ]}
        />
        <Segmented
          ariaLabel="showcase density"
          variant="ghost"
          value={appearance}
          onChange={setAppearance}
          options={[
            { value: "light", label: "Roomy", count: 2 },
            { value: "dark", label: "Dense", count: 9 },
          ]}
        />
      </Specimen>

      <Specimen file="form-primitives" hint="FieldLabel · SubsectionHeading · StringListEditor">
        <div style={{ display: "grid", gap: 12, minWidth: 320, maxWidth: 420 }}>
          <SubsectionHeading title="Aliases" hint="what else this brand is called" />
          <FieldLabel>Known as</FieldLabel>
          <StringListEditor
            value={aliases}
            onChange={setAliases}
            placeholder="add an alias"
          />
        </div>
      </Specimen>

      <Specimen file="facet-bar" hint="chips derived from the data, not authored">
        <FacetBar
          items={[
            { kind: "neutral", state: "ready" },
            { kind: "signal", state: "flagged" },
            { kind: "brand", state: "ready" },
          ]}
          groups={[
            { label: "Kind", field: "kind" },
            { label: "State", field: "state" },
          ]}
          active={facets}
          onChange={setFacets}
        />
      </Specimen>
    </Group>
  );
}

function OverlaysGroup() {
  const container = useFrameBody();
  const [detailOpen, setDetailOpen] = React.useState(false);
  const [sidePanelOpen, setSidePanelOpen] = React.useState(false);
  const [paletteOpen, setPaletteOpen] = React.useState(false);
  const [bookmarked, setBookmarked] = React.useState(true);
  const [sort, setSort] = React.useState("recent");

  return (
    <Group id="overlays">
      <Specimen file="dialog" hint="focus trap, Escape, click-outside">
        <Dialog>
          <DialogTrigger asChild>
            <Button variant="outline">Open a dialog</Button>
          </DialogTrigger>
          <DialogContent container={container}>
            <DialogHeader>
              <DialogTitle>Retire this brand?</DialogTitle>
              <DialogDescription>
                Nothing is deleted. The kit stays readable and stops being
                offered as a host.
              </DialogDescription>
            </DialogHeader>
            <DialogFooter>
              <DialogClose asChild>
                <Button variant="ghost">Cancel</Button>
              </DialogClose>
              <DialogClose asChild>
                <Button variant="destructive">Retire</Button>
              </DialogClose>
            </DialogFooter>
          </DialogContent>
        </Dialog>
      </Specimen>

      <Specimen file="sheet" hint="four sides; this one slides from the right">
        <Sheet>
          <SheetTrigger asChild>
            <Button variant="outline">Open a sheet</Button>
          </SheetTrigger>
          <SheetContent container={container} side="right">
            <SheetHeader>
              <SheetTitle>Authored input</SheetTitle>
              <SheetDescription>
                Signals, neutrals, the brand's three slots, the type stack and
                the ladders. Everything else computes.
              </SheetDescription>
            </SheetHeader>
            <div style={{ padding: 16 }}>
              <p className="ds-n5 ds-paragraphs" data-slot="foreground" data-render="foreground">
                A sheet is a dialog that keeps one edge. It carries the ground it
                was opened from, so this panel is painted by the same block as
                the canvas behind it.
              </p>
            </div>
            <SheetFooter>
              <SheetClose asChild>
                <Button variant="ghost">Close</Button>
              </SheetClose>
            </SheetFooter>
          </SheetContent>
        </Sheet>
      </Specimen>

      <Specimen
        file="side-panel"
        hint="an inline aside above 768px, a sheet below it"
      >
        <SidePanel
          title="Kit inspector"
          container={container}
          open={sidePanelOpen}
          onOpenChange={setSidePanelOpen}
          desktopClassName="w-[240px] shrink-0 rounded-md border border-border p-3"
        >
          <SectionLabel as="h4" size="sm">
            Reachable grounds
          </SectionLabel>
          <p className="ds-n6 ds-paragraphs" data-slot="foreground" data-render="foreground">
            Nine, once the threshold moved back to 0.675.
          </p>
        </SidePanel>
        <MobilePanelTrigger
          label="Open panel"
          onClick={() => setSidePanelOpen(true)}
        />
      </Specimen>

      <Specimen file="detail-modal" hint="the reader-scale variant of dialog">
        <Button variant="outline" onClick={() => setDetailOpen(true)}>
          Open a detail modal
        </Button>
        <DetailModal
          open={detailOpen}
          onOpenChange={setDetailOpen}
          container={container}
          eyebrow="GROUND"
          title="The brand's own white"
          subtitle="L 1.000 · light · nine values read it"
        >
          <DetailFields>
            <DetailField label="Polarity">light</DetailField>
            <DetailField label="Foreground" mono>
              #030303
            </DetailField>
            <DetailField label="Rule">2</DetailField>
          </DetailFields>
          <DetailSection title="Why">
            <DetailProse>
              {"OKLab **L 1.000** is above the threshold, so this ground is light and takes the brand's own black."}
            </DetailProse>
          </DetailSection>
        </DetailModal>
      </Specimen>

      <Specimen file="dropdown-menu" hint="items, checkboxes, a submenu">
        <DropdownMenu>
          <DropdownMenuTrigger asChild>
            <Button variant="outline">Actions</Button>
          </DropdownMenuTrigger>
          <DropdownMenuContent container={container}>
            <DropdownMenuLabel>This kit</DropdownMenuLabel>
            <DropdownMenuGroup>
              <DropdownMenuItem>
                Duplicate
                <DropdownMenuShortcut>⌘D</DropdownMenuShortcut>
              </DropdownMenuItem>
              <DropdownMenuCheckboxItem
                checked={bookmarked}
                onCheckedChange={setBookmarked}
              >
                Pin to the rail
              </DropdownMenuCheckboxItem>
            </DropdownMenuGroup>
            <DropdownMenuSeparator />
            <DropdownMenuSub>
              <DropdownMenuSubTrigger>Export as</DropdownMenuSubTrigger>
              {/* NO `container` HERE, and its absence is correct rather than an
                  omission. `DropdownMenuSubContent` is not wrapped in a Portal:
                  it renders inside the parent Content, which IS portalled with
                  the container above — so the submenu is already in this frame
                  and already on the ground the parent stamped. Passing one would
                  be a second, contradicting mount point. */}
              <DropdownMenuSubContent>
                <DropdownMenuItem>CSS</DropdownMenuItem>
                <DropdownMenuItem>JSON</DropdownMenuItem>
              </DropdownMenuSubContent>
            </DropdownMenuSub>
            <DropdownMenuSeparator />
            <DropdownMenuItem variant="destructive">
              Retire
              <DropdownMenuShortcut>⌫</DropdownMenuShortcut>
            </DropdownMenuItem>
          </DropdownMenuContent>
        </DropdownMenu>
      </Specimen>

      <Specimen file="context-menu" hint="right-click the target">
        <ContextMenu>
          <ContextMenuTrigger asChild>
            <div
              data-context-target
              className="ds-transparent ds-bordered ds-radius-m ds-padding ds-n6"
              data-slot="transparent"
              data-render="transparent"
              style={{ minWidth: 220, textAlign: "center", cursor: "context-menu" }}
            >
              Right-click me
            </div>
          </ContextMenuTrigger>
          <ContextMenuContent container={container}>
            <ContextMenuLabel>Sort the ladder</ContextMenuLabel>
            <ContextMenuRadioGroup value={sort} onValueChange={setSort}>
              <ContextMenuRadioItem value="recent">By rung</ContextMenuRadioItem>
              <ContextMenuRadioItem value="name">By name</ContextMenuRadioItem>
            </ContextMenuRadioGroup>
            <ContextMenuSeparator />
            <ContextMenuCheckboxItem checked>Show flags</ContextMenuCheckboxItem>
            <ContextMenuItem>
              Copy value
              <ContextMenuShortcut>⌘C</ContextMenuShortcut>
            </ContextMenuItem>
          </ContextMenuContent>
        </ContextMenu>
      </Specimen>

      <Specimen file="tooltip" hint="states a constraint, never the label again">
        <Tooltip>
          <TooltipTrigger asChild>
            <Button variant="outline">Why 0.675?</Button>
          </TooltipTrigger>
          <TooltipContent container={container}>
            It is the fit of a 242-trial contrast study, not a round number
            someone liked.
          </TooltipContent>
        </Tooltip>
      </Specimen>

      <Specimen
        file="command-palette"
        hint="controlled here, so the app's own ⌘K keeps working"
      >
        <Button variant="outline" onClick={() => setPaletteOpen(true)}>
          Open the palette
        </Button>
        {/* CONTROLLED ON PURPOSE. `app-shell` already mounts the palette the
            shortcut belongs to; an uncontrolled second instance would answer
            the same ⌘K and two would open at once. See the prop's own note. */}
        <CommandPalette
          container={container}
          open={paletteOpen}
          onOpenChange={setPaletteOpen}
          onCreateTask={() => setPaletteOpen(false)}
        />
      </Specimen>
    </Group>
  );
}

function NavigationGroup() {
  const [picked, setPicked] = React.useState("root");

  return (
    <Group id="navigation">
      <Specimen file="tabs" hint="panels swap, the marker moves">
        <Tabs defaultValue="grounds" style={{ minWidth: 320 }}>
          <TabsList>
            <TabsTrigger value="grounds">Grounds</TabsTrigger>
            <TabsTrigger value="ladders">Ladders</TabsTrigger>
            <TabsTrigger value="guests">Guests</TabsTrigger>
          </TabsList>
          <TabsContent value="grounds">
            A ground is never asked what kind it is, only what value it has.
          </TabsContent>
          <TabsContent value="ladders">
            Seventeen steps of the foreground, and seventeen of its inverse.
          </TabsContent>
          <TabsContent value="guests">
            Two brands on one host ground can go different ways, from one rule.
          </TabsContent>
        </Tabs>
      </Specimen>

      <Specimen file="separator" hint="horizontal and vertical">
        <div style={{ display: "grid", gap: 12, minWidth: 260 }}>
          <span className="ds-n6" data-slot="foreground" data-render="foreground">
            Above
          </span>
          <Separator />
          <span className="ds-n6" data-slot="foreground" data-render="foreground">
            Below
          </span>
        </div>
        <div style={{ display: "flex", alignItems: "center", gap: 12, height: 32 }}>
          <span className="ds-n6" data-slot="foreground" data-render="foreground">
            Left
          </span>
          <Separator orientation="vertical" />
          <span className="ds-n6" data-slot="foreground" data-render="foreground">
            Right
          </span>
        </div>
      </Specimen>

      <Specimen file="section-label" hint="default and micro">
        <div style={{ display: "grid", gap: 8, minWidth: 220 }}>
          <SectionLabel as="h4">Reachable grounds</SectionLabel>
          <SectionLabel as="h5" variant="micro">
            Nine, at this threshold
          </SectionLabel>
        </div>
      </Specimen>

      <Specimen file="row" hint="three densities; the last one is a button">
        <div style={{ display: "grid", minWidth: 320, width: "100%", maxWidth: 520 }}>
          <Row density="dense" divider>
            <span>dense · 32px</span>
          </Row>
          <Row divider>
            <span>default · 40px</span>
          </Row>
          <Row
            as="button"
            density="spacious"
            hover
            data-row-pick
            onClick={() => setPicked(picked === "root" ? "guest" : "root")}
          >
            <span>spacious · click to select — now {picked}</span>
          </Row>
        </div>
      </Specimen>

      <Specimen file="scroll-area" hint="the bar is a ladder rung, not a grey">
        <ScrollArea style={{ height: 120, width: 280 }}>
          <div style={{ display: "grid", gap: 8, padding: 8 }}>
            {[
              "no-background",
              "solid",
              "solid-inverse",
              "solid-brand",
              "solid-brand-inverse",
              "transparent",
              "transparent-inverse",
            ].map((style) => (
              <span
                key={style}
                className="ds-n6"
                data-slot="foreground"
                data-render="foreground"
              >
                {style}
              </span>
            ))}
          </div>
        </ScrollArea>
      </Specimen>
    </Group>
  );
}

/** Title and description per tone, for the static at-rest toast specimens. */
const TOAST_SPECIMENS: Record<(typeof TOAST_TONES)[number], [string, string]> = {
  success: ["Kit saved", "Nine grounds, 149 consumer names."],
  error: ["Contrast too low", "3.1:1 against this ground, the floor is 4.5:1."],
  warning: ["Weakly supported", "`color-mix()` needs a 2023 engine or newer."],
  info: ["Recomputed", "The ladder moved, so 42 consumers were re-resolved."],
};

function FeedbackGroup() {
  const [progress, setProgress] = React.useState(62);

  return (
    <Group id="feedback">
      {/* NO `<Toaster/>` HERE, AND ITS ABSENCE IS THE SPECIMEN'S CORRECTNESS.
          Sonner's store is a MODULE SINGLETON with no portal, so a Toaster
          mounted in this frame renders every toast a SECOND time, in this
          document, 12px left of the app's own (this frame's `right:24px` is
          measured from the iframe's edge) and in a different resolved colour.
          That duplicate is what read as "a left border detached from the box".
          The shell's one Toaster answers the fire buttons; the four boxes below
          are STATIC markup wearing `TOAST_CLASSNAMES`, so the appearance of all
          four tones is visible AT REST without a second live instance. Same rule
          the old canvas already wrote down under SINGLETONS. */}
      <Specimen
        file="toast"
        hint="four tones at rest — a fired one appears OUTSIDE this frame, in the app's own corner"
      >
        <div style={{ display: "grid", gap: 8, minWidth: 300 }}>
          {TOAST_TONES.map((tone) => (
            <div
              key={tone}
              data-toast-specimen={tone}
              // The severity classes are qualified `data-[type=…]:` so they
              // outrank the base fill on specificity rather than on Tailwind's
              // emission order. Sonner writes this attribute; a specimen must.
              data-type={tone}
              className={`${TOAST_CLASSNAMES.toast} ${TOAST_CLASSNAMES[tone]} px-4 py-3`}
            >
              <div className={TOAST_CLASSNAMES.title}>{TOAST_SPECIMENS[tone][0]}</div>
              <div className={TOAST_CLASSNAMES.description}>
                {TOAST_SPECIMENS[tone][1]}
              </div>
            </div>
          ))}
        </div>
        {/* THE LABEL IS THE POINT, not the buttons — they already worked.
            The owner ruled the showcase should demonstrate BOTH the at-rest
            specimens and a real fire, provided the fire says where it goes.
            Without that sentence the specimen reads as broken: you press a
            button inside the frame and nothing appears inside the frame, and
            the reason (sonner's module singleton renders into the app document,
            recorded and accepted) is invisible from here. */}
        <div style={{ display: "grid", gap: 8 }}>
          <div style={{ display: "flex", flexWrap: "wrap", gap: 12 }}>
            <Button
              variant="outline"
              data-toast-fire
              onClick={() =>
                toast.success("Kit saved", {
                  description: "Nine grounds, 149 consumer names.",
                })
              }
            >
              Fire a toast
            </Button>
            <Button variant="ghost" onClick={() => toast.error("Contrast too low")}>
              Error
            </Button>
            <Button variant="ghost" onClick={() => toast.warning("Weakly supported")}>
              Warning
            </Button>
            <Button variant="ghost" onClick={() => toast.info("Recomputed")}>
              Info
            </Button>
          </div>
          <span
            className="ds-n8"
            data-toast-escape-note
            data-slot="foreground"
            data-render="foreground"
            style={{ opacity: 0.66, maxWidth: "34ch" }}
          >
            Fires land in the app's corner, not in this frame — sonner keeps one
            store per module, and this frame deliberately mounts no second
            Toaster.
          </span>
        </div>
      </Specimen>

      <Specimen file="progress" hint="drag the buttons, the bar follows">
        <div style={{ display: "grid", gap: 8, minWidth: 280 }}>
          <Progress value={progress} />
          <div style={{ display: "flex", gap: 8 }}>
            <Button
              size="sm"
              variant="outline"
              data-progress-down
              onClick={() => setProgress((v) => Math.max(0, v - 20))}
            >
              −20
            </Button>
            <Button
              size="sm"
              variant="outline"
              data-progress-up
              onClick={() => setProgress((v) => Math.min(100, v + 20))}
            >
              +20
            </Button>
            <span className="ds-n7" data-slot="foreground" data-render="foreground">
              {progress}%
            </span>
          </div>
        </div>
      </Specimen>

      <Specimen file="badge" hint="five variants">
        <Badge>ready</Badge>
        <Badge variant="secondary">review</Badge>
        <Badge variant="outline">draft</Badge>
        <Badge variant="destructive">failing</Badge>
        <Badge variant="ghost">archived</Badge>
      </Specimen>

      <Specimen file="status-badge" hint="the four signals plus accent and neutral">
        <StatusBadge tone="success" label="passes AA" showDot />
        <StatusBadge tone="warning" label="weakly supported" showDot />
        {/* WRAPPED FOR THE GATE, because `StatusBadge` forwards `className` and
            nothing else — the same swallow-every-unnamed-prop shape `Row` had.
            A wrapper is the honest way to hook it without changing a component
            this scene did not otherwise need to change. */}
        <span data-status-sample="error" style={{ display: "inline-flex" }}>
          <StatusBadge tone="error" label="must fix" showDot />
        </span>
        <StatusBadge tone="info" label="computed" showDot />
        <StatusBadge tone="accent" label="brand-full" showDot />
        <StatusBadge tone="neutral" label="inherited" />
      </Specimen>

      <Specimen file="empty-state">
        <EmptyState
          icon={<Inbox className="h-10 w-10" />}
          title="No guests on this ground"
          description="A guest is a second brand placed on a host. Author one to see the two go different ways from the same rule."
          action={
            <Button size="sm">
              <Plus /> Add a guest
            </Button>
          }
          className="max-w-[420px]"
        />
      </Specimen>

      <Specimen file="loading-skeleton">
        <div style={{ minWidth: 280, maxWidth: 420, width: "100%" }}>
          <LoadingSkeleton lines={3} />
        </div>
      </Specimen>

      <Specimen file="timeline">
        <Timeline>
          <TimelineEntry timestamp={new Date().toISOString()} tone="accent">
            <TimelineTitle>Threshold re-ratified at 0.675</TimelineTitle>
            <TimelineBadges>
              <TimelineBadge tone="accent">ruling</TimelineBadge>
              <TimelineBadge>register r4</TimelineBadge>
            </TimelineBadges>
            <TimelineBody>
              Every ink judgement comes out right at 0.675, and 0.5 got all but
              one of them wrong.
            </TimelineBody>
          </TimelineEntry>
          <TimelineEntry timestamp={new Date().toISOString()} tone="success">
            <TimelineTitle muted>Adapter reaches 149 names</TimelineTitle>
            <TimelineBody>
              The brand's radius ladder now drives Card, Dialog and Sheet.
            </TimelineBody>
          </TimelineEntry>
        </Timeline>
      </Specimen>
    </Group>
  );
}

const MARKDOWN_SAMPLE = `## What the ground decides

One value passes down: the **resolved background**. Recursion alternates
forever, with no counter.

- \`no request\` — inherit
- \`emphasis\` — the neutral of opposite polarity
- \`branded\` — rule 4

> A ground is a colour. Nothing branches on its kind.
`;

function DataGroup() {
  const [expanded, setExpanded] = React.useState(false);

  return (
    <Group id="data">
      <Specimen file="card" hint="header, action, content, footer">
        <Card style={{ maxWidth: 380 }}>
          <CardHeader>
            <CardTitle>Author a brand</CardTitle>
            <CardDescription>
              One colour and one typeface is the whole authored input.
            </CardDescription>
            <CardAction>
              <Button size="icon" variant="ghost" aria-label="dismiss">
                <Trash2 />
              </Button>
            </CardAction>
          </CardHeader>
          <CardContent>
            <p className="ds-n5 ds-paragraphs" data-slot="foreground" data-render="foreground">
              The corner of this card is the brand's L rung — it used to be
              Tailwind's, which no brand could author.
            </p>
          </CardContent>
          <CardFooter>
            <Button size="sm" data-card-expand onClick={() => setExpanded((v) => !v)}>
              {expanded ? "Show less" : "Show more"}
            </Button>
          </CardFooter>
        </Card>
        {expanded && (
          <Card style={{ maxWidth: 380 }}>
            <CardContent>
              <p className="ds-n5 ds-paragraphs" data-slot="foreground" data-render="foreground">
                Elevation is a shadow on light grounds and a border on dark ones.
                One rule; the ground picks the outcome.
              </p>
            </CardContent>
          </Card>
        )}
      </Specimen>

      <Specimen file="metric-card" hint="plain and accent">
        <MetricCard label="Reachable grounds" value={9} hint="at threshold 0.675" />
        <MetricCard label="Consumer names" value={149} accent />
        <MetricCard label="Ladder steps" value="17 × 2" />
      </Specimen>

      <Specimen file="markdown-content" hint="the compact variant">
        <div style={{ maxWidth: 520 }}>
          <MarkdownContent variant="compact">{MARKDOWN_SAMPLE}</MarkdownContent>
        </div>
      </Specimen>

      <Specimen
        file="markdown-render"
        hint="the renderer under markdown-content, driven directly"
      >
        <div style={{ maxWidth: 520 }}>
          <MarkdownRender
            components={{
              p: ({ children }) => (
                <p
                  className="ds-n5 ds-paragraphs"
                  data-slot="foreground"
                  data-render="foreground"
                  style={{ margin: 0 }}
                >
                  {children}
                </p>
              ),
              strong: ({ children }) => <strong>{children}</strong>,
            }}
          >
            {"The **same renderer**, with the component map handed in by the caller."}
          </MarkdownRender>
        </div>
      </Specimen>
    </Group>
  );
}

/* -------------------------------------------------------------- the surface */

/**
 * The frame's own `<body>`, as a container for every portal in this file.
 *
 * Read off a React context rather than through `document`, because `document`
 * in this module is the PARENT's — the mistake the preview-frame header warns
 * about in capitals, and the reason an overlay would open outside the iframe it
 * was clicked in.
 */
const FrameDocument = React.createContext<Document | null>(null);

/* ----------------------------------------------------------------- matrix */

/**
 * THE STATES A LIBRARY VIEW HAS TO SHOW, and the attribute that forces each.
 *
 * `rest` carries no attribute at all rather than `data-demo="rest"`, because
 * rest is the ABSENCE of a state and giving it a name would invite a rule for
 * it. `disabled` is the real DOM property, not a demo attribute — it changes
 * behaviour as well as paint, and faking it would show a lie.
 */
const DEMO_STATES = [
  { key: "rest", demo: undefined, disabled: false },
  { key: "hover", demo: "hover", disabled: false },
  { key: "focus", demo: "focus", disabled: false },
  { key: "pressed", demo: "pressed", disabled: false },
  { key: "disabled", demo: undefined, disabled: true },
] as const;

/**
 * EVERY VARIANT x EVERY SIZE, AND EVERY VARIANT x EVERY STATE.
 *
 * His item 11: "i don't see a complete component overview with their variants.
 * I need a complete library. nicely layouted to see what components behave
 * correctly and what components don't."
 *
 * TWO PROPERTIES MAKE IT ANSWER THAT RATHER THAN DECORATE IT:
 *
 * 1. THE AXES ARE THE COMPONENT'S OWN. `BUTTON_VARIANTS` and `BUTTON_SIZES` are
 *    `Object.keys` of the table `cva` is configured with, so this grid cannot
 *    disagree with the component. The previous scene hand-listed 11 of 48
 *    combinations under a caption claiming a different number again — that is
 *    what a hand-list does, not what someone did wrong.
 *
 * 2. THE STATES ARE FORCED BY THE ENGINE'S OWN RULE. Each state cell carries
 *    `data-demo`, and `emit.py` writes `:hover` and `[data-demo="hover"]` into
 *    ONE declaration block. So a specimen is painted by the same bytes as a real
 *    cursor and cannot drift from it. Nothing here declares a colour.
 *
 * Why this section is not a `Group`: the five groups are the file census, keyed
 * to `SHOWCASE_INVENTORY`, and this is a second VIEW of components that census
 * already covers rather than a sixth bucket of files.
 */
function VariantMatrix() {
  return (
    <section
      data-variant-matrix
      className="ds-transparent ds-bordered ds-radius-l ds-padding-double"
      data-slot="transparent"
      data-render="transparent"
      style={{ display: "grid", gap: 32, borderStyle: "solid" }}
    >
      <div style={{ display: "grid", gap: 8 }}>
        <span
          className="ds-n8 ds-leads"
          data-slot="foreground"
          data-render="foreground"
          style={{ opacity: 0.5, letterSpacing: "0.1em" }}
        >
          PRIMARY SPECIMEN / BUTTON
        </span>
        <h3
          className="ds-h3 ds-headings"
          data-slot="foreground"
          data-render="foreground"
          style={{ margin: 0 }}
        >
          The whole matrix — {BUTTON_VARIANTS.length} variants ×{" "}
          {BUTTON_SIZES.length} sizes, and every state
        </h3>
        <p
          className="ds-n5 ds-paragraphs"
          data-slot="foreground"
          data-render="foreground"
          style={{ margin: 0, maxWidth: "70ch" }}
        >
          Both axes are read off the component's own variant table, so this grid
          cannot fall behind it. The state rows are forced with{" "}
          <code>data-demo</code>, which the engine emits in the same rule as the
          real pseudo-class — what you see is what a cursor gets.
        </p>
      </div>

      {/* SIZE GRID — one row per variant, one column per size, labelled on both
          axes so a reader can name what they are looking at. */}
      <div
        data-matrix-sizes
        style={{
          display: "grid",
          gridTemplateColumns: `max-content repeat(${BUTTON_SIZES.length}, max-content)`,
          gap: 12,
          alignItems: "center",
          overflowX: "auto",
        }}
      >
        <span />
        {BUTTON_SIZES.map((size) => (
          <span
            key={size}
            className="ds-n8 ds-leads"
            data-slot="foreground"
            data-render="foreground"
            style={{ fontFamily: "var(--font-mono)", opacity: 0.66 }}
          >
            {size}
          </span>
        ))}
        {BUTTON_VARIANTS.map((variant) => (
          <React.Fragment key={variant}>
            <span
              className="ds-n8 ds-leads"
              data-slot="foreground"
              data-render="foreground"
              style={{ fontFamily: "var(--font-mono)", opacity: 0.66 }}
            >
              {variant}
            </span>
            {BUTTON_SIZES.map((size) => (
              <Button
                key={size}
                variant={variant}
                size={size}
                data-matrix-cell={`${variant}/${size}`}
              >
                {size.startsWith("icon") ? <Plus /> : variant}
              </Button>
            ))}
          </React.Fragment>
        ))}
      </div>

      {/* STATE BAND — one row per variant, one column per state. */}
      <div
        data-matrix-states
        style={{
          display: "grid",
          gridTemplateColumns: `max-content repeat(${DEMO_STATES.length}, max-content)`,
          gap: 12,
          alignItems: "center",
          overflowX: "auto",
        }}
      >
        <span />
        {DEMO_STATES.map((state) => (
          <span
            key={state.key}
            className="ds-n8 ds-leads"
            data-slot="foreground"
            data-render="foreground"
            style={{ fontFamily: "var(--font-mono)", opacity: 0.66 }}
          >
            {state.key}
          </span>
        ))}
        {BUTTON_VARIANTS.map((variant) => (
          <React.Fragment key={variant}>
            <span
              className="ds-n8 ds-leads"
              data-slot="foreground"
              data-render="foreground"
              style={{ fontFamily: "var(--font-mono)", opacity: 0.66 }}
            >
              {variant}
            </span>
            {DEMO_STATES.map((state) => (
              <Button
                key={state.key}
                variant={variant}
                data-demo={state.demo}
                disabled={state.disabled}
                data-matrix-state={`${variant}/${state.key}`}
              >
                {state.key}
              </Button>
            ))}
          </React.Fragment>
        ))}
      </div>
    </section>
  );
}

function ComponentContractTable() {
  return (
    <section className="component-contract" aria-labelledby="component-contract-title">
      <div className="component-contract-intro">
        <span className="ds-n8 ds-leads" data-slot="foreground" data-render="foreground">
          COMPONENT CONTRACT
        </span>
        <h3
          id="component-contract-title"
          className="ds-h3 ds-headings"
          data-slot="foreground"
          data-render="foreground"
        >
          What the system guarantees—and what each family still owns.
        </h3>
        <p className="ds-n5 ds-paragraphs" data-slot="foreground" data-render="foreground">
          Components consume the computed vocabulary. They own behavior and
          state, never a private colour model.
        </p>
      </div>
      <div className="component-contract-table">
        <table>
          <thead>
            <tr><th>Family</th><th>Coverage</th><th>Inherited inputs</th><th>Local responsibility</th><th>Evidence on this page</th></tr>
          </thead>
          <tbody>
            {GROUP_LABELS.map((group) => {
              const contract = GROUP_CONTRACTS[group.id];
              const count = SHOWCASE_INVENTORY.filter((item) => item.group === group.id).length;
              return (
                <tr key={group.id}>
                  <th>{group.label}</th>
                  <td>{count} primitives</td>
                  <td>{contract.inherits}</td>
                  <td>{contract.owns}</td>
                  <td>{contract.proof}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </section>
  );
}

function useFrameBody(): HTMLElement | null {
  const doc = React.useContext(FrameDocument);
  return doc?.body ?? null;
}

export function ComponentShowcase({ doc }: { doc: Document }) {
  return (
    <FrameDocument.Provider value={doc}>
      <TooltipProvider>
        <section
          data-component-showcase
          style={{ display: "grid", gap: 0, paddingBottom: 64 }}
        >
          <header
            data-showcase-hero
            style={{
              display: "grid",
              gridTemplateColumns: "minmax(0, 1.15fr) minmax(260px, 0.85fr)",
              gap: 64,
              alignItems: "end",
              minHeight: 480,
              padding: "64px 0 80px",
            }}
          >
            <div style={{ display: "grid", gap: 20 }}>
              <span
                className="ds-n7 ds-leads"
                data-slot="foreground"
                data-render="foreground"
                style={{ opacity: 0.58, letterSpacing: "0.1em" }}
              >
                COMPONENT SYSTEM / LIVE
              </span>
            <h2
              className="ds-h1 ds-headings"
              data-slot="foreground"
              data-render="foreground"
              style={{ margin: 0, maxWidth: "13ch", letterSpacing: "-0.055em" }}
            >
              One system.<br />Every interaction.
            </h2>
            <p
              className="ds-n4 ds-leads"
              data-slot="foreground"
              data-render="foreground"
              style={{ margin: 0, maxWidth: "58ch", opacity: 0.76 }}
            >
              All {SHOWCASE_INVENTORY.length} product primitives, rendered live
              against the active ground. Interact with them. Change the rung.
              Move the ground. The library answers as one system.
            </p>
            </div>

            <div
              className="ds-transparent ds-bordered ds-radius-l ds-padding-double"
              data-slot="transparent"
              data-render="transparent"
              style={{ display: "grid", gap: 24, borderStyle: "solid" }}
            >
              {[
                ["33", "primitives"],
                ["05", "families"],
                ["08", "size rungs"],
                ["∞", "nested grounds"],
              ].map(([value, label]) => (
                <div
                  key={label}
                  style={{
                    display: "flex",
                    alignItems: "baseline",
                    justifyContent: "space-between",
                    gap: 20,
                    paddingBottom: 12,
                    borderBottom: "1px solid color-mix(in srgb, currentColor 16%, transparent)",
                  }}
                >
                  <span className="ds-h4 ds-titles" data-slot="foreground" data-render="foreground">
                    {value}
                  </span>
                  <span className="ds-n7" data-slot="foreground" data-render="foreground">
                    {label}
                  </span>
                </div>
              ))}
            </div>
          </header>

          <ComponentContractTable />
          <VariantMatrix />
          <FormsGroup />
          <OverlaysGroup />
          <NavigationGroup />
          <FeedbackGroup />
          <DataGroup />
        </section>
      </TooltipProvider>
    </FrameDocument.Provider>
  );
}
