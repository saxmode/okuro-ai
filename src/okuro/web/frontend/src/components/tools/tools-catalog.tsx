import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Search, X } from "lucide-react";
import { toolApi } from "@/lib/api";
import type { ToolInfo } from "@/types/api";
import { Input } from "@/components/ui/input";
import { ScrollArea } from "@/components/ui/scroll-area";
import { EmptyState } from "@/components/ui/empty-state";
import { cn } from "@/lib/utils";

/**
 * /agents > Tools — searchable catalog of every MCP tool the okuro unified
 * server exposes, with namespace filtering and role→tool mapping.
 *
 * Master/detail layout:
 *   Left:  filter bar (search + namespace chips) + scrollable tool list
 *   Right: selected tool detail (description, input schema, role usage)
 */
export function ToolsCatalog() {
  const [search, setSearch] = useState("");
  const [activeNs, setActiveNs] = useState<string | undefined>();
  const [selectedName, setSelectedName] = useState<string | undefined>();

  const { data, isLoading, isError, error } = useQuery({
    queryKey: ["tools", "catalog"],
    queryFn: () => toolApi.catalog(),
    staleTime: 60_000,
  });

  const tools = data?.tools ?? [];
  const nsDescriptions = data?.namespaces ?? {};

  const namespaces = useMemo(
    () => [...new Set(tools.map((t) => t.namespace))].sort(),
    [tools],
  );

  const filtered = useMemo(() => {
    const q = search.trim().toLowerCase();
    return tools.filter((t) => {
      if (activeNs && t.namespace !== activeNs) return false;
      if (!q) return true;
      return (
        t.name.toLowerCase().includes(q) ||
        t.description.toLowerCase().includes(q)
      );
    });
  }, [tools, search, activeNs]);

  const selected = selectedName
    ? tools.find((t) => t.name === selectedName)
    : undefined;

  // Keep selection in sync with filtering — if selection is hidden, clear it.
  const selectedHiddenByFilter =
    selected && !filtered.some((t) => t.name === selected.name);

  if (isLoading) {
    return (
      <div className="p-4 text-sm text-tertiary">Loading tool catalog...</div>
    );
  }

  if (isError) {
    return (
      <EmptyState
        title="Failed to load tool catalog"
        description={
          error instanceof Error
            ? error.message
            : "Could not reach /api/tools/catalog."
        }
      />
    );
  }

  if (tools.length === 0) {
    return (
      <EmptyState
        title="No tools registered"
        description="The MCP registry returned zero tools. Check the okuro MCP server logs."
      />
    );
  }

  return (
    <div className="space-y-4">
      {/* Header row */}
      <div className="flex items-baseline justify-between">
        <div>
          <h2 className="text-sm font-medium text-fg">MCP tool catalog</h2>
          <p className="text-2xs text-tertiary">
            {data?.count ?? tools.length} tools across {namespaces.length}{" "}
            namespaces
          </p>
        </div>
        <span className="text-2xs text-tertiary">
          Showing {filtered.length} of {tools.length}
        </span>
      </div>

      {/* Filters */}
      <div className="space-y-2">
        <div className="relative">
          <Search className="absolute left-2.5 top-2.5 h-3.5 w-3.5 text-tertiary" />
          <Input
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder="Search name or description..."
            className="bg-surface pl-8 text-sm text-fg placeholder:text-tertiary"
          />
          {search && (
            <button
              onClick={() => setSearch("")}
              aria-label="Clear search"
              className="absolute right-2 top-2 text-tertiary hover:text-fg-muted focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent/50 focus-visible:rounded-sm"
            >
              <X className="h-4 w-4" aria-hidden="true" />
            </button>
          )}
        </div>

        {/* Namespace chips */}
        <div className="flex flex-wrap items-center gap-1.5 rounded-md border border-border-subtle bg-surface-subtle px-3 py-2">
          <span className="text-3xs uppercase tracking-wider text-tertiary">
            Namespace
          </span>
          <div className="flex flex-wrap gap-1">
            {namespaces.map((ns) => {
              const count = tools.filter((t) => t.namespace === ns).length;
              const selectedChip = activeNs === ns;
              return (
                <button
                  key={ns}
                  onClick={() => setActiveNs(selectedChip ? undefined : ns)}
                  title={nsDescriptions[ns] || ns}
                  className={cn(
                    "rounded-full border px-2 py-0.5 text-3xs transition-colors",
                    "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent/50",
                    selectedChip
                      ? "border-accent bg-accent-subtle text-accent"
                      : "border-border bg-transparent text-fg-muted hover:border-border-hover",
                  )}
                >
                  <span className="font-mono">{ns}</span>
                  <span className="ml-1 opacity-60">{count}</span>
                </button>
              );
            })}
          </div>
          {activeNs && (
            <button
              onClick={() => setActiveNs(undefined)}
              className="ml-auto text-3xs uppercase tracking-wider text-tertiary hover:text-fg-muted focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent/50 rounded-sm"
            >
              Clear
            </button>
          )}
        </div>
      </div>

      {/* Master / detail */}
      <div className="grid gap-4 md:grid-cols-[minmax(0,1fr)_minmax(0,1.35fr)]">
        <ToolList
          tools={filtered}
          selectedName={selectedHiddenByFilter ? undefined : selectedName}
          onSelect={setSelectedName}
        />

        <ToolDetail tool={selected} />
      </div>
    </div>
  );
}

