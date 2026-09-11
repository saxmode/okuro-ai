import { useEffect, useState } from "react";
import { useNavigate } from "react-router";
import { onboardingApi } from "@/lib/api";
import type { DetectionResult, OnboardingState } from "@/types/api";

import { OnboardingShell } from "@/components/onboarding/shell";
import { OnboardingSplashContent } from "@/components/onboarding/splash";
import { OnboardingPage } from "@/components/onboarding/page";
import { BigTextInput } from "@/components/onboarding/inputs/big-text-input";
import { ChoiceCard } from "@/components/onboarding/inputs/choice-card";
import { ChipMulti } from "@/components/onboarding/inputs/chip-multi";
import { WindowChrome } from "@/components/shell/window-chrome";

import { CliSetupStep } from "@/components/onboarding/steps/cli-setup";
import { EmbedSetupStep } from "@/components/onboarding/steps/embed-setup";
import { CanonDeployStep } from "@/components/onboarding/steps/canon-deploy";
import { ProfileSourcesStep } from "@/components/onboarding/steps/profile-sources";
import { QuestionnaireStep } from "@/components/onboarding/steps/questionnaire";
import { PrinciplesStep } from "@/components/onboarding/steps/principles";
import { DesignStep } from "@/components/onboarding/steps/design";

/**
 * Full onboarding flow on the new shell.
 *
 * Controller owns accumulated profile state (hydrated from the existing
 * user_profile row, patched back section by section as the user advances).
 * Each page receives the fields it needs and a local `save()` that patches
 * just its section via onboardingApi.patchProfile — no monolithic "save all
 * at end" — so interrupting the wizard at any page leaves a consistent profile.
 */

type Profile = Record<string, unknown>;

// Common UI timezones for the ChoiceCard on the location page. The backend's
// `detect.timezone` is offered as a shortcut if it isn't already in the list.
const TIMEZONE_OPTIONS = [
  { value: "Europe/Zurich",       label: "Europe / Zurich",       hint: "CET / CEST" },
  { value: "Europe/Berlin",       label: "Europe / Berlin",       hint: "CET / CEST" },
  { value: "Europe/London",       label: "Europe / London",       hint: "GMT / BST" },
  { value: "America/New_York",    label: "America / New York",    hint: "EST / EDT" },
  { value: "America/Los_Angeles", label: "America / Los Angeles", hint: "PST / PDT" },
  { value: "Asia/Tokyo",          label: "Asia / Tokyo",          hint: "JST" },
  { value: "Asia/Singapore",      label: "Asia / Singapore",      hint: "SGT" },
  { value: "Australia/Sydney",    label: "Australia / Sydney",    hint: "AEST / AEDT" },
];

const AUTONOMOUS_SUGGESTIONS = [
  "run tests", "fix typos", "format code", "install packages",
  "read files", "search codebase", "commit trivial changes",
];

const ASK_FIRST_SUGGESTIONS = [
  "delete files", "push to remote", "expose ports", "modify prod",
  "install system packages", "change credentials", "rebase published commits",
];

// Backend step keys → frontend page key. Currently unused — the wizard
// always starts at the splash so every user sees the brand intro on
// first launch (see `initialIndex = 0` below). Kept here as a reference
// table for whoever re-introduces resume mid-flow once the render-time
// side-effect issue is solved with a different mechanism (URL hash,
// dedicated route, or post-mount programmatic scroll).
const _BACKEND_STEP_TO_PAGE_KEY_REFERENCE = {
  cli_setup: "cli_setup",
  canon_deploy: "canon_deploy",
  identity: "identity_name",
  profile_sources: "profile_sources",
  communication: "communication",
  cognitive_style: "communication",
  principles: "principles",
  boundaries: "boundaries",
  keyring: "keyring",
  design: "design",
} as const;
void _BACKEND_STEP_TO_PAGE_KEY_REFERENCE;

