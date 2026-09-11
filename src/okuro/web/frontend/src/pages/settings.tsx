import { useEffect, useRef, useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { Link } from "react-router";
import { useUrlTab } from "@/hooks/use-url-tab";
import {
  integrationsApi,
  type Integration,
  type Challenge as IntegrationChallenge,
} from "@/lib/integrations-api";
import { PageHeader } from "@/components/shell/page-header";
import { Tabs, TabsList, TabsTrigger, TabsContent } from "@/components/ui/tabs";
import { EmptyState } from "@/components/ui/empty-state";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { Button } from "@/components/ui/button";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Switch } from "@/components/ui/switch";
import { toast } from "@/components/ui/toast";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  Loader2,
  Globe,
  Plus,
  X,
  ArrowUp,
  ArrowDown,
  Sparkles,
  Upload,
  Github,
  Link as LinkIcon,
  Palette,
  RefreshCw,
  Lock,
  Unlock,
  Eye,
  EyeOff,
  Pencil,
  Trash2,
  KeyRound,
} from "lucide-react";
import { api, apiText, onboardingApi, sttApi, ttsApi } from "@/lib/api";
import { ConsumersPanel } from "@/components/settings/consumers-panel";
import {
  applyPulseOutlineOverride,
  clearPulseOutlineOverride,
  PULSE_OUTLINE_DEFAULTS,
  reloadEngineSheet,
  type PulseOutlineOverride,
} from "@/lib/theme";
import {
  keyringApi,
  setKeyringSession,
  getKeyringSession,
  type KeyringStatus,
  type SecretRef,
} from "@/lib/keyring-api";

/**
 * /settings — edit every onboarding-collected profile section after install.
 *
 * Onboarding is a one-shot flow; once complete, nothing it collected was
 * editable in the web UI. This page closes that hole. Each tab maps to
 * one top-level key on the profile document. Save goes through
 * PATCH /api/onboarding/profile — the backend replaces the entire section,
 * so forms MUST spread the existing section onto the new values.
 *
 * Subagent #1 ships the scaffold + Identity tab.
 * Subagent #2 ships Communication + Work Style.
 * Subagents #3-#7 fill in the remaining tabs (see each EmptyState for ownership).
 */

type Profile = Record<string, unknown>;

type IdentitySection = {
  name?: string;
  handle?: string;
  language?: string;
  timezone?: string;
  role?: string;
  main_goal?: string;
  // Onboarding may have written these; we preserve them on PATCH.
  location?: string;
  profession?: string;
  [key: string]: unknown;
};

/**
 * Some profile list fields (patterns, pet_peeves, hates, loves, preferred,
 * avoid, …) were upgraded from `string[]` to `Array<string | {rule,
 * rationale?, evidence?}>` by the Meta-Harness rule-enrichment work. The
 * frontend editor is still string-based, so rows that arrive as objects
 * render as "[object Object]" unless we flatten first.
 *
 * Contract: lossy — rationale/evidence fields are dropped on save. The
 * enrichment daemon regenerates them on the next refresh.
 */
function flattenRuleList(value: unknown): string[] {
  if (!Array.isArray(value)) return [];
  return value
    .map((v) => {
      if (typeof v === "string") return v;
      if (v && typeof v === "object" && typeof (v as { rule?: unknown }).rule === "string") {
        return (v as { rule: string }).rule;
      }
      return "";
    })
    .filter((s): s is string => s.length > 0);
}

type SwearingShape = {
  meaning?: string;
  agent_response?: string;
  [key: string]: unknown;
};

type AngerProtocolShape = {
  trigger?: string;
  response?: string;
  do?: string[];
  never?: string[];
  self_check?: string;
  [key: string]: unknown;
};

type DangerProtocolShape = {
  label?: string;
  format?: string;
  threshold?: string[];
  [key: string]: unknown;
};

type FormatPreferencesShape = {
  avoid?: string[];
  preferred?: string[];
  [key: string]: unknown;
};

type CommunicationSection = {
  patterns?: string[];
  swearing?: SwearingShape;
  pet_peeves?: string[];
  anger_protocol?: AngerProtocolShape;
  danger_protocol?: DangerProtocolShape;
  response_length?: string;
  format_preferences?: FormatPreferencesShape;
  [key: string]: unknown;
};

type HoursShape = {
  active?: string[];
  breaks?: string[];
  [key: string]: unknown;
};

type UiPhilosophyShape = {
  approach?: string;
  agent_autonomy?: string;
  [key: string]: unknown;
};

type WorkStyleSection = {
  hates?: string[];
  loves?: string[];
  hours?: HoursShape;
  rabbit_holes?: string;
  ui_philosophy?: UiPhilosophyShape;
  venv?: string;
  modules?: string;
  secrets?: string;
  database?: string;
  domain?: string;
  cleanup?: string | boolean;
  gpu_allocation?: string;
  process_naming?: string;
  [key: string]: unknown;
};

type DesignSection = {
  kit?: string;                        // ACTIVE design system — a design_engine kit id
  profile?: string;                    // legacy: a v1 design profile id, read only as a fallback
  brand?: string | null;               // active brand — resolves to its design profile, wins over `profile`
  overrides?: Record<string, string>;  // { "--color-accent": "#ff0066", ... }
  scraped_sources?: Array<{ url?: string; name?: string; [k: string]: unknown }>;
  [key: string]: unknown;
};

// NOTE: The backend BearerAuthMiddleware (main.py L155) requires Bearer auth
// for every mutating method. GET stays open. The shared `api()` helper in
// lib/api.ts transparently fetches + rotates the token via /api/auth/token,
// so we route PATCHes through it.
function getProfile(): Promise<Profile> {
  return api<Profile>("/api/onboarding/profile");
}

function patchProfile(body: unknown): Promise<Profile> {
  return api<Profile>("/api/onboarding/profile", {
    method: "PATCH",
    body: JSON.stringify(body),
  });
}

import { FeedbackTab } from "@/components/settings/feedback-tab";

export function SettingsPage() {
  // Controlled Tabs so Decision Style can switch to Principles programmatically
  // ("Edit in Principles tab" link from key_principles chips).
  const { value: activeTab, onValueChange: setActiveTab } = useUrlTab("identity");
  return (
    <div className="page-shell space-y-8">
      <PageHeader
        title="Settings"
        subtitle="Edit every field onboarding collected — identity, communication, principles, boundaries, and more."
      />

      <Tabs
        value={activeTab}
        onValueChange={setActiveTab}
        orientation="vertical"
        className="flex gap-8"
      >
        <TabsList>
          <TabsRailLabel>You</TabsRailLabel>
          <TabsTrigger value="identity">Identity</TabsTrigger>
          <TabsTrigger value="expertise">Expertise</TabsTrigger>
          <TabsRailLabel>How you work</TabsRailLabel>
          <TabsTrigger value="work_style">Work style</TabsTrigger>
          <TabsTrigger value="cognitive_style">Cognitive style</TabsTrigger>
          <TabsTrigger value="decision_style">Decision style</TabsTrigger>
          <TabsRailLabel>How agents behave</TabsRailLabel>
          <TabsTrigger value="communication">Communication</TabsTrigger>
          <TabsTrigger value="boundaries">Boundaries</TabsTrigger>
          <TabsTrigger value="principles">Principles</TabsTrigger>
          <TabsRailLabel>System</TabsRailLabel>
          <TabsTrigger value="design">Design</TabsTrigger>
          <TabsTrigger value="dictation">Dictation</TabsTrigger>
          <TabsTrigger value="voices">Voices</TabsTrigger>
          <TabsTrigger value="feedback">Feedback</TabsTrigger>
          <TabsTrigger value="keyring">Keyring</TabsTrigger>
          <TabsTrigger value="consumers">Tools &amp; MCP</TabsTrigger>
          <TabsTrigger value="integrations">Integrations</TabsTrigger>
        </TabsList>

        <div className="min-w-0 flex-1">
          <TabsContent value="identity">
            <IdentityTab />
          </TabsContent>

          <TabsContent value="feedback">
            <FeedbackTab />
          </TabsContent>

          <TabsContent value="communication">
            <CommunicationTab />
          </TabsContent>

          <TabsContent value="work_style">
            <WorkStyleTab />
          </TabsContent>

          <TabsContent value="cognitive_style">
            <CognitiveStyleTab />
          </TabsContent>

          <TabsContent value="principles">
            <PrinciplesTab />
          </TabsContent>

          <TabsContent value="boundaries">
            <BoundariesTab />
          </TabsContent>

          <TabsContent value="decision_style">
            <DecisionStyleTab onGotoPrinciples={() => setActiveTab("principles")} />
          </TabsContent>

          <TabsContent value="expertise">
            <ExpertiseTab />
          </TabsContent>

          <TabsContent value="design">
            <DesignTab />
          </TabsContent>

          <TabsContent value="dictation">
            <DictationTab />
          </TabsContent>

          <TabsContent value="voices">
            <VoicesTab />
          </TabsContent>

          <TabsContent value="keyring">
            <KeyringTab />
          </TabsContent>

          <TabsContent value="consumers">
            <ConsumersPanel />
          </TabsContent>

          <TabsContent value="integrations">
            <IntegrationsTab />
          </TabsContent>
        </div>
      </Tabs>
    </div>
  );
}

// ── Shared primitives ───────────────────────────────────────────────

function detectBrowserTimezone(): string {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone ?? "";
  } catch {
    return "";
  }
}

function FieldLabel({ children }: { children: React.ReactNode }) {
  return (
    <label className="mb-1.5 block text-xs font-medium text-fg-muted">
      {children}
    </label>
  );
}

/**
 * Section divider inside the vertical Settings rail. Not interactive —
 * purely a visual grouping cue so the 11 tabs read as 4 clusters.
 */
function TabsRailLabel({ children }: { children: React.ReactNode }) {
  return (
    <span
      aria-hidden="true"
      className="mt-4 mb-1 px-3 text-3xs font-semibold uppercase tracking-wider text-fg-subtle first:mt-0"
    >
      {children}
    </span>
  );
}

/**
 * Collapsible subsection. Defaults to closed — used for fields that
 * are less frequently edited (e.g. Swearing, Anger protocol) so the
 * common case shows a short scroll instead of a dense wall.
 */
function CollapsibleSubsection({
  title,
  hint,
  defaultOpen = false,
  children,
}: {
  title: string;
  hint?: string;
  defaultOpen?: boolean;
  children: React.ReactNode;
}) {
  const [open, setOpen] = useState(defaultOpen);
  return (
    <div className="space-y-4 border-t border-border-subtle pt-5 first:border-0 first:pt-0">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        className="group flex w-full items-start gap-2 text-left"
      >
        <span
          aria-hidden="true"
          className="mt-1 text-2xs text-tertiary transition-transform group-hover:text-fg-muted"
          style={{ transform: open ? "rotate(90deg)" : "rotate(0deg)" }}
        >
          ▸
        </span>
        <div className="flex-1">
          <h3 className="text-sm font-semibold text-fg group-hover:text-accent">
            {title}
          </h3>
          {hint ? <p className="mt-1 text-2xs text-tertiary">{hint}</p> : null}
        </div>
      </button>
      {open && <div className="space-y-4 pl-5">{children}</div>}
    </div>
  );
}

function SubsectionHeading({
  title,
  hint,
}: {
  title: string;
  hint?: string;
}) {
  return (
    <div>
      <h3 className="text-lg font-semibold text-fg">{title}</h3>
      {hint ? <p className="mt-1.5 text-xs text-fg-muted">{hint}</p> : null}
    </div>
  );
}

/**
 * Minimal array-of-strings editor. Add/remove/reorder rows.
 * Empty rows are stripped on save (parent handles via .filter(Boolean)).
 */
function StringListEditor({
  value,
  onChange,
  placeholder,
  multiline = false,
}: {
  value: string[];
  onChange: (next: string[]) => void;
  placeholder?: string;
  multiline?: boolean;
}) {
  const update = (idx: number, next: string) => {
    const out = value.slice();
    out[idx] = next;
    onChange(out);
  };
  const remove = (idx: number) => {
    onChange(value.filter((_, i) => i !== idx));
  };
  const move = (idx: number, delta: number) => {
    const target = idx + delta;
    if (target < 0 || target >= value.length) return;
    const out = value.slice();
    const row = out[idx] ?? "";
    out.splice(idx, 1);
    out.splice(target, 0, row);
    onChange(out);
  };
  const add = () => {
    onChange([...value, ""]);
  };

  return (
    <div className="space-y-1.5">
      {value.length === 0 && (
        <p className="text-2xs text-tertiary italic">No items yet.</p>
      )}
      {value.map((row, idx) => (
        <div key={idx} className="flex items-start gap-1.5">
          {multiline ? (
            <Textarea
              value={row}
              onChange={(e) => update(idx, e.target.value)}
              placeholder={placeholder}
              className="min-h-9"
            />
          ) : (
            <Input
              value={row}
              onChange={(e) => update(idx, e.target.value)}
              placeholder={placeholder}
            />
          )}
          <div className="flex shrink-0 items-center">
            <button
              type="button"
              onClick={() => move(idx, -1)}
              disabled={idx === 0}
              className="p-1 text-tertiary hover:text-fg disabled:opacity-30"
              aria-label="Move up"
            >
              <ArrowUp className="h-3.5 w-3.5" />
            </button>
            <button
              type="button"
              onClick={() => move(idx, 1)}
              disabled={idx === value.length - 1}
              className="p-1 text-tertiary hover:text-fg disabled:opacity-30"
              aria-label="Move down"
            >
              <ArrowDown className="h-3.5 w-3.5" />
            </button>
            <button
              type="button"
              onClick={() => remove(idx)}
              className="p-1 text-tertiary hover:text-error"
              aria-label="Remove"
            >
              <X className="h-3.5 w-3.5" />
            </button>
          </div>
        </div>
      ))}
      <Button
        size="sm"
        variant="outline"
        onClick={add}
        type="button"
        className="mt-1"
      >
        <Plus className="h-3.5 w-3.5" />
        Add row
      </Button>
    </div>
  );
}

function stripEmpty(list: string[]): string[] {
  return list.map((s) => s.trim()).filter((s) => s.length > 0);
}

function LoadingPane() {
  return (
    <div className="flex items-center gap-2 text-sm text-tertiary">
      <Loader2 className="h-3.5 w-3.5 animate-spin" />
      Loading profile…
    </div>
  );
}

function ErrorPane({ error }: { error: unknown }) {
  return (
    <EmptyState
      title="Could not load profile"
      description={error instanceof Error ? error.message : "unknown error"}
    />
  );
}

function SaveButton({
  pending,
  seeded,
  success,
  onClick,
}: {
  pending: boolean;
  seeded: boolean;
  success: boolean;
  onClick: () => void;
}) {
  return (
    <div className="flex items-center gap-3 pt-2">
      <Button size="sm" onClick={onClick} disabled={pending || !seeded}>
        {pending ? (
          <>
            <Loader2 className="h-3.5 w-3.5 animate-spin" />
            Saving…
          </>
        ) : (
          "Save changes"
        )}
      </Button>
      {success && <span className="text-2xs text-success">Saved.</span>}
    </div>
  );
}

// ── Identity tab ────────────────────────────────────────────────────

