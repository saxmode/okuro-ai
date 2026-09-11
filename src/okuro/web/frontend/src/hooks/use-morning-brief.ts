import { useCallback, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { mediaApi, type MediaAsset } from "@/lib/assets-api";

const HEARD_KEY = "okuro.morningBrief.heardId";

function loadHeardId(): string | null {
  try {
    return localStorage.getItem(HEARD_KEY);
  } catch {
    return null;
  }
}

/**
 * Latest morning-brief audio asset (tag="brief", registered by
 * peer.delivery.morning_brief_audio via the shared assets bucket) + the
 * heard/unheard gate for the pulse sidepanel indicator. Heard state persists
 * across reloads in localStorage, keyed by asset id so a NEW brief (tomorrow's
 * render, a fresh asset id) is unheard again even if today's was played.
 */
export function useMorningBrief() {
  const { data } = useQuery({
    queryKey: ["assets", "morning-brief", "latest"],
    queryFn: () => mediaApi.list({ kind: "audio", tag: "brief", limit: 1 }),
    refetchInterval: 5 * 60_000,
  });
  const brief: MediaAsset | undefined = data?.items[0];

  const [heardId, setHeardId] = useState<string | null>(loadHeardId);

  const markHeard = useCallback((id: string) => {
    setHeardId(id);
    try {
      localStorage.setItem(HEARD_KEY, id);
    } catch {
      /* ignore — heard state degrades to session-only, not fatal */
    }
  }, []);

  return { brief, unheard: !!brief && brief.id !== heardId, markHeard };
}
