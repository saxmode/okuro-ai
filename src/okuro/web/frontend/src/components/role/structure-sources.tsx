import type {
  KnownStructureSourceAlarm,
  StructureSource,
  StructureSourceAlarm,
} from "@/types/api";
import { formatAge } from "@/lib/format";

/**
 * THE SOURCE REGISTRY, AS A ROW PER SOURCE.
 *
 * What this panel exists to make visible is not "we have nine sources". It is
 * the difference between a source that said nothing changed and a source that
 * said NOTHING — a 404, a 200 serving a login wall, a redirect that left the
 * registered host. Those two read identically in any UI that only shows a
 * green tick, and a dead feed that renders green stays dead for six months.
 *
 * So three states, never two:
 *
 *   unchanged  — polled, answered, same content. The common and correct case.
 *   changed    — polled, answered, content moved. What the researcher reads.
 *   alarm      — answered wrongly, or did not answer. Claims nothing.
 *
 * plus `never polled`, which is none of the three. A registry nothing has
 * fetched is not healthy; it is unmeasured, and it says so in those words.
 */

/** One line per alarm, in the words the repair would be described in. */
export const ALARM_LABEL: Record<KnownStructureSourceAlarm, string> = {
  http_status: "no 200",
  anchor_missing: "anchor gone",
  cross_host_redirect: "host moved",
  length_collapse: "body collapsed",
  stale_unchanged: "stale",
  no_stored_body: "no body",
  poll_failed: "poll failed",
  poll_timeout: "timed out",
};

export const ALARM_MEANING: Record<KnownStructureSourceAlarm, string> = {
  http_status: "The source did not answer 200 or 304.",
  anchor_missing:
    "It answered 200, but the sentence we registered is gone from the body — a login wall, a cookie interstitial or a page that no longer exists where it used to.",
  cross_host_redirect:
    "The request ended on a different host than the one registered. The content may be fine; the registry entry is not.",
  length_collapse:
    "The body lost more than half its length against the last stored one.",
  stale_unchanged:
    "The content hash has not moved for longer than this source's staleness window. Either the spec is finished, or the feed is dead. Both need somebody to look.",
  no_stored_body:
    "The run ended with no body behind this source, so nothing quoted from it can be verified. It reported nothing, which is not the same as reporting no change.",
  poll_failed: "The poll raised before it could reach a verdict for this source.",
  poll_timeout:
    "The source did not answer inside the whole-run deadline, so it contributed nothing to this run.",
};

/**
 * The backend owns the alarm vocabulary and adds to it — three of the eight
 * above arrived in one review round. An unknown name must still render as
 * ITSELF: a blank cell where an alarm should be is the one thing this panel
 * exists to prevent, and it would appear exactly when a new failure mode ships.
 */
export function alarmLabel(alarm: StructureSourceAlarm): string {
  return ALARM_LABEL[alarm as KnownStructureSourceAlarm] ?? alarm;
}

export function alarmMeaning(alarm: StructureSourceAlarm): string {
  return (
    ALARM_MEANING[alarm as KnownStructureSourceAlarm] ??
    `This build has no description for "${alarm}" — it is an alarm the API added after this page was built.`
  );
}

type SourceState = "never" | "alarm" | "changed" | "unchanged";

export function sourceState(source: StructureSource): SourceState {
  if (source.never_polled) return "never";
  if (source.alarms.length) return "alarm";
  return source.last_changed ? "changed" : "unchanged";
}

const STATE_LABEL: Record<SourceState, string> = {
  never: "never polled",
  alarm: "alarm",
  changed: "changed",
  unchanged: "unchanged",
};

/** Never green for `never`: unmeasured is not healthy. */
const STATE_TONE: Record<SourceState, string> = {
  never: "text-tertiary",
  alarm: "text-error",
  changed: "text-info",
  unchanged: "text-success",
};

export function StructureSourceRow({ source }: { source: StructureSource }) {
  const state = sourceState(source);
  const alarmText = source.alarms
    .map((a) => `${alarmLabel(a)}: ${alarmMeaning(a)}`)
    .join("\n");

  return (
    <li
      className="flex items-baseline gap-2 py-1 text-2xs"
      data-testid={`structure-source-${source.id}`}
      data-state={state}
    >
      <span className="min-w-0 flex-1 truncate text-fg" title={source.url}>
        {source.name}
      </span>
      <span className="shrink-0 text-tertiary">{source.vendor ?? "—"}</span>
      <span
        className="shrink-0 text-fg-muted"
        title={
          source.last_checked_at
            ? `Last polled ${source.last_checked_at}`
            : "This source has never been polled."
        }
      >
        {source.last_checked_at ? formatAge(source.last_checked_at) : "—"}
      </span>
      <span className={`shrink-0 ${STATE_TONE[state]}`}>
        {STATE_LABEL[state]}
      </span>
      {source.alarms.length ? (
        <span className="shrink-0 text-error" title={alarmText}>
          {source.alarms.map((a) => alarmLabel(a)).join(" · ")}
        </span>
      ) : null}
    </li>
  );
}

export function StructureSourcesPanel({
  sources,
  alarmed,
  neverPolled,
}: {
  sources: StructureSource[];
  alarmed: string[];
  neverPolled: string[];
}) {
  if (!sources.length) {
    return (
      <p className="type-small text-fg-muted" data-testid="structure-sources-empty">
        No structure sources registered.
      </p>
    );
  }

  return (
    <div className="space-y-1" data-testid="structure-sources-panel">
      <p className="type-small text-fg-muted">
        {sources.length} structure sources
        {alarmed.length ? (
          <>
            {" · "}
            <span
              className="text-error"
              title="A source that alarmed did not report 'no change' — it reported nothing. Nothing is claimed about it."
            >
              {alarmed.length} alarmed
            </span>
          </>
        ) : null}
        {neverPolled.length ? (
          <>
            {" · "}
            <span
              className="text-tertiary"
              title="Registered but never fetched. Unmeasured is not healthy, so these are not counted as green."
            >
              {neverPolled.length} never polled
            </span>
          </>
        ) : null}
      </p>
      <ul className="divide-y divide-border/40">
        {sources.map((source) => (
          <StructureSourceRow key={source.id} source={source} />
        ))}
      </ul>
    </div>
  );
}
