import React, { createContext, useCallback, useContext, useMemo, useRef, useState } from 'react';
import { AlertTriangle, CheckCircle2, Info, X, XCircle } from 'lucide-react';
import { ApiError } from '../services/apiClient';

export type ToastVariant = 'success' | 'error' | 'warning' | 'info';

export interface Toast {
  id: string;
  variant: ToastVariant;
  title: string;
  description?: string;
  /** Request id from the backend envelope, shown so users can quote it. */
  requestId?: string;
  action?: { label: string; onClick: () => void };
}

interface ToastContextValue {
  toasts: Toast[];
  push: (toast: Omit<Toast, 'id'>, durationMs?: number) => string;
  dismiss: (id: string) => void;
  success: (title: string, description?: string) => string;
  info: (title: string, description?: string) => string;
  warning: (title: string, description?: string) => string;
  error: (title: string, description?: string) => string;
  /** Turn an unknown thrown value into a useful toast. */
  fromError: (err: unknown, fallbackTitle?: string, action?: Toast['action']) => string;
}

const ToastContext = createContext<ToastContextValue | undefined>(undefined);

const ICONS: Record<ToastVariant, React.ReactNode> = {
  success: <CheckCircle2 className="h-4 w-4 text-emerald-400" />,
  error: <XCircle className="h-4 w-4 text-rose-400" />,
  warning: <AlertTriangle className="h-4 w-4 text-amber-400" />,
  info: <Info className="h-4 w-4 text-brand-400" />,
};

const ACCENTS: Record<ToastVariant, string> = {
  success: 'border-emerald-500/30',
  error: 'border-rose-500/30',
  warning: 'border-amber-500/30',
  info: 'border-brand-500/30',
};

export const ToastProvider: React.FC<{ children: React.ReactNode }> = ({ children }) => {
  const [toasts, setToasts] = useState<Toast[]>([]);
  const timers = useRef<Record<string, number>>({});

  const dismiss = useCallback((id: string) => {
    setToasts((prev) => prev.filter((t) => t.id !== id));
    const timer = timers.current[id];
    if (timer) {
      window.clearTimeout(timer);
      delete timers.current[id];
    }
  }, []);

  const push = useCallback(
    (toast: Omit<Toast, 'id'>, durationMs = 6000) => {
      const id = `t-${Date.now()}-${Math.random().toString(36).slice(2, 7)}`;
      setToasts((prev) => [...prev.slice(-4), { ...toast, id }]);
      if (durationMs > 0) {
        timers.current[id] = window.setTimeout(() => dismiss(id), durationMs);
      }
      return id;
    },
    [dismiss]
  );

  const value = useMemo<ToastContextValue>(() => {
    const make = (variant: ToastVariant) => (title: string, description?: string) =>
      push({ variant, title, description });

    return {
      toasts,
      push,
      dismiss,
      success: make('success'),
      info: make('info'),
      warning: make('warning'),
      error: make('error'),
      fromError: (err: unknown, fallbackTitle = 'Something went wrong', action) => {
        if (err instanceof ApiError) {
          // Field-level validation messages are far more useful than the
          // generic envelope message, so surface them when present.
          const description =
            err.fieldMessages.length > 0 ? err.fieldMessages.join(' ') : undefined;
          return push({
            variant: err.status === 0 ? 'warning' : 'error',
            title: err.message || fallbackTitle,
            description,
            requestId: err.requestId,
            action,
          });
        }
        return push({
          variant: 'error',
          title: fallbackTitle,
          description: err instanceof Error ? err.message : undefined,
          action,
        });
      },
    };
  }, [toasts, push, dismiss]);

  return (
    <ToastContext.Provider value={value}>
      {children}
      <div
        aria-live="polite"
        aria-atomic="false"
        className="pointer-events-none fixed bottom-4 right-4 z-[100] flex w-full max-w-sm flex-col gap-2"
      >
        {toasts.map((t) => (
          <div
            key={t.id}
            role="status"
            className={`pointer-events-auto flex items-start gap-3 rounded-xl border ${ACCENTS[t.variant]} bg-slate-900/95 p-3.5 shadow-2xl backdrop-blur-xl animate-in slide-in-from-bottom-2 fade-in duration-200`}
          >
            <span className="mt-0.5 shrink-0">{ICONS[t.variant]}</span>
            <div className="min-w-0 flex-1">
              <p className="text-sm font-medium text-slate-100">{t.title}</p>
              {t.description && (
                <p className="mt-0.5 text-xs leading-relaxed text-slate-400">{t.description}</p>
              )}
              {t.requestId && (
                <p className="mt-1 font-mono text-[10px] text-slate-600">
                  ref {t.requestId.slice(0, 8)}
                </p>
              )}
              {t.action && (
                <button
                  onClick={() => {
                    t.action!.onClick();
                    dismiss(t.id);
                  }}
                  className="mt-2 text-xs font-semibold text-brand-400 hover:text-brand-300"
                >
                  {t.action.label}
                </button>
              )}
            </div>
            <button
              onClick={() => dismiss(t.id)}
              aria-label="Dismiss notification"
              className="shrink-0 rounded p-1 text-slate-500 transition-colors hover:bg-slate-800 hover:text-slate-300"
            >
              <X className="h-3.5 w-3.5" />
            </button>
          </div>
        ))}
      </div>
    </ToastContext.Provider>
  );
};

export const useToast = () => {
  const ctx = useContext(ToastContext);
  if (!ctx) throw new Error('useToast must be used within ToastProvider');
  return ctx;
};
