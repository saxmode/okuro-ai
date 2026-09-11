import { lazy, Suspense, useEffect, type ReactNode } from "react";
import { QueryClient, QueryClientProvider, useQuery } from "@tanstack/react-query";
import { BrowserRouter, Routes, Route, Navigate, useParams, Outlet } from "react-router";
import { TooltipProvider } from "@/components/ui/tooltip";
import { Toaster } from "@/components/ui/toast";
import { DownloadsProvider } from "@/lib/downloads-context";
import { DownloadsTray } from "@/components/models/downloads-tray";
import { AppShell } from "@/components/shell/app-shell";
import { ErrorBoundary } from "@/components/error-boundary";
import { HomePage } from "@/pages/home";
import { openExternal, api } from "@/lib/api";
import {
  applyPulseOutlineOverrideFromProfile,
} from "@/lib/theme";

// Lazy import hardened against "Importing a module script failed" — a stale
// deploy or a transient/tunnelled fetch can 404 a code-split chunk after a
// rebuild. On the first such failure we reload once (index.html is no-cache, so
// the reload pulls the current chunk map); a repeat failure surfaces normally.
function lazyWithRetry<T extends React.ComponentType<any>>(factory: () => Promise<{ default: T }>) {
  return lazy(async () => {
    const KEY = "okuro:chunk-reloaded";
    try {
      const mod = await factory();
      sessionStorage.removeItem(KEY);
      return mod;
    } catch (err) {
      if (!sessionStorage.getItem(KEY)) {
        sessionStorage.setItem(KEY, "1");
        window.location.reload();
        return await new Promise<{ default: T }>(() => {}); // hold render until reload
      }
      throw err;
    }
  });
}

