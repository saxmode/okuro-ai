/**
 * okuro'S OWN PRIMITIVES, WEARING THE EMITTED KIT — his item 6, and the heart of
 * the demo.
 *
 * WHAT WAS WRONG. The canvas built its components as HTML STRINGS inside the
 * frame (`preview-frame.tsx`'s seven blocks): a `<button class="btn ds-solid">`
 * that LOOKS like okuro's Button and shares no line of code with it. Measured on
 * the deployed page 2026-08-18: zero React roots and zero shadcn part names in
 * the frame. So the page demonstrated a sheet against a mock-up of the app, and
 * the one claim that matters — "okuro's real components wear this kit" — was the
 * one thing it could not show.
 *
 * WHAT THIS IS. The vendored `ui/` primitives, imported normally, rendered
 * through a portal into the frame's document. Same components, same props, same
 * Radix behaviour as everywhere else in okuro. Nothing here styles anything: the
 * primitives read `--color-*`, `--radius-*` and `--type-*`, the adapter emits all
 * 144 of those names inside every ground block, and the frame carries okuro's own
 * stylesheets so the utility classes resolve. What you see is the engine driving
 * the app.
 *
 * THE PORTAL-CARRYING ONES NEED THE FRAME'S BODY. `Select` and `Tooltip` mount
 * their overlay through `Radix.Portal`, which defaults to `document.body` — the
 * PARENT's body, so the dropdown would open outside the iframe it was clicked
 * in. Both take a `container`, which `lib/ground-portal.ts` documents as existing
 * for exactly this surface, and both already stamp `data-ground` on their content
 * via `useFrameStamp`, so an overlay opened inside a dark panel stays dark. That
 * is the portal contract of register rule 14, demonstrated rather than asserted.
 *
 * IT IS NOT A SECOND SPECIMEN. The seven hand-built blocks stay: they are the
 * evidence that the SHEET alone paints a document, with no React and no app CSS
 * in the frame at all. This section is the evidence that the ADAPTER reaches
 * okuro's real vocabulary. Two different claims, two different artefacts.
 */

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Separator } from "@/components/ui/separator";
import { Switch } from "@/components/ui/switch";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip";

/** The part names the gate counts, so "real primitives are present" is a
 *  measurement rather than a claim about this file. */
export const SHOWCASED_PARTS = [
  "button",
  "input",
  "select-trigger",
  "card",
  "tabs-trigger",
  "switch",
  "badge",
  "label",
] as const;

