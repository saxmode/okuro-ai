import { Suspense, useEffect, type ReactNode } from "react";
import { QueryClient, QueryClientProvider, useQuery } from "@tanstack/react-query";
import { BrowserRouter, Routes, Route, Navigate, Outlet } from "react-router";
import { TooltipProvider } from "@/components/ui/tooltip";
import { Toaster } from "@/components/ui/toast";
import { DownloadsProvider } from "@/lib/downloads-context";
import { DownloadsTray } from "@/components/models/downloads-tray";
import { ErrorBoundary } from "@/components/error-boundary";
import { FeaturesProvider } from "@/lib/features-context";
import { lazyWithRetry } from "@/lib/lazy-with-retry";
import { openExternal, api } from "@/lib/api";
import { applyPulseOutlineOverrideFromProfile } from "@/lib/theme";
import { Frame } from "@/shell/Frame";

/* ---------------------------------------------------------------------------
 * THE ROUTE TABLE IS GONE, AND THAT IS THE WHOLE OF p2's W3.
 * ---------------------------------------------------------------------------
 * This file used to declare 54 paths and 40 lazy page imports. Both moved: the
 * ADDRESSES are resolved by `shell/routes.ts` from the path and the IA, and the
 * IMPORTS are `shell/views/registry.ts`, one line per leaf, one chunk per leaf.
 *
 * THE SHELL IS THE FRAME, NOT A ROUTE, and the splat below is how that is
 * expressed in a router. Law 4 says the topic bars are never unmounted — the
 * slide needs both the outgoing and the incoming bar present to move — and a
 * `<Route element={<Shell />}>` per path would remount them on every
 * navigation. ONE `path="*"` route matches every leaf address, so React
 * reconciles the same element instead of mounting a new one, and the shell
 * reads `useLocation()` for itself. (Memory 2b036d78, the address layer.)
 *
 * FOUR ROUTES STAY ROUTES, and each for a reason that predates the redesign:
 *   q/:token           public and unauthenticated — outside OnboardingGate too
 *   onboarding         the gate's own destination; it precedes the shell
 *   inline/:sessionId  per-session bearer via ?t=, deliberately chrome-free
 *   dev/crash          the ErrorBoundary fixture
 *
 * `dev/crash` IS THE ONE THAT MOVED OUT rather than staying where it was. It sat
 * inside AppShell + FeatureGate; under the shell it would not be addressable at
 * all, because `/dev/crash` is not a topic and the resolver would forgive it
 * straight to `/start/now` — the fixture would have silently stopped existing.
 * As a top-level route it still throws during render and still proves
 * `withBoundary`, which is its only job.
 *
 * ORDER MATTERS: the splat is last, so a specific path above it always wins.
 * --------------------------------------------------------------------------- */

// The four that are not leaves. Every other page is imported by the leaf
// registry, which carries the same stale-chunk guard (lib/lazy-with-retry.ts).
const QuestionnairePage = lazyWithRetry(() =>
  import("@/pages/questionnaire").then((m) => ({ default: m.QuestionnairePage })),
);
const OnboardingPage = lazyWithRetry(() =>
  import("@/pages/onboarding-preview").then((m) => ({ default: m.OnboardingPreviewPage })),
);
const InlineChatPage = lazyWithRetry(() =>
  import("@/pages/inline").then((m) => ({ default: m.InlineChatPage })),
);

