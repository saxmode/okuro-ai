import { useState } from "react";
import { Inbox, Plus, Settings, Trash2 } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardAction, CardContent, CardDescription, CardFooter, CardHeader, CardTitle } from "@/components/ui/card";
import { CommandPalette } from "@/components/ui/command-palette";
import { ContextMenu, ContextMenuCheckboxItem, ContextMenuContent, ContextMenuItem, ContextMenuLabel, ContextMenuRadioGroup, ContextMenuRadioItem, ContextMenuSeparator, ContextMenuShortcut, ContextMenuTrigger } from "@/components/ui/context-menu";
import { DetailField, DetailFields, DetailModal, DetailProse, DetailSection } from "@/components/ui/detail-modal";
import { Dialog, DialogClose, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle, DialogTrigger } from "@/components/ui/dialog";
import { DropdownMenu, DropdownMenuCheckboxItem, DropdownMenuContent, DropdownMenuGroup, DropdownMenuItem, DropdownMenuLabel, DropdownMenuSeparator, DropdownMenuShortcut, DropdownMenuTrigger } from "@/components/ui/dropdown-menu";
import { EmptyState } from "@/components/ui/empty-state";
import { FacetBar } from "@/components/ui/facet-bar";
import { FieldLabel, StringListEditor, SubsectionHeading } from "@/components/ui/form-primitives";
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
import { Select, SelectContent, SelectGroup, SelectItem, SelectLabel, SelectSeparator, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Separator } from "@/components/ui/separator";
import { Sheet, SheetClose, SheetContent, SheetDescription, SheetFooter, SheetHeader, SheetTitle, SheetTrigger } from "@/components/ui/sheet";
import { MobilePanelTrigger, SidePanel } from "@/components/ui/side-panel";
import { StatusBadge } from "@/components/ui/status-badge";
import { Switch } from "@/components/ui/switch";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Textarea } from "@/components/ui/textarea";
import { Timeline, TimelineBadge, TimelineBadges, TimelineBody, TimelineEntry, TimelineTitle } from "@/components/ui/timeline";
import { toast } from "@/components/ui/toast";
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from "@/components/ui/tooltip";

export function LiveDemo({ system, grounds, components }: { system: string; grounds: number; components: number }) {
  const [progress, setProgress] = useState(68);
  const [automation, setAutomation] = useState(true);

  return (
    <div className="dsc-native-demo ds-surface ds-n5 ds-paragraphs" data-native-demo>
      <header className="dsc-native-demo-head ds-n8 ds-leads">
        <div className="dsc-native-brand ds-n8 ds-leads"><i /> NORTHSTAR</div>
        <div className="dsc-native-demo-nav ds-n8 ds-paragraphs"><span>Overview</span><span>Activity</span><span>Systems</span></div>
        <Badge variant="outline">{system}</Badge>
      </header>
      <div className="dsc-native-demo-grid">
        <section className="dsc-native-demo-copy">
          <StatusBadge tone="success" label="System operational" showDot />
          <h3 className="ds-h2 ds-titles">Design decisions,<br />resolved in context.</h3>
          <p className="ds-n5 ds-paragraphs">This is a real product composition. Every control, surface, border and type role consumes the selected system directly.</p>
          <div className="dsc-native-actions">
            <Button onClick={() => setProgress((value) => Math.min(100, value + 8))}>Run resolution</Button>
            <Button variant="outline">Inspect logic</Button>
          </div>
        </section>
        <Card className="dsc-native-activity">
          <CardHeader>
            <CardTitle>System resolution</CardTitle>
            <CardDescription>Live output from the current identity</CardDescription>
            <CardAction><Badge variant="secondary">{progress}%</Badge></CardAction>
          </CardHeader>
          <CardContent>
            <Progress value={progress} />
            <div className="dsc-native-rows">
              <Row divider><span>Ground graph</span><StatusBadge tone="success" label={`${grounds} ready`} showDot /></Row>
              <Row divider><span>Component adapter</span><StatusBadge tone="success" label={`${components} mapped`} showDot /></Row>
              <Row><span>Inherited contexts</span><Badge variant="outline">Deterministic</Badge></Row>
            </div>
          </CardContent>
          <CardFooter className="dsc-native-switch-row">
            <div><strong className="ds-n7 ds-leads">Continuous validation</strong><small className="ds-n8 ds-paragraphs">Resolve whenever authored input changes</small></div>
            <Switch checked={automation} onCheckedChange={setAutomation} aria-label="Continuous validation" />
          </CardFooter>
        </Card>
      </div>
      <div className="dsc-native-metrics">
        <MetricCard label="Resolved grounds" value={grounds} hint="computed contexts" />
        <MetricCard label="Live primitives" value={components} hint="complete inventory" accent />
        <MetricCard label="Resolution" value={`${progress}%`} hint="current pass" />
      </div>
    </div>
  );
}

