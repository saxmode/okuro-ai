/**
 * Commercial-use verdict badge — the licensing check recorded at model
 * qualification. Conservative: an unrecognised licence reads "verify", never a
 * false "commercial". Same visual language on the Discover list and in Studio.
 */
import { Badge } from "@/components/ui/badge";

type Status = "commercial" | "conditional" | "non_commercial" | "unknown" | string;

type Verdict = { label: string; className: string; title: string };

// Concrete fallback so `MAP[key] ?? FALLBACK` is never undefined under
// noUncheckedIndexedAccess (a Record<string> index is always possibly-undefined).
const FALLBACK: Verdict = {
  label: "License: verify",
  className: "border-border-subtle text-fg-subtle",
  title: "Licence not recognised — verify terms before commercial use",
};

const MAP: Record<string, Verdict> = {
  commercial: {
    label: "Commercial ✓",
    className: "border-success/40 text-success",
    title: "Permissive licence — commercial use permitted",
  },
  conditional: {
    label: "Conditional",
    className: "border-warning/40 text-warning",
    title: "Community licence — commercial use only under a revenue cap (verify)",
  },
  non_commercial: {
    label: "Non-commercial",
    className: "border-error/40 text-error",
    title: "Non-commercial licence — needs a separate licence from the model's provider",
  },
  unknown: FALLBACK,
};

export function CommercialBadge({
  status,
  licenseId,
  className,
}: {
  status?: Status;
  licenseId?: string | null;
  className?: string;
}) {
  const m = MAP[status ?? "unknown"] ?? FALLBACK;
  const title = licenseId ? `${m.title} · licence: ${licenseId}` : m.title;
  return (
    <Badge
      variant="outline"
      className={`${m.className} ${className ?? ""} text-3xs font-normal`}
      title={title}
    >
      {m.label}
    </Badge>
  );
}