export function OnboardingPreviewPage() {
  const navigate = useNavigate();
  const [profile, setProfile] = useState<Profile>({});
  const [detection, setDetection] = useState<DetectionResult | null>(null);
  const [loading, setLoading] = useState(true);
  const [completing, setCompleting] = useState(false);
  const [completionState, setCompletionState] = useState<OnboardingState | null>(null);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    // NOTE: preview route never redirects on `completed` — it always renders
    // the new flow so we can sign it off on a machine where onboarding has
    // already been completed in the legacy route. The real /onboarding
    // route will still auto-redirect.
    //
    // We fetch profile + detection so the wizard pre-fills any data the
    // user already has; the wizard always lands on the splash regardless
    // (see `initialIndex = 0` below).
    Promise.all([
      onboardingApi.profile().catch(() => ({})),
      onboardingApi.detect().catch(() => null),
    ])
      .then(([p, d]) => {
        setProfile(p);
        setDetection(d);
      })
      .catch((e) => setErr(e instanceof Error ? e.message : "Load failed"))
      .finally(() => setLoading(false));
  }, []);

  const patch = async (section: string, data: Record<string, unknown>) => {
    const merged = await onboardingApi.patchProfile({ [section]: data });
    setProfile(merged);
  };

  const refreshProfile = async () => {
    const p = await onboardingApi.profile();
    setProfile(p);
  };

  const complete = async () => {
    setCompleting(true);
    try {
      const state = await onboardingApi.complete();
      const serviceInstall = state.services_install ?? {};
      const statuses = Object.values(serviceInstall);
      const hasDeferredStart = "_start_deferred" in serviceInstall;
      const hasServiceIssue =
        Boolean(state.services_install_error || state.install_report_error) ||
        hasDeferredStart ||
        statuses.some((status) => status.startsWith("error:") || status.includes("failed"));
      if (hasServiceIssue) {
        setCompletionState(state);
        setCompleting(false);
        return;
      }
      navigate("/", { replace: true });
    } catch (e) {
      setErr(e instanceof Error ? e.message : "Complete failed");
      setCompleting(false);
    }
  };

  if (loading) {
    return (
      <div className="flex h-screen w-screen items-center justify-center bg-surface text-fg-tertiary text-sm">
        Loading…
      </div>
    );
  }

  if (err && !completing) {
    return (
      <div className="flex h-screen w-screen items-center justify-center bg-surface text-error text-sm px-8 text-center">
        {err}
      </div>
    );
  }

  if (completionState) {
    return (
      <CompletionStatus
        state={completionState}
        onContinue={() => navigate("/", { replace: true })}
      />
    );
  }

  // Hydrate initial section values from the loaded profile, so a user
  // returning to the wizard picks up where they left off. communication
  // is hydrated inside QuestionnaireStep directly from the `profile` prop.
  const identity = (profile.identity as Record<string, string>) || {};
  const boundaries = (profile.boundaries as Record<string, unknown>) || {};

  const pages = [
    { key: "splash", splash: true, node: <OnboardingSplashContent /> },

    {
      key: "cli_setup",
      label: "AI tools",
      node: <CliSetupStep />,
    },

    {
      key: "embed_setup",
      label: "Embedding model",
      node: <EmbedSetupStep />,
    },

    {
      key: "canon_deploy",
      label: "Agent CLIs",
      node: <CanonDeployStep />,
    },

    {
      key: "identity_name",
      label: "Your name",
      node: (
        <NameHandlePage
          initialName={identity.name ?? ""}
          initialHandle={identity.handle ?? ""}
          onSave={(name, handle) => patch("identity", { ...identity, name, handle })}
        />
      ),
    },

    {
      key: "identity_role",
      label: "Your role",
      node: (
        <RolePage
          initial={identity.role ?? identity.profession ?? ""}
          onSave={(role) => patch("identity", { ...identity, role })}
        />
      ),
    },

    {
      key: "identity_location",
      label: "Location",
      node: (
        <LocationPage
          initialLocation={identity.location ?? ""}
          initialTimezone={identity.timezone ?? detection?.timezone ?? ""}
          detectedTimezone={detection?.timezone}
          onSave={(location, timezone) => patch("identity", { ...identity, location, timezone })}
        />
      ),
    },

    {
      key: "profile_sources",
      label: "Import profile",
      node: <ProfileSourcesStep onAfterMerge={refreshProfile} />,
    },

    {
      key: "communication",
      label: "Communication",
      // Single questionnaire-driven page. The 10-item IPIP pairs drive the
      // backend's _map_answers derivation, which writes every field
      // build_behavioral_section reads (patterns, response_length,
      // directness, format_preferences.preferred, decision_style.framing,
      // decision_style.speed_vs_quality, error_handling, cognitive_style.
      // abstraction). Per-field override UI shows after submit.
      node: (
        <QuestionnaireStep profile={profile} onAfterSave={refreshProfile} />
      ),
    },

    {
      key: "principles",
      label: "Principles",
      node: <PrinciplesStep onAfterSave={refreshProfile} />,
    },

    {
      key: "boundaries",
      label: "Boundaries",
      node: (
        <BoundariesPage
          initialAutonomous={(boundaries.ok_autonomous as string[]) ?? []}
          initialAskFirst={(boundaries.never_without_asking as string[]) ?? []}
          onSave={(ok_autonomous, never_without_asking) =>
            patch("boundaries", { ...boundaries, ok_autonomous, never_without_asking })
          }
        />
      ),
    },

    {
      key: "keyring",
      label: "Master password",
      node: <KeyringPage />,
    },

    {
      key: "design",
      label: "Visual identity",
      node: <DesignStep onAfterSave={complete} />,
    },
  ];

  // The wizard always opens on the splash. Auto-resume to the backend's
  // "first not-done step" used to skip past the splash on machines where
  // unrelated state made early steps look complete (e.g. claude already
  // installed for prior dev work → cli_setup passes "for free", wizard
  // jumps to identity → user lands on Location instead of seeing the
  // brand intro). The previous fix tried to gate resume behind a
  // sessionStorage sentinel computed during render; that's a render-time
  // side effect, and React 18's multi-call rendering (StrictMode dev,
  // concurrent rendering prod) caused the second invocation to see its
  // own write and return the resume index anyway. Splash-always is the
  // reliable behaviour across every render path.
  //
  // The cost — users who refresh mid-wizard see the splash again — is
  // small: scroll-snap moves them through completed sections quickly,
  // and partial answers persist in the profile so re-entering a page
  // pre-fills its inputs.
  const initialIndex = 0;

  // WindowChrome renders `null` in a regular browser, so the shell stays
  // full-bleed there. Inside pywebview it renders a 32px drag + min/max/
  // close bar — without this the onboarding route has no drag region and
  // the frameless window can't be moved. The shell is wrapped in a
  // flex-col so the chrome takes its natural height and the wizard fills
  // the rest of the viewport.
  return (
    <div className="flex h-screen w-screen flex-col overflow-hidden bg-surface">
      <WindowChrome />
      <div className="flex-1 min-h-0 relative">
        <OnboardingShell
          pages={pages}
          onFinish={complete}
          initialIndex={initialIndex}
        />
      </div>
    </div>
  );
}

