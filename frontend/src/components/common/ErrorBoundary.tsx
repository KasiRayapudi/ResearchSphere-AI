import React from 'react';
import { AlertTriangle, RefreshCw } from 'lucide-react';
import { Button } from './Button';

interface Props {
  children: React.ReactNode;
  /** Shown instead of the default panel. */
  fallback?: React.ReactNode;
  /** Label used in the default panel, e.g. "Analytics". */
  section?: string;
}

interface State {
  error: Error | null;
}

/**
 * Catches render-time errors so one broken widget cannot blank the whole app.
 * Wrap route content and any independently-failing panel.
 */
export class ErrorBoundary extends React.Component<Props, State> {
  state: State = { error: null };

  static getDerivedStateFromError(error: Error): State {
    return { error };
  }

  componentDidCatch(error: Error, info: React.ErrorInfo) {
    // Kept to console: there is no frontend error-reporting sink yet.
    console.error('[ErrorBoundary]', this.props.section ?? 'app', error, info.componentStack);
  }

  private reset = () => this.setState({ error: null });

  render() {
    if (!this.state.error) return this.props.children;
    if (this.props.fallback) return <>{this.props.fallback}</>;

    return (
      <div
        role="alert"
        className="flex min-h-[240px] flex-col items-center justify-center gap-3 rounded-xl border border-rose-500/25 bg-rose-500/5 p-8 text-center"
      >
        <AlertTriangle className="h-8 w-8 text-rose-400" />
        <div>
          <h3 className="text-sm font-semibold text-slate-100">
            {this.props.section ? `${this.props.section} failed to render` : 'Something went wrong'}
          </h3>
          <p className="mx-auto mt-1 max-w-md text-xs leading-relaxed text-slate-400">
            This section hit an unexpected error. The rest of the application is unaffected.
          </p>
          <p className="mt-2 font-mono text-[10px] text-slate-600">{this.state.error.message}</p>
        </div>
        <Button
          size="sm"
          variant="secondary"
          onClick={this.reset}
          icon={<RefreshCw className="h-3.5 w-3.5" />}
        >
          Try again
        </Button>
      </div>
    );
  }
}
