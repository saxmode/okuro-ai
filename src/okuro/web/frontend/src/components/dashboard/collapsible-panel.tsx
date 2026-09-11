import { useState, type ReactNode } from "react";
import { ChevronDown, ChevronRight } from "lucide-react";

interface CollapsiblePanelProps {
  title: string;
  count?: number;
  defaultOpen?: boolean;
  children: ReactNode;
}

export function CollapsiblePanel({
  title,
  count,
  defaultOpen = true,
  children,
}: CollapsiblePanelProps) {
  const [open, setOpen] = useState(defaultOpen);

  return (
    <div className="border-b border-border last:border-0">
      <button
        onClick={() => setOpen(!open)}
        className="flex w-full items-center gap-2 px-3 py-2 text-left transition-colors hover:bg-surface-elevated"
      >
        {open ? (
          <ChevronDown className="h-3 w-3 text-tertiary" />
        ) : (
          <ChevronRight className="h-3 w-3 text-tertiary" />
        )}
        <span className="text-xs font-medium uppercase tracking-wider text-tertiary">
          {title}
        </span>
        {count !== undefined && (
          <span className="text-3xs text-tertiary">{count}</span>
        )}
      </button>
      {open && <div className="px-3 pb-2">{children}</div>}
    </div>
  );
}