function CompletionStatus({
  state,
  onContinue,
}: {
  state: OnboardingState;
  onContinue: () => void;
}) {
  const services = Object.entries(state.services_install ?? {});
  const issues = services.filter(([, status]) =>
    status.startsWith("error:") || status.includes("failed")
  );

  return (
    <div className="flex h-screen w-screen items-center justify-center bg-surface px-8 text-fg-primary">
      <div className="w-full max-w-2xl space-y-6">
        <div className="space-y-2">
          <p className="text-xs uppercase tracking-[0.3em] text-fg-tertiary">
            setup completed
          </p>
          <h1 className="text-3xl font-light">Service handoff needs attention</h1>
          <p className="text-sm text-fg-secondary">
            Your profile was saved. Review the background service handoff before continuing.
          </p>
        </div>

        {services.length > 0 && (
          <div className="border border-border bg-surface-elevated">
            {services.map(([name, status]) => (
              <div
                key={name}
                className="grid grid-cols-[12rem_1fr] gap-4 border-b border-border px-4 py-3 text-sm last:border-b-0"
              >
                <span className="text-fg-tertiary">{name}</span>
                <span
                  className={
                    status.includes("failed") || status.startsWith("error:")
                      ? "text-error"
                      : "text-fg-secondary"
                  }
                >
                  {status}
                </span>
              </div>
            ))}
          </div>
        )}

        {state.services_install_error && (
          <p className="text-sm text-error">{state.services_install_error}</p>
        )}
        {state.install_report_error && (
          <p className="text-sm text-error">{state.install_report_error}</p>
        )}
        {state.install_report_path && (
          <p className="text-xs text-fg-tertiary">
            Report: <span className="text-fg-secondary">{state.install_report_path}</span>
          </p>
        )}

        <div className="flex items-center gap-3">
          <button
            type="button"
            onClick={onContinue}
            className="border border-border bg-surface-elevated px-4 py-2 text-sm text-fg-primary hover:border-fg-tertiary"
          >
            Continue to dashboard
          </button>
          {issues.length > 0 && (
            <span className="text-xs text-fg-tertiary">
              Open Services or Health after entering the dashboard.
            </span>
          )}
        </div>
      </div>
    </div>
  );
}

