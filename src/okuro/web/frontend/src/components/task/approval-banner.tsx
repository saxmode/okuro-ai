import { Button } from "@/components/ui/button";
import type { ColorClass, SubtaskSummary } from "@/types/api";
import { TONE_TEXT } from "@/lib/color-class";
import { summaryLine } from "@/lib/format";

interface ApprovalBannerProps {
  subtask: SubtaskSummary;
  onApprove: (subtaskId: string) => void;
  onSkip: (subtaskId: string) => void;
  onReject: (subtaskId: string) => void;
}

/**
 * Map a free-form risk string onto the design-system `ColorClass` enum so
 * the risk level reads at the same visual stakes as the rest of the app
 * (TONE_TEXT). HIGH and MED both -> warning: risk is a reason to read
 * carefully before approving, not a failure that already happened
 * (ROCK-SOLID v5 P1.3, evidence inventory mismatch #11 — this used to map
 * HIGH to "error", the same red as a genuine failure). LOW -> success.
 * Anything unrecognised collapses to neutral — never an ad-hoc colour.
 */
function riskTone(risk: string): ColorClass {
  switch (risk.trim().toLowerCase()) {
    case "high":
    case "med":
    case "medium":
      return "warning";
    case "low":
      return "success";
    default:
      return "neutral";
  }
}

export function ApprovalBanner({
  subtask,
  onApprove,
  onSkip,
  onReject,
}: ApprovalBannerProps) {
  const riskClass = TONE_TEXT[riskTone(subtask.risk)];
  return (
    <div className="border-b border-warning/30 bg-warning/5 px-4 py-3">
      <div className="flex items-center justify-between">
        <div>
          <span className="text-xs font-bold uppercase tracking-wider text-warning">
            Approval Required
          </span>
          <p className="mt-1 text-sm text-fg">
            {subtask.role} — {summaryLine(subtask.description)}
          </p>
          <div className="mt-1 flex gap-3 text-2xs text-tertiary">
            <span data-testid="approval-risk">
              Risk:{" "}
              <span className={`font-semibold uppercase ${riskClass}`}>
                {subtask.risk}
              </span>
            </span>
            <span>Complexity: {subtask.complexity}</span>
          </div>
        </div>
        <div className="flex gap-2">
          <Button
            variant="outline"
            size="sm"
            onClick={() => onReject(subtask.id)}
            className="border-error/50 text-error"
            data-subtask-id={subtask.id}
          >
            Reject
          </Button>
          <Button
            variant="outline"
            size="sm"
            onClick={() => onSkip(subtask.id)}
            data-subtask-id={subtask.id}
          >
            Skip
          </Button>
          <Button
            size="sm"
            onClick={() => onApprove(subtask.id)}
            data-subtask-id={subtask.id}
          >
            Approve
          </Button>
        </div>
      </div>
    </div>
  );
}
