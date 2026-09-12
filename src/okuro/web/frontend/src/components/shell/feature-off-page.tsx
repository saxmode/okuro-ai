import { useLocation } from "react-router";
import { PageHeader } from "@/components/shell/page-header";
import { useFeatures } from "@/lib/features-context";
import { featureForRoute } from "@/lib/features-api";

/**
 * What a withheld route renders instead of its page.
 *
 * A DIRECT URL MUST EXPLAIN ITSELF. The CLI and MCP seams hide a feature by
 * absence — the command is not in `--help`, the tool is not in the list — and
 * absence is a complete answer there. The SPA cannot do that: it ships as one
 * bundle, its router already knows every path, and an unknown path in okuro
 * does not 404 anyway (web/app.py's `spa_fallback` answers 200 text/html for
 * everything under the SPA). So the honest options are "a page that looks
 * broken" or "a page that says which switch to flip". This is the second.
 *
 * It names the config key so the answer is actionable without a support
 * round trip, and says nothing about WHY the feature is off — that is the
 * install owner's business, not this page's.
 */
export function FeatureOffPage() {
  const { pathname } = useLocation();
  const { features } = useFeatures();
  const name = featureForRoute(pathname, features);
  const summary = name ? features[name]?.summary : undefined;

  return (
    <div className="page-shell space-y-6">
      <PageHeader
        title="Switched off"
        subtitle="This feature is not enabled on this install"
      />

      <div className="max-w-prose space-y-4 text-sm text-tertiary">
        {summary && <p className="text-fg-muted">{summary}</p>}

        <p>
          This feature is switched off on this install. Enable it in{" "}
          <code className="rounded bg-surface-elevated px-1 py-0.5 text-xs text-fg">
            ~/.okuro/config.yaml
          </code>{" "}
          under:
        </p>

        <pre className="overflow-x-auto rounded border border-border bg-surface-elevated p-3 text-xs text-fg">
          {`features:\n  ${name ?? "<name>"}: true`}
        </pre>

        <p>
          Then reload this page. Run{" "}
          <code className="rounded bg-surface-elevated px-1 py-0.5 text-xs text-fg">
            okuro features
          </code>{" "}
          to see every switch and its current state.
        </p>
      </div>
    </div>
  );
}