// ── Inline page components ────────────────────────────────────────────────

function NameHandlePage({
  initialName,
  initialHandle,
  onSave,
}: {
  initialName: string;
  initialHandle: string;
  onSave: (name: string, handle: string) => Promise<void>;
}) {
  const [name, setName] = useState(initialName);
  const [handle, setHandle] = useState(initialHandle);
  const canAdvance = name.trim().length > 0;

  return (
    <OnboardingPage
      question="What should we call you?"
      subtitle="First name is fine, plus a short handle. Agents use these when they address you."
      canAdvance={canAdvance}
      onAdvance={() => onSave(name.trim(), handle.trim())}
      onSkip={() => void onSave(name.trim(), handle.trim())}
    >
      <div className="flex flex-col gap-4">
        <BigTextInput value={name} onChange={setName} placeholder="Your name" />
        <div className="text-xs uppercase tracking-widest text-fg-tertiary">and a handle —</div>
        <BigTextInput
          value={handle}
          onChange={setHandle}
          placeholder="your-handle"
          autoFocus={false}
        />
      </div>
    </OnboardingPage>
  );
}

function RolePage({
  initial,
  onSave,
}: {
  initial: string;
  onSave: (role: string) => Promise<void>;
}) {
  const [role, setRole] = useState(initial);
  const canAdvance = role.trim().length > 0;
  return (
    <OnboardingPage
      question="What's your role?"
      subtitle="One short line — agents use this to frame suggestions at the right level."
      canAdvance={canAdvance}
      onAdvance={() => onSave(role.trim())}
      onSkip={() => void onSave(role.trim())}
    >
      <BigTextInput value={role} onChange={setRole} placeholder="UX design team lead" />
    </OnboardingPage>
  );
}