/* --------------------------- Tool list (left) ---------------------------- */

interface ToolListProps {
  tools: ToolInfo[];
  selectedName?: string;
  onSelect: (name: string) => void;
}

function ToolList({ tools, selectedName, onSelect }: ToolListProps) {
  if (tools.length === 0) {
    return (
      <div className="rounded-md border border-border-subtle bg-surface-subtle p-6 text-center text-xs text-tertiary">
        No tools match the current filters.
      </div>
    );
  }

  // Group by namespace for visual grouping.
  const grouped = tools.reduce<Record<string, ToolInfo[]>>((acc, t) => {
    (acc[t.namespace] ??= []).push(t);
    return acc;
  }, {});
  const namespaces = Object.keys(grouped).sort();

  return (
    <ScrollArea className="h-[calc(100vh-22rem)] min-h-[480px] rounded-md border border-border-subtle bg-surface">
      <div className="space-y-3 p-2">
        {namespaces.map((ns) => (
          <div key={ns}>
            <h3 className="mb-1 px-2 text-3xs font-medium uppercase tracking-wider text-tertiary">
              {ns}
              <span className="ml-1.5 opacity-60">{grouped[ns]!.length}</span>
            </h3>
            <div className="space-y-0.5">
              {grouped[ns]!.map((t) => (
                <ToolRow
                  key={t.name}
                  tool={t}
                  selected={t.name === selectedName}
                  onSelect={() => onSelect(t.name)}
                />
              ))}
            </div>
          </div>
        ))}
      </div>
    </ScrollArea>
  );
}

interface ToolRowProps {
  tool: ToolInfo;
  selected: boolean;
  onSelect: () => void;
}

function ToolRow({ tool, selected, onSelect }: ToolRowProps) {
  return (
    <button
      onClick={onSelect}
      className={cn(
        "group block w-full rounded-sm px-2 py-1.5 text-left transition-colors",
        "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent/50",
        selected
          ? "bg-accent-subtle"
          : "hover:bg-surface-subtle",
      )}
    >
      <div className="flex items-center gap-2">
        <span
          className={cn(
            "truncate font-mono text-2xs",
            selected ? "text-accent" : "text-fg",
          )}
        >
          {tool.name}
        </span>
        <span className="ml-auto shrink-0 rounded-sm border border-border-subtle px-1 py-px font-mono text-3xs text-tertiary">
          {tool.namespace}
        </span>
      </div>
      {tool.description && (
        <p className="mt-0.5 line-clamp-1 text-3xs text-tertiary">
          {tool.description}
        </p>
      )}
    </button>
  );
}

