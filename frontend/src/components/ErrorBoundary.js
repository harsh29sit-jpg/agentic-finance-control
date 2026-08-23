import React from "react";

/** Route-level error boundary: a crashed page shows a recoverable panel
 *  instead of a white screen. Logs to console with request context. */
class ErrorBoundary extends React.Component {
  constructor(props) {
    super(props);
    this.state = { error: null };
  }

  static getDerivedStateFromError(error) {
    return { error };
  }

  componentDidCatch(error, info) {
    // Hook point for external error tracking (Sentry etc.)
    console.error("[ErrorBoundary]", error, info?.componentStack);
  }

  render() {
    if (this.state.error) {
      return (
        <div className="p-6">
          <div className="card-surface mx-auto mt-10 max-w-lg rounded-md border border-destructive/30 bg-card p-6 text-center">
            <h3 className="text-sm font-bold text-destructive">Something broke on this page</h3>
            <p className="mt-1 text-xs text-muted-foreground">
              {String(this.state.error?.message || this.state.error)}
            </p>
            <div className="mt-4 flex justify-center gap-2">
              <button
                onClick={() => this.setState({ error: null })}
                className="rounded border border-border px-3 py-1.5 text-xs font-semibold hover:border-brand hover:text-brand"
              >
                Try again
              </button>
              <a
                href="/"
                className="rounded bg-brand px-3 py-1.5 text-xs font-semibold text-white hover:bg-brand/90"
              >
                Back to Overview
              </a>
            </div>
          </div>
        </div>
      );
    }
    return this.props.children;
  }
}

export default ErrorBoundary;