function IdentityTab() {
  const qc = useQueryClient();
  const profile = useQuery({
    queryKey: ["onboarding", "profile"],
    queryFn: getProfile,
    staleTime: 10_000,
  });

  // Form state. Seeded when profile loads. Uses spec field set —
  // location/profession from onboarding are preserved on save via spread.
  const [name, setName] = useState("");
  const [handle, setHandle] = useState("");
  const [language, setLanguage] = useState("");
  const [timezone, setTimezone] = useState("");
  const [role, setRole] = useState("");
  const [mainGoal, setMainGoal] = useState("");
  const [seeded, setSeeded] = useState(false);
  const [detectedTz] = useState<string>(detectBrowserTimezone());

  useEffect(() => {
    if (seeded || !profile.data) return;
    const id = (profile.data.identity as IdentitySection | undefined) ?? {};
    setName(id.name ?? "");
    setHandle(id.handle ?? "");
    setLanguage(id.language ?? "");
    // Pre-fill with detected browser tz if identity has none.
    setTimezone(id.timezone ?? detectedTz);
    // Fall back to onboarding's `profession` field until users migrate to `role`.
    setRole(id.role ?? id.profession ?? "");
    setMainGoal(id.main_goal ?? "");
    setSeeded(true);
  }, [profile.data, seeded, detectedTz]);

  const save = useMutation({
    mutationFn: async () => {
      const current = (profile.data?.identity as IdentitySection | undefined) ?? {};
      // Merge: preserve any fields this form doesn't know about (e.g.
      // legacy `location`, `profession`). Backend replaces the whole
      // section, so we must spread.
      const identity: IdentitySection = {
        ...current,
        name: name.trim(),
        handle: handle.trim(),
        language: language.trim(),
        timezone: timezone.trim(),
        role: role.trim(),
        main_goal: mainGoal.trim(),
      };
      return patchProfile({ identity });
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["onboarding", "profile"] });
      toast.success("Identity saved");
    },
    onError: (err) => {
      toast.error("Save failed", {
        description: err instanceof Error ? err.message : "unknown error",
      });
    },
  });

  if (profile.isLoading) {
    return (
      <div className="flex items-center gap-2 text-sm text-tertiary">
        <Loader2 className="h-3.5 w-3.5 animate-spin" />
        Loading profile…
      </div>
    );
  }

  if (profile.isError) {
    return (
      <EmptyState
        title="Could not load profile"
        description={
          profile.error instanceof Error ? profile.error.message : "unknown error"
        }
      />
    );
  }

  return (
    <section className="space-y-4 max-w-xl">
      <div>
        <h2 className="text-lg font-semibold text-fg">Who are you?</h2>
        <p className="mt-2 text-xs text-fg-muted">
          Agents read these fields at the top of every bootstrap.
        </p>
      </div>

      <div className="space-y-6">
        <div>
          <FieldLabel>Name</FieldLabel>
          <Input
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="Zora"
            className="w-field-md"
          />
        </div>

        <div>
          <FieldLabel>Handle</FieldLabel>
          <Input
            value={handle}
            onChange={(e) => setHandle(e.target.value)}
            placeholder="zora"
            className="w-field-sm"
          />
        </div>

        <div>
          <FieldLabel>Role</FieldLabel>
          <Input
            value={role}
            onChange={(e) => setRole(e.target.value)}
            placeholder="UX design team lead"
            className="w-field-md"
          />
        </div>

        <div>
          <FieldLabel>Main goal</FieldLabel>
          <Input
            value={mainGoal}
            onChange={(e) => setMainGoal(e.target.value)}
            placeholder="Build an agent-native OS"
            className="w-field-md"
          />
        </div>

        <div className="flex flex-wrap items-start gap-6">
          <div>
            <FieldLabel>Language</FieldLabel>
            <Input
              value={language}
              onChange={(e) => setLanguage(e.target.value)}
              placeholder="en"
              className="w-field-xs"
            />
          </div>
          <div>
            <FieldLabel>Timezone</FieldLabel>
            <div className="flex items-center gap-2">
              <Input
                value={timezone}
                onChange={(e) => setTimezone(e.target.value)}
                placeholder="Europe/Zurich"
                className="w-field-sm"
              />
              {detectedTz && timezone !== detectedTz && (
                <button
                  type="button"
                  onClick={() => setTimezone(detectedTz)}
                  className="flex shrink-0 items-center gap-0.5 whitespace-nowrap text-xs text-accent hover:underline"
                >
                  <Globe className="h-3 w-3" />
                  detected
                </button>
              )}
            </div>
          </div>
        </div>
      </div>

      <div className="flex items-center gap-3 pt-2">
        <Button
          size="sm"
          onClick={() => save.mutate()}
          disabled={save.isPending || !seeded}
        >
          {save.isPending ? (
            <>
              <Loader2 className="h-3.5 w-3.5 animate-spin" />
              Saving…
            </>
          ) : (
            "Save changes"
          )}
        </Button>
        {save.isSuccess && (
          <span className="text-2xs text-success">Saved.</span>
        )}
      </div>
    </section>
  );
}

// ── Communication tab ───────────────────────────────────────────────

function CommunicationTab() {
  const qc = useQueryClient();
  const profile = useQuery({
    queryKey: ["onboarding", "profile"],
    queryFn: getProfile,
    staleTime: 10_000,
  });

  const [seeded, setSeeded] = useState(false);

  const [responseLength, setResponseLength] = useState<string>("concise");
  const [patterns, setPatterns] = useState<string[]>([]);
  const [petPeeves, setPetPeeves] = useState<string[]>([]);
  const [preferred, setPreferred] = useState<string[]>([]);
  const [avoid, setAvoid] = useState<string[]>([]);
  const [swearingMeaning, setSwearingMeaning] = useState("");
  const [swearingResponse, setSwearingResponse] = useState("");
  const [angerTrigger, setAngerTrigger] = useState("");
  const [angerResponse, setAngerResponse] = useState("");
  const [angerDo, setAngerDo] = useState<string[]>([]);
  const [angerNever, setAngerNever] = useState<string[]>([]);
  const [angerSelfCheck, setAngerSelfCheck] = useState("");
  const [dangerLabel, setDangerLabel] = useState("");
  const [dangerFormat, setDangerFormat] = useState("");
  const [dangerThreshold, setDangerThreshold] = useState<string[]>([]);

  useEffect(() => {
    if (seeded || !profile.data) return;
    const c = (profile.data.communication as CommunicationSection | undefined) ?? {};
    setResponseLength(c.response_length ?? "concise");
    setPatterns(flattenRuleList(c.patterns));
    setPetPeeves(flattenRuleList(c.pet_peeves));
    setPreferred(flattenRuleList(c.format_preferences?.preferred));
    setAvoid(flattenRuleList(c.format_preferences?.avoid));
    setSwearingMeaning(c.swearing?.meaning ?? "");
    setSwearingResponse(c.swearing?.agent_response ?? "");
    setAngerTrigger(c.anger_protocol?.trigger ?? "");
    setAngerResponse(c.anger_protocol?.response ?? "");
    setAngerDo(c.anger_protocol?.do ?? []);
    setAngerNever(c.anger_protocol?.never ?? []);
    setAngerSelfCheck(c.anger_protocol?.self_check ?? "");
    setDangerLabel(c.danger_protocol?.label ?? "");
    setDangerFormat(c.danger_protocol?.format ?? "");
    setDangerThreshold(c.danger_protocol?.threshold ?? []);
    setSeeded(true);
  }, [profile.data, seeded]);

  const save = useMutation({
    mutationFn: async () => {
      const current =
        (profile.data?.communication as CommunicationSection | undefined) ?? {};
      const communication: CommunicationSection = {
        ...current,
        response_length: responseLength.trim(),
        patterns: stripEmpty(patterns),
        pet_peeves: stripEmpty(petPeeves),
        format_preferences: {
          ...(current.format_preferences ?? {}),
          preferred: stripEmpty(preferred),
          avoid: stripEmpty(avoid),
        },
        swearing: {
          ...(current.swearing ?? {}),
          meaning: swearingMeaning.trim(),
          agent_response: swearingResponse.trim(),
        },
        anger_protocol: {
          ...(current.anger_protocol ?? {}),
          trigger: angerTrigger.trim(),
          response: angerResponse.trim(),
          do: stripEmpty(angerDo),
          never: stripEmpty(angerNever),
          self_check: angerSelfCheck.trim(),
        },
        danger_protocol: {
          ...(current.danger_protocol ?? {}),
          label: dangerLabel.trim(),
          format: dangerFormat.trim(),
          threshold: stripEmpty(dangerThreshold),
        },
      };
      return patchProfile({ communication });
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["onboarding", "profile"] });
      toast.success("Communication saved");
    },
    onError: (err) => {
      toast.error("Save failed", {
        description: err instanceof Error ? err.message : "unknown error",
      });
    },
  });

  if (profile.isLoading) return <LoadingPane />;
  if (profile.isError) return <ErrorPane error={profile.error} />;

  return (
    <section className="space-y-8 max-w-2xl">
      <div>
        <h2 className="text-sm font-semibold text-fg">
          How should agents talk to you?
        </h2>
        <p className="mt-1 text-2xs text-tertiary">
          These fields shape the behavioral contract every agent reads at
          session start.
        </p>
      </div>

      {/* Response shape */}
      <div className="space-y-4">
        <SubsectionHeading
          title="Response shape"
          hint="Length + format patterns agents follow by default."
        />

        <div>
          <FieldLabel>Response length</FieldLabel>
          <Select value={responseLength} onValueChange={setResponseLength}>
            <SelectTrigger className="w-56">
              <SelectValue placeholder="Select length" />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="concise">concise</SelectItem>
              <SelectItem value="balanced">balanced</SelectItem>
              <SelectItem value="detailed">detailed</SelectItem>
            </SelectContent>
          </Select>
        </div>

        <div>
          <FieldLabel>Patterns</FieldLabel>
          <p className="mb-2 text-2xs text-tertiary">
            Communication rules agents must follow (e.g. "lead with the
            answer, then explain").
          </p>
          <StringListEditor
            value={patterns}
            onChange={setPatterns}
            placeholder="lead with the answer, then explain"
            multiline
          />
        </div>
      </div>

      {/* Format preferences */}
      <div className="space-y-4">
        <SubsectionHeading
          title="Format preferences"
          hint="Shapes agents should reach for vs avoid."
        />

        <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
          <div>
            <FieldLabel>Preferred</FieldLabel>
            <StringListEditor
              value={preferred}
              onChange={setPreferred}
              placeholder="tables"
            />
          </div>
          <div>
            <FieldLabel>Avoid</FieldLabel>
            <StringListEditor
              value={avoid}
              onChange={setAvoid}
              placeholder="long paragraphs"
            />
          </div>
        </div>
      </div>

      {/* Pet peeves */}
      <div className="space-y-4">
        <SubsectionHeading
          title="Pet peeves"
          hint="Behaviors that erode trust — agents must never do these."
        />
        <StringListEditor
          value={petPeeves}
          onChange={setPetPeeves}
          placeholder="verbose explanations when a table would do"
          multiline
        />
      </div>

      {/* Advanced protocols — collapsed by default */}
      <div className="space-y-0">
        <h3 className="mb-3 text-2xs font-medium uppercase tracking-wider text-tertiary">
          Advanced · how to respond to emotional signals
        </h3>

        <CollapsibleSubsection
          title="Swearing"
          hint="What swearing means and how agents should respond to it."
        >
          <div>
            <FieldLabel>Meaning</FieldLabel>
            <Textarea
              value={swearingMeaning}
              onChange={(e) => setSwearingMeaning(e.target.value)}
              placeholder="normal, lets off steam"
            />
          </div>
          <div>
            <FieldLabel>Agent response</FieldLabel>
            <Textarea
              value={swearingResponse}
              onChange={(e) => setSwearingResponse(e.target.value)}
              placeholder="match the energy, stay focused"
            />
          </div>
        </CollapsibleSubsection>

        <CollapsibleSubsection
          title="Anger protocol"
          hint="How agents detect frustration and how they should respond."
        >
          <div>
            <FieldLabel>Trigger</FieldLabel>
            <Textarea
              value={angerTrigger}
              onChange={(e) => setAngerTrigger(e.target.value)}
              placeholder="caps lock, exclamation marks"
            />
          </div>
          <div>
            <FieldLabel>Response (fallback if DO/NEVER are empty)</FieldLabel>
            <Textarea
              value={angerResponse}
              onChange={(e) => setAngerResponse(e.target.value)}
              placeholder="research more, not less. slow down."
            />
          </div>
          <div>
            <FieldLabel>DO (one per line)</FieldLabel>
            <Textarea
              value={angerDo.join("\n")}
              onChange={(e) => setAngerDo(e.target.value.split("\n"))}
              placeholder={"slow down\nwiden the research\nfind the ROOT cause first"}
            />
          </div>
          <div>
            <FieldLabel>NEVER (one per line)</FieldLabel>
            <Textarea
              value={angerNever.join("\n")}
              onChange={(e) => setAngerNever(e.target.value.split("\n"))}
              placeholder={"shortcut\npatch the symptom\nship a first guess"}
            />
          </div>
          <div>
            <FieldLabel>Self-check</FieldLabel>
            <Textarea
              value={angerSelfCheck}
              onChange={(e) => setAngerSelfCheck(e.target.value)}
              placeholder="root cause, or the first plausible one?"
            />
          </div>
        </CollapsibleSubsection>

        <CollapsibleSubsection
          title="Danger protocol"
          hint="How agents flag high-stakes actions."
        >
          <div>
            <FieldLabel>Label</FieldLabel>
            <Input
              value={dangerLabel}
              onChange={(e) => setDangerLabel(e.target.value)}
              placeholder="DANGER"
            />
          </div>
          <div>
            <FieldLabel>Format</FieldLabel>
            <Textarea
              value={dangerFormat}
              onChange={(e) => setDangerFormat(e.target.value)}
              placeholder="[what could happen]. [why it matters]."
            />
          </div>
          <div>
            <FieldLabel>Threshold</FieldLabel>
            <p className="mb-2 text-2xs text-tertiary">
              Situations that trigger the danger label.
            </p>
            <StringListEditor
              value={dangerThreshold}
              onChange={setDangerThreshold}
              placeholder="security"
            />
          </div>
        </CollapsibleSubsection>
      </div>

      <SaveButton
        pending={save.isPending}
        seeded={seeded}
        success={save.isSuccess}
        onClick={() => save.mutate()}
      />
    </section>
  );
}

// ── Work Style tab ──────────────────────────────────────────────────