/* --------------------------- Tool detail (right) -------------------------- */

interface ToolDetailProps {
  tool?: ToolInfo;
}

function ToolDetail({ tool }: ToolDetailProps) {
  if (!tool) {
    return (
      <div className="flex h-[calc(100vh-22rem)] min-h-[480px] items-center justify-center rounded-md border border-border-subtle bg-surface-subtle">
        <div className="text-center">
          <p className="text-sm text-fg-muted">Select a tool</p>
          <p className="mt-1 text-2xs text-tertiary">
            Click any row on the left to see its description, input schema, and
            role usage.
          </p>
        </div>
      </div>
    );
  }

  const schemaJson = JSON.stringify(tool.input_schema, null, 2);
  const complianceHint = deriveComplianceHint(tool);

  return (
    <ScrollArea className="h-[calc(100vh-22rem)] min-h-[480px] rounded-md border border-border-subtle bg-surface">
      <div className="space-y-4 p-4">
        {/* Header */}
        <div>
          <div className="flex flex-wrap items-center gap-2">
            <h3 className="font-mono text-sm font-medium text-fg">
              {tool.name}
            </h3>
            <span className="rounded-sm border border-border-subtle px-1.5 py-0.5 font-mono text-3xs text-tertiary">
              {tool.namespace}
            </span>
          </div>
          {tool.description && (
            <p className="mt-1.5 text-xs leading-relaxed text-fg-muted">
              {tool.description}
            </p>
          )}
        </div>

        {complianceHint && (
          <div className="rounded-md border border-warning/40 bg-warning/10 px-3 py-2 text-2xs text-warning">
            {complianceHint}
          </div>
        )}

        {/* Input schema */}
        <div>
          <h4 className="mb-1.5 text-3xs font-medium uppercase tracking-wider text-tertiary">
            Input schema
          </h4>
          <pre className="overflow-x-auto rounded-md border border-border-subtle bg-surface-subtle p-3 text-3xs leading-relaxed text-fg-muted">
            <code>{schemaJson}</code>
          </pre>
        </div>

        {/* Roles using this tool */}
        <div>
          <h4 className="mb-1.5 text-3xs font-medium uppercase tracking-wider text-tertiary">
            Roles using this tool
            <span className="ml-1.5 opacity-60">{tool.roles.length}</span>
          </h4>
          {tool.roles.length === 0 ? (
            <p className="text-2xs text-tertiary">
              No role explicitly lists this tool in its{" "}
              <span className="font-mono">tools</span> field.
            </p>
          ) : (
            <div className="flex flex-wrap gap-1">
              {tool.roles.map((roleId) => (
                <span
                  key={roleId}
                  className="rounded-sm border border-border-subtle bg-surface-subtle px-1.5 py-0.5 font-mono text-3xs text-fg-muted"
                  title={`Used by role: ${roleId}`}
                >
                  {roleId}
                </span>
              ))}
            </div>
          )}
        </div>
      </div>
    </ScrollArea>
  );
}

/**
 * Heuristic compliance / gotcha hint for tools the user should know about.
 * Kept as plain lookup — no backend round-trip.
 */
function deriveComplianceHint(tool: ToolInfo): string | null {
  if (tool.name === "write_memory" || tool.name === "log_progress") {
    return "Write-path — may block briefly during SQLite contention. Safe to retry.";
  }
  if (tool.name === "session_report") {
    return "Mandatory end-of-session call. Rates the tools you used for telemetry.";
  }
  if (tool.name === "bootstrap") {
    return "Call first in every session. Loads profile, conventions, and project context.";
  }
  if (tool.name.startsWith("keyring_")) {
    return "Touches encrypted vault. Never log returned secret values.";
  }
  return null;
}
