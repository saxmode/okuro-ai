/**
 * EngineStalenessChip — the engine is running older code than the API (P4.6).
 *
 * An engine subprocess is pinned to the code it was spawned with for the whole
 * task lifetime. After an API restart onto a newer commit the two silently
 * disagree, and a mismatched-engine bug looks exactly like a working-engine
 * bug — you debug the fix you just shipped against an engine that never saw
 * it. The engine has logged its git sha at boot since long before this; NOTHING
 * read it.
 *
 * INFO, not a warning, and no action offered. Nothing is broken: a pinned
 * engine is the designed behaviour, and P4.5's restart exists so it picks up
 * fixes at the next pause on its own. The chip makes the disagreement legible,
 * which is a different job from making it look like a fault — the same rule
 * that took the red pill off healthy gates in P1.
 *
 * Renders nothing when the shas agree, either is unknown, or the task predates
 * the engine_version row. The backend sends null in all of those cases, so the
 * common case costs one null check.
 */
import type { TaskSnapshot } from "@/types/api";

export function EngineStalenessChip({
  staleness,
}: {
  staleness: TaskSnapshot["engine_staleness"];
}) {
  if (!staleness) return null;

  return (
    <div
      data-testid="engine-staleness-chip"
      className="flex items-center gap-2 border-b border-info/30 bg-info-subtle/30 px-10 py-1.5 text-2xs text-info"
    >
      <span className="font-medium">{staleness.label}</span>
      <span className="font-mono text-3xs text-tertiary">
        engine {staleness.engine_sha} · api {staleness.api_sha}
      </span>
    </div>
  );
}

export default EngineStalenessChip;
