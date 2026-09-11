import { useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { Plus, Trash2, Star, Loader2, Sparkles, Building2 } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Input } from "@/components/ui/input";
import { EmptyState } from "@/components/ui/empty-state";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { toast } from "@/components/ui/toast";
import { FieldLabel, SubsectionHeading } from "@/components/ui/form-primitives";
import { crmApi, type AffiliationInput } from "@/lib/crm-api";

/**
 * RelationsPanel — the relational layer of a person (migration 056):
 *   Hats (affiliations) · Connections · Engagements + a resolve preview.
 *
 * Purely additive: dropped into PersonDetail as extra <section> blocks. It only
 * reads/writes the /api/crm surface — it never touches person CRUD, so the
 * existing detail pane is unaffected.
 */

// Mirrors ROLE_DEFAULT_SLIDERS keys (peer/cognitive_profile.py) — seeds slider
// priors for a hat. "" = none.
const ROLE_CLASSES = [
  "ceo", "cto", "engineer", "designer",
  "product_manager", "marketer", "sales", "client",
] as const;

// Mirrors KNOWN_CONNECTION_TYPES (peer/crm.py).
const CONNECTION_TYPES = [
  "co-shareholder", "business-partner", "customer", "client", "vendor",
  "supplier", "board-peer", "investor", "advisor", "colleague",
  "friend", "family", "acquaintance", "other",
] as const;

const NONE = "__none__";

function Row({ children }: { children: React.ReactNode }) {
  return (
    <div className="flex items-center justify-between gap-2 rounded-md border border-border bg-surface px-3 py-2">
      {children}
    </div>
  );
}

// ── Add-company dialog (reused from the hats add form) ──────────────────────

