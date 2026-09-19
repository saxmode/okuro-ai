import { useEffect, useRef, useState, useMemo } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { Loader2, Mic, RefreshCw, Repeat, User } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import { Input } from "@/components/ui/input";
import { Switch } from "@/components/ui/switch";
import { Badge } from "@/components/ui/badge";
import { Progress } from "@/components/ui/progress";
import { SectionLabel } from "@/components/ui/section-label";
import { EmptyState } from "@/components/ui/empty-state";
import { FieldLabel } from "@/components/ui/form-primitives";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { toast } from "@/components/ui/toast";
import { peopleApi } from "@/lib/people-api";
import {
  mediaApi,
  withToken,
  type MediaItem,
  type MediaJob,
} from "@/lib/media-api";
import { parseApiDate } from "@/lib/format";
import { usePaneInterval } from "@/lib/pane-active";
import { useSectionTitle } from "@/shell/components/PageTitle";

/**
 * /media — request a podcast on a topic, for a target person, optionally
 * recurring (e.g. a verbal brief every morning).
 *
 * Flow: topic -> backend builds a briefing artifact -> delivery pipeline
 * renders the chosen channel (default Podcast) into audio. Recurring writes
 * an orchestrator mandate the daemon scheduler picks up on its cron.
 */

// Production mode: audio summary (one friendly narrator explaining what went
// on) vs podcast (two-host chat about the topic). Podcast is Beta.
const MODES = [
  { value: "summary", label: "Audio summary", beta: false },
  { value: "podcast", label: "Podcast", beta: true },
] as const;

type Mode = (typeof MODES)[number]["value"];

const NO_RECIPIENT = "__none__";

