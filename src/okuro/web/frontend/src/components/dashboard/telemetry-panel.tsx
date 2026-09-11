import { displayAgent } from "@/lib/format";

interface TelemetryData {
  heatmap: Record<string, Record<string, number>>;
  providers: string[];
  hours: number[];
}

const PROVIDER_COLORS: Record<string, string> = {
  "claude-code": "bg-accent",
  gemini: "bg-info",
  codex: "bg-warning",
  cursor: "bg-fg-muted",
};

/**
 * Telemetry heatmap — hours (x) × providers (y), intensity by call count.
 */
export function TelemetryPanel({ data }: { data: TelemetryData | undefined }) {
  if (!data || data.providers.length === 0) {
    return <p className="text-2xs text-tertiary">No telemetry data</p>;
  }

  const providers = data.providers.filter(
    (p) => p && p.toLowerCase() !== "unknown",
  );

  // Find max for normalization
  let maxCount = 1;
  for (const provider of providers) {
    const hourMap = data.heatmap[provider] ?? {};
    for (const h of data.hours) {
      const c = hourMap[String(h)] ?? 0;
      if (c > maxCount) maxCount = c;
    }
  }

  return (
    <div className="space-y-1">
      {/* Hour labels */}
      <div className="flex gap-[1px] pl-16">
        {data.hours
          .filter((h) => h % 4 === 0)
          .map((h) => (
            <span
              key={h}
              className="text-3xs text-tertiary"
              style={{ width: `${(4 / 24) * 100}%` }}
            >
              {String(h).padStart(2, "0")}
            </span>
          ))}
      </div>

      {/* Provider rows */}
      {providers.map((provider) => {
        const hourMap = data.heatmap[provider] ?? {};
        return (
          <div key={provider} className="flex items-center gap-1">
            <span
              className="w-14 shrink-0 truncate text-3xs text-tertiary text-right pr-1"
              title={provider}
            >
              {displayAgent(provider.replace("claude-code", "claude"))}
            </span>
            <div className="flex flex-1 gap-[1px]">
              {data.hours.map((h) => {
                const count = hourMap[String(h)] ?? 0;
                const alpha = count > 0 ? 0.2 + (count / maxCount) * 0.8 : 0;
                const color = PROVIDER_COLORS[provider] ?? "bg-accent";
                return (
                  <div
                    key={h}
                    className={`h-3 flex-1 rounded-[1px] ${count > 0 ? color : "bg-border/30"}`}
                    style={{ opacity: count > 0 ? alpha : 0.3 }}
                    title={`${provider} ${String(h).padStart(2, "0")}:00 — ${count} calls`}
                  />
                );
              })}
            </div>
          </div>
        );
      })}
    </div>
  );
}