// THE TWO UNRULED ROUTES, KEPT REACHABLE RATHER THAN DECIDED.
//
// `sparring` (507 ln) and `prism-gallery` (197 ln) render real pages and have no
// leaf. The owner has not ruled either — on `prism-gallery` he said "I don't know.
// Prism is work in progress" and asked for okuro-prism's own history to be read
// first; `sparring` has no proposal at all yet. Giving either one a leaf, or a
// section of PRISM, would be p3's analysis done without the analysis.
//
// SO THEY STAY ROUTES, and that is not a shrug: under the shell alone they would
// be UNREACHABLE. Neither is a topic, so the resolver would forgive `/sparring`
// straight to `/start/now` and a live page would quietly stop existing —
// precisely the "swallowed by the forgiving unknown-path rule" that
// `routes.ts::UNHOMED` was written to prevent. Declared here, above the splat,
// they keep their addresses and their bookmarks. What they lose until they are
// ruled is the chrome: they render full-page, with no sidebar and no topic bars.
//
// They sit INSIDE OnboardingGate, so a user who has not finished the wizard is
// still redirected, exactly as before.
const SparringPage = lazyWithRetry(() =>
  import("@/pages/sparring").then((m) => ({ default: m.SparringPage })),
);
const PrismGalleryPage = lazyWithRetry(() =>
  import("@/pages/prism-gallery").then((m) => ({ default: m.PrismGalleryPage })),
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

/** The fallback for the four full-page routes outside the shell. */
function PageLoader() {
  return (
    <div className="flex h-full items-center justify-center text-sm text-tertiary">
      Loading...
    </div>
  );
}

/* ---------------------------------------------------------------------------
 * FOUR HELPERS LEFT THIS FILE IN p2, AND EACH LEFT FOR A DIFFERENT REASON.
 * ---------------------------------------------------------------------------
 * `PageLoader` — it was the Suspense fallback and the two gates' loading state.
 *   The shell's per-pane Suspense renders NOTHING on purpose
 *   (`shell/components/LeafView.tsx`): a spinner that appears and vanishes
 *   inside the 650ms slide reads as debris, and the pane is already animating.
 *   The remaining Suspense here covers the four non-leaf routes, which are
 *   full-page and have no slide to compete with, so it keeps a plain loader.
 *
 * `LegacyTaskRedirect` — it turned `/tasks/:id` into `/work/:id`. The shell's
 *   `LEGACY_PREFIXES` (`shell/routes.ts:287`) does the same job as data:
 *   `/tasks/abc123` -> `/work/tasks/abc123`, asserted in `tests/routes.test.ts`.
 *
 * `FeatureGate` — moved to `shell/components/LeafView.tsx`, per leaf instead of
 *   per route, because there is no route table left to sit in. It needed one
 *   change on the way, and NOT a cosmetic one: it was keyed on `pathname`, and
 *   the only `routes=` declaration okuro has is `("/lessons",)`, which the new
 *   address `/know/lessons` does not match. See that file.
 *
 * `FeatureAwareNavigate` — it kept `/roles` and `/dashboard` from landing on
 *   FeatureOffPage when AGENTS is off, sending them home instead. Both are
 *   `LEGACY` entries now, so they resolve to `/work/agents` and meet the same
 *   per-leaf gate as any other way of reaching that leaf. The behaviour it
 *   bought — "a moved bookmark should not read as broken" — is a question about
 *   what a withheld leaf shows, which is now one decision in one place rather
 *   than a special case for two paths.
 * --------------------------------------------------------------------------- */

/**
 * Gate the whole shell on onboarding completion.
 *
 * A post-mac-wipe user who launched the app from Finder/Dock could land on
 * the dashboard before completing the wizard — because `okuro dashboard`
 * skips the first-run check. The install.sh shim now runs plain `okuro`
 * (which respects first-run routing), but this belt-and-braces gate covers
 * the URL-paste / stale-daemon / legacy-bundle cases too.
 *
 * Behavior:
 *   - completed=true  → render children (the shell frame)
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
      {/* Inside the query client (it fetches), outside the router (the whole
          app reads it — nav, palette, routes and the welcome panel alike). */}
      <FeaturesProvider>
      <DownloadsProvider>
      <TooltipProvider>
        <Toaster />
        <DownloadsTray />
        <BrowserRouter>
          {/* This Suspense covers the four non-leaf routes only. Every leaf has
              its own, per pane, inside the shell — and that one renders nothing
              rather than a loader, for the reason LeafView gives. */}
          <Suspense fallback={<PageLoader />}>
            <Routes>
              {/* Public recipient-facing form. Lives OUTSIDE OnboardingGate
                  and outside the shell so an unauthenticated visitor can fill
                  it in. */}
              <Route path="q/:token" element={withBoundary(<QuestionnairePage />)} />
              <Route path="onboarding" element={withBoundary(<OnboardingPage />)} />
              {/* Inline streaming session — full-page chat, outside the shell
                  so a 'Solve' popout has no dashboard chrome. Auth is a
                  per-session bearer via ?t=, not the global token. */}
              <Route path="inline/:sessionId" element={withBoundary(<InlineChatPage />)} />
              {import.meta.env.DEV && (
                <Route path="dev/crash" element={withBoundary(<DevCrashPage />)} />
              )}
              {/* EVERYTHING ELSE IS THE SHELL, and it is ONE route so the topic
                  bars are never remounted. The gate stays a pathless layout
                  route ABOVE it: it has to be able to redirect to /onboarding
                  before the frame paints. */}
              <Route element={<OnboardingGate />}>
                {/* Unruled, and above the splat so they stay addressable. */}
                <Route path="sparring" element={withBoundary(<SparringPage />)} />
                <Route path="prism-gallery" element={withBoundary(<PrismGalleryPage />)} />
                <Route path="*" element={withBoundary(<Frame />)} />
              </Route>
            </Routes>
          </Suspense>
        </BrowserRouter>
      </TooltipProvider>
      </DownloadsProvider>
      </FeaturesProvider>
    </QueryClientProvider>
  );
}