function LocationPage({
  initialLocation,
  initialTimezone,
  detectedTimezone,
  onSave,
}: {
  initialLocation: string;
  initialTimezone: string;
  detectedTimezone?: string;
  onSave: (location: string, timezone: string) => Promise<void>;
}) {
  const [location, setLocation] = useState(initialLocation);
  const [timezone, setTimezone] = useState(initialTimezone);

  // Seed the TIMEZONE_OPTIONS with the detected TZ if it isn't already there.
  const choices = [...TIMEZONE_OPTIONS];
  if (detectedTimezone && !choices.some((c) => c.value === detectedTimezone)) {
    choices.unshift({ value: detectedTimezone, label: detectedTimezone, hint: "detected" });
  }

  const canAdvance = !!timezone;
  return (
    <OnboardingPage
      question="Where are you based?"
      subtitle="Country or region + timezone. Timezone drives when reminders fire."
      canAdvance={canAdvance}
      onAdvance={() => onSave(location.trim(), timezone)}
      onSkip={() => void onSave(location.trim(), timezone)}
    >
      <div className="flex flex-col gap-6">
        <div>
          <div className="text-xs uppercase tracking-widest text-fg-tertiary mb-2">
            Location
          </div>
          <BigTextInput value={location} onChange={setLocation} placeholder="Switzerland" />
        </div>
        <div>
          <div className="text-xs uppercase tracking-widest text-fg-tertiary mb-2">
            Timezone
          </div>
          <ChoiceCard value={timezone} onChange={setTimezone} choices={choices} />
        </div>
      </div>
    </OnboardingPage>
  );
}

function BoundariesPage({
  initialAutonomous,
  initialAskFirst,
  onSave,
}: {
  initialAutonomous: string[];
  initialAskFirst: string[];
  onSave: (autonomous: string[], askFirst: string[]) => Promise<void>;
}) {
  const [autonomous, setAutonomous] = useState(initialAutonomous);
  const [askFirst, setAskFirst] = useState(initialAskFirst);
  return (
    <OnboardingPage
      question="What can agents do on their own?"
      subtitle="Split up green-light actions from ones you want agents to check with you on first."
      canAdvance
      onAdvance={() => onSave(autonomous, askFirst)}
      onSkip={() => void onSave(autonomous, askFirst)}
      moreInfo={
        <p>
          Agents consult these lists before taking an action. Anything in
          "autonomous" runs without interruption; anything in "ask first"
          surfaces a confirmation. You can refine this list any time from
          Settings → Boundaries.
        </p>
      }
    >
      <div className="flex flex-col gap-6">
        <div>
          <div className="text-xs uppercase tracking-widest text-fg-tertiary mb-2">
            OK to do autonomously
          </div>
          <ChipMulti
            values={autonomous}
            onChange={setAutonomous}
            suggestions={AUTONOMOUS_SUGGESTIONS}
            placeholder="Add an action"
          />
        </div>
        <div>
          <div className="text-xs uppercase tracking-widest text-fg-tertiary mb-2">
            Always ask first
          </div>
          <ChipMulti
            values={askFirst}
            onChange={setAskFirst}
            suggestions={ASK_FIRST_SUGGESTIONS}
            placeholder="Add an action"
          />
        </div>
      </div>
    </OnboardingPage>
  );
}