function WorkStyleTab() {
  const qc = useQueryClient();
  const profile = useQuery({
    queryKey: ["onboarding", "profile"],
    queryFn: getProfile,
    staleTime: 10_000,
  });

  const [seeded, setSeeded] = useState(false);

  const [hates, setHates] = useState<string[]>([]);
  const [loves, setLoves] = useState<string[]>([]);
  const [hoursActive, setHoursActive] = useState<string[]>([]);
  const [hoursBreaks, setHoursBreaks] = useState<string[]>([]);
  const [rabbitHoles, setRabbitHoles] = useState("");
  const [uiApproach, setUiApproach] = useState("");
  const [uiAutonomy, setUiAutonomy] = useState("");

  // Scalars
  const [venv, setVenv] = useState("");
  const [domain, setDomain] = useState("");
  const [processNaming, setProcessNaming] = useState("");
  const [gpuAllocation, setGpuAllocation] = useState("");
  const [database, setDatabase] = useState("");
  const [secrets, setSecrets] = useState("");
  const [cleanup, setCleanup] = useState("");
  const [modules, setModules] = useState("");

  useEffect(() => {
    if (seeded || !profile.data) return;
    const w = (profile.data.work_style as WorkStyleSection | undefined) ?? {};
    setHates(flattenRuleList(w.hates));
    setLoves(flattenRuleList(w.loves));
    setHoursActive(w.hours?.active ?? []);
    setHoursBreaks(w.hours?.breaks ?? []);
    setRabbitHoles(w.rabbit_holes ?? "");
    setUiApproach(w.ui_philosophy?.approach ?? "");
    setUiAutonomy(w.ui_philosophy?.agent_autonomy ?? "");
    setVenv(w.venv ?? "");
    setDomain(w.domain ?? "");
    setProcessNaming(w.process_naming ?? "");
    setGpuAllocation(w.gpu_allocation ?? "");
    setDatabase(w.database ?? "");
    setSecrets(w.secrets ?? "");
    // cleanup may arrive as boolean — coerce to string for the text input.
    setCleanup(
      typeof w.cleanup === "boolean" ? String(w.cleanup) : w.cleanup ?? "",
    );
    setModules(w.modules ?? "");
    setSeeded(true);
  }, [profile.data, seeded]);

  const save = useMutation({
    mutationFn: async () => {
      const current =
        (profile.data?.work_style as WorkStyleSection | undefined) ?? {};
      const work_style: WorkStyleSection = {
        ...current,
        hates: stripEmpty(hates),
        loves: stripEmpty(loves),
        hours: {
          ...(current.hours ?? {}),
          active: stripEmpty(hoursActive),
          breaks: stripEmpty(hoursBreaks),
        },
        rabbit_holes: rabbitHoles.trim(),
        ui_philosophy: {
          ...(current.ui_philosophy ?? {}),
          approach: uiApproach.trim(),
          agent_autonomy: uiAutonomy.trim(),
        },
        venv: venv.trim(),
        domain: domain.trim(),
        process_naming: processNaming.trim(),
        gpu_allocation: gpuAllocation.trim(),
        database: database.trim(),
        secrets: secrets.trim(),
        cleanup: cleanup.trim(),
        modules: modules.trim(),
      };
      return patchProfile({ work_style });
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["onboarding", "profile"] });
      toast.success("Work style saved");
    },
    onError: (err) => {
      toast.error("Save failed", {
        description: err instanceof Error ? err.message : "unknown error",
      });
    },
  });

  if (profile.isLoading) return <LoadingPane />;
  if (profile.isError) return <ErrorPane error={profile.error} />;

  return (
    <section className="space-y-8 max-w-2xl">
      <div>
        <h2 className="text-sm font-semibold text-fg">
          How do you work?
        </h2>
        <p className="mt-1 text-2xs text-tertiary">
          Active hours, loves/hates, and the system conventions every agent
          must follow.
        </p>
      </div>

      {/* Loves + Hates */}
      <div className="space-y-4">
        <SubsectionHeading
          title="Energy"
          hint="What drains you vs what gives energy. Agents steer toward loves, away from hates."
        />

        <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
          <div>
            <FieldLabel>Hates</FieldLabel>
            <StringListEditor
              value={hates}
              onChange={setHates}
              placeholder="repetition"
            />
          </div>
          <div>
            <FieldLabel>Loves</FieldLabel>
            <StringListEditor
              value={loves}
              onChange={setLoves}
              placeholder="novelty"
            />
          </div>
        </div>
      </div>

      {/* Hours */}
      <div className="space-y-4">
        <SubsectionHeading
          title="Hours"
          hint="When you work. Use HH:MM-HH:MM for active blocks; HH:MM-HH:MM label for breaks."
        />

        <div>
          <FieldLabel>Active</FieldLabel>
          <StringListEditor
            value={hoursActive}
            onChange={setHoursActive}
            placeholder="09:00-12:00"
          />
        </div>

        <div>
          <FieldLabel>Breaks</FieldLabel>
          <StringListEditor
            value={hoursBreaks}
            onChange={setHoursBreaks}
            placeholder="12:00-13:00 lunch"
          />
        </div>
      </div>

      {/* Philosophy */}
      <div className="space-y-4">
        <SubsectionHeading
          title="Philosophy"
          hint="Guardrails agents follow when making aesthetic or depth-of-research decisions."
        />

        <div>
          <FieldLabel>Rabbit holes</FieldLabel>
          <Textarea
            value={rabbitHoles}
            onChange={(e) => setRabbitHoles(e.target.value)}
            placeholder="go deep enough to stay ahead, but not so deep it kills momentum"
          />
        </div>

        <div>
          <FieldLabel>UI approach</FieldLabel>
          <Input
            value={uiApproach}
            onChange={(e) => setUiApproach(e.target.value)}
            placeholder="design-system-first, single source of truth"
          />
        </div>

        <div>
          <FieldLabel>Agent autonomy</FieldLabel>
          <Textarea
            value={uiAutonomy}
            onChange={(e) => setUiAutonomy(e.target.value)}
            placeholder="can make aesthetic decisions via design system, never inline"
          />
        </div>
      </div>

      {/* System config */}
      <div className="space-y-4">
        <SubsectionHeading
          title="System config"
          hint="Machine-level conventions (venv, domain, secrets store, GPU allocator)."
        />

        <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
          <div>
            <FieldLabel>Venv</FieldLabel>
            <Input
              value={venv}
              onChange={(e) => setVenv(e.target.value)}
              placeholder="/path/to/.venv/bin/python"
            />
          </div>
          <div>
            <FieldLabel>Domain</FieldLabel>
            <Input
              value={domain}
              onChange={(e) => setDomain(e.target.value)}
              placeholder="demo.lan"
            />
          </div>
          <div>
            <FieldLabel>Process naming</FieldLabel>
            <Input
              value={processNaming}
              onChange={(e) => setProcessNaming(e.target.value)}
              placeholder="tm-{name}"
            />
          </div>
          <div>
            <FieldLabel>GPU allocation</FieldLabel>
            <Input
              value={gpuAllocation}
              onChange={(e) => setGpuAllocation(e.target.value)}
              placeholder="gpu-lease"
            />
          </div>
          <div>
            <FieldLabel>Database</FieldLabel>
            <Input
              value={database}
              onChange={(e) => setDatabase(e.target.value)}
              placeholder="local_supabase"
            />
          </div>
          <div>
            <FieldLabel>Secrets</FieldLabel>
            <Input
              value={secrets}
              onChange={(e) => setSecrets(e.target.value)}
              placeholder="tm-keyring"
            />
          </div>
          <div>
            <FieldLabel>Cleanup</FieldLabel>
            <Input
              value={cleanup}
              onChange={(e) => setCleanup(e.target.value)}
              placeholder="true"
            />
          </div>
          <div>
            <FieldLabel>Modules</FieldLabel>
            <Input
              value={modules}
              onChange={(e) => setModules(e.target.value)}
              placeholder="subprocess"
            />
          </div>
        </div>
      </div>

      <SaveButton
        pending={save.isPending}
        seeded={seeded}
        success={save.isSuccess}
        onClick={() => save.mutate()}
      />
    </section>
  );
}

// ── Cognitive Style tab ─────────────────────────────────────────────

type CognitiveStyleSection = {
  neurotype?: string[];
  strengths?: string[];
  implications?: string[];
  ocd_tendency?: boolean;
  [key: string]: unknown;
};

function CognitiveStyleTab() {
  const qc = useQueryClient();
  const profile = useQuery({
    queryKey: ["onboarding", "profile"],
    queryFn: getProfile,
    staleTime: 10_000,
  });

  const [seeded, setSeeded] = useState(false);
  const [neurotype, setNeurotype] = useState<string[]>([]);
  const [strengths, setStrengths] = useState<string[]>([]);
  const [implications, setImplications] = useState<string[]>([]);
  const [ocdTendency, setOcdTendency] = useState<boolean>(false);
  const [showOverrides, setShowOverrides] = useState(false);

  useEffect(() => {
    if (seeded || !profile.data) return;
    const c =
      (profile.data.cognitive_style as CognitiveStyleSection | undefined) ?? {};
    setNeurotype(c.neurotype ?? []);
    setStrengths(flattenRuleList(c.strengths));
    setImplications(flattenRuleList(c.implications));
    setOcdTendency(Boolean(c.ocd_tendency));
    setSeeded(true);
  }, [profile.data, seeded]);

  // behavioral_overrides lives at top-level of the profile (written by the
  // cognitive-traits questionnaire). Read-only display here — the user re-runs
  // the questionnaire via /onboarding?step=cognitive_style to regenerate it.
  const behavioralOverrides =
    (profile.data?.behavioral_overrides as Record<string, unknown> | undefined) ??
    null;

  const save = useMutation({
    mutationFn: async () => {
      const current =
        (profile.data?.cognitive_style as CognitiveStyleSection | undefined) ??
        {};
      const cognitive_style: CognitiveStyleSection = {
        ...current,
        neurotype: stripEmpty(neurotype),
        strengths: stripEmpty(strengths),
        implications: stripEmpty(implications),
        ocd_tendency: ocdTendency,
      };
      return patchProfile({ cognitive_style });
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["onboarding", "profile"] });
      toast.success("Cognitive style saved");
    },
    onError: (err) => {
      toast.error("Save failed", {
        description: err instanceof Error ? err.message : "unknown error",
      });
    },
  });

  if (profile.isLoading) return <LoadingPane />;
  if (profile.isError) return <ErrorPane error={profile.error} />;

  return (
    <section className="space-y-8 max-w-2xl">
      <div>
        <h2 className="text-sm font-semibold text-fg">How does your mind work?</h2>
        <p className="mt-1 text-2xs text-tertiary">
          Neurotype, strengths, and implications agents read at bootstrap.
          Behavioral overrides are derived from the cognitive trait
          questionnaire — see below.
        </p>
      </div>

      <div className="space-y-4">
        <SubsectionHeading
          title="Neurotype"
          hint="Labels you identify with (e.g. ADHD, dyslexia). Free-form — no clinical validation."
        />
        <StringListEditor
          value={neurotype}
          onChange={setNeurotype}
          placeholder="e.g. ADHD"
        />
      </div>

      <div className="space-y-4">
        <SubsectionHeading
          title="Strengths"
          hint="Cognitive strengths agents should lean on."
        />
        <StringListEditor
          value={strengths}
          onChange={setStrengths}
          placeholder="rapid pattern recognition across domains"
          multiline
        />
      </div>

      <div className="space-y-4">
        <SubsectionHeading
          title="Implications"
          hint="How these traits translate into agent-facing behavioral rules."
        />
        <StringListEditor
          value={implications}
          onChange={setImplications}
          placeholder="systems thinker — thinks in relations and overall solutions"
          multiline
        />
      </div>

      <div className="space-y-4">
        <SubsectionHeading
          title="OCD tendency"
          hint="When on, agents treat duplication/inconsistency as a higher-cost defect."
        />
        <div className="flex items-center gap-3">
          <Switch
            id="ocd-tendency"
            checked={ocdTendency}
            onCheckedChange={setOcdTendency}
          />
          <label htmlFor="ocd-tendency" className="text-xs text-fg">
            {ocdTendency ? "Enabled" : "Disabled"}
          </label>
        </div>
      </div>

      <SaveButton
        pending={save.isPending}
        seeded={seeded}
        success={save.isSuccess}
        onClick={() => save.mutate()}
      />

      {/* Behavioral overrides — read-only */}
      <div className="space-y-3 border-t border-border pt-6">
        <SubsectionHeading
          title="Behavioral overrides"
          hint="Derived from the cognitive trait questionnaire. Don't hand-edit — re-run the questionnaire to regenerate."
        />
        <div className="flex flex-wrap items-center gap-3">
          <Button
            size="sm"
            variant="outline"
            type="button"
            onClick={() => setShowOverrides((v) => !v)}
            disabled={!behavioralOverrides}
          >
            {showOverrides ? "Hide" : "Show"} current overrides
          </Button>
          <Link
            to="/onboarding?step=cognitive_style"
            className="text-2xs text-accent hover:underline"
          >
            Re-run cognitive traits questionnaire →
          </Link>
        </div>
        {!behavioralOverrides && (
          <p className="text-2xs text-tertiary italic">
            No overrides yet — run the cognitive traits questionnaire to
            populate this.
          </p>
        )}
        {showOverrides && behavioralOverrides && (
          <pre className="max-h-80 overflow-auto rounded border border-border bg-surface-elevated p-3 text-2xs text-fg">
            {JSON.stringify(behavioralOverrides, null, 2)}
          </pre>
        )}
      </div>
    </section>
  );
}

// ── Principles tab ──────────────────────────────────────────────────

type Principle = { id: string; name: string; description: string };

