/**
 * Settings → Feedback. Where an operator decides whether their reviews leave
 * this machine, and where an install that UPDATED into the feature is sent to
 * fill the one field it is missing.
 *
 * CONSENT IS SHOWN, NOT DESCRIBED. The preview renders the literal payload
 * from the same builder the sync uses, so "what gets sent" cannot drift into a
 * comforting summary of what gets sent. The comment text is in there, because
 * the comment text is what leaves.
 *
 * Endpoint and key are not editable: they are the maintainer's, shipped as
 * defaults. The operator owns two things — whether to send, and who they are.
 */

import { useState, useEffect } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { AlertCircle, Check, Send } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { toast } from "@/components/ui/toast";
import { reviewSyncApi } from "@/lib/api";
import { cn } from "@/lib/utils";

export function FeedbackTab() {
  const qc = useQueryClient();
  const { data: status } = useQuery({
    queryKey: ["reviewSync"],
    queryFn: reviewSyncApi.status,
    retry: false,
  });
  const { data: preview } = useQuery({
    queryKey: ["reviewSyncPreview"],
    queryFn: reviewSyncApi.preview,
    retry: false,
  });

  const [org, setOrg] = useState("");
  // Seed from the server once it answers, without clobbering typing.
  useEffect(() => {
    if (status?.org_label !== undefined && org === "") setOrg(status.org_label);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [status?.org_label]);

  const save = useMutation({
    mutationFn: (body: { sync_enabled?: boolean; org_label?: string }) =>
      reviewSyncApi.save(body),
    onSuccess: (s) => {
      qc.setQueryData(["reviewSync"], s);
      qc.invalidateQueries({ queryKey: ["reviewSyncPreview"] });
      toast.success(s.ready ? "Saved — queued reviews are on their way" : "Saved");
    },
    onError: (e: unknown) =>
      toast.error(e instanceof Error ? e.message : "Could not save"),
  });

  const retry = useMutation({
    mutationFn: reviewSyncApi.retry,
    onSuccess: (r) => {
      qc.invalidateQueries({ queryKey: ["reviewSync"] });
      toast.success(`Released ${r.released} — sent ${r.sent ?? 0}`);
    },
  });

  const enabled = !!status?.enabled;
  const missingOrg = status?.missing?.includes("org_label");

  return (
    <section className="max-w-2xl space-y-8" id="feedback">
      <div>
        <h2 className="text-sm font-semibold text-fg">
          Feedback — sending your reviews to the maintainer
        </h2>
        <p className="mt-1 text-xs text-tertiary">
          Reviews you write with the feedback button are always saved on this
          machine. Turning this on also forwards them, so the maintainer can see
          what is not working and fix it.
        </p>
      </div>

      {/* The upgrade gap, stated where it can be fixed. */}
      {enabled && missingOrg && (
        <div className="flex items-start gap-2 rounded border border-warning/40 bg-warning/5 p-3 text-xs text-warning">
          <AlertCircle className="mt-0.5 h-4 w-4 shrink-0" />
          <span>
            Sending is on, but your organisation name is empty — so{" "}
            {status?.pending ?? 0} review
            {(status?.pending ?? 0) === 1 ? "" : "s"} {" "}
            {(status?.pending ?? 0) === 1 ? "is" : "are"} waiting here. Fill it
            in below and they go automatically. Nothing is lost.
          </span>
        </div>
      )}

      <div className="space-y-2">
        <label className="text-xs font-medium text-fg" htmlFor="fb-org">
          Who is this? <span className="text-tertiary">(shown to the maintainer)</span>
        </label>
        <div className="flex gap-2">
          <Input
            id="fb-org"
            value={org}
            placeholder="e.g. Acme AG"
            onChange={(e) => setOrg(e.target.value)}
            className="text-xs"
          />
          <Button
            size="sm"
            disabled={save.isPending || org.trim() === (status?.org_label ?? "")}
            onClick={() => save.mutate({ org_label: org })}
          >
            Save
          </Button>
        </div>
        <p className="text-3xs text-tertiary">
          Your install is identified by a random id
          {status?.install_id ? ` (${status.install_id.slice(0, 8)}…)` : ""} —
          this label is what makes it a name rather than a number.
        </p>
      </div>

      <div className="space-y-2">
        <div className="flex items-center gap-2">
          <Button
            size="sm"
            variant={enabled ? "default" : "outline"}
            disabled={save.isPending}
            onClick={() => save.mutate({ sync_enabled: !enabled })}
          >
            {enabled ? (
              <>
                <Check className="mr-1 h-3 w-3" /> Sending is on
              </>
            ) : (
              <>
                <Send className="mr-1 h-3 w-3" /> Turn sending on
              </>
            )}
          </Button>
          {enabled && status?.ready && (
            <span className="text-3xs text-tertiary">
              {status.pending} waiting
              {status.failed ? ` · ${status.failed} gave up` : ""}
            </span>
          )}
        </div>
        {!!status?.failed && (
          <Button
            size="sm"
            variant="ghost"
            disabled={retry.isPending}
            onClick={() => retry.mutate()}
          >
            Retry {status.failed} that gave up
          </Button>
        )}
      </div>

      {/* Literal, not a summary. */}
      <div className="space-y-2">
        <h3 className="text-xs font-medium text-fg">
          Exactly what gets sent
        </h3>
        <p className="text-3xs text-tertiary">{preview?.note}</p>
        <pre
          className={cn(
            "max-h-64 overflow-auto rounded border border-border bg-surface p-3",
            "font-mono text-3xs text-fg-muted",
          )}
        >
          {preview ? JSON.stringify(preview.payload, null, 2) : "…"}
        </pre>
        <p className="text-3xs text-tertiary">
          Nothing else leaves this machine — not the task, note or person ids a
          review points at, and not the element path.
        </p>
      </div>
    </section>
  );
}
