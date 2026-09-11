import type { ResolvedModel, Rule, Stage } from "@/components/design-engine/types";

export function LogicAtlas({ model, rules, stages }: { model: ResolvedModel; rules: Rule[]; stages: Stage[] }) {
  const sizingRows = [
    ["Typography", `${model.sizes.text_rungs.length} rungs × ${model.sizes.text_styles.length} roles`, "Authored factor table", `Downscale ${model.sizes.downscale.text.M ?? "M → next rung"}`],
    ["Components", `${model.sizes.component_rungs.length} rungs × ${model.sizes.component_roles.length} roles`, "Anchor + rung step + role ratios", `Downscale ${model.sizes.downscale.components.M ?? "M → smaller rung"}`],
    ["Radius", `${Object.keys(model.sizes.radius_factors).length} roles`, `${model.sizes.radius_base}px base × factor`, "Inherited unchanged"],
  ];

  return <div className="dsc-atlas ds-n6 ds-paragraphs">
    <div className="dsc-atlas-intro"><span className="ds-n8 ds-leads">SYSTEM LOGIC / DOCUMENTED</span><h3 className="ds-h3 ds-headings">The interface is the last link in a readable chain.</h3><p className="ds-n5 ds-paragraphs">Okuro separates authorship, calculation, frame context and component behavior. The tables below expose who owns each decision and what the next layer receives.</p></div>

    <div className="dsc-stage-register" aria-label="Engine growth stages">
      {stages.map((stage, index) => <div key={stage.key}><span className="ds-n8 ds-paragraphs">{String(index + 1).padStart(2, "0")}</span><strong className="ds-n7 ds-leads">{stage.title}</strong><p className="ds-n8 ds-paragraphs">{stage.caption}</p><small className="ds-n8 ds-paragraphs">{stage.reveals.length} outputs revealed</small></div>)}
    </div>

    <section className="dsc-atlas-block"><div className="dsc-atlas-block-title"><span className="ds-n8 ds-leads">AUTHORITY MAP</span><p className="ds-n7 ds-paragraphs">Only the first row is authored. Later rows calculate, carry or consume the result.</p></div><div className="dsc-table-wrap"><table className="ds-n7 ds-paragraphs"><thead><tr><th>Layer</th><th>Owns</th><th>Receives</th><th>Produces</th></tr></thead><tbody><tr><th>System author</th><td>Identity · intent · factors</td><td>Design decisions</td><td>{model.authored.length} authored inputs</td></tr><tr><th>Engine</th><td>Threshold · derivation · lint</td><td>Authored inputs</td><td>{model.grounds.length} grounds + semantic vocabulary</td></tr><tr><th>Frame</th><td>Ground · rung · scale · brand boundary</td><td>Resolved system</td><td>Inherited context</td></tr><tr><th>Component</th><td>Behavior · state · content</td><td>Semantic roles</td><td>Product UI</td></tr></tbody></table></div></section>

    <section className="dsc-atlas-block"><div className="dsc-atlas-block-title"><span className="ds-n8 ds-leads">INHERITANCE DECISION</span><p className="ds-n7 ds-paragraphs">There is no depth counter and no component-kind branch. One resolved ground descends.</p></div><div className="dsc-table-wrap"><table className="ds-n7 ds-paragraphs"><thead><tr><th>Child request</th><th>Resolution</th><th>Reason</th></tr></thead><tbody><tr><th>No request</th><td>Parent ground</td><td>Inheritance is expressed by the absence of a new request.</td></tr><tr><th>Emphasis</th><td>Opposite neutral pole</td><td>The parent’s measured polarity chooses the alternate root.</td></tr><tr><th>Branded on neutral</th><td>Brand-full or fallback</td><td>Full brand is admitted only when its pole clears the shared threshold.</td></tr><tr><th>Branded on chromatic</th><td>Neutral fallback</td><td>The CTA adapts; the host never receives a second competing chromatic ground.</td></tr><tr><th>Signal</th><td>Signal ground</td><td>Error, success, info and warning enter the same polarity law.</td></tr></tbody></table></div></section>

    <section className="dsc-atlas-block"><div className="dsc-atlas-block-title"><span className="ds-n8 ds-leads">ROOT GROUND MATRIX</span><p className="ds-n7 ds-paragraphs">The engine measured these outcomes for the current system.</p></div><div className="dsc-table-wrap"><table className="ds-n7 ds-paragraphs"><thead><tr><th>Placement</th><th>Resolved ground</th><th>Polarity</th><th>Choice</th></tr></thead><tbody>{model.roots.map((root) => {
      const ground = model.grounds.find((item) => item.key === root.key);
      return <tr key={root.id}><th><span className="dsc-ground-dot" style={{ background: ground?.colour ?? "transparent" }} />{root.label}</th><td><code>{root.key}</code></td><td>{ground?.polarity ?? "resolved"}</td><td>{root.user_choice ? "User-selectable" : "Derived placement"}</td></tr>;
    })}</tbody></table></div></section>

    <section className="dsc-atlas-block"><div className="dsc-atlas-block-title"><span className="ds-n8 ds-leads">SIZING MODEL</span><p className="ds-n7 ds-paragraphs">The frame selects a row; descendants consume it until a nearer frame overrides it.</p></div><div className="dsc-table-wrap"><table className="ds-n7 ds-paragraphs"><thead><tr><th>System</th><th>Matrix</th><th>Authority</th><th>Context response</th></tr></thead><tbody>{sizingRows.map((row) => <tr key={row[0]}>{row.map((cell, index) => index === 0 ? <th key={cell}>{cell}</th> : <td key={cell}>{cell}</td>)}</tr>)}</tbody></table></div></section>

    <section className="dsc-atlas-block"><div className="dsc-atlas-block-title"><span className="ds-n8 ds-leads">RULE REGISTER</span><p className="ds-n7 ds-paragraphs">The complete backend-supplied rule catalogue for this engine.</p></div><div className="dsc-rule-register">{rules.map((rule) => <details key={rule.id}><summary className="ds-n7 ds-paragraphs"><code>{rule.id}</code><strong className="ds-n7 ds-leads">{rule.title}</strong><span>{rule.formula}</span></summary><p className="ds-n7 ds-paragraphs">{rule.source}</p></details>)}</div></section>
  </div>;
}