function PrinciplesTab() {
  const qc = useQueryClient();

  // /api/onboarding/principles returns {available, selected, custom}. It is
  // the single source of truth — we write back through the same endpoint
  // (POST) rather than the generic profile PATCH.
  const principlesQuery = useQuery({
    queryKey: ["onboarding", "principles"],
    queryFn: () => onboardingApi.principles(),
    staleTime: 10_000,
  });

  const [seeded, setSeeded] = useState(false);
  const [selected, setSelected] = useState<string[]>([]);
  const [custom, setCustom] = useState<Principle[]>([]);

  // Inline custom-principle editor state
  const [customNeed, setCustomNeed] = useState("");
  const [formulating, setFormulating] = useState(false);
  const [customId, setCustomId] = useState("");
  const [customName, setCustomName] = useState("");
  const [customDesc, setCustomDesc] = useState("");

  useEffect(() => {
    if (seeded || !principlesQuery.data) return;
    setSelected(principlesQuery.data.selected ?? []);
    setCustom(principlesQuery.data.custom ?? []);
    setSeeded(true);
  }, [principlesQuery.data, seeded]);

  const save = useMutation({
    mutationFn: async () =>
      onboardingApi.savePrinciples(stripEmpty(selected), custom),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["onboarding", "principles"] });
      qc.invalidateQueries({ queryKey: ["onboarding", "profile"] });
      toast.success("Principles saved");
    },
    onError: (err) => {
      toast.error("Save failed", {
        description: err instanceof Error ? err.message : "unknown error",
      });
    },
  });

  if (principlesQuery.isLoading) return <LoadingPane />;
  if (principlesQuery.isError) return <ErrorPane error={principlesQuery.error} />;

  const available: Principle[] = principlesQuery.data?.available ?? [];

  // Merged pool: available (built-in) + custom. Look up by id for display.
  const pool: Record<string, Principle> = {};
  for (const p of available) pool[p.id] = p;
  for (const p of custom) pool[p.id] = p;

  const selectedRows = selected
    .map((id) => pool[id])
    .filter((p): p is Principle => p !== undefined);
  const unselected = Object.values(pool).filter((p) => !selected.includes(p.id));

  const toggle = (id: string) => {
    setSelected((prev) =>
      prev.includes(id) ? prev.filter((x) => x !== id) : [...prev, id],
    );
  };
  const moveUp = (idx: number) => {
    if (idx <= 0) return;
    const next = selected.slice();
    const a = next[idx - 1] ?? "";
    const b = next[idx] ?? "";
    next[idx - 1] = b;
    next[idx] = a;
    setSelected(next);
  };
  const moveDown = (idx: number) => {
    if (idx >= selected.length - 1) return;
    const next = selected.slice();
    const a = next[idx] ?? "";
    const b = next[idx + 1] ?? "";
    next[idx] = b;
    next[idx + 1] = a;
    setSelected(next);
  };
  const removeSelected = (idx: number) => {
    setSelected(selected.filter((_, i) => i !== idx));
  };

  const slugify = (s: string): string =>
    s
      .trim()
      .toLowerCase()
      .replace(/[^a-z0-9]+/g, "-")
      .replace(/^-+|-+$/g, "")
      .slice(0, 40);

  const addCustomManual = () => {
    const id = customId.trim() || `C-${slugify(customName)}`;
    const name = customName.trim();
    const description = customDesc.trim();
    if (!id || !name || !description) {
      toast.error("Custom principle needs id, name, and description");
      return;
    }
    if (pool[id]) {
      toast.error(`Principle id "${id}" already exists`);
      return;
    }
    setCustom([...custom, { id, name, description }]);
    setSelected([...selected, id]);
    setCustomId("");
    setCustomName("");
    setCustomDesc("");
  };

  const formulateFromNeed = async () => {
    const need = customNeed.trim();
    if (need.length < 3) {
      toast.error("Describe the need in at least a few words");
      return;
    }
    setFormulating(true);
    try {
      const p = await onboardingApi.formulatePrinciple(need);
      const finalId = pool[p.id]
        ? `${p.id}-${Math.random().toString(36).slice(2, 6)}`
        : p.id;
      const next: Principle = { ...p, id: finalId };
      setCustom([...custom, next]);
      setSelected([...selected, next.id]);
      setCustomNeed("");
    } catch (err) {
      toast.error("Formulate failed", {
        description: err instanceof Error ? err.message : "unknown error",
      });
    } finally {
      setFormulating(false);
    }
  };

  const removeCustom = (id: string) => {
    setCustom(custom.filter((p) => p.id !== id));
    setSelected(selected.filter((x) => x !== id));
  };

  return (
    <section className="space-y-8 max-w-3xl">
      <div>
        <h2 className="text-sm font-semibold text-fg">
          What matters to you?
        </h2>
        <p className="mt-1 text-2xs text-tertiary">
          These principles guide how agents make decisions on your behalf.
          Selected principles appear in the behavioral contract; order sets
          priority (highest first).
        </p>
      </div>

      <div className="grid grid-cols-1 gap-6 md:grid-cols-2">
        {/* Available column */}
        <div className="space-y-3">
          <SubsectionHeading
            title="Available"
            hint="Built-in + custom principles not currently selected."
          />
          {unselected.length === 0 && (
            <p className="text-2xs text-tertiary italic">
              Everything is selected.
            </p>
          )}
          <ul className="space-y-1.5">
            {unselected.map((p) => (
              <li
                key={p.id}
                className="flex items-start gap-2 rounded border border-border bg-surface px-3 py-2"
              >
                <div className="flex-1 min-w-0">
                  <div className="text-2xs font-semibold text-fg" title={p.id}>
                    {p.name}
                  </div>
                  <div className="mt-0.5 text-2xs text-tertiary">
                    {p.description}
                  </div>
                </div>
                <button
                  type="button"
                  onClick={() => toggle(p.id)}
                  aria-label={`Select ${p.id}`}
                  className="shrink-0 rounded border border-border p-1 text-tertiary hover:border-accent hover:text-accent"
                >
                  <Plus className="h-3.5 w-3.5" />
                </button>
              </li>
            ))}
          </ul>
        </div>

        {/* Selected column */}
        <div className="space-y-3">
          <SubsectionHeading
            title="Selected"
            hint="Order = priority. Top is highest."
          />
          {selectedRows.length === 0 && (
            <p className="text-2xs text-tertiary italic">
              None selected yet — pick from the Available column.
            </p>
          )}
          <ol className="space-y-1.5">
            {selectedRows.map((p, idx) => (
              <li
                key={p.id}
                className="flex items-start gap-2 rounded border border-accent/30 bg-accent-subtle px-3 py-2"
              >
                <span className="mt-0.5 w-5 shrink-0 text-2xs font-bold text-accent">
                  {idx + 1}.
                </span>
                <div className="flex-1 min-w-0">
                  <div className="text-2xs font-semibold text-fg" title={p.id}>
                    {p.name}
                  </div>
                  <div className="mt-0.5 text-2xs text-tertiary">
                    {p.description}
                  </div>
                </div>
                <div className="flex shrink-0 items-center">
                  <button
                    type="button"
                    onClick={() => moveUp(idx)}
                    disabled={idx === 0}
                    aria-label="Move up"
                    className="p-1 text-tertiary hover:text-fg disabled:opacity-30"
                  >
                    <ArrowUp className="h-3.5 w-3.5" />
                  </button>
                  <button
                    type="button"
                    onClick={() => moveDown(idx)}
                    disabled={idx === selectedRows.length - 1}
                    aria-label="Move down"
                    className="p-1 text-tertiary hover:text-fg disabled:opacity-30"
                  >
                    <ArrowDown className="h-3.5 w-3.5" />
                  </button>
                  <button
                    type="button"
                    onClick={() => removeSelected(idx)}
                    aria-label="Remove"
                    className="p-1 text-tertiary hover:text-error"
                  >
                    <X className="h-3.5 w-3.5" />
                  </button>
                </div>
              </li>
            ))}
          </ol>
        </div>
      </div>

      {/* Custom principles list (for removing custom ones entirely) */}
      {custom.length > 0 && (
        <div className="space-y-3">
          <SubsectionHeading
            title="Your custom principles"
            hint="Remove entirely from the custom pool (unselecting above only hides them)."
          />
          <ul className="space-y-1.5">
            {custom.map((p) => (
              <li
                key={p.id}
                className="flex items-start gap-2 rounded border border-border bg-surface-elevated px-3 py-2"
              >
                <div className="flex-1 min-w-0">
                  <div className="text-2xs font-semibold text-fg">
                    {p.id}: {p.name}
                  </div>
                  <div className="mt-0.5 text-2xs text-tertiary">
                    {p.description}
                  </div>
                </div>
                <button
                  type="button"
                  onClick={() => removeCustom(p.id)}
                  aria-label={`Delete ${p.id}`}
                  className="shrink-0 p-1 text-tertiary hover:text-error"
                >
                  <X className="h-3.5 w-3.5" />
                </button>
              </li>
            ))}
          </ul>
        </div>
      )}

      {/* Add custom principle */}
      <div className="space-y-4 rounded border border-border bg-surface-elevated p-4">
        <SubsectionHeading
          title="Add a custom principle"
          hint="Either describe a need and let an LLM formulate it, or enter all three fields yourself."
        />

        {/* LLM formulation */}
        <div className="space-y-2">
          <FieldLabel>Need → formulate with LLM</FieldLabel>
          <div className="flex items-start gap-2">
            <Textarea
              value={customNeed}
              onChange={(e) => setCustomNeed(e.target.value)}
              placeholder="I want agents to always verify claims with citations before asserting"
              className="min-h-16"
            />
            <Button
              size="sm"
              type="button"
              onClick={formulateFromNeed}
              disabled={formulating || customNeed.trim().length < 3}
            >
              {formulating ? (
                <Loader2 className="h-3.5 w-3.5 animate-spin" />
              ) : (
                <Sparkles className="h-3.5 w-3.5" />
              )}
              Formulate
            </Button>
          </div>
        </div>

        {/* Manual entry */}
        <div className="space-y-2 border-t border-border pt-3">
          <FieldLabel>Or enter manually</FieldLabel>
          <div className="grid grid-cols-1 gap-2 md:grid-cols-[1fr_2fr]">
            <Input
              value={customId}
              onChange={(e) => setCustomId(e.target.value)}
              placeholder="id (auto from name)"
            />
            <Input
              value={customName}
              onChange={(e) => setCustomName(e.target.value)}
              placeholder="short name (e.g. CITE-FIRST)"
            />
          </div>
          <Textarea
            value={customDesc}
            onChange={(e) => setCustomDesc(e.target.value)}
            placeholder="One-sentence description of the principle."
          />
          <Button
            size="sm"
            type="button"
            variant="outline"
            onClick={addCustomManual}
            disabled={!customName.trim() || !customDesc.trim()}
          >
            <Plus className="h-3.5 w-3.5" />
            Add principle
          </Button>
        </div>
      </div>

      <SaveButton
        pending={save.isPending}
        seeded={seeded}
        success={save.isSuccess}
        onClick={() => save.mutate()}
      />
    </section>
  );
}

// ── Boundaries tab ──────────────────────────────────────────────────

type BoundariesSection = {
  ok_autonomous?: string[];
  never_without_asking?: string[];
  interruption_policy?: string;
  [key: string]: unknown;
};

function BoundariesTab() {
  const qc = useQueryClient();
  const profile = useQuery({
    queryKey: ["onboarding", "profile"],
    queryFn: getProfile,
    staleTime: 10_000,
  });

  const [seeded, setSeeded] = useState(false);
  const [okAutonomous, setOkAutonomous] = useState<string[]>([]);
  const [neverWithoutAsking, setNeverWithoutAsking] = useState<string[]>([]);
  const [interruptionPolicy, setInterruptionPolicy] = useState("");

  useEffect(() => {
    if (seeded || !profile.data) return;
    const b =
      (profile.data.boundaries as BoundariesSection | undefined) ?? {};
    setOkAutonomous(b.ok_autonomous ?? []);
    setNeverWithoutAsking(b.never_without_asking ?? []);
    setInterruptionPolicy(b.interruption_policy ?? "");
    setSeeded(true);
  }, [profile.data, seeded]);

  const save = useMutation({
    mutationFn: async () => {
      const current =
        (profile.data?.boundaries as BoundariesSection | undefined) ?? {};
      const boundaries: BoundariesSection = {
        ...current,
        ok_autonomous: stripEmpty(okAutonomous),
        never_without_asking: stripEmpty(neverWithoutAsking),
        interruption_policy: interruptionPolicy.trim(),
      };
      return patchProfile({ boundaries });
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["onboarding", "profile"] });
      toast.success("Boundaries saved");
    },
    onError: (err) => {
      toast.error("Save failed", {
        description: err instanceof Error ? err.message : "unknown error",
      });
    },
  });

  if (profile.isLoading) return <LoadingPane />;
  if (profile.isError) return <ErrorPane error={profile.error} />;

  return (
    <section className="space-y-8 max-w-3xl">
      <div>
        <h2 className="text-sm font-semibold text-fg">
          What can agents do without asking?
        </h2>
        <p className="mt-1 text-2xs text-tertiary">
          Pre-approved actions on the left, actions that always require your
          explicit OK on the right. Interruption policy below covers ambiguous
          cases.
        </p>
      </div>

      {/* CAN / MUST-ASK 2-col grid */}
      <div className="grid grid-cols-1 gap-6 md:grid-cols-2">
        <div className="space-y-2">
          <SubsectionHeading
            title="CAN — autonomous"
            hint="Pre-approved. Agents do these without stopping."
          />
          <StringListEditor
            value={okAutonomous}
            onChange={setOkAutonomous}
            placeholder="spend money if budget clear"
            multiline
          />
        </div>

        <div className="space-y-2">
          <SubsectionHeading
            title="MUST ASK — require approval"
            hint="Always stop and confirm — even if the task would be faster otherwise."
          />
          <StringListEditor
            value={neverWithoutAsking}
            onChange={setNeverWithoutAsking}
            placeholder="delete files"
            multiline
          />
        </div>
      </div>

      {/* Interruption policy full-width */}
      <div className="space-y-2">
        <SubsectionHeading
          title="Interruption policy"
          hint="When should agents interrupt you vs fix silently and log it? One prose rule."
        />
        <Textarea
          value={interruptionPolicy}
          onChange={(e) => setInterruptionPolicy(e.target.value)}
          placeholder="interrupt only if you need my decision. otherwise fix it and log it."
          className="min-h-20"
        />
      </div>

      <SaveButton
        pending={save.isPending}
        seeded={seeded}
        success={save.isSuccess}
        onClick={() => save.mutate()}
      />
    </section>
  );
}

// ── Decision Style tab ──────────────────────────────────────────────

type DecisionStyleSection = {
  build_vs_buy?: string;
  key_principles?: string[];
  research_first?: boolean;
  risk_tolerance?: string;
  speed_vs_quality?: string;
  evidence_over_assumptions?: boolean;
  [key: string]: unknown;
};

function DecisionStyleTab({
  onGotoPrinciples,
}: {
  onGotoPrinciples: () => void;
}) {
  const qc = useQueryClient();
  const profile = useQuery({
    queryKey: ["onboarding", "profile"],
    queryFn: getProfile,
    staleTime: 10_000,
  });

  const [seeded, setSeeded] = useState(false);
  const [buildVsBuy, setBuildVsBuy] = useState<string>("balanced");
  const [riskTolerance, setRiskTolerance] = useState<string>("moderate");
  const [speedVsQuality, setSpeedVsQuality] = useState<string>("systematic");
  const [researchFirst, setResearchFirst] = useState<boolean>(true);
  const [evidenceOverAssumptions, setEvidenceOverAssumptions] =
    useState<boolean>(true);
  // Read-only mirror of the principles tab — editing lives there.
  const [keyPrinciples, setKeyPrinciples] = useState<string[]>([]);

  useEffect(() => {
    if (seeded || !profile.data) return;
    const d =
      (profile.data.decision_style as DecisionStyleSection | undefined) ?? {};
    setBuildVsBuy(d.build_vs_buy ?? "balanced");
    setRiskTolerance(d.risk_tolerance ?? "moderate");
    setSpeedVsQuality(d.speed_vs_quality ?? "systematic");
    setResearchFirst(Boolean(d.research_first ?? true));
    setEvidenceOverAssumptions(Boolean(d.evidence_over_assumptions ?? true));
    setKeyPrinciples(d.key_principles ?? []);
    setSeeded(true);
  }, [profile.data, seeded]);

  const save = useMutation({
    mutationFn: async () => {
      const current =
        (profile.data?.decision_style as DecisionStyleSection | undefined) ??
        {};
      const decision_style: DecisionStyleSection = {
        ...current,
        build_vs_buy: buildVsBuy,
        risk_tolerance: riskTolerance,
        speed_vs_quality: speedVsQuality,
        research_first: researchFirst,
        evidence_over_assumptions: evidenceOverAssumptions,
        // key_principles is authored via Principles tab — preserve what's
        // on the server rather than what we seeded locally.
        key_principles: current.key_principles ?? [],
      };
      return patchProfile({ decision_style });
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["onboarding", "profile"] });
      toast.success("Decision style saved");
    },
    onError: (err) => {
      toast.error("Save failed", {
        description: err instanceof Error ? err.message : "unknown error",
      });
    },
  });

  if (profile.isLoading) return <LoadingPane />;
  if (profile.isError) return <ErrorPane error={profile.error} />;

  return (
    <section className="space-y-8 max-w-2xl">
      <div>
        <h2 className="text-sm font-semibold text-fg">
          How do you make decisions?
        </h2>
        <p className="mt-1 text-2xs text-tertiary">
          Agents use these defaults when choosing between alternatives
          (build-vs-buy, speed-vs-quality) and when weighing risk.
        </p>
      </div>

      {/* Tradeoff dropdowns */}
      <div className="space-y-4">
        <SubsectionHeading
          title="Tradeoffs"
          hint="Defaults for the three big axes. Override per-task when needed."
        />

        <div className="grid grid-cols-1 gap-3 md:grid-cols-3">
          <div>
            <FieldLabel>Build vs buy</FieldLabel>
            <Select value={buildVsBuy} onValueChange={setBuildVsBuy}>
              <SelectTrigger>
                <SelectValue placeholder="Select" />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="build">build</SelectItem>
                <SelectItem value="buy">buy</SelectItem>
                <SelectItem value="balanced">balanced</SelectItem>
              </SelectContent>
            </Select>
          </div>

          <div>
            <FieldLabel>Risk tolerance</FieldLabel>
            <Select value={riskTolerance} onValueChange={setRiskTolerance}>
              <SelectTrigger>
                <SelectValue placeholder="Select" />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="low">low</SelectItem>
                <SelectItem value="moderate">moderate</SelectItem>
                <SelectItem value="high">high</SelectItem>
              </SelectContent>
            </Select>
          </div>

          <div>
            <FieldLabel>Speed vs quality</FieldLabel>
            <Select value={speedVsQuality} onValueChange={setSpeedVsQuality}>
              <SelectTrigger>
                <SelectValue placeholder="Select" />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="speed">speed</SelectItem>
                <SelectItem value="systematic">systematic</SelectItem>
                <SelectItem value="quality">quality</SelectItem>
              </SelectContent>
            </Select>
          </div>
        </div>
      </div>

      {/* Bool toggles */}
      <div className="space-y-4">
        <SubsectionHeading
          title="Defaults"
          hint="On-by-default behaviors agents apply unless you override."
        />

        <div className="flex items-center gap-3">
          <Switch
            id="research-first"
            checked={researchFirst}
            onCheckedChange={setResearchFirst}
          />
          <label htmlFor="research-first" className="text-xs text-fg">
            Research first — investigate before acting on non-trivial changes
          </label>
        </div>

        <div className="flex items-center gap-3">
          <Switch
            id="evidence-over-assumptions"
            checked={evidenceOverAssumptions}
            onCheckedChange={setEvidenceOverAssumptions}
          />
          <label
            htmlFor="evidence-over-assumptions"
            className="text-xs text-fg"
          >
            Evidence over assumptions — cite sources, verify claims
          </label>
        </div>
      </div>

      {/* Key principles (read-only chips) */}
      <div className="space-y-3">
        <SubsectionHeading
          title="Key principles"
          hint="IDs of the principles that govern decisions. Edit in the Principles tab — this view is read-only."
        />
        {keyPrinciples.length === 0 ? (
          <p className="text-2xs text-tertiary italic">
            No principles selected yet.
          </p>
        ) : (
          <div className="flex flex-wrap gap-1.5">
            {keyPrinciples.map((id) => (
              <span
                key={id}
                className="rounded border border-accent/30 bg-accent-subtle px-2 py-0.5 text-2xs font-semibold text-accent"
              >
                {id}
              </span>
            ))}
          </div>
        )}
        <Button
          size="sm"
          variant="outline"
          type="button"
          onClick={onGotoPrinciples}
        >
          Edit in Principles tab
        </Button>
      </div>

      <SaveButton
        pending={save.isPending}
        seeded={seeded}
        success={save.isSuccess}
        onClick={() => save.mutate()}
      />
    </section>
  );
}

