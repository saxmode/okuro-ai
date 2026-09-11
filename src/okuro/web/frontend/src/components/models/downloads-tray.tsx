/**
 * Downloads tray — a fixed, app-wide stack of model-pull cards with live
 * progress. Driven entirely by the server-backed downloads store, so it shows
 * the same in-flight pulls on any page and after a refresh.
 */
import { useEffect } from "react";
import { AlertCircle, Check, Loader2, X } from "lucide-react";
import { useDownloads } from "@/lib/downloads-context";
import { Progress } from "@/components/ui/progress";
import { Button } from "@/components/ui/button";
import { formatBytes } from "@/lib/utils";
import type { PullStatus } from "@/lib/models-api";

export function DownloadsTray() {
  const { jobs, dismiss } = useDownloads();
  if (jobs.length === 0) return null;
  return (
    <div className="fixed bottom-4 right-4 z-50 flex w-80 max-w-[calc(100vw-2rem)] flex-col gap-2">
      {jobs.map((job) => (
        <DownloadCard
          key={job.catalog_id}
          job={job}
          onDismiss={() => job.catalog_id && dismiss(job.catalog_id)}
        />
      ))}
    </div>
  );
}

function DownloadCard({
  job,
  onDismiss,
}: {
  job: PullStatus;
  onDismiss: () => void;
}) {
  const done = job.state === "done";
  const error = job.state === "error";

  // Auto-dismiss a completed card after a beat so the stack self-clears.
  useEffect(() => {
    if (!done) return;
    const t = setTimeout(onDismiss, 12_000);
    return () => clearTimeout(t);
  }, [done, onDismiss]);

  const pct = Math.round((job.progress ?? 0) * 100);
  const known = (job.total_bytes ?? 0) > 0;

  return (
    <div className="rounded-lg border border-border-subtle bg-surface-elevated p-3 shadow-lg">
      <div className="flex items-start gap-2">
        <div className="mt-0.5 shrink-0">
          {done ? (
            <Check className="h-4 w-4 text-success" aria-hidden="true" />
          ) : error ? (
            <AlertCircle className="h-4 w-4 text-error" aria-hidden="true" />
          ) : (
            <Loader2 className="h-4 w-4 animate-spin text-fg-muted" aria-hidden="true" />
          )}
        </div>
        <div className="min-w-0 flex-1">
          <div className="truncate text-sm font-medium text-fg">
            {job.display_name || job.catalog_id}
          </div>
          <div className="mt-0.5 text-3xs text-fg-subtle">
            {done
              ? "Installed"
              : error
                ? (job.error ?? "Failed")
                : known
                  ? `${formatBytes(job.downloaded_bytes)} / ${formatBytes(job.total_bytes)} · ${pct}%`
                  : "Preparing…"}
          </div>
        </div>
        {(done || error) && (
          <Button
            size="sm"
            variant="ghost"
            className="-mr-1 -mt-1 h-6 w-6 shrink-0 p-0"
            onClick={onDismiss}
            aria-label="Dismiss"
          >
            <X className="h-3 w-3" aria-hidden="true" />
          </Button>
        )}
      </div>
      {!error && (
        <Progress
          value={done ? 100 : pct}
          className={`mt-2 h-1.5 ${!known && !done ? "animate-pulse" : ""}`}
        />
      )}
    </div>
  );
}
