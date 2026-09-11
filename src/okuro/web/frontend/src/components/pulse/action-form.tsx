import { useEffect, useMemo, useState } from "react";
import { getActionSpec, dispatchChatAction, type ActionParam } from "@/lib/page-context";
import { api } from "@/lib/api";

/**
 * ActionForm — inline option form rendered in the chat when the agent emits a
 * ⟦FORM <fn>⟧ marker. Widgets are built dynamically from the action's declared
 * param spec (segmented control / toggle / picker / text). On submit it
 * dispatches the action with the collected params.
 */
export function ActionForm({
  name,
  context,
  onActivity,
}: {
  name: string;
  context: string;
  onActivity?: (msg: string) => void;
}) {
  const spec = getActionSpec(name);
  const [vals, setVals] = useState<Record<string, unknown>>({});
  const [people, setPeople] = useState<{ id: string; label: string }[]>([]);
  const [brands, setBrands] = useState<{ id: string; label: string }[]>([]);
  const [submitted, setSubmitted] = useState(false);

  // seed defaults + bind the freeform context to the contextKey param
  useEffect(() => {
    if (!spec) return;
    const seed: Record<string, unknown> = {};
    for (const p of spec.params) if (p.default !== undefined) seed[p.key] = p.default;
    if (spec.contextKey && context) seed[spec.contextKey] = context;
    setVals(seed);
  }, [spec, context]);

  const needsPeople = useMemo(() => spec?.params.some((p) => p.kind === "person"), [spec]);
  const needsBrands = useMemo(() => spec?.params.some((p) => p.kind === "brand"), [spec]);

  useEffect(() => {
    if (needsPeople)
      api<{ id: string; display_name?: string; name?: string }[]>("/api/people")
        .then((r) => setPeople((Array.isArray(r) ? r : []).map((p) => ({ id: p.id, label: p.display_name || p.name || p.id }))))
        .catch(() => setPeople([]));
    if (needsBrands)
      api<{ brands: { id: string; name?: string }[] }>("/api/stack/brands")
        .then((r) => setBrands((r.brands ?? []).map((b) => ({ id: b.id, label: b.name || b.id }))))
        .catch(() => setBrands([]));
  }, [needsPeople, needsBrands]);

  if (!spec) return null;

  const set = (k: string, v: unknown) => setVals((s) => ({ ...s, [k]: v }));

  const submit = () => {
    setSubmitted(true);
    const target = (spec.contextKey ? String(vals[spec.contextKey] ?? context) : context) || context;
    void dispatchChatAction({ name, target }, onActivity, vals);
  };

  const widget = (p: ActionParam) => {
    const v = vals[p.key];
    if (p.kind === "segmented" || p.kind === "select") {
      const choices = p.kind === "select" ? p.choices ?? [] : p.choices ?? [];
      if (p.kind === "segmented")
        return (
          <div className="flex gap-1">
            {choices.map((c) => (
              <button key={c.value} disabled={submitted} onClick={() => set(p.key, c.value)}
                className={`rounded px-2 py-1 text-xs transition-colors ${v === c.value ? "bg-accent/20 text-accent" : "border border-border text-fg hover:text-accent"}`}>
                {c.label}
              </button>
            ))}
          </div>
        );
      return (
        <select disabled={submitted} value={String(v ?? "")} onChange={(e) => set(p.key, e.target.value)}
          className="rounded border border-border bg-transparent px-2 py-1 text-xs text-fg">
          {choices.map((c) => <option key={c.value} value={c.value} className="bg-background text-fg">{c.label}</option>)}
        </select>
      );
    }
    if (p.kind === "toggle")
      return (
        <button disabled={submitted} onClick={() => set(p.key, !v)}
          className={`rounded-full px-3 py-0.5 text-xs ${v ? "bg-accent/20 text-accent" : "border border-border text-tertiary"}`}>
          {v ? "on" : "off"}
        </button>
      );
    if (p.kind === "person" || p.kind === "brand") {
      const opts = p.kind === "person" ? people : brands;
      return (
        <select disabled={submitted} value={String(v ?? "")} onChange={(e) => set(p.key, e.target.value)}
          className="min-w-[140px] rounded border border-border bg-transparent px-2 py-1 text-xs text-fg">
          <option value="" className="bg-background text-fg">— none —</option>
          {opts.map((o) => <option key={o.id} value={o.id} className="bg-background text-fg">{o.label}</option>)}
        </select>
      );
    }
    if (p.kind === "number")
      return <input type="number" disabled={submitted} value={Number(v ?? 0)} onChange={(e) => set(p.key, Number(e.target.value))}
        className="w-20 rounded border border-border bg-transparent px-2 py-1 text-xs text-fg" />;
    // text
    return <input disabled={submitted} value={String(v ?? "")} placeholder={p.placeholder} onChange={(e) => set(p.key, e.target.value)}
      className="min-w-[180px] flex-1 rounded border border-border bg-transparent px-2 py-1 text-xs text-fg placeholder:text-tertiary" />;
  };

  return (
    <div className="max-w-[95%] self-start rounded-lg border border-accent/30 bg-accent/5 p-3">
      <div className="mb-2 text-xs font-semibold uppercase tracking-wide text-accent">{spec.title || name}</div>
      <div className="flex flex-col gap-2">
        {spec.params.map((p) => (
          <label key={p.key} className="flex items-center justify-between gap-3 text-xs text-tertiary">
            <span className="shrink-0">{p.label}</span>
            {widget(p)}
          </label>
        ))}
      </div>
      <button disabled={submitted} onClick={submit}
        className="mt-3 rounded-md bg-accent/15 px-3 py-1 text-sm text-accent hover:bg-accent/25 disabled:opacity-50">
        {submitted ? "✓ sent" : spec.submitLabel || "Go"}
      </button>
    </div>
  );
}
