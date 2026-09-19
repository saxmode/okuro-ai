import { useEffect, useState, useMemo } from "react";
import { useNavigate } from "react-router";
import { usePaneInterval } from "@/lib/pane-active";
import type { LeafViewProps } from "@/shell/views/registry";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import {
  RefreshCw,
  Play,
  Square,
  RotateCw,
  Loader2,
  Download,
  X,
  Power,
  PowerOff,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { EmptyState } from "@/components/ui/empty-state";
import { toast } from "@/components/ui/toast";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { api } from "@/lib/api";
import { cn } from "@/lib/utils";
import { useSectionTitle } from "@/shell/components/PageTitle";

/**
 * /services — control okuro background services.
 *
 * Surfaces the service_manager module to the browser:
 * start/stop/restart/install/enable/disable + live state + journal logs.
 *
 * Backend: /api/services/* (see src/okuro/orchestrator/api/services.py).
 */

// ── Types ────────────────────────────────────────────────────────────

type ServiceRow = {
  name: string;
  description: string;
  state: string;
  active: boolean;
  running: boolean;
  backend: string;
  installed: boolean;
  uptime_seconds: number | null;
  memory_bytes: number | null;
  restart_count: number | null;
  enabled: boolean | null;
  pid: number | null;
  started_at: number | null;
};

type ServiceListResponse = {
  services: ServiceRow[];
  backend: string;
};

type ServiceDetail = {
  service: ServiceRow;
  exec_start: string[];
  working_directory: string | null;
  environment: Record<string, string>;
  logs: string[];
};

// ── API helpers ──────────────────────────────────────────────────────

const servicesApi = {
  list: () => api<ServiceListResponse>("/api/services"),
  get: (name: string) =>
    api<ServiceDetail>(`/api/services/${encodeURIComponent(name)}`),
  start: (name: string) =>
    api<ServiceRow>(`/api/services/${encodeURIComponent(name)}/start`, {
      method: "POST",
    }),
  stop: (name: string) =>
    api<ServiceRow>(`/api/services/${encodeURIComponent(name)}/stop`, {
      method: "POST",
    }),
  restart: (name: string) =>
    api<ServiceRow>(`/api/services/${encodeURIComponent(name)}/restart`, {
      method: "POST",
    }),
  install: (name: string) =>
    api<ServiceRow>(`/api/services/${encodeURIComponent(name)}/install`, {
      method: "POST",
    }),
  enable: (name: string) =>
    api<ServiceRow>(`/api/services/${encodeURIComponent(name)}/enable`, {
      method: "POST",
    }),
  disable: (name: string) =>
    api<ServiceRow>(`/api/services/${encodeURIComponent(name)}/disable`, {
      method: "POST",
    }),
};

// ── Formatters ───────────────────────────────────────────────────────

function formatUptime(seconds: number | null): string {
  if (seconds === null || seconds < 0) return "—";
  const d = Math.floor(seconds / 86400);
  const h = Math.floor((seconds % 86400) / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  const s = seconds % 60;
  if (d > 0) return `${d}d ${h}h`;
  if (h > 0) return `${h}h ${m}m`;
  if (m > 0) return `${m}m ${s}s`;
  return `${s}s`;
}

function formatMemory(bytes: number | null): string {
  if (bytes === null || bytes <= 0) return "—";
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(0)} KB`;
  if (bytes < 1024 * 1024 * 1024)
    return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
  return `${(bytes / (1024 * 1024 * 1024)).toFixed(2)} GB`;
}

type PillVariant = "success" | "error" | "warning" | "muted";

function statePill(state: string, active: boolean): PillVariant {
  if (state === "active" || active) return "success";
  if (state === "failed") return "error";
  if (state === "activating" || state === "reloading" || state === "deactivating")
    return "warning";
  return "muted";
}

// ── Page ─────────────────────────────────────────────────────────────

/**
 * THE DETAIL IS AN ADDRESS NOW — Q-L8 item (f).
 *
 *   /system/services          the card grid
 *   /system/services/{name}   the same grid with that service's detail open
 *
 * The detail holds `exec_start`, the working directory, the environment and
 * the last 200 journal lines — the densest view in the leaf and the one thing
 * a person would paste to someone else — and it lived in `useState`, so it
 * could not be linked, survived no refresh, and was reachable from nothing but
 * a click.
 *
 * NO MOUNT, UNLIKE KNOW/REPOS. There is no second PAGE here: the detail is a
 * dialog over the same grid, so `id` is read straight off `LeafViewProps`
 * (which the router has always resolved and this page always discarded) and
 * the dialog's open state follows the address instead of the other way round.
 * `mounts/know-repos.tsx` exists because REPOS really has two page modules.
 *
 * NO `LEGACY_PREFIXES` ENTRY EITHER, and that is a measurement rather than an
 * omission: `/services/{name}` was never a live URL, because the detail was
 * never addressable. There is no bookmark to keep working.
 */
export function ServicesPage({ id }: Partial<LeafViewProps> = {}) {
  const queryClient = useQueryClient();
  const navigate = useNavigate();
  /**
   * `navigate` IS ABSENT IN NOTHING, but `id` is absent outside the shell
   * (`?embed=1`, a unit test), so the local fallback keeps the detail openable
   * there — the same reason `pane-active` defaults to `true`.
   */
  const [localSelected, setLocalSelected] = useState<string | null>(null);
  const selected = id ?? localSelected;
  const open = (name: string) => {
    setLocalSelected(name);
    navigate(`/system/services/${encodeURIComponent(name)}`);
  };
  const close = () => {
    setLocalSelected(null);
    navigate("/system/services");
  };

  const listQuery = useQuery({
    queryKey: ["services", "list"],
    queryFn: servicesApi.list,
    // L4 / T9 — Law 3 keeps all five topic panes mounted, so an unguarded 5s
    // interval is the heaviest single poll in SYSTEM while nobody is looking
    // at it. Measured before this line.
    refetchInterval: usePaneInterval(5_000),
  });

  const invalidate = () => {
    queryClient.invalidateQueries({ queryKey: ["services", "list"] });
    if (selected) {
      queryClient.invalidateQueries({ queryKey: ["services", "detail", selected] });
    }
  };

  const installMut = useMutation({
    mutationFn: (name: string) => servicesApi.install(name),
    onSuccess: (_row, name) => {
      toast.success(`Installed ${name}`);
      invalidate();
    },
    onError: (err) => {
      toast.error(err instanceof Error ? err.message : "Install failed");
    },
  });

  const data = listQuery.data;
  const services = data?.services ?? [];
  const notInstalled = services.filter((s) => !s.installed);

  /* REFRESH IS THE LEAF'S ONE CHROME ACTION and it belongs on the plate. */
  const header = useMemo(
    () => ({
      actions: (
        <Button
          size="sm"
          variant="outline"
          onClick={() => listQuery.refetch()}
          disabled={listQuery.isRefetching}
        >
          {listQuery.isRefetching ? (
            <Loader2 className="mr-1.5 animate-spin" />
          ) : (
            <RefreshCw className="mr-1.5" />
          )}
          Refresh
        </Button>
      ),
    }),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [listQuery.isRefetching, listQuery.refetch],
  );
  useSectionTitle(header);

  return (
    <div className="page-shell space-y-8">
      {/* R1 (86b8f1f0) — THE LEAF TITLE IS THE SHELL'S. `TopicBar` renders
          `<h1 class="c-title">Services</h1>` above this pane, so the page's
          own PageHeader h1 is gone.

          WHAT MOVED AND WHY IT COULD NOT JUST BE DELETED. `PageHeader` carried
          a REAL control in its `right` slot — Refresh, the leaf's only chrome
          action — so the header becomes the first CONTENT block: the sentence
          that says what these services are keeps its job as the block's lead,
          and Refresh sits at the trailing edge of the same row, where the
          header put it. The same shape MODELS' pass landed.

          `text-fg-muted` rather than `PageHeader`'s `text-tertiary`: that token
          is the standing AA failure (kit todo c581c9b2). */}
      {/* REFRESH IS ON THE PLATE NOW, so the row that fought to keep it from
          wrapping is gone with it: there is no button beside the sentence to
          push onto a second line. The sentence stays as the block's lead. */}
      <p className="type-small text-fg-muted">
        okuro background services — start, stop, restart, and run on boot.
      </p>

      {/* Install banner — shown when any registry service has no unit file yet */}
      {notInstalled.length > 0 && (
        <InstallBanner
          rows={notInstalled}
          onInstall={(name) => installMut.mutate(name)}
          pending={installMut.isPending}
          pendingName={installMut.variables ?? null}
        />
      )}

      {listQuery.isLoading ? (
        <div className="text-sm text-tertiary">Loading services…</div>
      ) : listQuery.isError ? (
        <EmptyState
          title="Failed to load services"
          description={
            listQuery.error instanceof Error
              ? listQuery.error.message
              : "Unknown error"
          }
        />
      ) : services.length === 0 ? (
        <EmptyState
          title="No services registered"
          description="The built-in registry is empty — this should never happen."
        />
      ) : (
        /* R3 (8546865f) — THE COLUMN COUNT ASKS THE PANE, NOT THE WINDOW.
           `md:` is 384px and `lg:` is 512px under the engine's 8px root, and
           `matchMedia` answers for the WINDOW, so both were TRUE in every state
           the shell can produce: three columns whether the pane was 751.63px or
           1537.63px. MEASURED at window 1366 before this line:

             pane                     751.63
             grid-template-columns    241.344px x3  (+ 2 x 13.8 gap = 751.6, fits)
             pane scrollWidth         787   vs clientWidth 752  -> 35px CLIPPED
             per card                 scrollWidth 275 vs clientWidth 239
             what overflowed          the Restart button, 35px past the card edge

           SO THE p3 SPEC'S CAUSE IS ONE LEVEL OFF AND ITS NUMBER IS STALE. It
           recorded "766 in 754, 12px" and blamed the track arithmetic. The
           tracks FIT; what does not fit is the card's own action row — Start
           and Stop are `flex-1` with an icon and a label each, plus the
           `shrink-0` Restart button, and their combined min-content width is
           275px in a 241px track. A `minmax(0, 1fr)` track lets a child
           overflow, so nothing clipped at the grid: `.c-panes overflow-x: clip`
           cut it 35px later.

           THE RUNGS ARE ARITHMETIC, not taste. A card needs 275px measured, so
           two columns need 275 x 2 + 13.8 = 564 and three need
           275 x 3 + 27.6 = 853. `@2xl` is 672px and `@4xl` is 896px under the
           8px root (globals.css:403-415, the Tailwind scale x2), so both rungs
           clear their requirement with room, and they flip against the named
           `pane` container (shell.css:963). */
        <div className="grid grid-cols-1 @2xl:grid-cols-2 @4xl:grid-cols-3 gap-3">
          {services.map((row) => (
            <ServiceCard
              key={row.name}
              row={row}
              onOpenDetail={() => open(row.name)}
              onActed={invalidate}
            />
          ))}
        </div>
      )}

      {selected && (
        <DetailDialog name={selected} open onClose={close} />
      )}
    </div>
  );
}

// ── Install banner ───────────────────────────────────────────────────

function InstallBanner({
  rows,
  onInstall,
  pending,
  pendingName,
}: {
  rows: ServiceRow[];
  onInstall: (name: string) => void;
  pending: boolean;
  pendingName: string | null;
}) {
  return (
    /* D1 — kit names, and each one measured as a swatch over the real page
       ground (isolated chromium, kit `standard`, both appearances, PNG pixels
       compared). `bg-accent/5` and `bg-surface-subtle` are the SAME pixel in
       dark (26,26,26) and 6 values apart in light. `border-accent/50` has no
       engine name at 50 %: `border-accent` is 156,156,156 against the
       modifier's 87,87,87, `border-border` is 66,66,66 — so the banner keeps
       its emphasis from the fill and the accent icon and takes the border
       token, which is what every other bordered block on this page uses. */
    <div className="rounded border border-border bg-surface-subtle p-3 space-y-2">
      <div className="flex items-start gap-2">
        <Download className="h-4 w-4 text-accent mt-0.5 shrink-0" />
        <div className="text-xs text-fg">
          <div className="font-medium">
            {rows.length === 1
              ? "1 service not installed"
              : `${rows.length} services not installed`}
          </div>
          <p className="mt-0.5 text-tertiary">
            These services have specs but no unit file yet. Install before
            start/stop will work.
          </p>
        </div>
      </div>
      <div className="flex flex-wrap gap-2 pl-6">
        {rows.map((r) => (
          <Button
            key={r.name}
            size="sm"
            variant="outline"
            onClick={() => onInstall(r.name)}
            disabled={pending && pendingName === r.name}
          >
            {pending && pendingName === r.name ? (
              <Loader2 className="h-3.5 w-3.5 mr-1.5 animate-spin" />
            ) : (
              <Download className="h-3.5 w-3.5 mr-1.5" />
            )}
            Install {r.name}
          </Button>
        ))}
      </div>
    </div>
  );
}

// ── Service card ─────────────────────────────────────────────────────

function ServiceCard({
  row,
  onOpenDetail,
  onActed,
}: {
  row: ServiceRow;
  onOpenDetail: () => void;
  onActed: () => void;
}) {
  const pill = statePill(row.state, row.active);

  /**
   * R5 (372ccdb2), REVERSIBLE BRANCH — A TWO-STEP ARM, PER ACTION.
   *
   * Six clicks on this card change system state and none of them asked. Three
   * of the six INTERRUPT a running service, and the three services on screen
   * are okuro's own orchestrator, embed and daemon — so one click could stop
   * the application the user is looking at. R5 sends a recoverable act to the
   * arm rather than to a modal: Start and Enable bring a service BACK, so
   * slowing them down would make the fix slower than the break.
   *
   *   Stop     armed — interrupts a running service
   *   Restart  armed — interrupts a running service
   *   Disable  armed — changes what runs at boot
   *   Start    not armed — this is the recovery action
   *   Enable   not armed — this is the recovery action
   *   Install  not armed — writes a unit file; nothing is interrupted
   *
   * ARMED PER ACTION, WHICH THE TASKS PATTERN GETS WRONG (`tasks.tsx:188`
   * tests a single `confirming` flag, so arming one action fires the other on
   * its first click). Keyed here, so arming Restart while Stop is armed
   * re-arms rather than firing.
   */
  type Armable = "stop" | "restart" | "disable";
  const [armed, setArmed] = useState<Armable | null>(null);

  /**
   * SIX BARE `await` CALLS BECAME SIX MUTATIONS — Q5, ruled C ("convert only
   * if the confirms land, since both changes touch the same call sites").
   * They did, so this is paid for once. What it buys, beyond one pending
   * mechanism: the errors now reach the same place as the rest of the app
   * instead of a local `catch`, and `isPending` is react-query's rather than a
   * hand-rolled `busy` string that had to be reset in a `finally`.
   */
  const lifecycle = useMutation({
    mutationFn: ({ which }: { which: "start" | "stop" | "restart" }) =>
      which === "start"
        ? servicesApi.start(row.name)
        : which === "stop"
          ? servicesApi.stop(row.name)
          : servicesApi.restart(row.name),
    onSuccess: (_r, { which }) => {
      toast.success(`${which} ${row.name}`);
      setArmed(null);
      onActed();
    },
    onError: (err, { which }) =>
      toast.error(err instanceof Error ? err.message : `${which} failed`),
  });

  const bootToggle = useMutation({
    mutationFn: ({ enable }: { enable: boolean }) =>
      enable ? servicesApi.enable(row.name) : servicesApi.disable(row.name),
    onSuccess: (_r, { enable }) => {
      toast.success(
        enable ? `Enabled ${row.name} on boot` : `Disabled ${row.name} on boot`,
      );
      setArmed(null);
      onActed();
    },
    onError: (err, { enable }) =>
      toast.error(
        err instanceof Error ? err.message : `${enable ? "enable" : "disable"} failed`,
      ),
  });

  const busy = lifecycle.isPending ? lifecycle.variables.which : null;
  const enableBusy = bootToggle.isPending;

  /** Arm on the first click, act on the second. Start needs no arm. */
  const request = (which: "start" | "stop" | "restart") => {
    if (which !== "start" && armed !== which) {
      setArmed(which);
      return;
    }
    setArmed(null);
    lifecycle.mutate({ which });
  };

  const requestBoot = () => {
    const enable = !row.enabled;
    if (!enable && armed !== "disable") {
      setArmed("disable");
      return;
    }
    setArmed(null);
    bootToggle.mutate({ enable });
  };

  const canStart = row.installed && !row.active && busy === null;
  const canStop = row.installed && row.active && busy === null;
  const canRestart = row.installed && busy === null;

  return (
    <div className="rounded border border-border bg-surface p-3 flex flex-col gap-3">
      {/* Header: name + state */}
      <button
        onClick={onOpenDetail}
        className="text-left space-y-1 focus:outline-none"
      >
        <div className="flex items-center justify-between gap-2">
          <div className="font-mono text-sm font-medium text-fg truncate">
            {row.name}
          </div>
          <StatePill variant={pill} label={row.state} />
        </div>
        <div className="text-2xs text-tertiary line-clamp-2">
          {row.description}
        </div>
      </button>

      {/* Metrics row */}
      <div className="grid grid-cols-3 gap-2 text-2xs">
        <Metric label="Uptime" value={formatUptime(row.uptime_seconds)} />
        <Metric label="Memory" value={formatMemory(row.memory_bytes)} />
        <Metric
          label="Restarts"
          value={row.restart_count === null ? "—" : String(row.restart_count)}
        />
      </div>

      {/* Install warning */}
      {!row.installed && (
        /* `bg-warning-subtle` is the kit's name for this fill. Measured
           delta, dark 37,21,32 -> 55,23,46: the tint gets stronger, because
           the engine mixes its status tints at 25 % where the literal took
           10 %. */
        <div className="text-2xs text-warning rounded bg-warning-subtle px-2 py-1">
          Not installed — use the install banner above.
        </div>
      )}

      {/* Actions */}
      <div className="flex items-center gap-1.5">
        <Button
          size="sm"
          variant="outline"
          onClick={() => request("start")}
          disabled={!canStart}
          className="flex-1"
        >
          {busy === "start" ? (
            <Loader2 className="h-3.5 w-3.5 animate-spin" />
          ) : (
            <Play className="h-3.5 w-3.5" />
          )}
          <span className="ml-1.5">Start</span>
        </Button>
        <Button
          size="sm"
          variant="outline"
          onClick={() => request("stop")}
          disabled={!canStop}
          className={cn("flex-1", armed === "stop" && "border-error text-error")}
          aria-label={armed === "stop" ? `Confirm stop ${row.name}` : `Stop ${row.name}`}
        >
          {busy === "stop" ? (
            <Loader2 className="h-3.5 w-3.5 animate-spin" />
          ) : (
            <Square className="h-3.5 w-3.5" />
          )}
          <span className="ml-1.5">{armed === "stop" ? "Confirm" : "Stop"}</span>
        </Button>
        <Button
          size="sm"
          variant="outline"
          onClick={() => request("restart")}
          disabled={!canRestart}
          title={armed === "restart" ? "Click again to restart" : "Restart"}
          aria-label={
            armed === "restart" ? `Confirm restart ${row.name}` : `Restart ${row.name}`
          }
          className={cn(
            "shrink-0 px-2",
            armed === "restart" && "border-error text-error",
          )}
        >
          {busy === "restart" ? (
            <Loader2 className="h-3.5 w-3.5 animate-spin" />
          ) : (
            <RotateCw className="h-3.5 w-3.5" />
          )}
          {/* The armed state needs a WORD, not only a tint: this is the one
              icon-only control in the row, and a colour change alone is not a
              signal a colourblind reader can act on. */}
          {armed === "restart" && <span className="ml-1.5">Confirm</span>}
        </Button>
      </div>

      {/* Enable/Disable toggle */}
      <div className="flex items-center justify-between text-2xs">
        <div className="text-tertiary">
          Boot:{" "}
          <span
            className={cn(
              row.enabled === true
                ? "text-success"
                : row.enabled === false
                  ? "text-tertiary"
                  : "text-tertiary",
            )}
          >
            {row.enabled === null
              ? "unknown"
              : row.enabled
                ? "enabled"
                : "disabled"}
          </span>
        </div>
        <button
          onClick={requestBoot}
          disabled={!row.installed || enableBusy || row.enabled === null}
          className={cn(
            /* `case-label`, not a literal — 0d37d05e. */
            "case-label inline-flex items-center gap-1 text-2xs tracking-wider transition-colors",
            row.installed && !enableBusy
              ? armed === "disable"
                ? "text-error"
                : "text-tertiary hover:text-fg-muted"
              /* `text-fg-disabled` is the engine's name for a disabled ink
                 tier — ruled 6ba4d789, which retired opacity-as-a-tier. Dark
                 113,113,113 and light 161,161,161, both from the kit. */
              : "text-fg-disabled cursor-not-allowed",
          )}
        >
          {enableBusy ? (
            <Loader2 className="h-3 w-3 animate-spin" />
          ) : row.enabled ? (
            <PowerOff className="h-3 w-3" />
          ) : (
            <Power className="h-3 w-3" />
          )}
          {armed === "disable" ? "Confirm" : row.enabled ? "Disable" : "Enable"}
        </button>
      </div>
    </div>
  );
}

function Metric({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <div className="case-label tracking-wider text-tertiary">{label}</div>
      <div className="text-fg font-medium tabular-nums">{value}</div>
    </div>
  );
}

function StatePill({
  variant,
  label,
}: {
  variant: PillVariant;
  label: string;
}) {
  /* NINE ALPHA MODIFIERS GONE, AND EVERY REPLACEMENT WAS MEASURED, NOT
     GUESSED. Each pair was painted over the real page ground and the PNG
     pixels compared, in both appearances:

       before             after                dark              light
       bg-success/15  ->  bg-success-subtle    19,48,22 -> 19,58,23
       bg-error/15    ->  bg-error-subtle      53,23,34 -> 65,25,39
       bg-warning/15  ->  bg-warning-subtle    45,22,39 -> 55,23,46
       the three 30 % borders -> border-border   see below

     THE FILLS GET SLIGHTLY STRONGER because the engine mixes its status tints
     at 25 % where these literals took 15 %. THE BORDERS LOSE THEIR HUE, and
     that is the trade this file's own doctrine already accepts: there is no
     engine name at 30 % of a status colour (success at 30 % composites to
     18,77,24 against `border-border`'s 66,66,66), and `ui/status-badge`'s
     rule is that
     a tone is carried by the label and the fill, NEVER by colour alone. The
     fill and the text still carry it; only the 1px rule is now neutral.

     THE REAL FIX IS ONE LEVEL UP AND IT IS A QUESTION, NOT A COMMIT: this
     component duplicates `ui/status-badge` (10 importers), which resolves
     tones from `--color-status-*` without any arithmetic — and this page
     imports BOTH. Deleting `StatePill` would remove the arithmetic at its
     source, and it would also change how a state reads on two surfaces, which
     is the owner's call. */
  const cls =
    variant === "success"
      ? "bg-success-subtle text-success border-border"
      : variant === "error"
        ? "bg-error-subtle text-error border-border"
        : variant === "warning"
          ? "bg-warning-subtle text-warning border-border"
          : "bg-surface border-border text-tertiary";
  return (
    <span
      className={cn(
        "case-label inline-flex items-center rounded border px-1.5 py-0.5 text-2xs font-medium tracking-wider shrink-0",
        cls,
      )}
    >
      {label}
    </span>
  );
}

// ── Detail drawer ────────────────────────────────────────────────────

function DetailDialog({
  name,
  open,
  onClose,
}: {
  name: string;
  open: boolean;
  onClose: () => void;
}) {
  const detailQuery = useQuery({
    queryKey: ["services", "detail", name],
    queryFn: () => servicesApi.get(name),
    enabled: open,
    // A CONDITIONAL INTERVAL IS STILL UNGATED — the trap memory 48c3c5c6
    // recorded on MODELS. `open ? 3000 : false` can be TRUE off-screen: Law 3
    // keeps the pane mounted through a topic change, and a dialog that was
    // open when the user left stayed open and kept polling every 3s.
    // (`usePaneModalOpen` in `ui/dialog` now closes it, but the query lives
    // here and the guard belongs on the query.)
    refetchInterval: usePaneInterval(open ? 3_000 : false),
  });

  // Refetch immediately when re-opened.
  useEffect(() => {
    if (open) {
      detailQuery.refetch();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  const detail = detailQuery.data;
  const row = detail?.service;

  return (
    <Dialog open={open} onOpenChange={(v) => !v && onClose()}>
      <DialogContent className="max-w-3xl max-h-[85vh] overflow-auto">
        <DialogHeader>
          <div className="flex items-start justify-between gap-2">
            <div className="min-w-0">
              <DialogTitle className="font-mono text-base">{name}</DialogTitle>
              {/*
                THE DESCRIPTION IS UNCONDITIONAL, and the conditional version
                is what put a warning in all 24 contract states:

                  Missing `Description` or `aria-describedby={undefined}` for
                  {DialogContent}

                `row` comes from the detail query, so on the mount frame it is
                undefined — and the mount frame is exactly when Radix looks for
                the description id. The dialog therefore opened every single
                time without one, which is a real screen-reader gap and not
                only a console line. Rendering the element always and switching
                its CONTENT keeps the id present from the first frame.
              */}
              <DialogDescription className="mt-1 flex items-center gap-2">
                {row ? (
                  <>
                    <StatePill
                      variant={statePill(row.state, row.active)}
                      label={row.state}
                    />
                    <span className="text-2xs text-tertiary">
                      {row.backend} · uptime {formatUptime(row.uptime_seconds)}
                      {row.pid !== null && ` · pid ${row.pid}`}
                    </span>
                  </>
                ) : (
                  <span className="text-2xs text-tertiary">
                    Reading the unit's state…
                  </span>
                )}
              </DialogDescription>
            </div>
            <button
              onClick={onClose}
              className="text-tertiary hover:text-fg-muted"
              aria-label="Close"
            >
              <X className="h-4 w-4" />
            </button>
          </div>
        </DialogHeader>

        {detailQuery.isLoading ? (
          <div className="text-sm text-tertiary">Loading…</div>
        ) : detailQuery.isError ? (
          <div className="text-xs text-error">
            {detailQuery.error instanceof Error
              ? detailQuery.error.message
              : "Load failed"}
          </div>
        ) : detail ? (
          <div className="space-y-4">
            {/* Spec block */}
            <div className="space-y-2">
              <div className="case-label text-2xs tracking-wider text-tertiary">
                Spec
              </div>
              <div className="rounded border border-border bg-surface p-2 space-y-1">
                <div className="text-2xs">
                  <span className="text-tertiary">ExecStart:</span>{" "}
                  <span className="font-mono text-fg break-all">
                    {detail.exec_start.join(" ")}
                  </span>
                </div>
                {detail.working_directory && (
                  <div className="text-2xs">
                    <span className="text-tertiary">WorkingDirectory:</span>{" "}
                    <span className="font-mono text-fg break-all">
                      {detail.working_directory}
                    </span>
                  </div>
                )}
                {Object.keys(detail.environment).length > 0 && (
                  <div className="text-2xs">
                    <span className="text-tertiary">Environment:</span>
                    <ul className="mt-0.5 pl-3 space-y-0.5">
                      {Object.entries(detail.environment).map(([k, v]) => (
                        <li key={k} className="font-mono text-fg">
                          {k}={v}
                        </li>
                      ))}
                    </ul>
                  </div>
                )}
                {row && (
                  <div className="text-2xs text-tertiary pt-1 flex flex-wrap gap-x-4">
                    <span>Memory: {formatMemory(row.memory_bytes)}</span>
                    <span>
                      Restarts:{" "}
                      {row.restart_count === null ? "—" : row.restart_count}
                    </span>
                    <span>
                      Boot:{" "}
                      {row.enabled === null
                        ? "unknown"
                        : row.enabled
                          ? "enabled"
                          : "disabled"}
                    </span>
                  </div>
                )}
              </div>
            </div>

            {/* Logs */}
            <div className="space-y-2">
              <div className="flex items-center justify-between">
                <div className="case-label text-2xs tracking-wider text-tertiary">
                  Last {detail.logs.length} log lines
                  {/* Same ruling: the quietest tier has a name. */}
                  <span className="ml-1.5 text-fg-disabled">(auto-refresh 3s)</span>
                </div>
                <Badge variant="outline" className="text-2xs">
                  journalctl --user
                </Badge>
              </div>
              <div className="rounded border border-border bg-surface max-h-96 overflow-auto">
                {detail.logs.length === 0 ? (
                  <div className="p-3 text-2xs text-tertiary">
                    No log lines — service may not have run yet, or journalctl
                    is unavailable.
                  </div>
                ) : (
                  <pre className="p-3 text-2xs font-mono text-fg-muted whitespace-pre-wrap break-words leading-relaxed">
                    {detail.logs.join("\n")}
                  </pre>
                )}
              </div>
            </div>
          </div>
        ) : null}
      </DialogContent>
    </Dialog>
  );
}
