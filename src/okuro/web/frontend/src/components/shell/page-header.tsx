import type { ReactNode } from "react";
import { cn } from "@/lib/utils";

interface PageHeaderProps {
  title: ReactNode;
  subtitle?: ReactNode;
  right?: ReactNode;
  className?: string;
}

/**
 * Canonical page header. First child of `<div className="page-shell">` on
 * every top-level page. Locks title + subtitle typography to the design
 * system so drift cannot re-emerge: changing the scale happens here, never
 * at call sites. Right slot hosts page-level actions (buttons, refresh,
 * keyboard hints) when needed.
 */
export function PageHeader({ title, subtitle, right, className }: PageHeaderProps) {
  const titleBlock = (
    <div>
      <h1 className="type-title text-fg">{title}</h1>
      {subtitle ? (
        <p className="mt-1 type-small text-tertiary">{subtitle}</p>
      ) : null}
    </div>
  );

  if (!right) {
    return <div className={className}>{titleBlock}</div>;
  }

  return (
    <div className={cn("flex items-end justify-between gap-4", className)}>
      {titleBlock}
      {right}
    </div>
  );
}
