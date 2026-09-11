/**
 * "See result" / build button for finished tasks.
 *
 * One self-contained component — handles the full preview lifecycle:
 * status polling, start/stop, log drawer, and opening the live URL.
 * Rendered full-width so it fills its container (typically the bottom
 * of the task-detail right panel).
 *
 * Renders nothing (returns null) when status hasn't loaded yet. All
 * lifecycle states (idle/building/starting/ready/failed/stopped) share
 * the same big primary button — copy and icon swap to match state.
 */

import { useEffect, useRef, useState } from "react";
import { Bot, ChevronDown, ChevronUp, ExternalLink, Loader2, Play, RotateCw, Square } from "lucide-react";
import { Button } from "@/components/ui/button";
import { toast } from "@/components/ui/toast";
import { ApiError, previewApi } from "@/lib/api";
import {
  usePreviewLogs,
  usePreviewStatus,
  useStartPreview,
  useStopPreview,
} from "@/hooks/use-preview";
import type { PreviewState } from "@/types/api";

/**
 * Extract a useful message from a start() error. The backend returns a
 * structured 409 body when auto-detect can't infer a recipe — we surface
 * the recognised template list so the user knows what shapes work.
 */
function describeStartError(err: unknown): string {
  if (err instanceof ApiError) {
    const detail = (err.details as { detail?: unknown } | undefined)?.detail;
    if (detail && typeof detail === "object" && "error" in detail) {
      const d = detail as {
        error: string;
        message?: string;
        known_templates?: string[];
        /** P5.6 — openable things the scan found but no detector claimed. */
        candidates?: { source: string; label: string; path?: string; id?: string; kind?: string }[];
      };
      if (d.error === "auto_detect_miss") {
        // P5.6 — say what was scanned and what was found, in the reader's
        // language. The old copy ended at "add a preview.yaml in the task dir
        // to override", which answers a request to SEE the result with an
        // instruction to write a config file. The candidate list is the
        // actionable part; PreviewCandidates renders it as a chooser.
        const found = d.candidates?.length ?? 0;
        if (found > 0) {
          return `Nothing here matched a known preview shape, but ${found} file${
            found === 1 ? "" : "s"
          } turned up — pick the one you meant.`;
        }
        return "Nothing to preview yet: this task has not produced a file or an artifact.";
      }
      if (d.message) return d.message;
    }
  }
  return err instanceof Error ? err.message : "Failed to start preview";
}

interface PreviewButtonProps {
  taskId: string;
  /**
   * Optional handler invoked from the failed-state "Fix with agent"
   * button. The component passes a ready-to-go continuation prompt that
   * embeds the recipe error and journal tail; the parent decides whether
   * to fire `taskApi.continue` (or any other agent dispatch flow).
   * When omitted, the failed state shows only the Retry button.
   */
  onFixWithAgent?: (continuationPrompt: string) => void;
}

const ACTIVE: Array<PreviewState["state"]> = ["building", "starting"];