// ── Expertise tab ───────────────────────────────────────────────────

type ExpertiseSection = {
  strong?: string[];
  working?: string[];
  [key: string]: unknown;
};

type ExpertiseExtract = {
  strong: string[];
  working: string[];
};

// Normalizes the extractor response (`{expertise: {strong, working}}`) into
// a flat shape we can render + merge/replace with.
function extractExpertise(raw: Record<string, unknown>): ExpertiseExtract {
  const exp = (raw.expertise as Record<string, unknown> | undefined) ?? {};
  const strong = Array.isArray(exp.strong)
    ? (exp.strong as unknown[]).filter((x): x is string => typeof x === "string")
    : [];
  const working = Array.isArray(exp.working)
    ? (exp.working as unknown[]).filter((x): x is string => typeof x === "string")
    : [];
  return { strong, working };
}

function unionUnique(a: string[], b: string[]): string[] {
  const out: string[] = [];
  const seen = new Set<string>();
  for (const item of [...a, ...b]) {
    const trimmed = item.trim();
    if (!trimmed) continue;
    const key = trimmed.toLowerCase();
    if (seen.has(key)) continue;
    seen.add(key);
    out.push(trimmed);
  }
  return out;
}

function ExpertiseTab() {
  const qc = useQueryClient();
  const profile = useQuery({
    queryKey: ["onboarding", "profile"],
    queryFn: getProfile,
    staleTime: 10_000,
  });

  const [seeded, setSeeded] = useState(false);
  const [strong, setStrong] = useState<string[]>([]);
  const [working, setWorking] = useState<string[]>([]);

  useEffect(() => {
    if (seeded || !profile.data) return;
    const e = (profile.data.expertise as ExpertiseSection | undefined) ?? {};
    setStrong(e.strong ?? []);
    setWorking(e.working ?? []);
    setSeeded(true);
  }, [profile.data, seeded]);

  const save = useMutation({
    mutationFn: async (override?: ExpertiseExtract) => {
      const current =
        (profile.data?.expertise as ExpertiseSection | undefined) ?? {};
      const next: ExpertiseSection = {
        ...current,
        strong: stripEmpty(override?.strong ?? strong),
        working: stripEmpty(override?.working ?? working),
      };
      return patchProfile({ expertise: next });
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["onboarding", "profile"] });
      toast.success("Expertise saved");
    },
    onError: (err) => {
      toast.error("Save failed", {
        description: err instanceof Error ? err.message : "unknown error",
      });
    },
  });

  // Replace = overwrite strong+working with extracted, then save
  const replaceWithExtract = async (extract: ExpertiseExtract) => {
    const next: ExpertiseExtract = {
      strong: stripEmpty(extract.strong),
      working: stripEmpty(extract.working),
    };
    setStrong(next.strong);
    setWorking(next.working);
    await save.mutateAsync(next);
  };

  // Merge = dedup union with current
  const mergeWithExtract = async (extract: ExpertiseExtract) => {
    const next: ExpertiseExtract = {
      strong: unionUnique(strong, extract.strong),
      working: unionUnique(working, extract.working),
    };
    setStrong(next.strong);
    setWorking(next.working);
    await save.mutateAsync(next);
  };

  if (profile.isLoading) return <LoadingPane />;
  if (profile.isError) return <ErrorPane error={profile.error} />;

  return (
    <section className="space-y-8 max-w-3xl">
      <div>
        <h2 className="text-sm font-semibold text-fg">
          What are you expert in?
        </h2>
        <p className="mt-1 text-2xs text-tertiary">
          <strong>Strong</strong> = deep-skill domains agents should defer to
          you on. <strong>Working</strong> = active-learning domains where
          agents should teach / help you grow. Import from LinkedIn / GitHub /
          URLs below to backfill fast.
        </p>
      </div>

      {/* Direct editing */}
      <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
        <div>
          <FieldLabel>Strong domains</FieldLabel>
          <StringListEditor
            value={strong}
            onChange={setStrong}
            placeholder="system architecture"
          />
        </div>
        <div>
          <FieldLabel>Working domains</FieldLabel>
          <StringListEditor
            value={working}
            onChange={setWorking}
            placeholder="prompt engineering"
          />
        </div>
      </div>

      <SaveButton
        pending={save.isPending}
        seeded={seeded}
        success={save.isSuccess}
        onClick={() => save.mutate(undefined)}
      />

      {/* Import from sources */}
      <div className="space-y-4 border-t border-border pt-6">
        <SubsectionHeading
          title="Import from sources"
          hint="Scrape LinkedIn / GitHub / URLs, preview what the extractor inferred, then Replace or Merge into your expertise."
        />

        <div className="grid grid-cols-1 gap-4 md:grid-cols-3">
          <LinkedInCard
            onReplace={replaceWithExtract}
            onMerge={mergeWithExtract}
          />
          <GitHubCard
            onReplace={replaceWithExtract}
            onMerge={mergeWithExtract}
          />
          <UrlCard
            onReplace={replaceWithExtract}
            onMerge={mergeWithExtract}
          />
        </div>
      </div>
    </section>
  );
}

// ── Import cards ────────────────────────────────────────────────────

function ImportCard({
  title,
  icon,
  children,
}: {
  title: string;
  icon: React.ReactNode;
  children: React.ReactNode;
}) {
  return (
    <div className="flex flex-col gap-3 rounded border border-border bg-surface-elevated p-4">
      <div className="flex items-center gap-2">
        <span className="text-tertiary">{icon}</span>
        <h4 className="text-xs font-semibold text-fg">{title}</h4>
      </div>
      {children}
    </div>
  );
}

function ExtractPreview({
  extract,
  onReplace,
  onMerge,
  pending,
}: {
  extract: ExpertiseExtract;
  onReplace: () => void;
  onMerge: () => void;
  pending: boolean;
}) {
  const empty = extract.strong.length === 0 && extract.working.length === 0;
  return (
    <div className="space-y-2 border-t border-border pt-2">
      <FieldLabel>Extracted</FieldLabel>
      {empty ? (
        <p className="text-2xs text-tertiary italic">
          Nothing inferred. Try another source.
        </p>
      ) : (
        <pre className="max-h-40 overflow-auto rounded border border-border bg-surface p-2 text-2xs text-fg">
          {JSON.stringify(extract, null, 2)}
        </pre>
      )}
      <div className="flex flex-wrap gap-2 pt-1">
        <Button
          size="sm"
          type="button"
          onClick={onReplace}
          disabled={pending || empty}
        >
          {pending ? (
            <Loader2 className="h-3.5 w-3.5 animate-spin" />
          ) : null}
          Replace
        </Button>
        <Button
          size="sm"
          variant="outline"
          type="button"
          onClick={onMerge}
          disabled={pending || empty}
        >
          Merge
        </Button>
      </div>
    </div>
  );
}

function LinkedInCard({
  onReplace,
  onMerge,
}: {
  onReplace: (x: ExpertiseExtract) => Promise<void>;
  onMerge: (x: ExpertiseExtract) => Promise<void>;
}) {
  const [file, setFile] = useState<File | null>(null);
  const [extracting, setExtracting] = useState(false);
  const [applying, setApplying] = useState(false);
  const [extract, setExtract] = useState<ExpertiseExtract | null>(null);

  const upload = async () => {
    if (!file) return;
    setExtracting(true);
    setExtract(null);
    try {
      const raw = await onboardingApi.extractLinkedIn(file);
      setExtract(extractExpertise(raw));
    } catch (err) {
      toast.error("LinkedIn extraction failed", {
        description: err instanceof Error ? err.message : "unknown error",
      });
    } finally {
      setExtracting(false);
    }
  };

  const apply = async (fn: (x: ExpertiseExtract) => Promise<void>) => {
    if (!extract) return;
    setApplying(true);
    try {
      await fn(extract);
    } finally {
      setApplying(false);
    }
  };

  return (
    <ImportCard title="LinkedIn PDF" icon={<Upload className="h-3.5 w-3.5" />}>
      <p className="text-2xs text-tertiary">
        Export your profile as PDF from LinkedIn → More → Save to PDF, then
        upload it here.
      </p>
      <Input
        type="file"
        accept=".pdf,application/pdf"
        onChange={(e) => {
          const f = e.target.files?.[0] ?? null;
          setFile(f);
          setExtract(null);
        }}
      />
      <Button
        size="sm"
        type="button"
        onClick={upload}
        disabled={!file || extracting}
      >
        {extracting ? (
          <Loader2 className="h-3.5 w-3.5 animate-spin" />
        ) : (
          <Upload className="h-3.5 w-3.5" />
        )}
        Extract
      </Button>
      {extract && (
        <ExtractPreview
          extract={extract}
          onReplace={() => apply(onReplace)}
          onMerge={() => apply(onMerge)}
          pending={applying}
        />
      )}
    </ImportCard>
  );
}

function GitHubCard({
  onReplace,
  onMerge,
}: {
  onReplace: (x: ExpertiseExtract) => Promise<void>;
  onMerge: (x: ExpertiseExtract) => Promise<void>;
}) {
  const [handle, setHandle] = useState("");
  const [fetching, setFetching] = useState(false);
  const [applying, setApplying] = useState(false);
  const [extract, setExtract] = useState<ExpertiseExtract | null>(null);

  const fetchProfile = async () => {
    const h = handle.trim();
    if (!h) return;
    setFetching(true);
    setExtract(null);
    try {
      const raw = await onboardingApi.extractGitHub(h);
      setExtract(extractExpertise(raw));
    } catch (err) {
      toast.error("GitHub extraction failed", {
        description: err instanceof Error ? err.message : "unknown error",
      });
    } finally {
      setFetching(false);
    }
  };

  const apply = async (fn: (x: ExpertiseExtract) => Promise<void>) => {
    if (!extract) return;
    setApplying(true);
    try {
      await fn(extract);
    } finally {
      setApplying(false);
    }
  };

  return (
    <ImportCard title="GitHub" icon={<Github className="h-3.5 w-3.5" />}>
      <p className="text-2xs text-tertiary">
        Public profile only. Fetches your user info, top languages, and recent
        repo topics, then the LLM infers expertise.
      </p>
      <div>
        <FieldLabel>Username</FieldLabel>
        <Input
          value={handle}
          onChange={(e) => setHandle(e.target.value)}
          placeholder="octocat"
        />
      </div>
      <Button
        size="sm"
        type="button"
        onClick={fetchProfile}
        disabled={!handle.trim() || fetching}
      >
        {fetching ? (
          <Loader2 className="h-3.5 w-3.5 animate-spin" />
        ) : (
          <Github className="h-3.5 w-3.5" />
        )}
        Fetch
      </Button>
      {extract && (
        <ExtractPreview
          extract={extract}
          onReplace={() => apply(onReplace)}
          onMerge={() => apply(onMerge)}
          pending={applying}
        />
      )}
    </ImportCard>
  );
}

function UrlCard({
  onReplace,
  onMerge,
}: {
  onReplace: (x: ExpertiseExtract) => Promise<void>;
  onMerge: (x: ExpertiseExtract) => Promise<void>;
}) {
  const [urls, setUrls] = useState<string[]>([""]);
  const [scraping, setScraping] = useState(false);
  const [applying, setApplying] = useState(false);
  const [extract, setExtract] = useState<ExpertiseExtract | null>(null);

  const scrape = async () => {
    const cleaned = stripEmpty(urls).slice(0, 6);
    if (cleaned.length === 0) return;
    setScraping(true);
    setExtract(null);
    try {
      // Backend takes one URL per call; fan out then merge via /sources/merge
      // for N>1. Each call times out at 20s server-side.
      const extracts: Record<string, unknown>[] = [];
      for (const url of cleaned) {
        try {
          const raw = await onboardingApi.extractUrl(url);
          extracts.push(raw);
        } catch (err) {
          toast.error(`Scrape failed: ${url}`, {
            description: err instanceof Error ? err.message : "unknown error",
          });
        }
      }
      if (extracts.length === 0) {
        return;
      }
      const merged =
        extracts.length === 1
          ? extracts[0]!
          : await onboardingApi.mergeSources(extracts);
      setExtract(extractExpertise(merged));
    } finally {
      setScraping(false);
    }
  };

  const apply = async (fn: (x: ExpertiseExtract) => Promise<void>) => {
    if (!extract) return;
    setApplying(true);
    try {
      await fn(extract);
    } finally {
      setApplying(false);
    }
  };

  return (
    <ImportCard title="URL scrape" icon={<LinkIcon className="h-3.5 w-3.5" />}>
      <p className="text-2xs text-tertiary">
        Up to 6 public URLs (blog, portfolio, about page). Each is fetched and
        sent to the LLM extractor; results are merged.
      </p>
      <StringListEditor
        value={urls}
        onChange={(next) => setUrls(next.slice(0, 6))}
        placeholder="https://example.com/about"
      />
      <Button
        size="sm"
        type="button"
        onClick={scrape}
        disabled={scraping || stripEmpty(urls).length === 0}
      >
        {scraping ? (
          <Loader2 className="h-3.5 w-3.5 animate-spin" />
        ) : (
          <LinkIcon className="h-3.5 w-3.5" />
        )}
        Scrape
      </Button>
      {extract && (
        <ExtractPreview
          extract={extract}
          onReplace={() => apply(onReplace)}
          onMerge={() => apply(onMerge)}
          pending={applying}
        />
      )}
    </ImportCard>
  );
}

// ── Design tab ──────────────────────────────────────────────────────
//
// THIS TAB CHOOSES. IT DOES NOT AUTHOR — the charter's rule, applied on
// 2026-09-06 to the one screen that had never got it. A design system is
// created, edited, forked, scanned from a website and deleted in ONE place,
// /design-engine, and okuro-ds is duplicated rather than adjusted.
//
// Four controls left with that ruling and each was untrue in its own way:
//   Active brand      — wrote design.brand; measured, nothing has read that key
//                       since v0 went. Dead, and its hint claimed it overrode
//                       the preset.
//   Accent picker     — worked, but it was the single exception to "okuro-ds is
//                       not editable", and silently inert on every other kit.
//   Token overrides   — "applied on next page reload" was false for every name
//                       but three; the two that still matter have their own
//                       control below (the pulse outline).
//   Scrape a design   — forks a kit from a website. Real, and authoring: it now
//                       lives on /design-engine beside Duplicate.
//
// What is left is the choice, the preview of what that choice paints, and the
// two pulse knobs, which are a canvas preference rather than a design token.
//
// Persistence flows through PATCH /api/onboarding/profile. ProfilePatch
// uses extra="allow" (orchestrator/api/onboarding.py:139) so the `design`
// section round-trips end-to-end without an explicit field declaration.