function KeyringPage() {
  // Two paths: (a) let the OS keychain generate + store a strong password,
  // so the user never types or remembers it; (b) user-chosen password. We
  // default to (a) — it's the right answer for every mac/linux laptop that
  // has a native credential store, which is ~100% of the user base.
  const [mode, setMode] = useState<"generate" | "manual">("generate");
  const [pw, setPw] = useState("");
  const [confirm, setConfirm] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [done, setDone] = useState(false);
  const [storage, setStorage] = useState<string | undefined>();

  const submit = async () => {
    setErr(null);
    try {
      setSubmitting(true);
      if (mode === "generate") {
        const r = await onboardingApi.initKeyringGenerated();
        setStorage(r.storage);
      } else {
        if (pw.length < 8) {
          setSubmitting(false);
          return setErr("At least 8 characters.");
        }
        if (pw !== confirm) {
          setSubmitting(false);
          return setErr("Passwords don't match.");
        }
        const r = await onboardingApi.initKeyring(pw);
        setStorage(r.storage);
      }
      setDone(true);
    } catch (e) {
      setErr(e instanceof Error ? e.message : "Init failed");
    } finally {
      setSubmitting(false);
    }
  };

  const osKeychainName =
    typeof navigator !== "undefined" && /Mac|iPhone|iPad/.test(navigator.platform)
      ? "macOS Keychain"
      : typeof navigator !== "undefined" && /Win/.test(navigator.platform)
        ? "Windows Credential Locker"
        : "your OS keychain";

  return (
    <OnboardingPage
      question="Secrets vault"
      subtitle="Encrypts your API keys, tokens, and credentials. The vault unlocks with a master password."
      canAdvance={done}
      moreInfo={
        <p>
          Okuro stores API keys, tokens, and credentials encrypted under this
          master password. It never leaves your machine. Rotate it later from
          Settings → Keyring.
        </p>
      }
    >
      <div className="flex flex-col gap-4">
        {/* Mode picker — generate (default) vs manual */}
        <div className="flex flex-col gap-2" role="radiogroup" aria-label="Master password mode">
          <ModeOption
            selected={mode === "generate"}
            onSelect={() => setMode("generate")}
            title="Let okuro generate one"
            description={`Okuro generates a strong random key and stores it in ${osKeychainName} (or, when that's not available, encrypts it to this Mac's hardware identity). You never type or remember it.`}
            tag="recommended"
            disabled={done}
          />
          <ModeOption
            selected={mode === "manual"}
            onSelect={() => setMode("manual")}
            title="Set my own password"
            description="Type a password you'll remember. Stored in your OS keychain when available; otherwise the vault is bound to this Mac."
            disabled={done}
          />
        </div>

        {mode === "manual" && !done && (
          <div className="flex flex-col gap-4">
            <div>
              <div className="text-xs uppercase tracking-widest text-fg-tertiary mb-2">
                Master password
              </div>
              <BigTextInput
                type="password"
                value={pw}
                onChange={setPw}
                placeholder="At least 8 characters"
              />
            </div>
            <div>
              <div className="text-xs uppercase tracking-widest text-fg-tertiary mb-2">
                Confirm
              </div>
              <BigTextInput
                type="password"
                value={confirm}
                onChange={setConfirm}
                placeholder="Type it again"
                autoFocus={false}
              />
            </div>
          </div>
        )}

        {err && <p className="text-xs text-error">{err}</p>}
        {done && (
          <p className="text-xs text-success">
            Vault initialized.
            {storage === "os-keychain" && ` Master key stored in ${osKeychainName}.`}
            {storage === "file" && ` Encrypted with a key bound to this Mac (the OS keychain isn't available to background services on this macOS version — your password is NOT stored on disk; the encryption key is derived from this machine's hardware identity each time okuro starts). The vault won't decrypt on another machine.`}
          </p>
        )}

        {!done && (
          <button
            type="button"
            onClick={submit}
            disabled={submitting || (mode === "manual" && (!pw || !confirm))}
            className="self-start rounded bg-accent px-4 py-2 text-sm text-fg-inverse hover:bg-accent-hover disabled:opacity-50 transition-colors"
          >
            {submitting ? "Creating…" : "Create vault"}
          </button>
        )}
      </div>
    </OnboardingPage>
  );
}

function ModeOption({
  selected,
  onSelect,
  title,
  description,
  tag,
  disabled,
}: {
  selected: boolean;
  onSelect: () => void;
  title: string;
  description: string;
  tag?: string;
  disabled?: boolean;
}) {
  return (
    <button
      type="button"
      role="radio"
      aria-checked={selected}
      onClick={onSelect}
      disabled={disabled}
      className={`group text-left rounded border px-4 py-3 transition-colors disabled:opacity-60 ${
        selected
          ? "border-accent bg-accent/5"
          : "border-border hover:border-border-hover"
      }`}
    >
      <div className="flex items-center gap-2">
        <span
          className={`inline-block h-3 w-3 rounded-full border ${
            selected ? "border-accent bg-accent" : "border-border"
          }`}
          aria-hidden="true"
        />
        <span className="text-sm font-medium text-fg">{title}</span>
        {tag && (
          <span className="rounded-full bg-accent/20 px-2 py-0.5 text-2xs uppercase tracking-wider text-accent">
            {tag}
          </span>
        )}
      </div>
      <div className="mt-1 pl-5 text-xs text-fg-muted">{description}</div>
    </button>
  );
}