export function PreviewButton({ taskId, onFixWithAgent }: PreviewButtonProps) {
  const { data: status } = usePreviewStatus(taskId);
  const startMut = useStartPreview(taskId);
  const stopMut = useStopPreview(taskId);

  const [drawerOpen, setDrawerOpen] = useState(false);
  const wasActiveRef = useRef(false);

  // Auto-open the log drawer when a build starts; auto-close on idle.
  useEffect(() => {
    if (!status) return;
    const active = ACTIVE.includes(status.state);
    if (active && !wasActiveRef.current) setDrawerOpen(true);
    wasActiveRef.current = active;
  }, [status]);

  if (!status) return null;
  // Auto-detect runs server-side on click, so show the button even when no
  // recipe exists yet — the user explicitly asked never to write recipes
  // by hand. A detection miss surfaces as a toast (see describeStartError).

  const handleStart = async (force = false) => {
    try {
      await startMut.mutateAsync({ force_rebuild: force });
    } catch (err) {
      toast.error(describeStartError(err));
    }
  };

  const handleStop = async () => {
    try {
      await stopMut.mutateAsync();
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "Failed to stop preview");
    }
  };

  const handleOpen = async () => {
    if (!status.url) return;
    try {
      await previewApi.openResult(status.url);
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "Failed to open result");
    }
  };

  /**
   * Build a continuation prompt that embeds the recipe error + recent
   * journal output, then hand it to the parent. The parent owns the
   * agent dispatch so we don't double-import taskApi here.
   */
  const handleFix = async () => {
    if (!onFixWithAgent) return;
    let logs: string[] = [];
    try {
      const res = await previewApi.logs(taskId, 80);
      logs = res.lines.slice(-60);
    } catch {
      // best effort — the agent can pull logs itself if the prompt has none.
    }
    const parts = [
      `The "See result" preview build for this task failed.`,
      ``,
      `last_error: ${status.last_error ?? "(none reported)"}`,
    ];
    if (logs.length) {
      parts.push(``, `Last journal lines:`, "```", ...logs, "```");
    }
    parts.push(
      ``,
      `Fix the preview build so it succeeds. Inspect preview.yaml, the cwd,`,
      `the build/serve commands, and the journal. After your fix the user`,
      `will click "Build & see result" to verify.`,
    );
    onFixWithAgent(parts.join("\n"));
  };

  const showLogDrawer =
    ACTIVE.includes(status.state) ||
    status.state === "ready" ||
    status.state === "failed";

  return (
    <div className="flex w-full flex-col gap-2">
      {status.state === "ready" && (
        <Button
          variant="default"
          onClick={handleOpen}
          className="w-full justify-center gap-2 py-3 text-sm font-semibold"
          title={status.url ?? undefined}
        >
          <ExternalLink className="h-4 w-4" />
          See result
          {status.port != null && (
            <span className="ml-1 rounded bg-surface-elevated/40 px-1.5 py-0.5 text-2xs font-normal text-fg-muted">
              :{status.port}
            </span>
          )}
        </Button>
      )}

      {(status.state === "idle" || status.state === "stopped") && (
        <Button
          variant="default"
          onClick={() => handleStart(false)}
          disabled={startMut.isPending}
          className="w-full justify-center gap-2 py-3 text-sm font-semibold"
        >
          <Play className="h-4 w-4" />
          {startMut.isPending ? "Starting…" : "Build & see result"}
        </Button>
      )}

      {ACTIVE.includes(status.state) && (
        <Button
          variant="outline"
          disabled
          className="w-full justify-center gap-2 py-3 text-sm font-semibold"
        >
          <Loader2 className="h-4 w-4 animate-spin" />
          {status.state === "building" ? "Building…" : "Starting…"}
        </Button>
      )}

      {status.state === "failed" && (
        <>
          {onFixWithAgent && (
            <Button
              variant="default"
              onClick={handleFix}
              className="w-full justify-center gap-2 py-3 text-sm font-semibold"
              title="Spawn an agent to investigate the failure and fix the preview build"
            >
              <Bot className="h-4 w-4" />
              Fix with agent
            </Button>
          )}
          <Button
            variant="outline"
            onClick={() => handleStart(true)}
            disabled={startMut.isPending}
            className={`w-full justify-center gap-2 ${
              onFixWithAgent
                ? "py-2 text-2xs"
                : "border-error py-3 text-sm font-semibold text-error"
            }`}
            title={status.last_error ?? "Preview failed"}
          >
            <RotateCw className="h-3 w-3" />
            {onFixWithAgent ? "Retry build manually" : "Retry — last run failed"}
          </Button>
        </>
      )}

      {/* Secondary action: rebuild + rediscover. The recipe is regenerated
          server-side on every build, so this re-scans the task's artifacts
          and opens the NEWEST deliverable — the fix for an extended task
          still showing its first run's result. */}
      {status.state === "ready" && (
        <Button
          variant="outline"
          size="sm"
          onClick={() => handleStart(true)}
          disabled={startMut.isPending}
          className="w-full justify-center gap-1 text-2xs"
          title="Re-scan this task's outputs and rebuild the preview from the newest result"
        >
          <RotateCw className="h-3 w-3" />
          {startMut.isPending ? "Rebuilding…" : "Rebuild result"}
        </Button>
      )}

      {/* Stop a live preview. Only rendered when there's a server to stop. */}
      {status.state === "ready" && status.unit && (
        <Button
          variant="outline"
          size="sm"
          onClick={handleStop}
          disabled={stopMut.isPending}
          className="w-full justify-center gap-1 text-2xs"
          title="Stop preview server"
        >
          <Square className="h-3 w-3" />
          Stop preview server
        </Button>
      )}

      {showLogDrawer && (
        <PreviewLogDrawer
          taskId={taskId}
          open={drawerOpen}
          onToggle={() => setDrawerOpen((v) => !v)}
          tailing={ACTIVE.includes(status.state)}
        />
      )}
    </div>
  );
}

// ── Log drawer ──────────────────────────────────────────────────────

interface PreviewLogDrawerProps {
  taskId: string;
  open: boolean;
  onToggle: () => void;
  tailing: boolean;
}

function PreviewLogDrawer({ taskId, open, onToggle, tailing }: PreviewLogDrawerProps) {
  const { data, connected } = usePreviewLogs(taskId, { enabled: open, lines: 500 });
  const scrollRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (open && scrollRef.current) {
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
    }
  }, [data.lines.length, open]);

  // Empty-state copy depends on the SSE connection: while we're
  // negotiating the stream the user should see "Connecting…", not a
  // misleading "No log output yet." that the previous polled-JSON
  // implementation showed for kind=doc/image (which never hit the
  // journal) regardless of whether logging was actually working.
  const emptyMessage = !connected
    ? "Connecting to log stream…"
    : tailing
      ? "Waiting for first build line…"
      : "No log output yet.";

  return (
    <div className="w-full rounded-md border border-border bg-surface text-xs">
      <button
        type="button"
        onClick={onToggle}
        aria-expanded={open}
        aria-label={open ? "Hide preview logs" : "Show preview logs"}
        className="flex w-full items-center justify-between px-2 py-1 hover:bg-surface-elevated/40"
      >
        <span className="flex items-center gap-2">
          {open ? (
            <ChevronDown className="h-3 w-3 text-tertiary" />
          ) : (
            <ChevronUp className="h-3 w-3 text-tertiary" />
          )}
          <span className="text-2xs uppercase tracking-wider text-tertiary">
            Preview log
          </span>
          {open && (
            <span
              className={`h-1.5 w-1.5 rounded-full ${
                connected
                  ? tailing
                    ? "animate-pulse bg-success"
                    : "bg-success"
                  : "bg-tertiary"
              }`}
              title={connected ? "Live" : "Disconnected"}
            />
          )}
        </span>
        {data.lines.length > 0 && (
          <span className="text-3xs text-tertiary">{data.lines.length} lines</span>
        )}
      </button>
      {open && (
        <div
          ref={scrollRef}
          className="max-h-64 overflow-y-auto border-t border-border p-2 font-mono text-3xs leading-snug"
        >
          {data.lines.length === 0 ? (
            <p className="text-center text-tertiary">{emptyMessage}</p>
          ) : (
            data.lines.map((line, i) => (
              <div key={i} className="whitespace-pre-wrap text-fg-muted">
                {line}
              </div>
            ))
          )}
        </div>
      )}
    </div>
  );
}