export function KitShowcase({ doc }: { doc: Document }) {
  /* THE FRAME'S OWN BODY is where an overlay from inside the canvas belongs.
     Read off the document the parent was handed rather than through a global,
     because `document` in this file is the PARENT's — the mistake the
     preview-frame header warns about in capitals. */
  const container = doc.body;

  return (
    <TooltipProvider>
      <section
        data-kit-showcase
        style={{ display: "grid", gap: 24 }}
      >
        <Card style={{ overflow: "hidden" }}>
          <CardHeader
            style={{
              display: "flex",
              flexDirection: "row",
              alignItems: "center",
              justifyContent: "space-between",
              gap: 24,
            }}
          >
            <div style={{ display: "grid", gap: 6 }}>
              <CardDescription>DESIGN SYSTEM / LIVE APPLICATION</CardDescription>
              <CardTitle>Meridian operations</CardTitle>
            </div>
            <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
              <Badge>healthy</Badge>
              <Tooltip>
                <TooltipTrigger asChild>
                  <Button variant="outline">Inspect</Button>
                </TooltipTrigger>
                <TooltipContent container={container}>
                  This overlay portals into the preview and keeps the ground it
                  opened from.
                </TooltipContent>
              </Tooltip>
              <Button>New run</Button>
            </div>
          </CardHeader>

          <Separator />

          <CardContent style={{ display: "grid", gap: 24, paddingTop: 24 }}>
            <div
              style={{
                display: "grid",
                gridTemplateColumns: "repeat(3, minmax(0, 1fr))",
                gap: 12,
              }}
            >
              {[
                ["Systems", "08", "all resolved"],
                ["Components", "33", "live primitives"],
                ["Flags", "02", "review requested"],
              ].map(([label, value, note]) => (
                <Card key={label}>
                  <CardHeader style={{ gap: 8 }}>
                    <CardDescription>{label}</CardDescription>
                    <CardTitle>{value}</CardTitle>
                    <CardDescription>{note}</CardDescription>
                  </CardHeader>
                </Card>
              ))}
            </div>

            <div
              style={{
                display: "grid",
                gridTemplateColumns: "minmax(0, 1.15fr) minmax(260px, 0.85fr)",
                gap: 16,
                alignItems: "start",
              }}
            >
              <Card>
                <CardHeader>
                  <CardTitle>Configure a release</CardTitle>
                  <CardDescription>
                    A real task surface, rendered with the same primitives as
                    the product.
                  </CardDescription>
                </CardHeader>
                <CardContent style={{ display: "grid", gap: 16 }}>
                  <div style={{ display: "grid", gap: 6 }}>
                    <Label htmlFor="kit-name">Release name</Label>
                    <Input id="kit-name" defaultValue="North star" />
                  </div>

                  <div style={{ display: "grid", gap: 6 }}>
                    <Label htmlFor="kit-rung">Density</Label>
                    <Select defaultValue="L">
                      <SelectTrigger id="kit-rung" aria-label="showcase rung">
                        <SelectValue />
                      </SelectTrigger>
                      <SelectContent container={container}>
                        {["XXL", "XL", "L", "M", "S"].map((rung) => (
                          <SelectItem key={rung} value={rung}>
                            {rung}
                          </SelectItem>
                        ))}
                      </SelectContent>
                    </Select>
                  </div>

                  <div
                    style={{
                      display: "flex",
                      alignItems: "center",
                      justifyContent: "space-between",
                      gap: 12,
                    }}
                  >
                    <div style={{ display: "grid", gap: 2 }}>
                      <Label htmlFor="kit-two">Automatic adaptation</Label>
                      <CardDescription>
                        Resolve nested grounds without local overrides.
                      </CardDescription>
                    </div>
                    <Switch id="kit-two" defaultChecked />
                  </div>

                  <Separator />

                  <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
                    <Button>Publish release</Button>
                    <Button variant="secondary">Preview</Button>
                    <Button variant="ghost">Save draft</Button>
                  </div>
                </CardContent>
              </Card>

              <Card>
                <CardHeader>
                  <CardTitle>System readout</CardTitle>
                  <CardDescription>
                    The relationships remain visible where their consequence is
                    used.
                  </CardDescription>
                </CardHeader>
                <CardContent>
                  <Tabs defaultValue="grounds">
                    <TabsList>
                      <TabsTrigger value="grounds">Ground</TabsTrigger>
                      <TabsTrigger value="ladders">Ladder</TabsTrigger>
                      <TabsTrigger value="states">States</TabsTrigger>
                    </TabsList>
                    <TabsContent value="grounds">
                      One resolved background descends through the tree.
                    </TabsContent>
                    <TabsContent value="ladders">
                      Foreground generates both seventeen-step ladders.
                    </TabsContent>
                    <TabsContent value="states">
                      Hover, press and focus resolve from the component ground.
                    </TabsContent>
                  </Tabs>
                </CardContent>
              </Card>
            </div>

            <div
              style={{
                display: "flex",
                justifyContent: "space-between",
                gap: 16,
                alignItems: "center",
                flexWrap: "wrap",
              }}
            >
              <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
                <Badge>ready</Badge>
                <Badge variant="secondary">computed</Badge>
                <Badge variant="outline">inherited</Badge>
                <Badge variant="destructive">must fix</Badge>
              </div>
              <CardDescription>
                Real controls · real states · real portal behaviour
              </CardDescription>
            </div>
          </CardContent>
        </Card>
      </section>
    </TooltipProvider>
  );
}