function DesignTab() {
  const qc = useQueryClient();
  const profile = useQuery({
    queryKey: ["onboarding", "profile"],
    queryFn: getProfile,
    staleTime: 10_000,
  });

  // THE LIST IS KITS. This listed v0 design profiles, which is the store that
  // is being retired — and the preset picker it feeds was already measured
  // INERT for the app's appearance, because /engine.css never read a v0
  // profile. Listing kits makes the control name things that actually exist in
  // the one design-system module.
  const profilesQuery = useQuery({
    queryKey: ["design", "profiles"],
    queryFn: async () => {
      const res = await api<{ kits: Array<{ id: string; origin?: string }> }>(
        "/api/design-engine/kits",
      );
      return res.kits.map((k) => ({
        id: k.id,
        name: k.id,
        description: k.origin === "package" ? "shipped" : "your kit",
      }));
    },
    staleTime: 5_000,
  });
  /**
   * WHICH SYSTEM IS ACTIVE, ASKED OF THE LAYER THAT ANSWERS IT.
   *
   * This asked `/api/design/active`, which reports the v0 PROFILE store — a
   * different layer, and one the engine reads only as a legacy fallback.
   * Measured: it returns `architecture-noir` while the picker's options are
   * KITS, so the select's value was never among its items and Radix rendered
   * the placeholder. The control could not show the running system even when
   * one was chosen.
   *
   * `/api/design-engine/kits` now carries `active`, resolved by the same
   * `_active_kit_id` that `/engine.css` uses — so the picker and the renderer
   * cannot disagree. `design.brand` is still read from the profile: it feeds a
   * different control below.
   */
  const activeQuery = useQuery({
    queryKey: ["design", "active"],
    queryFn: () => api<{ active: string }>("/api/design-engine/kits"),
    staleTime: 5_000,
  });
  const [selectedPreset, setSelectedPreset] = useState<string>("");
  const [seeded, setSeeded] = useState(false);

  useEffect(() => {
    if (seeded) return;
    if (!profile.data || !activeQuery.data) return;
    const design = (profile.data.design as DesignSection | undefined) ?? {};
    // The kit the engine RESOLVED wins over the raw profile key: a stored
    // `design.kit` naming a system that no longer exists falls back to okuro's
    // own, and the picker has to show what is painting rather than what was
    // last written.
    setSelectedPreset(activeQuery.data.active || design.kit || "");
    setSeeded(true);
  }, [profile.data, activeQuery.data, seeded]);

  if (profile.isLoading) return <LoadingPane />;
  if (profile.isError) return <ErrorPane error={profile.error} />;

  return (
    <section className="space-y-8 max-w-3xl">
      <div>
        <h2 className="text-sm font-semibold text-fg">Design &amp; visual identity</h2>
        <p className="mt-1 text-2xs text-tertiary">
          Choose which design system paints okuro. To make your own, duplicate
          one on the Design Engine page and edit it there — okuro's own system
          ships fixed.
        </p>
      </div>

      <DesignPresetSection
        profiles={profilesQuery.data ?? []}
        selected={selectedPreset}
        /* THE SELECT MOVES, THE SHEET DOES NOT — not yet. This also called
           `reloadEngineSheet`, which re-fetches `/engine.css`; that fetch
           raced the PATCH below and usually won, so the server answered with
           the OLD kit and the app only re-themed on the next full reload.
           Measured: picking a second system turned the whole UI yellow — but
           not until F5. Persist first, re-fetch second. */
        onSelect={(id) => setSelectedPreset(id)}
        onPersist={async (id) => {
          const current =
            (profile.data?.design as DesignSection | undefined) ?? {};
          // `design.kit` IS THE ACTIVE DESIGN SYSTEM, and after 2026-09-06 it
          // is the ONLY thing the profile says about appearance. `brand` is
          // null-deleted rather than merely left alone: the control that wrote
          // it is gone, nothing has read it since v0, and a key that survives
          // its last reader is the shape the last three defects had. `profile`
          // stays — the engine reads it as a fallback for installs made before
          // `kit` existed.
          const next: DesignSection = { ...current, kit: id, brand: null };
          await patchProfile({ design: next });
          try {
            reloadEngineSheet();
          } catch {
            /* no-op — dev env without the link */
          }
          qc.invalidateQueries({ queryKey: ["onboarding", "profile"] });
          qc.invalidateQueries({ queryKey: ["design", "active"] });
        }}
        refetchProfiles={() => profilesQuery.refetch()}
      />

      <PulseOutlineSection
        current={readPulseOutlineOverride(profile.data)}
        onChange={async (next) => {
          applyPulseOutlineOverride(next);
          const current =
            (profile.data?.design as DesignSection | undefined) ?? {};
          const dict: Record<string, string> = {
            ...(current.overrides ?? {}),
            "--pulse-outlines-only": next.outlinesOnly ? "1" : "0",
            "--pulse-outline-strength": String(next.strength),
          };
          await patchProfile({ design: { ...current, overrides: dict } });
          qc.invalidateQueries({ queryKey: ["onboarding", "profile"] });
        }}
        onReset={async () => {
          clearPulseOutlineOverride();
          // Null-as-delete: backend _deep_merge pops keys whose incoming
          // value is null. Preserves any non-pulse overrides unchanged.
          await patchProfile({
            design: {
              overrides: {
                "--pulse-outlines-only": null,
                "--pulse-outline-strength": null,
              },
            },
          });
          qc.invalidateQueries({ queryKey: ["onboarding", "profile"] });
        }}
      />
    </section>
  );
}

function DictationTab() {
  const info = useQuery({ queryKey: ["stt", "tiers"], queryFn: sttApi.tiers });
  return (
    <section className="space-y-4 max-w-2xl">
      <div>
        <h2 className="text-sm font-semibold text-fg-primary">Dictation (speech-to-text)</h2>
        <p className="text-xs text-fg-tertiary mt-1">
          Pick the model okuro uses to transcribe recorded notes. Higher tiers
          are more accurate; the top tier uses a GPU.
        </p>
      </div>
      <div className="rounded border border-border bg-surface-elevated p-4 text-sm space-y-2">
        {info.data ? (
          <>
            <div className="flex items-center gap-2">
              <span className="text-xs uppercase tracking-wider text-fg-tertiary">Active</span>
              <span className="font-mono text-fg-primary uppercase">{info.data.effective}</span>
              <span className="text-fg-tertiary text-xs">
                · this machine runs up to {info.data.recommended.toUpperCase()} ({info.data.recommended_device})
              </span>
            </div>
            <div className="font-mono text-2xs text-fg-tertiary">{info.data.stream_backend}</div>
          </>
        ) : (
          <span className="text-fg-tertiary text-xs">Loading…</span>
        )}
      </div>
      <Link
        to="/settings/stt"
        className="inline-flex items-center gap-1 text-xs text-accent hover:underline"
      >
        Choose dictation tier →
      </Link>
    </section>
  );
}

function VoicesTab() {
  const info = useQuery({ queryKey: ["tts", "config"], queryFn: ttsApi.getConfig });
  const d = info.data;
  const label = (id: string) => d?.voices.find((v) => v.id === id)?.label ?? id;
  const okuro = d ? (d.okuro_gender === "female" ? d.female : d.male) : "";
  const coHost = d ? (d.okuro_gender === "female" ? d.male : d.female) : "";
  return (
    <section className="space-y-4 max-w-2xl">
      <div>
        <h2 className="text-sm font-semibold text-fg-primary">Voices (podcast &amp; morning brief)</h2>
        <p className="text-xs text-fg-tertiary mt-1">
          Pick the voice okuro speaks in, and the voice that co-hosts your
          podcasts. Preview any voice before choosing.
        </p>
      </div>
      <div className="rounded border border-border bg-surface-elevated p-4 text-sm space-y-2">
        {d ? (
          <>
            <div className="flex items-center gap-2">
              <span className="text-xs uppercase tracking-wider text-fg-tertiary">okuro</span>
              <span className="text-fg-primary">{label(okuro)}</span>
              <span className="text-fg-tertiary text-xs">· {d.okuro_gender}</span>
            </div>
            <div className="flex items-center gap-2">
              <span className="text-xs uppercase tracking-wider text-fg-tertiary">Co-host</span>
              <span className="text-fg-primary">{label(coHost)}</span>
              <span className="text-fg-tertiary text-xs">
                · {d.speed === 1 ? "normal pace" : `${d.speed}× pace`}
              </span>
            </div>
          </>
        ) : (
          <span className="text-fg-tertiary text-xs">Loading…</span>
        )}
      </div>
      <Link
        to="/settings/tts"
        className="inline-flex items-center gap-1 text-xs text-accent hover:underline"
      >
        Choose voices →
      </Link>
    </section>
  );
}

