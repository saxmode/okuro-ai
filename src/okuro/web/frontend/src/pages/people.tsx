import { useEffect, useMemo, useRef, useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { Tabs, TabsList, TabsTrigger, TabsContent } from "@/components/ui/tabs";
import { PeopleGraph, type GraphSelection } from "@/components/people/PeopleGraph";
import { RightPanel } from "@/components/people/RightPanel";
import { hydrateGraphLayout } from "@/components/people/graph-storage";
import { SidePanel, MobilePanelTrigger } from "@/components/ui/side-panel";
import { PanelRight } from "lucide-react";
import { SlidersPanel } from "@/components/people/SlidersPanel";
import { RelationsPanel } from "@/components/people/RelationsPanel";
import {
  Plus,
  Search,
  Trash2,
  Loader2,
  Sparkles,
  User as UserIcon,
  RefreshCw,
  Save,
  X,
  Download,
  Upload,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { EmptyState } from "@/components/ui/empty-state";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { toast } from "@/components/ui/toast";
import { registerSnapshotContext } from "@/lib/handover-context";
import {
  FieldLabel,
  StringListEditor,
  SubsectionHeading,
  stripEmpty,
} from "@/components/ui/form-primitives";
import {
  peopleApi,
  peopleEventsUrl,
  type PersonRecord,
  type PersonSummary,
  type PersonUpsert,
} from "@/lib/people-api";
import { cn } from "@/lib/utils";
import { parseApiDate } from "@/lib/format";

/**
 * /people — manage communication partners.
 *
 * Each person has a tone/style profile the agent uses to adapt messages.
 * Backed by /api/people/* (people.py).
 *
 * Layout: master/detail.
 *  - Left: searchable list + relation chips + "Add person".
 *  - Right: editable detail pane with inline save + Lens generation.
 */

const RELATION_TYPES = [
  "colleague",
  "friend",
  "family",
  "professional",
  "acquaintance",
  "other",
] as const;

function PersonListItem({
  person,
  selected,
  onClick,
}: {
  person: PersonSummary;
  selected: boolean;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={cn(
        "w-full rounded-md border border-transparent px-3 py-2 text-left transition-colors",
        selected
          ? "border-accent/40 bg-accent/10"
          : "hover:border-border hover:bg-surface-elevated",
      )}
    >
      <div className="flex items-center justify-between gap-2">
        <span className="truncate text-sm font-medium text-fg">
          {person.display_name}
        </span>
        {person.relation_type ? (
          <Badge variant="outline" className="shrink-0 text-2xs">
            {person.relation_type}
          </Badge>
        ) : null}
      </div>
      {(person.organization || person.role) && (
        <div className="mt-0.5 truncate text-2xs text-tertiary">
          {[person.role, person.organization].filter(Boolean).join(" — ")}
        </div>
      )}
    </button>
  );
}

function AddPersonDialog({
  open,
  onOpenChange,
  onCreated,
}: {
  open: boolean;
  onOpenChange: (v: boolean) => void;
  onCreated: (id: string) => void;
}) {
  const [displayName, setDisplayName] = useState("");
  const [relationType, setRelationType] = useState<string>("professional");
  const [organization, setOrganization] = useState("");
  const [role, setRole] = useState("");
  const [presetRoleId, setPresetRoleId] = useState<string>("");
  const [notes, setNotes] = useState("");
  const [linkedin, setLinkedin] = useState("");

  // Role-preset catalog drives the dropdown. If one is chosen and the free-text
  // role field is empty, we prefill it on create so the person card shows a
  // sensible role label. Apply-preset happens server-side after create.
  const presetsQuery = useQuery({
    queryKey: ["people", "presets"],
    queryFn: () => peopleApi.listPresets(),
    staleTime: 5 * 60 * 1000,
  });

  const mutation = useMutation({
    mutationFn: async () => {
      const effectiveRole =
        role.trim() ||
        presetsQuery.data?.presets.find((p) => p.role_id === presetRoleId)?.label ||
        "";
      const created = await peopleApi.create({
        display_name: displayName.trim(),
        relation_type: relationType,
        organization: organization.trim() || undefined,
        role: effectiveRole || undefined,
        notes: notes.trim() || undefined,
        contact: linkedin.trim() ? { linkedin: linkedin.trim() } : undefined,
      });
      if (presetRoleId) {
        // Best-effort apply — if this fails the person still exists; user can
        // retry from the Enrich pane. We surface the error as a toast.
        try {
          await peopleApi.applyPreset(created.id, presetRoleId);
        } catch (err) {
          toast.error(
            `Created, but preset failed: ${
              err instanceof Error ? err.message : "unknown"
            }`,
          );
        }
      }
      return created;
    },
    onSuccess: (data) => {
      toast.success(`Added ${data.display_name}`);
      onCreated(data.id);
      onOpenChange(false);
      setDisplayName("");
      setRelationType("professional");
      setOrganization("");
      setRole("");
      setPresetRoleId("");
      setNotes("");
      setLinkedin("");
    },
    onError: (err: Error) => {
      toast.error(err.message || "Could not add person");
    },
  });

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle>Add person</DialogTitle>
          <DialogDescription>
            Create a communication partner. You can fine-tune the profile after.
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-3">
          <div>
            <FieldLabel>Name</FieldLabel>
            <Input
              value={displayName}
              onChange={(e) => setDisplayName(e.target.value)}
              placeholder="e.g. Marco Brenner"
              autoFocus
            />
          </div>
          <div>
            <FieldLabel>LinkedIn URL (optional)</FieldLabel>
            <Input
              value={linkedin}
              onChange={(e) => setLinkedin(e.target.value)}
              placeholder="https://linkedin.com/in/…"
            />
          </div>
          <div>
            <FieldLabel>Relation type</FieldLabel>
            <Select value={relationType} onValueChange={setRelationType}>
              <SelectTrigger>
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {RELATION_TYPES.map((v) => (
                  <SelectItem key={v} value={v}>
                    {v}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
          <div className="grid grid-cols-2 gap-3">
            <div>
              <FieldLabel>Role</FieldLabel>
              <Input
                value={role}
                onChange={(e) => setRole(e.target.value)}
                placeholder="e.g. CTO"
              />
            </div>
            <div>
              <FieldLabel>Organization</FieldLabel>
              <Input
                value={organization}
                onChange={(e) => setOrganization(e.target.value)}
                placeholder="e.g. DigiConnect"
              />
            </div>
          </div>
          <div>
            <FieldLabel>
              Starter preset
              <span className="ml-1 font-normal text-fg-subtle">
                — seeds communication + cognitive shape
              </span>
            </FieldLabel>
            <Select
              value={presetRoleId || "__none__"}
              onValueChange={(v) => setPresetRoleId(v === "__none__" ? "" : v)}
            >
              <SelectTrigger>
                <SelectValue placeholder="None — fill in later" />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="__none__">None — fill in later</SelectItem>
                {presetsQuery.data?.presets.map((p) => (
                  <SelectItem key={p.role_id} value={p.role_id}>
                    {p.label}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
          <div>
            <FieldLabel>Notes</FieldLabel>
            <Textarea
              value={notes}
              onChange={(e) => setNotes(e.target.value)}
              placeholder="What should the agent know?"
              className="min-h-[72px]"
            />
          </div>
        </div>

        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)}>
            Cancel
          </Button>
          <Button
            onClick={() => mutation.mutate()}
            disabled={!displayName.trim() || mutation.isPending}
          >
            {mutation.isPending ? (
              <Loader2 className="h-3.5 w-3.5 animate-spin" />
            ) : (
              <Plus className="h-3.5 w-3.5" />
            )}
            Create
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function PersonDetail({ id }: { id: string }) {
  const queryClient = useQueryClient();
  const { data: person, isLoading } = useQuery({
    queryKey: ["person", id],
    queryFn: () => peopleApi.get(id),
  });

  const [draft, setDraft] = useState<PersonRecord | null>(null);
  const [lensContext, setLensContext] = useState("");
  const [lensResult, setLensResult] = useState<string | null>(null);

  useEffect(() => {
    setDraft(person ? { ...person } : null);
    setLensResult(null);
  }, [person, id]);

  // L2 snapshot context — expose the loaded record so a captured snapshot of this
  // page hands over the person's DATA (not just pixels) to whatever tool receives
  // it (e.g. a new task to improve the profile). Unregisters on unmount / change.
  useEffect(() => {
    if (!person) return;
    return registerSnapshotContext(() => ({
      entity: { type: "person", id, name: person.display_name },
      data: person,
    }));
  }, [person, id]);

  const saveMutation = useMutation({
    mutationFn: (payload: PersonUpsert) => peopleApi.update(id, payload),
    onSuccess: () => {
      toast.success("Saved");
      queryClient.invalidateQueries({ queryKey: ["person", id] });
      queryClient.invalidateQueries({ queryKey: ["people"] });
    },
    onError: (err: Error) => toast.error(err.message || "Save failed"),
  });

  const deleteMutation = useMutation({
    mutationFn: () => peopleApi.delete(id, true),
    onSuccess: () => {
      toast.success("Deleted");
      queryClient.invalidateQueries({ queryKey: ["people"] });
      queryClient.removeQueries({ queryKey: ["person", id] });
    },
    onError: (err: Error) => toast.error(err.message || "Delete failed"),
  });

  const lensMutation = useMutation({
    mutationFn: () => peopleApi.lens(id, lensContext.trim() || undefined),
    onSuccess: (data) => setLensResult(data.markdown),
    onError: (err: Error) => toast.error(err.message || "Lens failed"),
  });

  if (isLoading || !draft) {
    return (
      <div className="flex h-full items-center justify-center text-sm text-tertiary">
        <Loader2 className="mr-2 h-4 w-4 animate-spin" /> Loading…
      </div>
    );
  }

  // The Communication + Cognitive form sections were removed in favour
  // of SlidersPanel (Mode B). The save path still preserves any legacy
  // values written by ingest/preset paths so we don't accidentally wipe
  // them on a Save click.
  const comm = (draft.communication ?? {}) as Record<string, unknown>;
  const cog = (draft.cognitive ?? {}) as Record<string, unknown>;

  const onSave = () => {
    const commOut: Record<string, unknown> = { ...comm };
    commOut.format_preferences = stripEmpty(
      (comm.format_preferences as string[] | undefined) ?? [],
    );
    commOut.avoid = stripEmpty((comm.avoid as string[] | undefined) ?? []);

    const cogOut: Record<string, unknown> = { ...cog };
    cogOut.accessibility = stripEmpty(
      (cog.accessibility as string[] | undefined) ?? [],
    );
    cogOut.pet_peeves = stripEmpty((cog.pet_peeves as string[] | undefined) ?? []);

    const tagsOut = stripEmpty(draft.tags ?? []);

    saveMutation.mutate({
      display_name: draft.display_name,
      organization: draft.organization ?? null,
      role: draft.role ?? null,
      relation_to_user: draft.relation_to_user ?? null,
      relation_type: draft.relation_type ?? "professional",
      notes: draft.notes ?? null,
      communication: commOut,
      cognitive: cogOut,
      contact: draft.contact ?? {},
      tags: tagsOut,
    });
  };

  const updatedAt = draft.updated_at
    ? parseApiDate(draft.updated_at).toLocaleString()
    : "—";

  return (
    <div className="flex h-full flex-col overflow-hidden">
      {/* Header */}
      <div className="flex items-start justify-between border-b border-border bg-surface px-5 py-3">
        <div className="min-w-0">
          <div className="flex items-center gap-2">
            <Input
              value={draft.display_name ?? ""}
              onChange={(e) => setDraft({ ...draft, display_name: e.target.value })}
              className="h-8 max-w-sm text-base font-semibold"
            />
            <Badge variant="outline">{draft.relation_type ?? "—"}</Badge>
          </div>
          <div className="mt-1 text-2xs text-tertiary">
            id: <span className="font-mono">{draft.id}</span> · updated {updatedAt}
          </div>
        </div>
        <div className="flex gap-2">
          <Button
            variant="outline"
            size="sm"
            onClick={() => deleteMutation.mutate()}
            disabled={deleteMutation.isPending}
          >
            <Trash2 className="h-3.5 w-3.5" />
            Delete
          </Button>
          <Button onClick={onSave} disabled={saveMutation.isPending} size="sm">
            {saveMutation.isPending ? (
              <Loader2 className="h-3.5 w-3.5 animate-spin" />
            ) : (
              <Save className="h-3.5 w-3.5" />
            )}
            Save
          </Button>
        </div>
      </div>

      {/* Body */}
      <div className="flex-1 overflow-y-auto px-5 py-4">
        <div className="mx-auto max-w-3xl space-y-6">
          {/* Core fields */}
          <section className="space-y-3">
            <SubsectionHeading
              title="Core"
              hint="Who this person is and how you relate."
            />
            <div className="grid grid-cols-2 gap-3">
              <div>
                <FieldLabel>Role</FieldLabel>
                <Input
                  value={draft.role ?? ""}
                  onChange={(e) => setDraft({ ...draft, role: e.target.value })}
                />
              </div>
              <div>
                <FieldLabel>Organization</FieldLabel>
                <Input
                  value={draft.organization ?? ""}
                  onChange={(e) =>
                    setDraft({ ...draft, organization: e.target.value })
                  }
                />
              </div>
              <div>
                <FieldLabel>Relation type</FieldLabel>
                <Select
                  value={draft.relation_type ?? "professional"}
                  onValueChange={(v) => setDraft({ ...draft, relation_type: v })}
                >
                  <SelectTrigger>
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    {RELATION_TYPES.map((v) => (
                      <SelectItem key={v} value={v}>
                        {v}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </div>
              <div>
                <FieldLabel>Relation to user</FieldLabel>
                <Input
                  value={draft.relation_to_user ?? ""}
                  onChange={(e) =>
                    setDraft({ ...draft, relation_to_user: e.target.value })
                  }
                  placeholder="e.g. my product manager"
                />
              </div>
            </div>
          </section>

          {/* Relations — hats / connections / engagements (migration 056).
              Additive: backed by the isolated /api/crm surface, independent of
              the person CRUD above. */}
          <section className="space-y-3">
            <SubsectionHeading
              title="Relations"
              hint="Multiple hats, your connection, and per-project engagements. Resolve = this person's language + the company's design."
            />
            <RelationsPanel personId={draft.id} />
          </section>

          {/* Profile — Mode B sliders. Replaces the prior 15-dropdown
              Communication + Cognitive form (deemed too noisy + too
              manual). Pairwise (Mode C) + Q-by-Q (Mode A) tabs are
              planned next; this is the first one we ship. */}
          <section className="space-y-3">
            <SubsectionHeading
              title="Profile"
              hint="8 axes that drive how content gets reshaped for this recipient. Click a number to set; saves immediately."
            />
            <SlidersPanel personId={draft.id} role={draft.role} />
          </section>

          {/* Tags */}
          <section className="space-y-3">
            <SubsectionHeading title="Tags" hint="Free-form labels for search." />
            <StringListEditor
              value={draft.tags ?? []}
              onChange={(next) => setDraft({ ...draft, tags: next })}
              placeholder="e.g. client"
            />
          </section>

          {/* Notes */}
          <section className="space-y-3">
            <SubsectionHeading title="Notes" />
            <Textarea
              value={draft.notes ?? ""}
              onChange={(e) => setDraft({ ...draft, notes: e.target.value })}
              className="min-h-[120px]"
              placeholder="Free-form notes the agent can reference."
            />
          </section>

          {/* Lens */}
          <section className="space-y-3 rounded-md border border-border bg-surface px-4 py-3">
            <SubsectionHeading
              title="Communication lens"
              hint="Generate tailored guidance the agent can apply before replying."
            />
            <div>
              <FieldLabel>Context (optional)</FieldLabel>
              <Input
                value={lensContext}
                onChange={(e) => setLensContext(e.target.value)}
                placeholder="e.g. email about Q2 numbers"
              />
            </div>
            <div>
              <Button
                size="sm"
                onClick={() => lensMutation.mutate()}
                disabled={lensMutation.isPending}
              >
                {lensMutation.isPending ? (
                  <Loader2 className="h-3.5 w-3.5 animate-spin" />
                ) : (
                  <Sparkles className="h-3.5 w-3.5" />
                )}
                Generate guidance
              </Button>
            </div>
            {lensResult ? (
              <pre className="mt-2 max-h-72 overflow-auto whitespace-pre-wrap rounded bg-surface-subtle px-3 py-2 text-xs text-fg">
                {lensResult}
              </pre>
            ) : null}
          </section>
        </div>
      </div>
    </div>
  );
}

function PeopleSettingsView() {
  const queryClient = useQueryClient();
  const [query, setQuery] = useState("");
  const [relationFilter, setRelationFilter] = useState<string | null>(null);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  // Mobile: detail lives in a right sheet, opened on tap (not on auto-select).
  const [detailOpen, setDetailOpen] = useState(false);
  const [addOpen, setAddOpen] = useState(false);

  const { data, isLoading, refetch, isFetching } = useQuery({
    queryKey: ["people"],
    queryFn: () => peopleApi.list(),
  });

  // Live-sync: refetch when ANY process mutates a person — including agent
  // writes from the separate stdio MCP process (the user's actual case). The
  // server tails persons_events (migration 066) over SSE.
  useEffect(() => {
    let es: EventSource | null = null;
    let closed = false;
    (async () => {
      const url = await peopleEventsUrl();
      if (closed || !url) return;
      es = new EventSource(url);
      const onEvt = (ev: MessageEvent) => {
        let d: { person_id?: string } = {};
        try {
          d = JSON.parse(ev.data);
        } catch {
          /* ignore malformed frame */
        }
        // Refresh the list; also refresh the open detail if it changed.
        queryClient.invalidateQueries({ queryKey: ["people"] });
        if (d.person_id) {
          queryClient.invalidateQueries({ queryKey: ["person", d.person_id] });
        }
      };
      es.addEventListener("added", onEvt);
      es.addEventListener("updated", onEvt);
      es.addEventListener("deleted", onEvt);
    })();
    return () => {
      closed = true;
      es?.close();
    };
  }, [queryClient]);

  const people = data?.people ?? [];

  const relations = useMemo(() => {
    const set = new Set<string>();
    for (const p of people) if (p.relation_type) set.add(p.relation_type);
    return Array.from(set).sort();
  }, [people]);

  const filtered = useMemo(() => {
    let out = people;
    if (relationFilter) {
      out = out.filter((p) => p.relation_type === relationFilter);
    }
    if (query.trim()) {
      const q = query.toLowerCase();
      out = out.filter((p) =>
        [p.display_name, p.organization, p.role, p.relation_to_user]
          .filter(Boolean)
          .some((f) => String(f).toLowerCase().includes(q)),
      );
    }
    return out;
  }, [people, query, relationFilter]);

  // Auto-select first person if nothing selected.
  useEffect(() => {
    if (!selectedId && filtered.length > 0) {
      setSelectedId(filtered[0]!.id);
    }
  }, [selectedId, filtered]);

  // ── Offline survey: download blank, import filled ──
  const fileInput = useRef<HTMLInputElement>(null);

  // Which language the blank survey is generated in. Persisted per browser
  // so the choice survives a reload — sending in one language is usually a
  // run, not a one-off.
  const [surveyLang, setSurveyLang] = useState(
    () => localStorage.getItem("okuro.surveyLang") || "en",
  );
  const surveyLangs = useQuery({
    queryKey: ["survey-languages"],
    queryFn: () => peopleApi.surveyLanguages(),
    staleTime: 5 * 60_000,
  });
  const langs = surveyLangs.data?.languages ?? [];
  const chosen = langs.find((l) => l.code === surveyLang);

  const downloadSurvey = useMutation({
    // An unreviewed translation needs the caller to say so on purpose. The
    // picker already shows which are unchecked, so by the time this fires the
    // choice is informed — the flag records it rather than re-asking.
    mutationFn: () =>
      peopleApi.downloadBlankSurvey(surveyLang, chosen?.reviewed === false),
    onSuccess: () =>
      toast.success(
        chosen?.reviewed === false
          ? `Survey downloaded in ${chosen.label} — translation not yet reviewed`
          : `Survey downloaded in ${chosen?.label ?? surveyLang}`,
      ),
    onError: (err: Error) => toast.error(err.message || "Download failed"),
  });

  const importSurvey = useMutation({
    mutationFn: async (file: File) => {
      const parsed = JSON.parse(await file.text());
      return peopleApi.importFilledSurvey(parsed);
    },
    onSuccess: (res) => {
      toast.success(
        `${res.created ? "Created" : "Updated"} ${res.display_name || res.person_id} — ${
          (res.measured_axes || []).length
        } axes${res.over_claim_flags ? ` · ${res.over_claim_flags} over-claim flagged` : ""}`,
      );
      queryClient.invalidateQueries({ queryKey: ["people"] });
      setSelectedId(res.person_id);
    },
    onError: (err: Error) =>
      toast.error(err.message || "Could not import that file"),
  });

  function onPickFile(e: React.ChangeEvent<HTMLInputElement>) {
    const file = e.target.files?.[0];
    if (file) importSurvey.mutate(file);
    e.target.value = ""; // allow re-importing the same filename
  }

  return (
    <div className="flex h-full overflow-hidden">
      {/* Left pane — list (full-width on mobile, fixed rail on desktop) */}
      <div className="flex w-full flex-col border-r border-border md:w-80 md:shrink-0">
        <div className="space-y-3 border-b border-border px-4 py-3">
          <div className="flex items-center justify-between">
            <h1 className="text-lg font-semibold text-fg">People</h1>
            <div className="flex gap-1">
              <Button
                size="sm"
                variant="ghost"
                onClick={() => refetch()}
                disabled={isFetching}
                aria-label="Refresh"
              >
                <RefreshCw
                  className={cn("h-3.5 w-3.5", isFetching && "animate-spin")}
                />
              </Button>
              <select
                className="h-8 rounded-md border border-border bg-surface-subtle px-2 text-xs text-fg outline-none focus:border-accent/60"
                value={surveyLang}
                onChange={(e) => {
                  setSurveyLang(e.target.value);
                  localStorage.setItem("okuro.surveyLang", e.target.value);
                }}
                aria-label="Survey language"
                title="Language the blank survey is generated in"
              >
                {langs.map((l) => (
                  <option key={l.code} value={l.code}>
                    {/* An asterisk, not a hidden option: an unreviewed
                        translation is sendable, just not silently. */}
                    {l.label}{l.reviewed ? "" : " *"}
                  </option>
                ))}
              </select>
              <Button
                size="sm"
                variant="ghost"
                onClick={() => downloadSurvey.mutate()}
                disabled={downloadSurvey.isPending}
                aria-label="Download blank survey"
                title={
                  chosen?.reviewed === false
                    ? `Download in ${chosen.label} — translation not yet reviewed by a native speaker`
                    : `Download a blank survey in ${chosen?.label ?? "English"}`
                }
              >
                <Download className="h-3.5 w-3.5" />
              </Button>
              <Button
                size="sm"
                variant="ghost"
                onClick={() => fileInput.current?.click()}
                disabled={importSurvey.isPending}
                aria-label="Import filled survey"
                title="Import a filled survey file (creates the person)"
              >
                {importSurvey.isPending ? (
                  <Loader2 className="h-3.5 w-3.5 animate-spin" />
                ) : (
                  <Upload className="h-3.5 w-3.5" />
                )}
              </Button>
              <input
                ref={fileInput}
                type="file"
                accept="application/json,.json"
                className="hidden"
                onChange={onPickFile}
              />
              <Button size="sm" onClick={() => setAddOpen(true)}>
                <Plus className="h-3.5 w-3.5" />
                Add
              </Button>
            </div>
          </div>

          <div className="relative">
            <Search className="pointer-events-none absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-tertiary" />
            <Input
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder="Search name, role, notes…"
              className="h-8 pl-8 text-xs"
            />
            {query && (
              <button
                type="button"
                onClick={() => setQuery("")}
                className="absolute right-2 top-1/2 -translate-y-1/2 text-tertiary hover:text-fg"
                aria-label="Clear search"
              >
                <X className="h-3 w-3" />
              </button>
            )}
          </div>

          {relations.length > 0 && (
            <div className="flex flex-wrap gap-1">
              <button
                type="button"
                onClick={() => setRelationFilter(null)}
                className={cn(
                  "rounded-full border px-2 py-0.5 text-2xs uppercase tracking-wider",
                  relationFilter === null
                    ? "border-accent bg-accent/10 text-accent"
                    : "border-border text-tertiary hover:text-fg",
                )}
              >
                all
              </button>
              {relations.map((r) => (
                <button
                  key={r}
                  type="button"
                  onClick={() => setRelationFilter(r)}
                  className={cn(
                    "rounded-full border px-2 py-0.5 text-2xs uppercase tracking-wider",
                    relationFilter === r
                      ? "border-accent bg-accent/10 text-accent"
                      : "border-border text-tertiary hover:text-fg",
                  )}
                >
                  {r}
                </button>
              ))}
            </div>
          )}
        </div>

        <div className="flex-1 overflow-y-auto p-2">
          {isLoading ? (
            <div className="flex h-full items-center justify-center text-xs text-tertiary">
              <Loader2 className="mr-2 h-3.5 w-3.5 animate-spin" /> Loading…
            </div>
          ) : filtered.length === 0 ? (
            <EmptyState
              icon={<UserIcon className="h-10 w-10" />}
              title="No one added yet"
              description="Add someone you talk to often — agents use this context when drafting messages."
              action={
                <Button size="sm" onClick={() => setAddOpen(true)}>
                  <Plus className="h-3.5 w-3.5" />
                  Add person
                </Button>
              }
            />
          ) : (
            <div className="space-y-1">
              {filtered.map((p) => (
                <PersonListItem
                  key={p.id}
                  person={p}
                  selected={p.id === selectedId}
                  onClick={() => { setSelectedId(p.id); setDetailOpen(true); }}
                />
              ))}
            </div>
          )}
        </div>
      </div>

      {/* Right pane — detail (inline rail on desktop, sheet on mobile) */}
      <SidePanel
        side="right"
        desktopClassName="flex flex-1 flex-col overflow-hidden"
        contentClassName="p-0"
        sheetClassName="w-[min(440px,94vw)]"
        open={detailOpen}
        onOpenChange={setDetailOpen}
        title="Person details"
      >
        {selectedId ? (
          <PersonDetail id={selectedId} />
        ) : (
          <div className="flex h-full items-center justify-center text-sm text-tertiary">
            Select or add a person
          </div>
        )}
      </SidePanel>

      <AddPersonDialog
        open={addOpen}
        onOpenChange={setAddOpen}
        onCreated={(id) => {
          setSelectedId(id);
          queryClient.invalidateQueries({ queryKey: ["people"] });
        }}
      />
    </div>
  );
}

/**
 * PeoplePage — graph-first view. The list-based editor from before is still
 * reachable under the "Detailed settings" tab; the graph is the default
 * landing because it reflects the feature's core intent: one person, one
 * edge, one translation path at a time.
 */

function PeopleGraphView() {
  const queryClient = useQueryClient();
  const [selection, setSelection] = useState<GraphSelection | null>(null);
  const [addOpen, setAddOpen] = useState(false);
  // Mobile: the detail rail lives in a right sheet, auto-opened on select.
  const [detailOpen, setDetailOpen] = useState(false);
  const handleSelect = (s: GraphSelection | null) => {
    setSelection(s);
    if (s) setDetailOpen(true);
  };

  const graphQuery = useQuery({
    queryKey: ["people", "graph"],
    queryFn: () => peopleApi.graph(),
  });

  // Pull the stored arrangement into localStorage BEFORE PeopleGraph
  // mounts. Its positions/groups/settings state comes from synchronous
  // useState/useRef initialisers, so hydrating afterwards would paint the
  // default layout first and could push it back over the server copy.
  // Never retried and never refetched: this runs once per mount, and a
  // refetch mid-session would clobber edits the user is making right now.
  const layoutQuery = useQuery({
    queryKey: ["people", "layout"],
    queryFn: async () => {
      await hydrateGraphLayout();
      return true;
    },
    staleTime: Infinity,
    retry: false,
    refetchOnWindowFocus: false,
  });

  const invalidateGraph = () => {
    queryClient.invalidateQueries({ queryKey: ["people", "graph"] });
  };

  if (graphQuery.isPending || layoutQuery.isPending) {
    return (
      <div className="flex h-full items-center justify-center text-xs text-tertiary">
        <Loader2 className="mr-2 h-3.5 w-3.5 animate-spin" /> Loading graph…
      </div>
    );
  }

  if (graphQuery.isError || !graphQuery.data) {
    return (
      <div className="flex h-full items-center justify-center p-6">
        <EmptyState
          icon={<UserIcon className="h-10 w-10" />}
          title="Couldn't load the graph"
          description={
            graphQuery.error instanceof Error
              ? graphQuery.error.message
              : "Unknown error"
          }
        />
      </div>
    );
  }

  const data = graphQuery.data;
  const hasPeople = data.nodes.length > 0;

  return (
    <div className="flex h-full min-h-0 overflow-hidden">
      <div className="relative flex-1 min-w-0">
        {hasPeople ? (
          <PeopleGraph
            graph={data}
            selection={selection}
            onSelect={handleSelect}
          />
        ) : (
          <div className="flex h-full items-center justify-center p-6">
            <EmptyState
              icon={<UserIcon className="h-10 w-10" />}
              title="No one on your graph yet"
              description="Add your first person — a CEO, a colleague, your partner. They'll appear connected to you."
              action={
                <Button size="sm" onClick={() => setAddOpen(true)}>
                  <Plus className="h-3.5 w-3.5" />
                  Add person
                </Button>
              }
            />
          </div>
        )}
        {/* Floating Add — always present on the graph pane so new connections
            can be made without jumping tabs. On create we invalidate the
            graph query; the new node renders orbiting me on the next paint. */}
        {hasPeople && (
          <div className="pointer-events-none absolute right-4 top-4 z-10 flex items-center gap-2">
            <MobilePanelTrigger
              icon={<PanelRight className="h-3 w-3" />}
              label="Details"
              onClick={() => setDetailOpen(true)}
              className="pointer-events-auto bg-surface shadow-md"
            />
            <Button
              size="sm"
              className="pointer-events-auto shadow-md"
              onClick={() => setAddOpen(true)}
            >
              <Plus className="h-3.5 w-3.5" />
              Add person
            </Button>
          </div>
        )}
      </div>
      <SidePanel
        side="right"
        desktopClassName="w-96 shrink-0 border-l border-border bg-surface"
        contentClassName="p-0"
        open={detailOpen}
        onOpenChange={setDetailOpen}
        title="Details"
      >
        <RightPanel
          graph={data}
          selection={selection}
          onStatsInvalidate={invalidateGraph}
        />
      </SidePanel>

      <AddPersonDialog
        open={addOpen}
        onOpenChange={setAddOpen}
        onCreated={(id) => {
          invalidateGraph();
          queryClient.invalidateQueries({ queryKey: ["people"] });
          setSelection({ type: "person", id });
        }}
      />
    </div>
  );
}

export function PeoplePage() {
  return (
    <Tabs defaultValue="graph" className="flex h-full flex-col overflow-hidden">
      <div className="flex items-center justify-between border-b border-border px-4 pt-3">
        <h1 className="text-lg font-semibold text-fg">People</h1>
        <TabsList>
          <TabsTrigger value="graph">Graph</TabsTrigger>
          <TabsTrigger value="settings">Detailed settings</TabsTrigger>
        </TabsList>
      </div>
      <TabsContent
        value="graph"
        className="min-h-0 flex-1 overflow-hidden data-[state=inactive]:hidden"
      >
        <PeopleGraphView />
      </TabsContent>
      <TabsContent
        value="settings"
        className="min-h-0 flex-1 overflow-hidden data-[state=inactive]:hidden"
      >
        <PeopleSettingsView />
      </TabsContent>
    </Tabs>
  );
}
