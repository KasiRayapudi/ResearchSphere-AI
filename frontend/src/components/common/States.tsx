import React from 'react';
import { AlertCircle, Inbox, RefreshCw } from 'lucide-react';
import { Button } from './Button';

/** Shimmering placeholder block. */
export const Skeleton: React.FC<{ className?: string }> = ({ className = '' }) => (
  <div
    aria-hidden="true"
    className={`animate-pulse rounded-md bg-slate-800/70 ${className}`}
  />
);

/** Skeleton shaped like a stat card. */
export const StatCardSkeleton: React.FC = () => (
  <div className="rounded-xl border border-slate-800/80 bg-slate-900/60 p-5">
    <Skeleton className="h-3 w-24" />
    <Skeleton className="mt-3 h-7 w-20" />
    <Skeleton className="mt-3 h-2 w-32" />
  </div>
);

/** Skeleton shaped like a list of rows. */
export const ListSkeleton: React.FC<{ rows?: number }> = ({ rows = 4 }) => (
  <div className="space-y-2">
    {Array.from({ length: rows }).map((_, i) => (
      <div
        key={i}
        className="flex items-center gap-3 rounded-lg border border-slate-800/60 bg-slate-900/40 p-3"
      >
        <Skeleton className="h-9 w-9 rounded-lg" />
        <div className="flex-1 space-y-2">
          <Skeleton className="h-3 w-2/5" />
          <Skeleton className="h-2 w-1/4" />
        </div>
      </div>
    ))}
  </div>
);

export const TableSkeleton: React.FC<{ rows?: number; cols?: number }> = ({
  rows = 6,
  cols = 5,
}) => (
  <div className="space-y-2">
    {Array.from({ length: rows }).map((_, r) => (
      <div key={r} className="grid gap-3" style={{ gridTemplateColumns: `repeat(${cols}, 1fr)` }}>
        {Array.from({ length: cols }).map((_, c) => (
          <Skeleton key={c} className="h-8" />
        ))}
      </div>
    ))}
  </div>
);

interface EmptyStateProps {
  icon?: React.ReactNode;
  title: string;
  description?: string;
  action?: { label: string; onClick: () => void; icon?: React.ReactNode };
  className?: string;
}

export const EmptyState: React.FC<EmptyStateProps> = ({
  icon,
  title,
  description,
  action,
  className = '',
}) => (
  <div
    className={`flex flex-col items-center justify-center gap-3 rounded-xl border border-dashed border-slate-800 bg-slate-900/30 px-6 py-12 text-center ${className}`}
  >
    <div className="rounded-full bg-slate-800/60 p-3 text-slate-500">
      {icon ?? <Inbox className="h-6 w-6" />}
    </div>
    <div>
      <h3 className="text-sm font-semibold text-slate-200">{title}</h3>
      {description && (
        <p className="mx-auto mt-1 max-w-sm text-xs leading-relaxed text-slate-500">
          {description}
        </p>
      )}
    </div>
    {action && (
      <Button size="sm" variant="secondary" onClick={action.onClick} icon={action.icon}>
        {action.label}
      </Button>
    )}
  </div>
);

interface ErrorStateProps {
  title?: string;
  message?: string;
  onRetry?: () => void;
  className?: string;
}

/** Shown when a real request failed - never replaced with placeholder data. */
export const ErrorState: React.FC<ErrorStateProps> = ({
  title = 'Could not load this data',
  message,
  onRetry,
  className = '',
}) => (
  <div
    role="alert"
    className={`flex flex-col items-center justify-center gap-3 rounded-xl border border-rose-500/25 bg-rose-500/5 px-6 py-10 text-center ${className}`}
  >
    <AlertCircle className="h-6 w-6 text-rose-400" />
    <div>
      <h3 className="text-sm font-semibold text-slate-200">{title}</h3>
      {message && (
        <p className="mx-auto mt-1 max-w-md text-xs leading-relaxed text-slate-400">{message}</p>
      )}
    </div>
    {onRetry && (
      <Button
        size="sm"
        variant="secondary"
        onClick={onRetry}
        icon={<RefreshCw className="h-3.5 w-3.5" />}
      >
        Retry
      </Button>
    )}
  </div>
);

/** Full-page loader used while the session bootstraps. */
export const FullPageLoader: React.FC<{ label?: string }> = ({ label = 'Loading' }) => (
  <div className="flex min-h-screen flex-col items-center justify-center gap-4 bg-slate-950">
    <div className="relative h-10 w-10">
      <div className="absolute inset-0 rounded-full border-2 border-slate-800" />
      <div className="absolute inset-0 animate-spin rounded-full border-2 border-transparent border-t-brand-500" />
    </div>
    <p className="text-xs font-medium tracking-wide text-slate-500">{label}</p>
  </div>
);