function DesignPresetSection({
  profiles,
  selected,
  onSelect,
  onPersist,
  refetchProfiles,
}: {
  profiles: Array<{ id: string; name: string; description?: string }>;
  selected: string;
  onSelect: (id: string) => void;
  onPersist: (id: string) => Promise<void>;
  refetchProfiles: () => void;
}) {
  const [saving, setSaving] = useState(false);

  /**
   * WHICH APPEARANCE THE PREVIEW IS FOR, tracked rather than assumed.
   *
   * The strip used to read the light block unconditionally while the app
   * shipped dark, so its two neutrals were shown inverted. Reading the live
   * attribute fixes the first paint; observing it is what keeps the strip
   * honest when the user presses the dark/light switch, which changes no URL
   * and fires no query invalidation of its own.
   */
  const [appearance, setAppearance] = useState<"dark" | "light">(liveAppearance);
  useEffect(() => {
    if (typeof document === "undefined") return;
    const root = document.documentElement;
    const observer = new MutationObserver(() => setAppearance(liveAppearance()));
    observer.observe(root, {
      attributes: true,
      attributeFilter: ["data-appearance"],
    });
    setAppearance(liveAppearance());
    return () => observer.disconnect();
  }, []);

  /**
   * THE SWATCHES COME FROM THE KIT'S OWN SHEET, not from the v0 generator.
   *
   * This fetched `/tokens.css?profile=<id>` while the picker above it lists
   * KITS. Measured: `okuro-ds` and every other kit come back as
   * `/* Profile not found *​/` with ZERO declarations, because v0 has never
   * heard of a kit id — only `architecture-noir` returned anything. So the
   * preview beside the picker was blank for every option the picker offered.
   *
   * `GET /api/design-engine/sheet/{id}.css` emits that kit. It is bearer-gated
   * like the rest of the module, which is why it goes through `apiText` rather
   * than a bare `fetch` — the same reason `/engine.css` needs its own
   * prefix-less route for the `<link>` the browser makes.
   *
   * AND SINCE 2026-09-06 IT IS THE SAME SHEET THE APP GETS. This route emits
   * the kit as authored; `/engine.css` used to additionally splice in the
   * user's accent, so the two disagreed the moment one was set — measured, the
   * panel showed mint while the whole UI was red. With the accent retired,
   * `/engine.css` for a kit and this route for that kit are byte-identical, so
   * previewing a system you have not switched to yet is honest.
   */
  const swatchQuery = useQuery({
    queryKey: ["design", "tokens", selected, appearance],
    queryFn: async () => {
      if (!selected) return [];
      const css = await apiText(
        `/api/design-engine/sheet/${encodeURIComponent(selected)}.css`,
      );
      return pickPreviewTokens(parseColorTokens(css, appearance));
    },
    enabled: Boolean(selected),
    staleTime: 2_000,
  });

  const handleChange = async (id: string) => {
    onSelect(id);
    setSaving(true);
    try {
      await onPersist(id);
    } catch (err) {
      toast.error("Could not switch design system", {
        description: err instanceof Error ? err.message : "unknown error",
      });
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="space-y-3">
      <SubsectionHeading
        title="Active design system"
        hint="Which system paints okuro. Saved to your profile, so it follows you across devices. Create and edit systems on the Design Engine page."
      />
      <div className="flex items-start gap-2">
        <div className="flex-1 min-w-0">
          <FieldLabel>Design system</FieldLabel>
          <Select value={selected} onValueChange={handleChange}>
            <SelectTrigger className="w-full max-w-sm">
              <SelectValue placeholder="Select a design system" />
            </SelectTrigger>
            <SelectContent>
              {profiles.length === 0 && (
                <SelectItem value="__empty__" disabled>
                  No design systems found
                </SelectItem>
              )}
              {profiles.map((p) => (
                <SelectItem key={p.id} value={p.id}>
                  {p.name}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>
        <Button
          size="sm"
          variant="outline"
          type="button"
          onClick={() => {
            refetchProfiles();
            swatchQuery.refetch();
          }}
          title="Reload the list of design systems + the token preview"
          className="mt-5"
        >
          <RefreshCw className="h-3.5 w-3.5" />
          Reload
        </Button>
      </div>
      {saving && (
        <p className="text-2xs text-tertiary">
          <Loader2 className="mr-1 inline h-3 w-3 animate-spin" />
          Saving…
        </p>
      )}

      <div className="pt-2">
        <FieldLabel>What it paints</FieldLabel>
        {swatchQuery.isLoading ? (
          <p className="text-2xs text-tertiary italic">Loading tokens…</p>
        ) : (swatchQuery.data ?? []).length === 0 ? (
          <p className="text-2xs text-tertiary italic">
            No color tokens found for this design system.
          </p>
        ) : (
          <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
            {(swatchQuery.data ?? []).map((t) => (
              <ColorChip key={t.name} name={t.name} value={t.value} />
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

function readPulseOutlineOverride(
  profile: Profile | undefined,
): PulseOutlineOverride {
  const design = profile?.design as DesignSection | undefined;
  const overrides = design?.overrides;
  const rawOnly = overrides?.["--pulse-outlines-only"];
  const rawStrength = overrides?.["--pulse-outline-strength"];
  const outlinesOnly = rawOnly === "1" || rawOnly === "true";
  const strengthNum = rawStrength !== undefined ? parseFloat(rawStrength) : NaN;
  return {
    outlinesOnly,
    strength: Number.isFinite(strengthNum) && strengthNum > 0
      ? strengthNum
      : PULSE_OUTLINE_DEFAULTS.strength,
  };
}

function PulseOutlineSection({
  current,
  onChange,
  onReset,
}: {
  current: PulseOutlineOverride;
  onChange: (next: PulseOutlineOverride) => Promise<void> | void;
  onReset: () => Promise<void> | void;
}) {
  const [outlinesOnly, setOutlinesOnly] = useState(current.outlinesOnly);
  const [strength, setStrength] = useState(current.strength);
  const [saving, setSaving] = useState(false);

  // Re-seed when the profile changes upstream (e.g. fresh fetch / reset).
  useEffect(() => {
    setOutlinesOnly(current.outlinesOnly);
    setStrength(current.strength);
  }, [current.outlinesOnly, current.strength]);

  const commit = async (next: PulseOutlineOverride) => {
    setSaving(true);
    try { await onChange(next); } finally { setSaving(false); }
  };

  const handleToggle = (checked: boolean) => {
    setOutlinesOnly(checked);
    void commit({ outlinesOnly: checked, strength });
  };

  const handleStrengthChange = (raw: string) => {
    const parsed = parseFloat(raw);
    if (!Number.isFinite(parsed) || parsed <= 0) return;
    setStrength(parsed);
  };

  const handleStrengthCommit = () => {
    if (!Number.isFinite(strength) || strength <= 0) return;
    void commit({ outlinesOnly, strength });
  };

  const reset = async () => {
    setSaving(true);
    try {
      await onReset();
      setOutlinesOnly(PULSE_OUTLINE_DEFAULTS.outlinesOnly);
      setStrength(PULSE_OUTLINE_DEFAULTS.strength);
    } finally { setSaving(false); }
  };

  return (
    <div className="space-y-3 rounded border border-border bg-surface-elevated p-4">
      <div className="flex items-center gap-2">
        <Palette className="h-3.5 w-3.5 text-tertiary" />
        <h3 className="text-xs font-semibold text-fg">Pulse style</h3>
      </div>
      <p className="text-2xs text-tertiary">
        When outlines-only is on, the pulse blob is rendered as a stroked
        outline at the chosen strength — glow layers are suppressed.
      </p>

      <div className="flex items-center justify-between gap-3">
        <div className="flex flex-col">
          <label htmlFor="pulse-outlines-only" className="text-2xs font-semibold text-fg">
            Outlines only
          </label>
          <span className="text-3xs text-tertiary">
            Draw only the blob silhouette
          </span>
        </div>
        <Switch
          id="pulse-outlines-only"
          checked={outlinesOnly}
          onCheckedChange={handleToggle}
          disabled={saving}
        />
      </div>

      <div className="flex items-center justify-between gap-3">
        <div className="flex flex-col">
          <label htmlFor="pulse-outline-strength" className="text-2xs font-semibold text-fg">
            Outline strength
          </label>
          <span className="text-3xs text-tertiary">
            Stroke width in pixels (0.5–8)
          </span>
        </div>
        <Input
          id="pulse-outline-strength"
          type="number"
          inputMode="decimal"
          min={0.5}
          max={8}
          step={0.5}
          value={strength}
          onChange={(e) => handleStrengthChange(e.target.value)}
          onBlur={handleStrengthCommit}
          onKeyDown={(e) => {
            if (e.key === "Enter") {
              e.preventDefault();
              handleStrengthCommit();
            }
          }}
          disabled={!outlinesOnly || saving}
          className="w-field-sm font-mono"
        />
      </div>

      <div className="flex items-center justify-end">
        <Button
          size="sm"
          variant="outline"
          type="button"
          onClick={reset}
          disabled={saving}
          title="Clear the override and use defaults"
        >
          {saving ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <RefreshCw className="h-3.5 w-3.5" />}
          Reset to defaults
        </Button>
      </div>
    </div>
  );
}

function ColorChip({ name, value }: { name: string; value: string }) {
  return (
    <div className="flex items-center gap-2 rounded border border-border bg-surface p-1.5">
      <span
        className="inline-block h-5 w-5 shrink-0 rounded border border-border"
        style={{ backgroundColor: value }}
        aria-label={`${name} = ${value}`}
        title={`${name} = ${value}`}
      />
      <span className="min-w-0 flex-1">
        <span className="block truncate font-mono text-2xs text-fg">
          {name}
        </span>
        <span className="block truncate font-mono text-2xs text-tertiary">
          {value}
        </span>
      </span>
    </div>
  );
}

/**
 * The eight swatches that actually tell two design systems apart.
 *
 * This was `.slice(0, 8)` — the first eight `--color-*` names in emission
 * order, which are the BACKGROUNDS and the INKS. Measured on two kits: every
 * one of the eight is byte-identical between okuro's own system and a fork of
 * it, because "rest same as base" means the neutrals are the base's. A preview
 * whose job is to distinguish systems was showing the eight values guaranteed
 * not to differ, and the one value that does — the brand colour — was not in
 * the list at all.
 *
 * NAMED IN AN ORDER, and the rest fills in. The brand and its derivations come
 * first because they are what a fork changes; the signals next, because a brand
 * may author its own; the neutrals last, so a system that DOES move its black
 * still shows it. Anything missing from a sheet is skipped rather than blanked.
 */
const PREVIEW_ORDER = [
  "--color-accent",
  "--color-accent-hover",
  "--color-accent-subtle",
  "--color-status-error",
  "--color-status-success",
  "--color-status-info",
  "--color-background-base",
  "--color-foreground-primary",
];

function pickPreviewTokens(
  tokens: Array<{ name: string; value: string }>,
): Array<{ name: string; value: string }> {
  const byName = new Map(tokens.map((t) => [t.name, t]));
  const out = PREVIEW_ORDER.map((name) => byName.get(name)).filter(
    (t): t is { name: string; value: string } => Boolean(t),
  );
  // A sheet that names none of them is not a sheet this preview understands;
  // showing its first few is better than showing an empty row.
  return out.length ? out : tokens.slice(0, 8);
}

/** The appearance the browser is showing, read off the attribute the engine
 *  keys its blocks on. NOT `currentThemeMode()`: that consults the stored
 *  choice and falls back to inferring one, which is a second answer to a
 *  question `<html data-appearance>` already answers. */
export function liveAppearance(): "dark" | "light" {
  if (typeof document === "undefined") return "dark";
  return document.documentElement.getAttribute("data-appearance") === "light"
    ? "light"
    : "dark";
}

/** Pull the declarations out of one `:root…{ }` block. */
function declarationsIn(block: string): Array<{ name: string; value: string }> {
  const out: Array<{ name: string; value: string }> = [];
  const re = /(--color-[a-zA-Z0-9-]+)\s*:\s*([^;]+);/g;
  let m: RegExpExecArray | null;
  while ((m = re.exec(block)) !== null) {
    out.push({ name: m[1]!, value: m[2]!.trim() });
  }
  return out;
}

/**
 * The `--color-*` values the page is ACTUALLY painted with, resolved the way
 * the cascade resolves them: the base `:root` block, then the appearance block
 * laid over it.
 *
 * THE OLD VERSION READ ONE BLOCK AND IT WAS ALWAYS THE WRONG ONE. Its regex
 * was `/:root\s*\{/` with `.match` — one hit, and the `[` in
 * `:root[data-appearance="dark"]` means it could never reach the dark block.
 * The app has shipped `data-appearance="dark"` from the first byte of
 * index.html since 2026-09-03, so `--color-background-base` and
 * `--color-foreground-primary` were shown EXACTLY INVERTED to what was on
 * screen — measured 2026-09-06, #ffffff/#030303 against a painted
 * #030303/#ffffff.
 *
 * The engine emits the dark block as an OVERLAY, not a replacement: a token
 * that is the same in both appearances is declared once, in the base. So the
 * two are merged rather than one being chosen, which is also why a missing
 * dark declaration is not a hole.
 */
export function parseColorTokens(
  css: string,
  appearance: "dark" | "light" = "dark",
): Array<{ name: string; value: string }> {
  const base = css.match(/:root\s*\{([\s\S]*?)\}/);
  const merged = new Map<string, string>();
  for (const d of declarationsIn(base ? base[1]! : css)) {
    merged.set(d.name, d.value);
  }
  if (appearance !== "light") {
    const dark = css.match(/:root\[data-appearance="dark"\]\s*\{([\s\S]*?)\}/);
    if (dark) {
      for (const d of declarationsIn(dark[1]!)) merged.set(d.name, d.value);
    }
  }
  return Array.from(merged, ([name, value]) => ({ name, value }));
}

// ── Keyring tab ─────────────────────────────────────────────────────

/**
 * Keyring tab — three distinct states in one component:
 *   1. Not initialized → "Initialize vault" CTA
 *   2. Initialized but locked → password + "Unlock"
 *   3. Unlocked → secret table with CRUD
 *
 * The session token lives in module-scoped memory (keyring-api.ts),
 * NOT in localStorage. A page reload drops it; the user must re-enter
 * their master password. We also drive a client-side 15-min auto-lock
 * with a visible countdown.
 */
function KeyringTab() {
  const qc = useQueryClient();
  // The frontend flips this when it gets an unlock response. The
  // backend is the source of truth; the query key is the same either
  // way so every refetch re-derives unlocked from the session cookie
  // + session_token header.
  const [sessionExpiresAt, setSessionExpiresAt] = useState<number | null>(null);

  const statusQ = useQuery({
    queryKey: ["keyring", "status"],
    queryFn: keyringApi.status,
    // Poll every 30s so we reflect server-side session expiry without
    // needing push. Cheap and never hits the encrypted vault.
    refetchInterval: 30_000,
  });

  const status: KeyringStatus | undefined = statusQ.data;

  const invalidate = () => {
    qc.invalidateQueries({ queryKey: ["keyring"] });
  };

  if (statusQ.isLoading && !status) {
    return <LoadingPane />;
  }

  if (statusQ.isError) {
    return <ErrorPane error={statusQ.error} />;
  }

  // ── Branch: not initialized ──
  if (status && !status.initialized) {
    return <KeyringInitPane onInitialized={invalidate} />;
  }

  // ── Branch: initialized but locked ──
  if (status && status.initialized && !status.unlocked) {
    return (
      <KeyringUnlockPane
        onUnlocked={(expiresAt) => {
          setSessionExpiresAt(expiresAt);
          invalidate();
        }}
      />
    );
  }

  // ── Branch: unlocked ──
  return (
    <KeyringUnlockedPane
      sessionExpiresAt={sessionExpiresAt}
      onLocked={() => {
        setSessionExpiresAt(null);
        invalidate();
      }}
      count={status?.count ?? null}
    />
  );
}

function KeyringInitPane({ onInitialized }: { onInitialized: () => void }) {
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [err, setErr] = useState<string | null>(null);

  const init = useMutation({
    mutationFn: async () => {
      if (password.length < 8) throw new Error("Password must be at least 8 characters");
      if (password !== confirm) throw new Error("Passwords do not match");
      return keyringApi.initVault(password);
    },
    onSuccess: () => {
      toast.success("Vault initialized");
      setPassword("");
      setConfirm("");
      setErr(null);
      onInitialized();
    },
    onError: (e) => {
      setErr(e instanceof Error ? e.message : "unknown error");
    },
  });

  return (
    <section className="max-w-md space-y-4">
      <div>
        <h2 className="text-sm font-semibold text-fg">Vault not initialized</h2>
        <p className="mt-1 text-2xs text-tertiary">
          The okuro keyring stores API keys and credentials encrypted at rest
          with PBKDF2 + Fernet. Choose a master password — it cannot be
          recovered if lost.
        </p>
      </div>

      <div className="space-y-3">
        <div>
          <FieldLabel>Master password</FieldLabel>
          <Input
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            placeholder="At least 8 characters"
            autoComplete="new-password"
          />
        </div>
        <div>
          <FieldLabel>Confirm</FieldLabel>
          <Input
            type="password"
            value={confirm}
            onChange={(e) => setConfirm(e.target.value)}
            placeholder="Type it again"
            autoComplete="new-password"
          />
        </div>
        {err && <p className="text-2xs text-error">{err}</p>}
      </div>

      <div className="flex items-center gap-3">
        <Button
          size="sm"
          onClick={() => init.mutate()}
          disabled={init.isPending || !password || !confirm}
        >
          {init.isPending ? (
            <>
              <Loader2 className="h-3.5 w-3.5 animate-spin" />
              Initializing…
            </>
          ) : (
            <>
              <KeyRound className="h-3.5 w-3.5" />
              Initialize vault
            </>
          )}
        </Button>
      </div>
    </section>
  );
}

function KeyringUnlockPane({
  onUnlocked,
}: {
  onUnlocked: (expiresAt: number) => void;
}) {
  const [password, setPassword] = useState("");
  const [err, setErr] = useState<string | null>(null);

  const unlock = useMutation({
    mutationFn: async () => {
      if (!password) throw new Error("Enter your master password");
      return keyringApi.unlock(password);
    },
    onSuccess: (res) => {
      toast.success("Vault unlocked");
      setPassword("");
      setErr(null);
      onUnlocked(res.expires_at);
    },
    onError: (e) => {
      setErr(e instanceof Error ? e.message : "unknown error");
    },
  });

  return (
    <section className="max-w-md space-y-4">
      <div>
        <h2 className="flex items-center gap-2 text-sm font-semibold text-fg">
          <Lock className="h-4 w-4" />
          Vault locked
        </h2>
        <p className="mt-1 text-2xs text-tertiary">
          Enter your master password to decrypt the vault for this session.
          Auto-locks after 15 minutes of inactivity.
        </p>
      </div>

      <div className="space-y-3">
        <div>
          <FieldLabel>Master password</FieldLabel>
          <Input
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !unlock.isPending) unlock.mutate();
            }}
            placeholder="••••••••"
            autoComplete="current-password"
            autoFocus
          />
        </div>
        {err && <p className="text-2xs text-error">{err}</p>}
      </div>

      <div className="flex items-center gap-3">
        <Button
          size="sm"
          onClick={() => unlock.mutate()}
          disabled={unlock.isPending || !password}
        >
          {unlock.isPending ? (
            <>
              <Loader2 className="h-3.5 w-3.5 animate-spin" />
              Unlocking…
            </>
          ) : (
            <>
              <Unlock className="h-3.5 w-3.5" />
              Unlock
            </>
          )}
        </Button>
      </div>
    </section>
  );
}

function KeyringUnlockedPane({
  sessionExpiresAt,
  onLocked,
  count,
}: {
  sessionExpiresAt: number | null;
  onLocked: () => void;
  count: number | null;
}) {
  const qc = useQueryClient();
  const [now, setNow] = useState(() => Date.now() / 1000);

  // Tick every second for the countdown. Stops when no session.
  useEffect(() => {
    if (!sessionExpiresAt) return;
    const id = window.setInterval(() => setNow(Date.now() / 1000), 1000);
    return () => window.clearInterval(id);
  }, [sessionExpiresAt]);

  // Auto-lock when the local countdown hits zero. The backend expires
  // on its own 15-min TTL too; this keeps the UI honest.
  //
  // Phase 5 — fire ONCE per session. `now` ticks every second and stays
  // >= sessionExpiresAt until the prop clears on the parent re-render, so
  // without this guard the auto-lock toast re-fired each tick in the gap.
  const autoLockedRef = useRef(false);
  // Re-arm whenever a new session starts (expiry changes to a future time).
  useEffect(() => {
    autoLockedRef.current = false;
  }, [sessionExpiresAt]);
  useEffect(() => {
    if (sessionExpiresAt && now >= sessionExpiresAt && !autoLockedRef.current) {
      autoLockedRef.current = true;
      setKeyringSession(null);
      toast.info("Vault auto-locked (timeout)");
      onLocked();
    }
  }, [now, sessionExpiresAt, onLocked]);

  const listQ = useQuery({
    queryKey: ["keyring", "secrets"],
    queryFn: keyringApi.list,
  });

  const lock = useMutation({
    mutationFn: keyringApi.lock,
    onSuccess: () => {
      toast.success("Vault locked");
      onLocked();
    },
  });

  const del = useMutation({
    mutationFn: (name: string) => keyringApi.delete(name),
    onSuccess: (_data, name) => {
      toast.success(`Deleted ${name}`);
      qc.invalidateQueries({ queryKey: ["keyring"] });
    },
    onError: (err) => {
      toast.error("Delete failed", {
        description: err instanceof Error ? err.message : "unknown error",
      });
    },
  });

  const secrets: SecretRef[] = listQ.data?.secrets ?? [];

  const [adding, setAdding] = useState(false);
  const [pendingDelete, setPendingDelete] = useState<string | null>(null);

  const remaining = sessionExpiresAt
    ? Math.max(0, Math.round(sessionExpiresAt - now))
    : null;

  return (
    <section className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h2 className="flex items-center gap-2 text-sm font-semibold text-fg">
            <Unlock className="h-4 w-4 text-success" />
            Vault unlocked
          </h2>
          <p className="mt-1 text-2xs text-tertiary">
            {count !== null ? `${count} secret${count === 1 ? "" : "s"} stored. ` : null}
            {remaining !== null
              ? `Auto-lock in ${formatDuration(remaining)}.`
              : "Session active."}
          </p>
        </div>
        <div className="flex items-center gap-2">
          <Button
            size="sm"
            variant="outline"
            onClick={() => setAdding(true)}
            disabled={adding}
          >
            <Plus className="h-3.5 w-3.5" />
            Add secret
          </Button>
          <Button
            size="sm"
            variant="outline"
            onClick={() => lock.mutate()}
            disabled={lock.isPending}
          >
            <Lock className="h-3.5 w-3.5" />
            Lock vault
          </Button>
        </div>
      </div>

      {listQ.isError && (
        <EmptyState
          title="Could not list secrets"
          description={
            listQ.error instanceof Error ? listQ.error.message : "unknown error"
          }
        />
      )}

      {adding && (
        <AddSecretRow
          existing={secrets.map((s) => s.name)}
          onCancel={() => setAdding(false)}
          onSaved={() => {
            setAdding(false);
            qc.invalidateQueries({ queryKey: ["keyring"] });
          }}
        />
      )}

      {secrets.length === 0 && !listQ.isLoading && !adding ? (
        <EmptyState
          title="No secrets yet"
          description="Click Add secret to store your first API key or credential."
        />
      ) : (
        <div className="rounded border border-border bg-surface">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-border text-2xs uppercase tracking-wider text-tertiary">
                <th className="px-3 py-2 text-left font-medium">Name</th>
                <th className="px-3 py-2 text-left font-medium">Value</th>
                <th className="px-3 py-2 text-right font-medium">Actions</th>
              </tr>
            </thead>
            <tbody>
              {secrets.map((s) => (
                <SecretRow
                  key={s.name}
                  name={s.name}
                  onDeleteRequest={() => setPendingDelete(s.name)}
                />
              ))}
            </tbody>
          </table>
        </div>
      )}

      <Dialog
        open={pendingDelete !== null}
        onOpenChange={(v) => !v && setPendingDelete(null)}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Delete secret?</DialogTitle>
            <DialogDescription>
              {pendingDelete ? (
                <>
                  This will permanently remove <span className="font-mono">{pendingDelete}</span>{" "}
                  from the vault. Agents and services relying on this key
                  will break.
                </>
              ) : null}
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button
              variant="outline"
              size="sm"
              onClick={() => setPendingDelete(null)}
            >
              Cancel
            </Button>
            <Button
              variant="destructive"
              size="sm"
              onClick={() => {
                if (pendingDelete) {
                  del.mutate(pendingDelete);
                  setPendingDelete(null);
                }
              }}
            >
              Delete
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </section>
  );
}

function formatDuration(seconds: number): string {
  const m = Math.floor(seconds / 60);
  const s = seconds % 60;
  return `${m}:${String(s).padStart(2, "0")}`;
}

/**
 * Row for a single secret. Handles:
 *  - masked display
 *  - reveal (10-second re-mask timer)
 *  - inline value edit (name immutable)
 *  - delete (parent owns confirmation)
 */
function SecretRow({
  name,
  onDeleteRequest,
}: {
  name: string;
  onDeleteRequest: () => void;
}) {
  const qc = useQueryClient();
  const [revealed, setRevealed] = useState<string | null>(null);
  const [editing, setEditing] = useState(false);
  const [editValue, setEditValue] = useState("");
  const revealTimer = useRef<number | null>(null);

  // Clear any pending re-mask timer on unmount.
  useEffect(() => {
    return () => {
      if (revealTimer.current !== null) {
        window.clearTimeout(revealTimer.current);
      }
    };
  }, []);

  const reveal = useMutation({
    mutationFn: () => keyringApi.reveal(name),
    onSuccess: (data) => {
      setRevealed(data.value);
      if (revealTimer.current !== null) window.clearTimeout(revealTimer.current);
      revealTimer.current = window.setTimeout(() => {
        setRevealed(null);
        revealTimer.current = null;
      }, 10_000);
    },
    onError: (err) => {
      toast.error("Reveal failed", {
        description: err instanceof Error ? err.message : "unknown error",
      });
    },
  });

  const save = useMutation({
    mutationFn: () => keyringApi.update(name, editValue),
    onSuccess: () => {
      toast.success(`Updated ${name}`);
      setEditing(false);
      setEditValue("");
      // Drop any cached reveal — value has changed.
      setRevealed(null);
      qc.invalidateQueries({ queryKey: ["keyring"] });
    },
    onError: (err) => {
      toast.error("Save failed", {
        description: err instanceof Error ? err.message : "unknown error",
      });
    },
  });

  return (
    <tr className="border-b border-border last:border-b-0">
      <td className="px-3 py-2 font-mono text-xs">{name}</td>
      <td className="px-3 py-2 font-mono text-xs">
        {editing ? (
          <Input
            value={editValue}
            onChange={(e) => setEditValue(e.target.value)}
            placeholder="New value"
            autoFocus
            onKeyDown={(e) => {
              if (e.key === "Enter" && editValue) save.mutate();
              if (e.key === "Escape") {
                setEditing(false);
                setEditValue("");
              }
            }}
          />
        ) : revealed !== null ? (
          <span className="break-all text-fg">{revealed}</span>
        ) : (
          <span className="text-tertiary">••••••••••••</span>
        )}
      </td>
      <td className="px-3 py-2 text-right">
        <div className="flex items-center justify-end gap-1">
          {editing ? (
            <>
              <Button
                size="sm"
                variant="outline"
                onClick={() => save.mutate()}
                disabled={save.isPending || !editValue}
              >
                {save.isPending ? (
                  <Loader2 className="h-3.5 w-3.5 animate-spin" />
                ) : (
                  "Save"
                )}
              </Button>
              <Button
                size="sm"
                variant="ghost"
                onClick={() => {
                  setEditing(false);
                  setEditValue("");
                }}
              >
                Cancel
              </Button>
            </>
          ) : (
            <>
              <button
                type="button"
                onClick={() => {
                  if (revealed !== null) {
                    if (revealTimer.current !== null) {
                      window.clearTimeout(revealTimer.current);
                      revealTimer.current = null;
                    }
                    setRevealed(null);
                  } else {
                    reveal.mutate();
                  }
                }}
                disabled={reveal.isPending}
                className="p-1.5 text-tertiary hover:text-fg"
                aria-label={revealed !== null ? "Hide value" : "Reveal value"}
                title={revealed !== null ? "Hide value" : "Reveal value (auto-masks after 10s)"}
              >
                {reveal.isPending ? (
                  <Loader2 className="h-3.5 w-3.5 animate-spin" />
                ) : revealed !== null ? (
                  <EyeOff className="h-3.5 w-3.5" />
                ) : (
                  <Eye className="h-3.5 w-3.5" />
                )}
              </button>
              <button
                type="button"
                onClick={() => {
                  setEditing(true);
                  setEditValue("");
                }}
                className="p-1.5 text-tertiary hover:text-fg"
                aria-label="Edit value"
                title="Edit value"
              >
                <Pencil className="h-3.5 w-3.5" />
              </button>
              <button
                type="button"
                onClick={onDeleteRequest}
                className="p-1.5 text-tertiary hover:text-error"
                aria-label="Delete secret"
                title="Delete secret"
              >
                <Trash2 className="h-3.5 w-3.5" />
              </button>
            </>
          )}
        </div>
      </td>
    </tr>
  );
}

function AddSecretRow({
  existing,
  onSaved,
  onCancel,
}: {
  existing: string[];
  onSaved: () => void;
  onCancel: () => void;
}) {
  const [name, setName] = useState("");
  const [value, setValue] = useState("");
  const [err, setErr] = useState<string | null>(null);

  const create = useMutation({
    mutationFn: async () => {
      const trimmed = name.trim();
      if (!trimmed) throw new Error("Name is required");
      if (existing.includes(trimmed)) throw new Error(`'${trimmed}' already exists`);
      if (!value) throw new Error("Value is required");
      return keyringApi.create(trimmed, value);
    },
    onSuccess: () => {
      toast.success(`Added ${name.trim()}`);
      setName("");
      setValue("");
      setErr(null);
      onSaved();
    },
    onError: (e) => {
      setErr(e instanceof Error ? e.message : "unknown error");
    },
  });

  return (
    <div className="space-y-3 rounded border border-dashed border-border bg-surface p-3">
      <SubsectionHeading
        title="Add secret"
        hint="Name is case-sensitive and must be unique. Value is stored encrypted."
      />
      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
        <div>
          <FieldLabel>Name</FieldLabel>
          <Input
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="OPENAI_API_KEY"
            className="font-mono"
            autoFocus
          />
        </div>
        <div>
          <FieldLabel>Value</FieldLabel>
          <Input
            type="password"
            value={value}
            onChange={(e) => setValue(e.target.value)}
            placeholder="sk-…"
            className="font-mono"
            onKeyDown={(e) => {
              if (e.key === "Enter" && name && value && !create.isPending) {
                create.mutate();
              }
            }}
          />
        </div>
      </div>
      {err && <p className="text-2xs text-error">{err}</p>}
      <div className="flex items-center gap-2">
        <Button
          size="sm"
          onClick={() => create.mutate()}
          disabled={create.isPending || !name || !value}
        >
          {create.isPending ? (
            <>
              <Loader2 className="h-3.5 w-3.5 animate-spin" />
              Saving…
            </>
          ) : (
            "Save"
          )}
        </Button>
        <Button size="sm" variant="ghost" onClick={onCancel}>
          Cancel
        </Button>
      </div>
    </div>
  );
}

// ── Integrations tab ──────────────────────────────────────────────

function IntegrationsTab() {
  const qc = useQueryClient();
  const listQ = useQuery({
    queryKey: ["integrations", "list"],
    queryFn: integrationsApi.list,
    refetchInterval: 10_000,
  });

  if (listQ.isLoading) return <LoadingPane />;
  if (listQ.isError) return <ErrorPane error={listQ.error} />;

  const items = listQ.data ?? [];
  const invalidate = () => {
    qc.invalidateQueries({ queryKey: ["integrations"] });
  };

  return (
    <div className="space-y-6">
      <div>
        <h2 className="text-sm font-semibold text-fg">Integrations</h2>
        <p className="mt-1 text-2xs text-tertiary">
          External channels that route inbound messages into okuro
          (thoughts, todos, reminders, orchestrator tasks).
        </p>
      </div>

      {items.length === 0 ? (
        <EmptyState title="No integrations configured" />
      ) : (
        items.map((it) => (
          <IntegrationCard key={it.channel} integration={it} onMutated={invalidate} />
        ))
      )}
    </div>
  );
}

function StatusPill({ state }: { state: string }) {
  const tone =
    state === "running"
      ? "text-accent border-accent/40"
      : state === "error"
        ? "text-error border-error/40"
        : "text-tertiary border-border";
  return (
    <span
      className={`inline-flex items-center rounded px-2 py-0.5 text-2xs uppercase tracking-wider border ${tone}`}
    >
      {state}
    </span>
  );
}

function IntegrationCard({
  integration,
  onMutated,
}: {
  integration: Integration;
  onMutated: () => void;
}) {
  const setEnabled = useMutation({
    mutationFn: (v: boolean) => integrationsApi.setEnabled(integration.channel, v),
    onSuccess: () => {
      toast.success(`Telegram ${integration.enabled ? "disabled" : "enabled"}`);
      onMutated();
    },
    onError: (e) => toast.error(e instanceof Error ? e.message : "failed"),
  });

  const approve = useMutation({
    mutationFn: () => integrationsApi.approvePending(integration.channel),
    onSuccess: () => {
      toast.success("Chat approved");
      onMutated();
    },
    onError: (e) => toast.error(e instanceof Error ? e.message : "failed"),
  });

  const reject = useMutation({
    mutationFn: () => integrationsApi.rejectPending(integration.channel),
    onSuccess: () => {
      toast.success("Chat rejected");
      onMutated();
    },
    onError: (e) => toast.error(e instanceof Error ? e.message : "failed"),
  });

  const revoke = useMutation({
    mutationFn: (id: number) => integrationsApi.revoke(integration.channel, id),
    onSuccess: () => {
      toast.success("Chat revoked");
      onMutated();
    },
    onError: (e) => toast.error(e instanceof Error ? e.message : "failed"),
  });

  return (
    <section className="rounded border border-border bg-surface-elevated p-4 space-y-4">
      <header className="flex items-center justify-between gap-4">
        <div className="flex items-center gap-3">
          <h3 className="text-sm font-semibold text-fg capitalize">
            {integration.channel}
          </h3>
          <StatusPill state={integration.adapter_health?.state ?? integration.status} />
        </div>
        <div className="flex items-center gap-3">
          <span className="text-2xs text-tertiary">
            {integration.has_token ? "token: present" : "token: missing"}
          </span>
          <Switch
            checked={integration.enabled}
            disabled={setEnabled.isPending}
            onCheckedChange={(v) => setEnabled.mutate(v)}
          />
        </div>
      </header>

      {integration.pending_chat_id !== null && (
        <div className="rounded border border-warning/30 bg-warning/5 p-3 space-y-2">
          <div className="text-2xs text-warning">
            Pending approval — chat_id{" "}
            <code className="font-mono">{integration.pending_chat_id}</code> wants to send messages.
          </div>
          <div className="flex gap-2">
            <Button
              size="sm"
              onClick={() => approve.mutate()}
              disabled={approve.isPending}
            >
              Approve
            </Button>
            <Button
              size="sm"
              variant="outline"
              onClick={() => reject.mutate()}
              disabled={reject.isPending}
            >
              Reject
            </Button>
          </div>
        </div>
      )}

      <div>
        <div className="text-2xs text-tertiary mb-1">
          Allowed chats ({integration.allowed_chat_ids.length})
        </div>
        {integration.allowed_chat_ids.length === 0 ? (
          <div className="text-2xs text-tertiary italic">none</div>
        ) : (
          <ul className="space-y-1">
            {integration.allowed_chat_ids.map((id) => (
              <li key={id} className="flex items-center justify-between text-2xs">
                <code className="font-mono text-fg">{id}</code>
                <button
                  type="button"
                  className="text-2xs text-tertiary hover:text-error"
                  onClick={() => revoke.mutate(id)}
                  disabled={revoke.isPending}
                >
                  revoke
                </button>
              </li>
            ))}
          </ul>
        )}
      </div>

      <dl className="grid grid-cols-2 gap-x-4 gap-y-1 text-2xs text-tertiary">
        <dt>last poll</dt>
        <dd>{integration.adapter_health?.last_poll_at ?? "—"}</dd>
        <dt>last message</dt>
        <dd>{integration.last_message_at ?? "—"}</dd>
        {integration.last_error && (
          <>
            <dt className="text-error">last error</dt>
            <dd className="text-error">{integration.last_error}</dd>
          </>
        )}
      </dl>

      <ChallengesSection channel={integration.channel} />
    </section>
  );
}

function ChallengesSection({ channel }: { channel: string }) {
  const qc = useQueryClient();
  const session = getKeyringSession();

  const listQ = useQuery({
    queryKey: ["integrations", channel, "challenges"],
    queryFn: () => integrationsApi.listChallenges(channel, session ?? ""),
    enabled: !!session,
  });

  const [question, setQuestion] = useState("");
  const [patterns, setPatterns] = useState("");

  const add = useMutation({
    mutationFn: async () => {
      if (!session) throw new Error("Keyring locked");
      const pats = patterns
        .split(",")
        .map((p) => p.trim())
        .filter(Boolean);
      if (!question.trim() || pats.length === 0) {
        throw new Error("Question + at least one pattern required");
      }
      return integrationsApi.addChallenge(channel, session, {
        question: question.trim(),
        accepted_patterns: pats,
      });
    },
    onSuccess: () => {
      toast.success("Challenge added");
      setQuestion("");
      setPatterns("");
      qc.invalidateQueries({ queryKey: ["integrations", channel, "challenges"] });
    },
    onError: (e) => toast.error(e instanceof Error ? e.message : "failed"),
  });

  const remove = useMutation({
    mutationFn: async (idx: number) => {
      if (!session) throw new Error("Keyring locked");
      return integrationsApi.deleteChallenge(channel, session, idx);
    },
    onSuccess: () => {
      toast.success("Challenge removed");
      qc.invalidateQueries({ queryKey: ["integrations", channel, "challenges"] });
    },
    onError: (e) => toast.error(e instanceof Error ? e.message : "failed"),
  });

  if (!session) {
    return (
      <div className="border-t border-border pt-3">
        <div className="text-2xs text-tertiary mb-1">Security challenges</div>
        <div className="text-2xs text-tertiary italic">
          Unlock the keyring (Settings → Keyring) to manage challenges.
          High-stakes intents (orchestrator task, reminder) require at
          least one challenge.
        </div>
      </div>
    );
  }

  const items: IntegrationChallenge[] = listQ.data ?? [];

  return (
    <div className="border-t border-border pt-3 space-y-3">
      <div className="text-2xs text-tertiary">
        Security challenges ({items.length})
      </div>
      {items.length === 0 ? (
        <div className="text-2xs text-warning italic">
          No challenges yet — orchestrator dispatches will be refused until you add one.
        </div>
      ) : (
        <ul className="space-y-1">
          {items.map((c, i) => (
            <li key={i} className="flex items-start justify-between gap-3 text-2xs">
              <div className="min-w-0 flex-1">
                <div className="text-fg">{c.question}</div>
                <div className="text-tertiary truncate">
                  patterns: {c.accepted_patterns.join(", ")}
                </div>
              </div>
              <button
                type="button"
                className="text-2xs text-tertiary hover:text-error shrink-0"
                onClick={() => remove.mutate(i)}
                disabled={remove.isPending}
              >
                remove
              </button>
            </li>
          ))}
        </ul>
      )}

      <div className="space-y-2 rounded border border-border bg-background p-3">
        <div className="text-2xs text-tertiary">Add challenge</div>
        <Input
          placeholder="Question (e.g. capital of France?)"
          value={question}
          onChange={(e) => setQuestion(e.target.value)}
        />
        <Input
          placeholder="Accepted patterns, comma-separated (e.g. paris, paris france)"
          value={patterns}
          onChange={(e) => setPatterns(e.target.value)}
        />
        <Button
          size="sm"
          onClick={() => add.mutate()}
          disabled={add.isPending || !question.trim() || !patterns.trim()}
        >
          Add
        </Button>
        <div className="text-2xs text-tertiary">
          Patterns are case-insensitive regex (matched as substring of the answer).
        </div>
      </div>
    </div>
  );
}
