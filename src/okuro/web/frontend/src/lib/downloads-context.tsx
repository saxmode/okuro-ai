/**
 * Downloads store — one source of truth for model pulls, app-wide.
 *
 * Pull state lives SERVER-side (okuro _PULL_JOBS), so this store rehydrates
 * from /api/models/pull/active on mount and polls while anything is
 * downloading. That is what makes a pull survive a page refresh or a
 * navigation away from /models: the tray reads the server, not React memory.
 */
import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { useQueryClient } from "@tanstack/react-query";
import { modelsApi, type PullStatus } from "@/lib/models-api";

type DownloadsApi = {
  jobs: PullStatus[];
  startPull: (catalogId: string, displayName?: string, force?: boolean) => Promise<void>;
  dismiss: (catalogId: string) => void;
  jobFor: (catalogId: string) => PullStatus | undefined;
};

const DownloadsContext = createContext<DownloadsApi | null>(null);

function upsert(list: PullStatus[], job: PullStatus): PullStatus[] {
  const i = list.findIndex((j) => j.catalog_id === job.catalog_id);
  if (i === -1) return [...list, job];
  const copy = list.slice();
  copy[i] = { ...copy[i], ...job };
  return copy;
}

export function DownloadsProvider({ children }: { children: ReactNode }) {
  const qc = useQueryClient();
  const [jobs, setJobs] = useState<PullStatus[]>([]);
  const doneSeen = useRef<Set<string>>(new Set());

  const hasActive = jobs.some((j) => j.state === "downloading");

  const refresh = useCallback(async () => {
    try {
      const r = await modelsApi.pullActive();
      const next = r.jobs ?? [];
      setJobs(next);
      // When a job newly completes, refresh the installed + discoveries lists.
      for (const j of next) {
        if (j.state === "done" && j.catalog_id && !doneSeen.current.has(j.catalog_id)) {
          doneSeen.current.add(j.catalog_id);
          qc.invalidateQueries({ queryKey: ["models", "list"] });
          qc.invalidateQueries({ queryKey: ["models", "discoveries"] });
        }
      }
    } catch {
      /* transient — keep last known jobs */
    }
  }, [qc]);

  // Rehydrate once on mount.
  useEffect(() => {
    refresh();
  }, [refresh]);

  // Poll only while something is actively downloading.
  useEffect(() => {
    if (!hasActive) return;
    const t = setInterval(refresh, 1200);
    return () => clearInterval(t);
  }, [hasActive, refresh]);

  const startPull = useCallback(
    async (catalogId: string, displayName?: string, force = false) => {
      // optimistic card so the tray appears the instant the user clicks
      setJobs((prev) =>
        prev.some((j) => j.catalog_id === catalogId)
          ? prev
          : [
              ...prev,
              {
                catalog_id: catalogId,
                display_name: displayName,
                state: "downloading",
                progress: 0,
              },
            ],
      );
      try {
        const job = await modelsApi.pull(catalogId, force, displayName);
        setJobs((prev) => upsert(prev, job));
      } catch (err) {
        setJobs((prev) =>
          upsert(prev, {
            catalog_id: catalogId,
            display_name: displayName,
            state: "error",
            error: String(err),
          }),
        );
      }
      refresh();
    },
    [refresh],
  );

  const dismiss = useCallback((catalogId: string) => {
    doneSeen.current.delete(catalogId);
    setJobs((prev) => prev.filter((j) => j.catalog_id !== catalogId));
    modelsApi.pullDismiss(catalogId).catch(() => {});
  }, []);

  const jobFor = useCallback(
    (catalogId: string) => jobs.find((j) => j.catalog_id === catalogId),
    [jobs],
  );

  return (
    <DownloadsContext.Provider value={{ jobs, startPull, dismiss, jobFor }}>
      {children}
    </DownloadsContext.Provider>
  );
}

export function useDownloads(): DownloadsApi {
  const ctx = useContext(DownloadsContext);
  if (!ctx) throw new Error("useDownloads must be used within DownloadsProvider");
  return ctx;
}