function AddCompanyDialog({
  open,
  onOpenChange,
  onCreated,
}: {
  open: boolean;
  onOpenChange: (v: boolean) => void;
  onCreated: (companyId: string) => void;
}) {
  const queryClient = useQueryClient();
  const [name, setName] = useState("");
  const [brandId, setBrandId] = useState<string>("");
  const [domain, setDomain] = useState("");

  const brandsQuery = useQuery({
    queryKey: ["crm", "brands"],
    queryFn: () => crmApi.listBrands(),
    staleTime: 5 * 60 * 1000,
  });

  const mutation = useMutation({
    mutationFn: () =>
      crmApi.createCompany({
        name: name.trim(),
        brand_id: brandId || undefined,
        domain: domain.trim() || undefined,
      }),
    onSuccess: (data) => {
      toast.success(data.message);
      queryClient.invalidateQueries({ queryKey: ["crm", "companies"] });
      const created = data.companies.find((c) => c.name === name.trim());
      if (created) onCreated(created.id);
      onOpenChange(false);
      setName("");
      setBrandId("");
      setDomain("");
    },
    onError: (err: Error) => toast.error(err.message || "Could not add company"),
  });

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle>Add company</DialogTitle>
          <DialogDescription>
            The entity that owns a design identity. Its brand drives the visual
            language; the person drives the information architecture.
          </DialogDescription>
        </DialogHeader>
        <div className="space-y-3">
          <div>
            <FieldLabel>Name</FieldLabel>
            <Input
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="e.g. Northwind"
              autoFocus
            />
          </div>
          <div className="grid grid-cols-2 gap-3">
            <div>
              <FieldLabel>
                Brand
                <span className="ml-1 font-normal text-fg-subtle">— design profile</span>
              </FieldLabel>
              <Select
                value={brandId || NONE}
                onValueChange={(v) => setBrandId(v === NONE ? "" : v)}
              >
                <SelectTrigger>
                  <SelectValue placeholder="None yet" />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value={NONE}>None yet</SelectItem>
                  {brandsQuery.data?.brands.map((b) => (
                    <SelectItem key={b.id} value={b.id}>
                      {b.name}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
            <div>
              <FieldLabel>Domain</FieldLabel>
              <Input
                value={domain}
                onChange={(e) => setDomain(e.target.value)}
                placeholder="example.com"
              />
            </div>
          </div>
        </div>
        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)}>
            Cancel
          </Button>
          <Button
            onClick={() => mutation.mutate()}
            disabled={!name.trim() || mutation.isPending}
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

// ── Hats (affiliations) ─────────────────────────────────────────────────────

function HatsSection({ personId }: { personId: string }) {
  const queryClient = useQueryClient();
  const [adding, setAdding] = useState(false);
  const [companyId, setCompanyId] = useState("");
  const [role, setRole] = useState("");
  const [roleClass, setRoleClass] = useState("");
  const [companyDialog, setCompanyDialog] = useState(false);

  const key = ["crm", "affiliations", personId];
  const { data, isLoading } = useQuery({
    queryKey: key,
    queryFn: () => crmApi.listAffiliations(personId),
  });
  const companiesQuery = useQuery({
    queryKey: ["crm", "companies"],
    queryFn: () => crmApi.listCompanies(),
  });

  const invalidatePeople = () => {
    // Primary hat feeds the persons.org/role cache server-side — refresh lists.
    queryClient.invalidateQueries({ queryKey: ["people"] });
    queryClient.invalidateQueries({ queryKey: ["person", personId] });
  };

  const add = useMutation({
    mutationFn: () => {
      const body: AffiliationInput = { company_id: companyId };
      if (role.trim()) body.role = role.trim();
      if (roleClass) body.role_class = roleClass;
      return crmApi.addAffiliation(personId, body);
    },
    onSuccess: (res) => {
      queryClient.setQueryData(key, { affiliations: res.affiliations });
      invalidatePeople();
      setAdding(false);
      setCompanyId("");
      setRole("");
      setRoleClass("");
    },
    onError: (e: Error) => toast.error(e.message || "Could not add hat"),
  });

  const setPrimary = useMutation({
    mutationFn: (id: string) => crmApi.setPrimaryAffiliation(personId, id),
    onSuccess: (res) => {
      queryClient.setQueryData(key, { affiliations: res.affiliations });
      invalidatePeople();
    },
    onError: (e: Error) => toast.error(e.message),
  });

  const remove = useMutation({
    mutationFn: (id: string) => crmApi.removeAffiliation(personId, id),
    onSuccess: (res) => {
      queryClient.setQueryData(key, { affiliations: res.affiliations });
      invalidatePeople();
    },
    onError: (e: Error) => toast.error(e.message),
  });

  const hats = data?.affiliations ?? [];
  const companies = companiesQuery.data?.companies ?? [];

  return (
    <section className="space-y-3">
      <div className="flex items-center justify-between">
        <SubsectionHeading
          title="Hats"
          hint="Roles this person holds at companies. ★ = headline (drives the list label + design profile)."
        />
        <Button size="sm" variant="ghost" onClick={() => setAdding((v) => !v)}>
          <Plus className="h-3.5 w-3.5" />
          Add hat
        </Button>
      </div>

      {isLoading ? (
        <div className="flex items-center gap-2 text-xs text-tertiary">
          <Loader2 className="h-3.5 w-3.5 animate-spin" /> Loading…
        </div>
      ) : hats.length === 0 && !adding ? (
        <p className="text-2xs text-tertiary">
          No hats yet. A hat ties this person to a company and a role.
        </p>
      ) : (
        <div className="space-y-1.5">
          {hats.map((h) => (
            <Row key={h.id}>
              <div className="flex min-w-0 items-center gap-2">
                {h.is_primary ? (
                  <Star className="h-3.5 w-3.5 shrink-0 fill-accent text-accent" />
                ) : (
                  <Building2 className="h-3.5 w-3.5 shrink-0 text-tertiary" />
                )}
                <span className="truncate text-sm text-fg">
                  {h.role || "—"}{" "}
                  <span className="text-tertiary">· {h.company_name}</span>
                </span>
                {h.role_class ? (
                  <Badge variant="outline" className="shrink-0 text-2xs">
                    {h.role_class}
                  </Badge>
                ) : null}
              </div>
              <div className="flex shrink-0 items-center gap-1">
                {!h.is_primary && (
                  <Button
                    size="sm"
                    variant="ghost"
                    onClick={() => setPrimary.mutate(h.id)}
                    disabled={setPrimary.isPending}
                    aria-label="Set as primary hat"
                  >
                    <Star className="h-3.5 w-3.5" />
                  </Button>
                )}
                <Button
                  size="sm"
                  variant="ghost"
                  onClick={() => remove.mutate(h.id)}
                  disabled={remove.isPending}
                  aria-label="Remove hat"
                >
                  <Trash2 className="h-3.5 w-3.5" />
                </Button>
              </div>
            </Row>
          ))}
        </div>
      )}

      {adding && (
        <div className="space-y-2 rounded-md border border-border bg-surface-subtle px-3 py-3">
          <div className="grid grid-cols-2 gap-2">
            <div>
              <FieldLabel>Company</FieldLabel>
              <Select
                value={companyId || ""}
                onValueChange={(v) =>
                  v === "__new__" ? setCompanyDialog(true) : setCompanyId(v)
                }
              >
                <SelectTrigger>
                  <SelectValue placeholder="Pick a company" />
                </SelectTrigger>
                <SelectContent>
                  {companies.map((c) => (
                    <SelectItem key={c.id} value={c.id}>
                      {c.name}
                    </SelectItem>
                  ))}
                  <SelectItem value="__new__">+ New company…</SelectItem>
                </SelectContent>
              </Select>
            </div>
            <div>
              <FieldLabel>Role</FieldLabel>
              <Input
                value={role}
                onChange={(e) => setRole(e.target.value)}
                placeholder="e.g. CEO"
              />
            </div>
          </div>
          <div className="grid grid-cols-2 gap-2">
            <div>
              <FieldLabel>
                Role class
                <span className="ml-1 font-normal text-fg-subtle">— seeds sliders</span>
              </FieldLabel>
              <Select
                value={roleClass || NONE}
                onValueChange={(v) => setRoleClass(v === NONE ? "" : v)}
              >
                <SelectTrigger>
                  <SelectValue placeholder="None" />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value={NONE}>None</SelectItem>
                  {ROLE_CLASSES.map((rc) => (
                    <SelectItem key={rc} value={rc}>
                      {rc}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
            <div className="flex items-end justify-end gap-2">
              <Button variant="outline" size="sm" onClick={() => setAdding(false)}>
                Cancel
              </Button>
              <Button
                size="sm"
                onClick={() => add.mutate()}
                disabled={!companyId || add.isPending}
              >
                {add.isPending ? (
                  <Loader2 className="h-3.5 w-3.5 animate-spin" />
                ) : (
                  <Plus className="h-3.5 w-3.5" />
                )}
                Add
              </Button>
            </div>
          </div>
        </div>
      )}

      <AddCompanyDialog
        open={companyDialog}
        onOpenChange={setCompanyDialog}
        onCreated={(id) => {
          companiesQuery.refetch();
          setCompanyId(id);
        }}
      />
    </section>
  );
}

// ── Connections (person → user) ─────────────────────────────────────────────

function ConnectionsSection({ personId }: { personId: string }) {
  const queryClient = useQueryClient();
  const [adding, setAdding] = useState(false);
  const [type, setType] = useState("");
  const [context, setContext] = useState("");

  const key = ["crm", "connections", personId];
  const { data, isLoading } = useQuery({
    queryKey: key,
    queryFn: () => crmApi.listConnections(personId),
  });

  const add = useMutation({
    mutationFn: () =>
      crmApi.addConnection(personId, {
        connection_type: type,
        context: context.trim() || undefined,
      }),
    onSuccess: (res) => {
      queryClient.setQueryData(key, { connections: res.connections });
      setAdding(false);
      setType("");
      setContext("");
    },
    onError: (e: Error) => toast.error(e.message || "Could not add connection"),
  });

  const remove = useMutation({
    mutationFn: (id: string) => crmApi.removeConnection(personId, id),
    onSuccess: (res) =>
      queryClient.setQueryData(key, { connections: res.connections }),
    onError: (e: Error) => toast.error(e.message),
  });

  const conns = data?.connections ?? [];

  return (
    <section className="space-y-3">
      <div className="flex items-center justify-between">
        <SubsectionHeading
          title="Connections"
          hint="How YOU relate to this person — per context (e.g. co-shareholder @ the joint venture)."
        />
        <Button size="sm" variant="ghost" onClick={() => setAdding((v) => !v)}>
          <Plus className="h-3.5 w-3.5" />
          Add
        </Button>
      </div>

      {isLoading ? (
        <div className="flex items-center gap-2 text-xs text-tertiary">
          <Loader2 className="h-3.5 w-3.5 animate-spin" /> Loading…
        </div>
      ) : conns.length === 0 && !adding ? (
        <p className="text-2xs text-tertiary">No connections recorded yet.</p>
      ) : (
        <div className="space-y-1.5">
          {conns.map((c) => (
            <Row key={c.id}>
              <div className="flex min-w-0 items-center gap-2">
                <Badge variant="outline" className="shrink-0 text-2xs">
                  {c.connection_type}
                </Badge>
                {c.context ? (
                  <span className="truncate text-sm text-tertiary">{c.context}</span>
                ) : null}
              </div>
              <Button
                size="sm"
                variant="ghost"
                onClick={() => remove.mutate(c.id)}
                disabled={remove.isPending}
                aria-label="Remove connection"
              >
                <Trash2 className="h-3.5 w-3.5" />
              </Button>
            </Row>
          ))}
        </div>
      )}

      {adding && (
        <div className="grid grid-cols-[1fr_1fr_auto] items-end gap-2 rounded-md border border-border bg-surface-subtle px-3 py-3">
          <div>
            <FieldLabel>Type</FieldLabel>
            <Select value={type || ""} onValueChange={setType}>
              <SelectTrigger>
                <SelectValue placeholder="Pick" />
              </SelectTrigger>
              <SelectContent>
                {CONNECTION_TYPES.map((t) => (
                  <SelectItem key={t} value={t}>
                    {t}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
          <div>
            <FieldLabel>Context</FieldLabel>
            <Input
              value={context}
              onChange={(e) => setContext(e.target.value)}
              placeholder="e.g. the joint venture"
            />
          </div>
          <Button
            size="sm"
            onClick={() => add.mutate()}
            disabled={!type || add.isPending}
          >
            {add.isPending ? (
              <Loader2 className="h-3.5 w-3.5 animate-spin" />
            ) : (
              <Plus className="h-3.5 w-3.5" />
            )}
            Add
          </Button>
        </div>
      )}
    </section>
  );
}

// ── Engagements (project × hat × company) ───────────────────────────────────

function EngagementsSection({ personId }: { personId: string }) {
  const queryClient = useQueryClient();
  const [adding, setAdding] = useState(false);
  const [project, setProject] = useState("");
  const [affiliationId, setAffiliationId] = useState("");
  const [preview, setPreview] = useState<string | null>(null);

  const key = ["crm", "engagements", personId];
  const { data, isLoading } = useQuery({
    queryKey: key,
    queryFn: () => crmApi.listEngagements(personId),
  });
  const projectsQuery = useQuery({
    queryKey: ["crm", "projects"],
    queryFn: () => crmApi.listProjects(),
    staleTime: 5 * 60 * 1000,
  });
  const affiliationsQuery = useQuery({
    queryKey: ["crm", "affiliations", personId],
    queryFn: () => crmApi.listAffiliations(personId),
  });

  const add = useMutation({
    mutationFn: () =>
      crmApi.addEngagement(personId, {
        project_slug: project,
        affiliation_id: affiliationId || undefined,
      }),
    onSuccess: (res) => {
      queryClient.setQueryData(key, { engagements: res.engagements });
      setAdding(false);
      setProject("");
      setAffiliationId("");
    },
    onError: (e: Error) => toast.error(e.message || "Could not add engagement"),
  });

  const remove = useMutation({
    mutationFn: (id: string) => crmApi.removeEngagement(personId, id),
    onSuccess: (res) =>
      queryClient.setQueryData(key, { engagements: res.engagements }),
    onError: (e: Error) => toast.error(e.message),
  });

  const resolve = useMutation({
    mutationFn: (proj: string) => crmApi.resolve(personId, proj),
    onSuccess: (res) => setPreview(res.markdown),
    onError: (e: Error) => toast.error(e.message),
  });

  const engagements = data?.engagements ?? [];
  const projects = projectsQuery.data?.projects ?? [];
  const affiliations = affiliationsQuery.data?.affiliations ?? [];

  return (
    <section className="space-y-3">
      <div className="flex items-center justify-between">
        <SubsectionHeading
          title="Engagements"
          hint="Per project: which hat is in play. Resolve = IA (this person) + design (the company's brand)."
        />
        <Button size="sm" variant="ghost" onClick={() => setAdding((v) => !v)}>
          <Plus className="h-3.5 w-3.5" />
          Bind to project
        </Button>
      </div>

      {isLoading ? (
        <div className="flex items-center gap-2 text-xs text-tertiary">
          <Loader2 className="h-3.5 w-3.5 animate-spin" /> Loading…
        </div>
      ) : engagements.length === 0 && !adding ? (
        <p className="text-2xs text-tertiary">
          Not bound to any project yet.
        </p>
      ) : (
        <div className="space-y-1.5">
          {engagements.map((e) => (
            <Row key={e.id}>
              <div className="flex min-w-0 items-center gap-2">
                <Badge className="shrink-0 text-2xs">{e.project_slug}</Badge>
                <span className="truncate text-sm text-tertiary">
                  {e.hat_role || "—"}
                  {e.company_name ? ` · ${e.company_name}` : ""}
                  {e.brand_id ? ` · ${e.brand_id} brand` : ""}
                </span>
              </div>
              <div className="flex shrink-0 items-center gap-1">
                <Button
                  size="sm"
                  variant="ghost"
                  onClick={() => resolve.mutate(e.project_slug)}
                  disabled={resolve.isPending}
                  aria-label="Preview resolve"
                >
                  <Sparkles className="h-3.5 w-3.5" />
                </Button>
                <Button
                  size="sm"
                  variant="ghost"
                  onClick={() => remove.mutate(e.id)}
                  disabled={remove.isPending}
                  aria-label="Remove engagement"
                >
                  <Trash2 className="h-3.5 w-3.5" />
                </Button>
              </div>
            </Row>
          ))}
        </div>
      )}

      {adding && (
        <div className="grid grid-cols-[1fr_1fr_auto] items-end gap-2 rounded-md border border-border bg-surface-subtle px-3 py-3">
          <div>
            <FieldLabel>Project</FieldLabel>
            <Select value={project || ""} onValueChange={setProject}>
              <SelectTrigger>
                <SelectValue placeholder="Pick a project" />
              </SelectTrigger>
              <SelectContent>
                {projects.map((p) => (
                  <SelectItem key={p.id} value={p.id}>
                    {p.id}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
          <div>
            <FieldLabel>Hat</FieldLabel>
            <Select
              value={affiliationId || NONE}
              onValueChange={(v) => setAffiliationId(v === NONE ? "" : v)}
            >
              <SelectTrigger>
                <SelectValue placeholder="Primary hat" />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value={NONE}>Primary hat (default)</SelectItem>
                {affiliations.map((a) => (
                  <SelectItem key={a.id} value={a.id}>
                    {(a.role || "—") + " · " + a.company_name}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
          <Button
            size="sm"
            onClick={() => add.mutate()}
            disabled={!project || add.isPending}
          >
            {add.isPending ? (
              <Loader2 className="h-3.5 w-3.5 animate-spin" />
            ) : (
              <Plus className="h-3.5 w-3.5" />
            )}
            Bind
          </Button>
        </div>
      )}

      {preview ? (
        <pre className="mt-1 max-h-72 overflow-auto whitespace-pre-wrap rounded bg-surface-subtle px-3 py-2 text-xs text-fg">
          {preview}
        </pre>
      ) : null}
    </section>
  );
}

// ── Public: the three sections stacked ──────────────────────────────────────

export function RelationsPanel({ personId }: { personId: string }) {
  if (!personId) {
    return (
      <EmptyState
        icon={<Building2 className="h-8 w-8" />}
        title="No relations"
        description="Save the person first to add hats, connections and engagements."
      />
    );
  }
  return (
    <div className="space-y-6">
      <HatsSection personId={personId} />
      <ConnectionsSection personId={personId} />
      <EngagementsSection personId={personId} />
    </div>
  );
}