export function ComponentSpecimen({ id }: { id: string }) {
  const [text, setText] = useState("okuro-ds");
  const [notes, setNotes] = useState("Appearance inherits. Behavior stays local.");
  const [choice, setChoice] = useState("M");
  const [enabled, setEnabled] = useState(true);
  const [segment, setSegment] = useState("light");
  const [aliases, setAliases] = useState(["okuro", "okuro-ds"]);
  const [facets, setFacets] = useState<Partial<Record<"kind" | "state", string>>>({});
  const [detailOpen, setDetailOpen] = useState(false);
  const [sidePanelOpen, setSidePanelOpen] = useState(false);
  const [paletteOpen, setPaletteOpen] = useState(false);
  const [bookmarked, setBookmarked] = useState(true);
  const [sort, setSort] = useState("recent");
  const [picked, setPicked] = useState("root");
  const [progress, setProgress] = useState(62);
  const [expanded, setExpanded] = useState(false);
  const container = typeof document === "undefined" ? undefined : document.body;
  let specimen: React.ReactNode;

  switch (id) {
    case "button": specimen = <><Button>Primary action</Button><Button variant="secondary">Secondary</Button><Button variant="outline">Outline</Button><Button variant="ghost">Ghost</Button><Button variant="destructive">Destructive</Button><Button size="icon" aria-label="Settings"><Settings /></Button></>; break;
    case "input": specimen = <div className="dsc-native-stack"><Input value={text} onChange={(event) => setText(event.target.value)} aria-label="System name" /><Input disabled value="Protected system" /></div>; break;
    case "textarea": specimen = <Textarea value={notes} onChange={(event) => setNotes(event.target.value)} rows={4} aria-label="System notes" />; break;
    case "label": specimen = <div className="dsc-native-stack"><Label htmlFor="dsc-label-input">System name</Label><Input id="dsc-label-input" value={text} onChange={(event) => setText(event.target.value)} /></div>; break;
    case "select": specimen = <Select value={choice} onValueChange={setChoice}><SelectTrigger aria-label="Default rung"><SelectValue /></SelectTrigger><SelectContent container={container}><SelectGroup><SelectLabel>Rung</SelectLabel><SelectItem value="S">Small · S</SelectItem><SelectItem value="M">Medium · M</SelectItem><SelectSeparator /><SelectItem value="L">Large · L</SelectItem></SelectGroup></SelectContent></Select>; break;
    case "switch": specimen = <div className="dsc-native-inline"><Switch id="dsc-switch" checked={enabled} onCheckedChange={setEnabled} /><Label htmlFor="dsc-switch">Continuous validation</Label></div>; break;
    case "segmented": specimen = <Segmented ariaLabel="Appearance" value={segment} onChange={setSegment} options={[{ value: "light", label: "Light" }, { value: "dark", label: "Dark" }]} />; break;
    case "form-primitives": specimen = <div className="dsc-native-stack"><SubsectionHeading title="Aliases" hint="alternate names" /><FieldLabel>Known as</FieldLabel><StringListEditor value={aliases} onChange={setAliases} placeholder="add an alias" /></div>; break;
    case "facet-bar": specimen = <FacetBar items={[{ kind: "neutral", state: "ready" }, { kind: "signal", state: "flagged" }, { kind: "brand", state: "ready" }]} groups={[{ label: "Kind", field: "kind" }, { label: "State", field: "state" }]} active={facets} onChange={setFacets} />; break;
    case "dialog": specimen = <Dialog><DialogTrigger asChild><Button variant="outline">Open dialog</Button></DialogTrigger><DialogContent container={container}><DialogHeader><DialogTitle>Save this system?</DialogTitle><DialogDescription>The editable kit will replace its last saved state.</DialogDescription></DialogHeader><DialogFooter><DialogClose asChild><Button variant="ghost">Cancel</Button></DialogClose><DialogClose asChild><Button>Save system</Button></DialogClose></DialogFooter></DialogContent></Dialog>; break;
    case "sheet": specimen = <Sheet><SheetTrigger asChild><Button variant="outline">Open sheet</Button></SheetTrigger><SheetContent container={container}><SheetHeader><SheetTitle>Configuration</SheetTitle><SheetDescription>Associated context remains available beside the page.</SheetDescription></SheetHeader><div className="dsc-native-panel-copy">This overlay consumes the same active system through the document root.</div><SheetFooter><SheetClose asChild><Button variant="ghost">Close</Button></SheetClose></SheetFooter></SheetContent></Sheet>; break;
    case "side-panel": specimen = <><SidePanel title="Kit inspector" container={container} open={sidePanelOpen} onOpenChange={setSidePanelOpen} desktopClassName="dsc-native-side-panel"><SectionLabel as="h4" size="sm">Reachable grounds</SectionLabel><p className="ds-n6 ds-paragraphs">Nine contexts resolve from the selected system.</p></SidePanel><MobilePanelTrigger label="Open panel" onClick={() => setSidePanelOpen(true)} /></>; break;
    case "detail-modal": specimen = <><Button variant="outline" onClick={() => setDetailOpen(true)}>Open detail</Button><DetailModal open={detailOpen} onOpenChange={setDetailOpen} container={container} eyebrow="GROUND" title="The brand's own white" subtitle="L 1.000 · light"><DetailFields><DetailField label="Polarity">light</DetailField><DetailField label="Foreground" mono>#030303</DetailField><DetailField label="Rule">2</DetailField></DetailFields><DetailSection title="Why"><DetailProse>{"The ground crosses the threshold and therefore consumes the selected system's dark foreground."}</DetailProse></DetailSection></DetailModal></>; break;
    case "dropdown-menu": specimen = <DropdownMenu><DropdownMenuTrigger asChild><Button variant="outline">Actions</Button></DropdownMenuTrigger><DropdownMenuContent container={container}><DropdownMenuLabel>This system</DropdownMenuLabel><DropdownMenuGroup><DropdownMenuItem>Duplicate<DropdownMenuShortcut>⌘D</DropdownMenuShortcut></DropdownMenuItem><DropdownMenuCheckboxItem checked={bookmarked} onCheckedChange={setBookmarked}>Pin to navigation</DropdownMenuCheckboxItem></DropdownMenuGroup><DropdownMenuSeparator /><DropdownMenuItem variant="destructive">Retire</DropdownMenuItem></DropdownMenuContent></DropdownMenu>; break;
    case "context-menu": specimen = <ContextMenu><ContextMenuTrigger asChild><div className="dsc-native-context-target">Right-click this system</div></ContextMenuTrigger><ContextMenuContent container={container}><ContextMenuLabel>Sort system</ContextMenuLabel><ContextMenuRadioGroup value={sort} onValueChange={setSort}><ContextMenuRadioItem value="recent">By recency</ContextMenuRadioItem><ContextMenuRadioItem value="name">By name</ContextMenuRadioItem></ContextMenuRadioGroup><ContextMenuSeparator /><ContextMenuCheckboxItem checked>Show findings</ContextMenuCheckboxItem><ContextMenuItem>Copy value<ContextMenuShortcut>⌘C</ContextMenuShortcut></ContextMenuItem></ContextMenuContent></ContextMenu>; break;
    case "tooltip": specimen = <TooltipProvider><Tooltip><TooltipTrigger asChild><Button variant="outline">Why 0.675?</Button></TooltipTrigger><TooltipContent container={container}>The threshold comes from measured contrast trials.</TooltipContent></Tooltip></TooltipProvider>; break;
    case "command-palette": specimen = <><Button variant="outline" onClick={() => setPaletteOpen(true)}>Open command palette</Button><CommandPalette container={container} open={paletteOpen} onOpenChange={setPaletteOpen} onCreateTask={() => setPaletteOpen(false)} /></>; break;
    case "tabs": specimen = <Tabs defaultValue="overview"><TabsList><TabsTrigger value="overview">Overview</TabsTrigger><TabsTrigger value="api">API</TabsTrigger><TabsTrigger value="accessibility">Accessibility</TabsTrigger></TabsList><TabsContent value="overview">The component inherits appearance from the nearest theme context.</TabsContent><TabsContent value="api">Configuration remains explicit and local.</TabsContent><TabsContent value="accessibility">Keyboard relationships remain native.</TabsContent></Tabs>; break;
    case "scroll-area": specimen = <ScrollArea className="dsc-native-scroll"><div className="dsc-native-stack">{["no-background", "solid", "solid-inverse", "solid-brand", "transparent", "transparent-inverse"].map((item) => <Row key={item} divider><span>{item}</span><Badge variant="outline">ground</Badge></Row>)}</div></ScrollArea>; break;
    case "separator": specimen = <div className="dsc-native-stack"><span>Identity and typography</span><Separator /><span>Geometry and motion</span></div>; break;
    case "section-label": specimen = <div className="dsc-native-stack"><SectionLabel as="h4">Reachable grounds</SectionLabel><SectionLabel as="h5" variant="micro">Nine at this threshold</SectionLabel></div>; break;
    case "row": specimen = <div className="dsc-native-stack"><Row density="dense" divider><span>Dense · 32px</span></Row><Row divider><span>Default · 40px</span></Row><Row as="button" density="spacious" hover onClick={() => setPicked(picked === "root" ? "guest" : "root")}><span>Selected context · {picked}</span></Row></div>; break;
    case "toast": specimen = <div className="dsc-native-inline"><Button onClick={() => toast.success("System saved", { description: "The editable kit is current." })}>Success toast</Button><Button variant="outline" onClick={() => toast.error("Contrast is below target")}>Error toast</Button></div>; break;
    case "progress": specimen = <div className="dsc-native-stack"><Progress value={progress} /><div className="dsc-native-inline"><Button size="sm" variant="outline" onClick={() => setProgress((value) => Math.max(0, value - 20))}>−20</Button><Button size="sm" variant="outline" onClick={() => setProgress((value) => Math.min(100, value + 20))}>+20</Button><Badge variant="outline">{progress}%</Badge></div></div>; break;
    case "badge": specimen = <><Badge>Ready</Badge><Badge variant="secondary">Review</Badge><Badge variant="outline">Draft</Badge><Badge variant="destructive">Failing</Badge><Badge variant="ghost">Archived</Badge></>; break;
    case "status-badge": specimen = <><StatusBadge tone="success" label="passes AA" showDot /><StatusBadge tone="warning" label="review" showDot /><StatusBadge tone="error" label="must fix" showDot /><StatusBadge tone="info" label="computed" showDot /><StatusBadge tone="accent" label="brand" showDot /></>; break;
    case "empty-state": specimen = <EmptyState icon={<Inbox className="h-10 w-10" />} title="No matching systems" description="Clear the filters or create an editable system from a protected source." action={<Button size="sm"><Plus /> Create system</Button>} />; break;
    case "loading-skeleton": specimen = <div className="dsc-native-skeleton"><LoadingSkeleton lines={3} /></div>; break;
    case "timeline": specimen = <Timeline><TimelineEntry timestamp="Just now" tone="accent"><TimelineTitle>Identity authored</TimelineTitle><TimelineBadges><TimelineBadge tone="accent">input</TimelineBadge></TimelineBadges><TimelineBody>Canonical color and typeface entered the pipeline.</TimelineBody></TimelineEntry><TimelineEntry timestamp="Resolved" tone="success"><TimelineTitle muted>Components adapted</TimelineTitle><TimelineBody>The semantic contract now serves every primitive.</TimelineBody></TimelineEntry></Timeline>; break;
    case "card": specimen = <><Card className="dsc-native-card"><CardHeader><CardTitle>Author a brand</CardTitle><CardDescription>One color and one typeface define the identity.</CardDescription><CardAction><Button size="icon" variant="ghost" aria-label="Dismiss"><Trash2 /></Button></CardAction></CardHeader><CardContent>Its resolved values flow into the complete component vocabulary.</CardContent><CardFooter><Button size="sm" onClick={() => setExpanded((value) => !value)}>{expanded ? "Show less" : "Show more"}</Button></CardFooter></Card>{expanded && <Badge variant="secondary">Elevation also inherits</Badge>}</>; break;
    case "metric-card": specimen = <><MetricCard label="Reachable grounds" value={9} hint="at threshold 0.675" /><MetricCard label="Consumer names" value={149} accent /></>; break;
    case "markdown-content": specimen = <MarkdownContent variant="compact">{"## Inheritance\n\nA child without a request inherits the parent ground. A child with a request resolves once, then becomes the next parent.\n\n- deterministic\n- contextual\n- composable"}</MarkdownContent>; break;
    case "markdown-render": specimen = <MarkdownRender components={{ p: ({ children }) => <p className="ds-n5 ds-paragraphs">{children}</p>, strong: ({ children }) => <strong>{children}</strong> }}>{"The **same renderer** consumes a component map supplied by its caller."}</MarkdownRender>; break;
    default: specimen = <Button>{id}</Button>;
  }

  return <div className="dsc-native-specimen ds-surface ds-n5 ds-paragraphs" data-native-specimen={id}>{specimen}</div>;
}