// Lazy-load heavy pages for code-splitting
const TasksPage = lazyWithRetry(() =>
  import("@/pages/tasks").then((m) => ({ default: m.TasksPage })),
);
const TaskDetailPage = lazyWithRetry(() =>
  import("@/pages/task-detail").then((m) => ({ default: m.TaskDetailPage })),
);
const WorkGanttPage = lazyWithRetry(() =>
  import("@/pages/work-gantt").then((m) => ({ default: m.WorkGanttPage })),
);
const AgentsPage = lazyWithRetry(() =>
  import("@/pages/agents").then((m) => ({ default: m.AgentsPage })),
);
const BrainPage = lazyWithRetry(() =>
  import("@/pages/brain").then((m) => ({ default: m.BrainPage })),
);
const KnowledgePage = lazyWithRetry(() =>
  import("@/pages/knowledge").then((m) => ({ default: m.KnowledgePage })),
);
const LessonsPage = lazyWithRetry(() =>
  import("@/pages/lessons").then((m) => ({ default: m.LessonsPage })),
);
const NotesPage = lazyWithRetry(() =>
  import("@/pages/notes").then((m) => ({ default: m.NotesPage })),
);
const HealthPage = lazyWithRetry(() =>
  import("@/pages/health").then((m) => ({ default: m.HealthPage })),
);
const ScheduledPage = lazyWithRetry(() =>
  import("@/pages/scheduled").then((m) => ({ default: m.ScheduledPage })),
);
const OnboardingPage = lazyWithRetry(() =>
  import("@/pages/onboarding-preview").then((m) => ({ default: m.OnboardingPreviewPage })),
);
const DesignEnginePage = lazyWithRetry(() =>
  import("@/pages/design-engine").then((m) => ({ default: m.DesignEnginePage })),
);
const DsEngineCodexPage = lazyWithRetry(() =>
  import("@/pages/ds-engine-codex").then((m) => ({ default: m.DsEngineCodexPage })),
);
const StackPage = lazyWithRetry(() =>
  import("@/pages/stack").then((m) => ({ default: m.StackPage })),
);
const AssetsPage = lazyWithRetry(() =>
  import("@/pages/assets").then((m) => ({ default: m.AssetsPage })),
);
const SettingsPage = lazyWithRetry(() =>
  import("@/pages/settings").then((m) => ({ default: m.SettingsPage })),
);
const SettingsEmbedPage = lazyWithRetry(() =>
  import("@/pages/settings-embed").then((m) => ({ default: m.SettingsEmbedPage })),
);
const SettingsSttPage = lazyWithRetry(() =>
  import("@/pages/settings-stt").then((m) => ({ default: m.SettingsSttPage })),
);
const SettingsTtsPage = lazyWithRetry(() =>
  import("@/pages/settings-tts").then((m) => ({ default: m.SettingsTtsPage })),
);
const CortexPage = lazyWithRetry(() =>
  import("@/pages/cortex").then((m) => ({ default: m.CortexPage })),
);
const ReposPage = lazyWithRetry(() =>
  import("@/pages/repos").then((m) => ({ default: m.ReposPage })),
);
const RepoDetailPage = lazyWithRetry(() =>
  import("@/pages/repo-detail").then((m) => ({ default: m.RepoDetailPage })),
);
const CorporaPage = lazyWithRetry(() =>
  import("@/pages/corpora").then((m) => ({ default: m.CorporaPage })),
);
const ServicesPage = lazyWithRetry(() =>
  import("@/pages/services").then((m) => ({ default: m.ServicesPage })),
);
const PeoplePage = lazyWithRetry(() =>
  import("@/pages/people").then((m) => ({ default: m.PeoplePage })),
);
const MediaPage = lazyWithRetry(() =>
  import("@/pages/media").then((m) => ({ default: m.MediaPage })),
);
const QuestionnairePage = lazyWithRetry(() =>
  import("@/pages/questionnaire").then((m) => ({ default: m.QuestionnairePage })),
);
const InboxPage = lazyWithRetry(() =>
  import("@/pages/inbox").then((m) => ({ default: m.InboxPage })),
);
const ProjectsPage = lazyWithRetry(() =>
  import("@/pages/projects").then((m) => ({ default: m.ProjectsPage })),
);
const BridgePage = lazyWithRetry(() =>
  import("@/pages/bridge").then((m) => ({ default: m.BridgePage })),
);
const ModelsPage = lazyWithRetry(() =>
  import("@/pages/models").then((m) => ({ default: m.ModelsPage })),
);
const AboutPage = lazyWithRetry(() =>
  import("@/pages/about").then((m) => ({ default: m.AboutPage })),
);
const InlineChatPage = lazyWithRetry(() =>
  import("@/pages/inline").then((m) => ({ default: m.InlineChatPage })),
);
const FlowPage = lazyWithRetry(() =>
  import("@/pages/flow").then((m) => ({ default: m.FlowPage })),
);
const WorkflowsPage = lazyWithRetry(() =>
  import("@/pages/workflows").then((m) => ({ default: m.WorkflowsPage })),
);
const SlidesPage = lazyWithRetry(() =>
  import("@/pages/slides").then((m) => ({ default: m.SlidesPage })),
);
const PrismPage = lazyWithRetry(() =>
  import("@/pages/prism").then((m) => ({ default: m.PrismPage })),
);
// Kit-first 2D deck v2 runtime (A4) — new render path behind its own route.
const PrismDeckPage = lazyWithRetry(() =>
  import("@/pages/prism-deck").then((m) => ({ default: m.PrismDeckPage })),
);
// Split-view HTML review: page in a sandboxed frame, comments beside it.
const RedlinePage = lazyWithRetry(() =>
  import("@/pages/redline").then((m) => ({ default: m.RedlinePage })),
);
const SparringPage = lazyWithRetry(() =>
  import("@/pages/sparring").then((m) => ({ default: m.SparringPage })),
);
const PrismGalleryPage = lazyWithRetry(() =>
  import("@/pages/prism-gallery").then((m) => ({ default: m.PrismGalleryPage })),
);

const ResonancePage = lazyWithRetry(() =>
  import("@/pages/resonance").then((m) => ({ default: m.ResonancePage })),
);
const StudioPage = lazyWithRetry(() =>
  import("@/pages/studio").then((m) => ({ default: m.StudioPage })),
);

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 5_000,
      retry: 1,
      refetchOnWindowFocus: false,
    },
  },
});

function PageLoader() {
  return (
    <div className="flex h-full items-center justify-center text-sm text-tertiary">
      Loading...
    </div>
  );
}

function LegacyTaskRedirect() {
  const { id } = useParams<{ id: string }>();
  return <Navigate to={`/work/${id ?? ""}`} replace />;
}

/**
 * Gate every AppShell route on onboarding completion.
 *
 * A post-mac-wipe user who launched the app from Finder/Dock could land on
 * the dashboard before completing the wizard — because `okuro dashboard`
 * skips the first-run check. The install.sh shim now runs plain `okuro`
 * (which respects first-run routing), but this belt-and-braces gate covers
 * the URL-paste / stale-daemon / legacy-bundle cases too.
 *
 * Behavior:
 *   - completed=true  → render children (AppShell)
 *   - completed=false → redirect to /onboarding (replace, so back button works)
 *   - loading         → render the app optimistically (see below)
 *   - fetch error     → let the dashboard render; don't trap the user
 *
 * Render-while-loading is deliberate: /api/onboarding/state spawns CLI
 * subprocess probes server-side and used to block first paint for seconds on
 * cold launches. The common case is an already-onboarded returning user, so
 * we paint immediately and only redirect once the query positively reports
 * completed===false. A brand-new user sees at most a brief dashboard flash
 * before the redirect — an acceptable trade for removing seconds off boot.
 */
