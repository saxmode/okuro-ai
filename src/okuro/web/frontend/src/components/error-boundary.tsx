import { Component, type ErrorInfo, type ReactNode } from "react";

interface ErrorBoundaryProps {
  children: ReactNode;
  fallback?: (error: Error, reset: () => void) => ReactNode;
}

interface ErrorBoundaryState {
  error: Error | null;
  componentStack: string | null;
}

/**
 * Top-level error boundary — catches render-time throws so a single
 * component crash can't white-screen the whole webapp.
 *
 * Wraps AppShell (routed pages) and each lazy route. Falls back to a
 * minimal dark-theme card with Reload + Copy-details actions. No stack
 * trace in the UI — copy-to-clipboard exposes it for bug reports.
 */
export class ErrorBoundary extends Component<ErrorBoundaryProps, ErrorBoundaryState> {
  state: ErrorBoundaryState = { error: null, componentStack: null };

  static getDerivedStateFromError(error: Error): Partial<ErrorBoundaryState> {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    // Route to console so devtools + the browser dashboard capture it.
    // Intentionally no backend POST — `/api/client-errors` does not exist.
    // eslint-disable-next-line no-console
    console.error("[ErrorBoundary] render threw:", error, info.componentStack);
    this.setState({ componentStack: info.componentStack ?? null });
  }

  reset = () => {
    this.setState({ error: null, componentStack: null });
  };

  render() {
    if (this.state.error) {
      if (this.props.fallback) {
        return this.props.fallback(this.state.error, this.reset);
      }
      return (
        <DefaultFallback
          error={this.state.error}
          componentStack={this.state.componentStack}
        />
      );
    }
    return this.props.children;
  }
}

function DefaultFallback({
  error,
  componentStack,
}: {
  error: Error;
  componentStack: string | null;
}) {
  const copyDetails = async () => {
    const payload = JSON.stringify(
      {
        message: error.message,
        stack: error.stack ?? null,
        componentStack: componentStack ?? null,
        url: typeof window !== "undefined" ? window.location.href : null,
        userAgent: typeof navigator !== "undefined" ? navigator.userAgent : null,
        ts: new Date().toISOString(),
      },
      null,
      2,
    );
    try {
      await navigator.clipboard.writeText(payload);
    } catch {
      // Best-effort; no toast here because Toaster may be above the boundary.
    }
  };

  const reload = () => {
    if (typeof window !== "undefined") {
      window.location.reload();
    }
  };

  return (
    <div className="flex min-h-[60vh] items-center justify-center p-6">
      <div className="w-full max-w-lg rounded border border-border-subtle bg-surface-elevated p-6">
        <div className="mb-2 text-xs uppercase tracking-widest text-accent">
          Something broke
        </div>
        <div className="mb-4 break-words text-sm text-fg-primary">
          {error.message || "Unknown render error"}
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <button
            type="button"
            onClick={reload}
            className="rounded border border-accent/40 px-3 py-1.5 text-xs text-accent hover:bg-accent-subtle transition-colors"
          >
            Reload page
          </button>
          <button
            type="button"
            onClick={copyDetails}
            className="rounded border border-border-subtle px-3 py-1.5 text-xs text-fg-primary hover:border-accent/50 hover:text-accent transition-colors"
          >
            Copy error details
          </button>
        </div>
      </div>
    </div>
  );
}