export function MediaPage() {
  const queryClient = useQueryClient();

  const [topic, setTopic] = useState("");
  const [personId, setPersonId] = useState<string>(NO_RECIPIENT);
  const [mode, setMode] = useState<Mode>("summary");
  const [recurring, setRecurring] = useState(false);
  const [time, setTime] = useState("06:00");

  /**
   * THE EPISODE LIST IS HISTORY, NOT A VIEW OF THE MODE SELECTOR, and the
   * client was the only half that still thought otherwise.
   *
   * This file used to derive `const channel = mode === "podcast" ? … : "tts"`
   * and pass it to `mediaApi.list`, and the p3 spec read that and concluded
   * "changing a FORM field silently replaces the LIST below it", raising a
   * question about giving the channel its own `?view=`. THE SERVER SAYS
   * OTHERWISE, in as many words (`orchestrator/api/media.py:302-308`):
   *
   *   "it returns ALL audio deliveries (podcast + summary/tts) newest-first,
   *    so switching the mode selector never hides recordings that already
   *    exist. `channel` is accepted for back-compat but no longer filters."
   *
   * Measured before removing it: switching to Podcast sent
   * `?limit=25&channel=podcast` and the rendered list was byte-identical —
   * the same 23 episode ids, and the badges still read PODCAST and TTS side
   * by side. So the parameter did nothing except key the react-query cache on
   * it, which threw a warm cache away and refetched the same rows on every
   * mode toggle. One dataset, one cache entry.
   */

  const peopleQuery = useQuery({
    queryKey: ["people", "media"],
    queryFn: () => peopleApi.list(),
  });

  const listQuery = useQuery({
    queryKey: ["media"],
    queryFn: () => mediaApi.list(25),
    // T9 / Q-D3 ruled A — AN INACTIVE PANE IS QUIET. All five topic bars keep
    // one pane mounted at all times, so once PODCAST is the remembered DELIVER
    // leaf these two polls ran for the whole session from every other topic:
    // 20 job requests a minute from an invisible pane. `usePaneInterval`
    // defaults to TRUE outside the shell, which is the load-bearing part —
    // the six chrome-free routes must keep polling.
    refetchInterval: usePaneInterval(15_000),
  });

  const jobsQuery = useQuery({
    queryKey: ["media-jobs"],
    queryFn: () => mediaApi.jobs(20),
    refetchInterval: usePaneInterval(3000),
  });

  const createMutation = useMutation({
    mutationFn: () =>
      mediaApi.create({
        topic: topic.trim(),
        person_id: personId === NO_RECIPIENT ? null : personId,
        mode,
        recurring: recurring ? { enabled: true, time } : { enabled: false },
      }),
    onSuccess: (res) => {
      if (res.status === "failed" || !res.job_id) {
        toast.error(res.error || "Could not start generation");
        return;
      }
      toast.success("Generation started — this takes a few minutes");
      setTopic("");
      queryClient.invalidateQueries({ queryKey: ["media-jobs"] });
    },
    onError: (err) => {
      toast.error(err instanceof Error ? err.message : "Generation failed");
    },
  });

  const people = peopleQuery.data?.people ?? [];
  const media = listQuery.data?.media ?? [];
  const jobs = jobsQuery.data?.jobs ?? [];
  // Show generating + failed, plus just-finished jobs for ~8s so the final
  // "ready" stage is visible before the episode drops into the list below.
  const activeJobs = jobs.filter(
    (j) =>
      j.status === "generating" ||
      j.status === "failed" ||
      // D3 — ONE DATE DECODER. This read `new Date(\`${j.updated_at}Z\`)`,
      // hand-appending the UTC marker the API omits, while the file already
      // imports `parseApiDate` for the same field family. Two decoders for one
      // server format is the shape of the "125 GB read 124.9 KB" class
      // (245d4942): the day the API starts sending an offset, `"…+02:00Z"` is
      // an Invalid Date, `NaN < 8000` is false, and the 8-second just-finished
      // window silently stops firing — so a finished episode appears only on
      // the next 15s poll or a manual Refresh. Silent, and not a crash.
      (j.status === "done" &&
        Date.now() - (parseApiDate(j.updated_at)?.getTime() ?? 0) < 8000),
  );
  const canSubmit = topic.trim().length > 0 && !createMutation.isPending;

  // When a generation finishes, pull the freshly-rendered episode into the list.
  const prevGenerating = useRef(0);
  useEffect(() => {
    const generating = jobs.filter((j) => j.status === "generating").length;
    if (generating < prevGenerating.current) {
      queryClient.invalidateQueries({ queryKey: ["media"] });
    }
    prevGenerating.current = generating;
  }, [jobs, queryClient]);

  /* REFRESH IS THE LEAF'S ONE CHROME ACTION, so it goes to the plate. */
  const header = useMemo(
    () => ({
      actions: (
        <Button
          variant="ghost"
          size="sm"
          onClick={() => listQuery.refetch()}
          disabled={listQuery.isFetching}
        >
          {/* THE WORD STAYS. The page rendered "Refresh" beside this glyph and
              publishing it dropped the label, leaving an unnamed circular
              arrow on the plate — SERVICES and STUDIO both kept theirs. */}
          <RefreshCw className={listQuery.isFetching ? "mr-1.5 animate-spin" : "mr-1.5"} />
          Refresh
        </Button>
      ),
    }),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [listQuery.isFetching, listQuery.refetch],
  );
  useSectionTitle(header);

  return (
    <div className="page-shell space-y-10">
      {/* R1 (86b8f1f0) — THE LEAF TITLE IS THE SHELL'S, AND THAT SETTLES Q-D7
          WITHOUT RENAMING ANYTHING. The p3 summary's Q-D7 asked whether to
          rename this page's `<h1>Media</h1>` to "Podcast", because the rail
          says PODCAST here while the leaf actually called MEDIA has no heading
          at all — so the word "Media" appeared on screen only on the leaf that
          is not called Media. Under R1 the page has no title to rename:
          `TopicBar` renders `<h1 class="c-title">Podcast</h1>` above this pane
          (measured), and the collision is gone at its cause rather than by
          choosing between two words.

          THE SENTENCE AND Refresh SURVIVE AS THE FIRST CONTENT BLOCK, the shape
          SERVICES and MODELS landed: `PageHeader`'s `right` slot held a real
          control, so the row keeps it at its trailing edge. `min-w-0 flex-1`
          on the paragraph and `items-start` on the row so the SENTENCE wraps
          rather than the button dropping to its own line — the defect the
          SERVICES pass measured at 1366 (pTop 199.2 vs btnTop 232.4).

          `text-fg-muted` rather than `PageHeader`'s `text-tertiary`: that token
          is the standing AA failure (kit todo c581c9b2). */}
      {/* REFRESH IS ON THE PLATE NOW — see the memo above — so the row that
          kept it beside the sentence, and the wrap guard that row needed, are
          gone with it. */}
      <p className="type-small text-fg-muted">
        Generate a podcast on any topic for someone — optionally as a
        recurring brief.
      </p>

      {/* Request form */}
      <section className="space-y-5 rounded-lg border border-border bg-surface-elevated p-6">
        <SectionLabel>New audio</SectionLabel>

        <div className="space-y-2">
          <FieldLabel>Topic</FieldLabel>
          <Textarea
            value={topic}
            onChange={(e) => setTopic(e.target.value)}
            placeholder="e.g. last developments of okuro"
            rows={3}
            className="w-full"
          />
        </div>

        <div className="grid gap-5 sm:grid-cols-2">
          <div className="space-y-2">
            <FieldLabel>Recipient</FieldLabel>
            <Select value={personId} onValueChange={setPersonId}>
              <SelectTrigger className="w-full">
                <SelectValue placeholder="Choose a recipient" />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value={NO_RECIPIENT}>No specific recipient</SelectItem>
                {people.map((p) => (
                  <SelectItem key={p.id} value={p.id}>
                    {p.display_name || p.id}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>

          <div className="space-y-2">
            <FieldLabel>Mode</FieldLabel>
            <Select value={mode} onValueChange={(v) => setMode(v as Mode)}>
              <SelectTrigger className="w-full">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {MODES.map((m) => (
                  <SelectItem key={m.value} value={m.value}>
                    <span className="flex items-center gap-2">
                      {m.label}
                      {m.beta && (
                        <Badge variant="outline" className="text-[10px] case-label">
                          Beta
                        </Badge>
                      )}
                    </span>
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            <p className="text-xs text-tertiary">
              {mode === "podcast"
                ? "Two hosts chatting about the topic."
                : "One friendly voice explaining what went on."}
            </p>
          </div>
        </div>

        <div className="flex flex-wrap items-center gap-4 rounded-md border border-border bg-surface px-4 py-3">
          <div className="flex items-center gap-3">
            <Switch checked={recurring} onCheckedChange={setRecurring} />
            <div className="flex items-center gap-2 text-sm text-fg">
              <Repeat className="h-4 w-4 text-tertiary" />
              Recurring brief
            </div>
          </div>
          {recurring && (
            <div className="flex items-center gap-2">
              <span className="text-sm text-tertiary">daily at</span>
              <Input
                type="time"
                value={time}
                onChange={(e) => setTime(e.target.value)}
                className="w-32"
              />
            </div>
          )}
        </div>

        <div className="flex justify-end">
          <Button onClick={() => createMutation.mutate()} disabled={!canSubmit}>
            {createMutation.isPending ? (
              <>
                <Loader2 className="h-4 w-4 animate-spin" />
                Generating…
              </>
            ) : (
              <>
                <Mic className="h-4 w-4" />
                Generate
              </>
            )}
          </Button>
        </div>
      </section>

      {/* In progress — live generation jobs with a progress bar */}
      {activeJobs.length > 0 && (
        <section className="space-y-3">
          <SectionLabel>In progress</SectionLabel>
          <ul className="space-y-3">
            {activeJobs.map((job) => (
              <JobRow key={job.id} job={job} />
            ))}
          </ul>
        </section>
      )}

      {/* Recent media */}
      <section className="space-y-4">
        <SectionLabel>Recent media</SectionLabel>
        {media.length === 0 ? (
          <EmptyState
            icon={<Mic className="h-10 w-10" />}
            title="No media yet"
            description="Generate your first podcast above."
          />
        ) : (
          <ul className="space-y-3">
            {media.map((item) => (
              <MediaRow key={item.id} item={item} />
            ))}
          </ul>
        )}
      </section>
    </div>
  );
}

function MediaRow({ item }: { item: MediaItem }) {
  const [audioSrc, setAudioSrc] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    if (item.audio_url) {
      withToken(item.audio_url).then((url) => {
        if (alive) setAudioSrc(url);
      });
    }
    return () => {
      alive = false;
    };
  }, [item.audio_url]);

  const created = item.created_at
    ? parseApiDate(item.created_at).toLocaleString()
    : "";

  return (
    <li className="space-y-3 rounded-lg border border-border bg-surface-elevated p-4">
      <div className="flex flex-wrap items-center gap-2">
        <Badge variant="outline" className="case-label tracking-wider">
          {item.channel || "podcast"}
        </Badge>
        <span className="text-sm font-medium text-fg">
          {item.title || "Untitled"}
        </span>
        {item.person_id && (
          <span className="flex items-center gap-1 text-xs text-tertiary">
            <User className="h-3 w-3" />→ {item.person_id}
          </span>
        )}
        {!item.success && (
          <Badge variant="outline" className="text-error">
            failed
          </Badge>
        )}
        {created && (
          <span className="ml-auto text-xs text-tertiary">{created}</span>
        )}
      </div>
      {item.audio_url && audioSrc && (
        <audio src={audioSrc} controls className="w-full" />
      )}
    </li>
  );
}

function JobRow({ job }: { job: MediaJob }) {
  const pct = Math.round((job.progress ?? 0) * 100);
  const failed = job.status === "failed";
  return (
    <li className="space-y-2 rounded-lg border border-border bg-surface-elevated p-4">
      <div className="flex flex-wrap items-center gap-2">
        <Badge variant="outline" className="case-label tracking-wider">
          {job.channel || "podcast"}
        </Badge>
        <span className="min-w-0 flex-1 truncate text-sm font-medium text-fg">
          {job.topic}
        </span>
        {job.person_id && (
          <span className="flex items-center gap-1 text-xs text-tertiary">
            <User className="h-3 w-3" />→ {job.person_id}
          </span>
        )}
        <span className="text-xs text-tertiary">
          {failed ? "failed" : `${job.stage} · ${pct}%`}
        </span>
      </div>
      {failed ? (
        <p className="text-xs text-error">{job.error || "generation failed"}</p>
      ) : (
        <Progress value={pct} className="h-1.5" />
      )}
    </li>
  );
}