function OnboardingGate() {
  const { data, isError } = useQuery({
    queryKey: ["onboarding", "state"],
    queryFn: async () =>
      api<{ completed?: boolean }>("/api/onboarding/state"),
    staleTime: 60_000,
    retry: 1,
  });

  if (!isError && data?.completed === false) {
    return <Navigate to="/onboarding" replace />;
  }
  return <Outlet />;
}

// Dev-only smoke page that throws during render so the ErrorBoundary fallback
// can be visually verified without mocking. Gated on import.meta.env.DEV so it
// cannot ship in a production build.
function DevCrashPage(): ReactNode {
  throw new Error("intentional render throw (DevCrashPage) — /dev/crash");
}

const withBoundary = (node: ReactNode): ReactNode => (
  <ErrorBoundary>{node}</ErrorBoundary>
);

/**
 * Global click delegate that routes ``<a target="_blank">`` clicks to
 * the user's default system browser when running inside pywebview.
 *
 * pywebview drops ``target="_blank"`` and ``window.open`` silently on
 * every backend, so without this every "open in new tab" link in the
 * app (about, knowledge panel, maintenance panel, markdown content,
 * etc.) is a dead click. In a normal browser ``window.pywebview`` is
 * undefined and we don't intercept — the browser handles it natively.
 */
