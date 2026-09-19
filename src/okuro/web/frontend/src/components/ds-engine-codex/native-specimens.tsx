/**
 * THE PRODUCT SURFACE THIS PAGE SHOWS OFF — and nothing else, since wave 4.
 *
 * `ComponentSpecimen` used to live here: a switch statement over 33 cases,
 * deciding a second time what a `Select` or a `Dialog` specimen looks like while
 * `components/design-authoring/component-showcase.tsx` decided it for the other
 * page. Two answers to one question, already one variant apart. The page now
 * selects a specimen out of the shared scene (`ComponentShowcase only=`), so
 * this file is back to the one thing it alone does.
 *
 * `LiveDemo` is NOT that duplicate. It is a composed product page — a header, a
 * hero, an activity card, a metric row — whose job is to show a real interface
 * wearing the selected system. The showcase demonstrates primitives; this
 * demonstrates a product.
 */
import { useState } from "react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardAction, CardContent, CardDescription, CardFooter, CardHeader, CardTitle } from "@/components/ui/card";
import { MetricCard } from "@/components/ui/metric-card";
import { Progress } from "@/components/ui/progress";
import { Row } from "@/components/ui/row";
import { StatusBadge } from "@/components/ui/status-badge";
import { Switch } from "@/components/ui/switch";

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