function useExternalLinkRouting(): void {
  useEffect(() => {
    if (!window.pywebview?.api?.open_external) {
      // Real browser, or bridge not yet ready. The bridge becomes
      // available before any user click in practice (it's wired before
      // create_window resolves), so we don't bother re-running on
      // pywebviewready — a missed early click is acceptable.
      return;
    }
    const onClick = (e: MouseEvent) => {
      // Honor modifier keys / middle-click in case the user has a
      // mental model from a real browser. Inside pywebview these don't
      // do anything special anyway, so this is harmless.
      if (e.defaultPrevented) return;
      if (e.button !== 0) return;
      if (e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
      const path = e.composedPath();
      const anchor = path.find(
        (n): n is HTMLAnchorElement =>
          n instanceof HTMLAnchorElement && n.tagName === "A",
      );
      if (!anchor) return;
      if (anchor.target !== "_blank") return;
      const href = anchor.href;
      if (!href) return;
      if (!/^https?:/i.test(href)) return;
      e.preventDefault();
      void openExternal(href);
    };
    document.addEventListener("click", onClick, true);
    return () => document.removeEventListener("click", onClick, true);
  }, []);
}

/**
 * Sync the pulse-blob outline override from the user's profile on app boot.
 *
 * This used to reconcile the ACCENT too. It no longer can and no longer needs
 * to: the accent is resolved into `/engine.css` server-side, so it arrives with
 * the stylesheet rather than being painted over it afterwards. The pulse blob
 * is genuinely a client-side canvas setting — pulse-engine.ts reads two CSS
 * variables off `:root` — so its override stays here.
 */
function usePulseOutlineSync(): void {
  useEffect(() => {
    let cancelled = false;
    api<Record<string, unknown>>("/api/onboarding/profile")
      .then((profile) => {
        if (cancelled) return;
        applyPulseOutlineOverrideFromProfile(profile);
      })
      .catch(() => { /* offline / pre-onboarding — keep cached value */ });
    return () => { cancelled = true; };
  }, []);
}

export function App() {
  useExternalLinkRouting();
  usePulseOutlineSync();
  return (
    <QueryClientProvider client={queryClient}>
      <DownloadsProvider>
      <TooltipProvider>
        <Toaster />
        <DownloadsTray />
        <BrowserRouter>
          <Suspense fallback={<PageLoader />}>
            <Routes>
              {/* Public recipient-facing form. Lives OUTSIDE OnboardingGate
                  + AppShell so an unauthenticated visitor can fill it in. */}
              <Route path="q/:token" element={withBoundary(<QuestionnairePage />)} />
              <Route path="onboarding" element={withBoundary(<OnboardingPage />)} />
              {/* Inline streaming session — full-page chat, lives outside
                  the AppShell so a 'Solve' popout has no dashboard chrome.
                  Auth is per-session bearer via ?t=, not the global token. */}
              <Route
                path="inline/:sessionId"
                element={withBoundary(<InlineChatPage />)}
              />
              <Route element={<OnboardingGate />}>
              <Route element={withBoundary(<AppShell />)}>
                <Route index element={withBoundary(<HomePage />)} />
                <Route path="inbox" element={withBoundary(<InboxPage />)} />
                <Route path="projects" element={withBoundary(<ProjectsPage />)} />
                <Route path="work" element={withBoundary(<TasksPage />)} />
                <Route path="work/gantt" element={withBoundary(<WorkGanttPage />)} />
                <Route path="work/:id" element={withBoundary(<TaskDetailPage />)} />
                <Route path="agents" element={withBoundary(<AgentsPage />)} />
                <Route path="signals" element={<Navigate to="/inbox" replace />} />
                <Route path="brain" element={withBoundary(<BrainPage />)} />
                <Route path="knowledge" element={withBoundary(<KnowledgePage />)} />
                <Route path="lessons" element={withBoundary(<LessonsPage />)} />
                <Route path="notes" element={withBoundary(<NotesPage />)} />
                <Route path="health" element={withBoundary(<HealthPage />)} />
                <Route path="scheduled" element={withBoundary(<ScheduledPage />)} />
                <Route path="stack" element={withBoundary(<StackPage />)} />
                <Route path="design-engine" element={withBoundary(<DesignEnginePage />)} />
                <Route path="ds-engine-codex" element={withBoundary(<DsEngineCodexPage />)} />
                <Route path="assets" element={withBoundary(<AssetsPage />)} />
                <Route path="cortex" element={withBoundary(<CortexPage />)} />
                <Route path="repos" element={withBoundary(<ReposPage />)} />
                <Route path="repos/:id" element={withBoundary(<RepoDetailPage />)} />
                <Route path="corpora" element={withBoundary(<CorporaPage />)} />
                <Route path="flow" element={withBoundary(<FlowPage />)} />
                <Route path="workflows" element={withBoundary(<WorkflowsPage />)} />
                <Route path="slides" element={withBoundary(<SlidesPage />)} />
          <Route path="prism" element={withBoundary(<PrismPage />)} />
                <Route path="prism/deck" element={withBoundary(<PrismDeckPage />)} />
                <Route path="prism-gallery" element={withBoundary(<PrismGalleryPage />)} />
                <Route path="redline" element={withBoundary(<RedlinePage />)} />
                <Route path="redline/:document_id" element={withBoundary(<RedlinePage />)} />
                <Route path="sparring" element={withBoundary(<SparringPage />)} />
                <Route path="resonance" element={withBoundary(<ResonancePage />)} />
                <Route path="studio" element={withBoundary(<StudioPage />)} />
                <Route path="services" element={withBoundary(<ServicesPage />)} />
                <Route path="people" element={withBoundary(<PeoplePage />)} />
                <Route path="media" element={withBoundary(<MediaPage />)} />
                <Route path="reminders" element={<Navigate to="/inbox" replace />} />
                <Route path="todos" element={<Navigate to="/inbox" replace />} />
                <Route path="bridge" element={withBoundary(<BridgePage />)} />
                <Route path="models" element={withBoundary(<ModelsPage />)} />
                <Route path="settings" element={withBoundary(<SettingsPage />)} />
                <Route path="settings/embed" element={withBoundary(<SettingsEmbedPage />)} />
                <Route path="settings/stt" element={withBoundary(<SettingsSttPage />)} />
                <Route path="settings/tts" element={withBoundary(<SettingsTtsPage />)} />
                <Route path="about" element={withBoundary(<AboutPage />)} />

                {/* Legacy path redirects — old bookmarks keep working. */}
                <Route path="tasks" element={<Navigate to="/work" replace />} />
                <Route path="tasks/:id" element={<LegacyTaskRedirect />} />
                <Route path="roles" element={<Navigate to="/agents" replace />} />
                <Route path="dashboard" element={<Navigate to="/agents" replace />} />
                <Route path="system" element={<Navigate to="/health" replace />} />

                {import.meta.env.DEV && (
                  <Route path="dev/crash" element={withBoundary(<DevCrashPage />)} />
                )}
              </Route>
              </Route>
            </Routes>
          </Suspense>
        </BrowserRouter>
      </TooltipProvider>
      </DownloadsProvider>
    </QueryClientProvider>
  );
}
