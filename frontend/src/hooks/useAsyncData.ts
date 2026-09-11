import { DependencyList, useCallback, useEffect, useRef, useState } from 'react';

export interface AsyncState<T> {
  data: T | null;
  isLoading: boolean;
  /** True only for the first load; refreshes keep the previous data visible. */
  isInitialLoading: boolean;
  error: Error | null;
  refresh: () => Promise<void>;
  setData: React.Dispatch<React.SetStateAction<T | null>>;
}

/**
 * Load data from the API with loading/error/retry state.
 *
 * Replaces the previous pattern where every page swallowed failures and
 * rendered mock data instead, which made outages invisible. Stale results from
 * a superseded request are discarded so switching workspaces quickly cannot
 * leave the wrong data on screen.
 */
export function useAsyncData<T>(
  loader: () => Promise<T>,
  deps: DependencyList = [],
  options: { enabled?: boolean } = {}
): AsyncState<T> {
  const { enabled = true } = options;
  const [data, setData] = useState<T | null>(null);
  const [isLoading, setLoading] = useState(true);
  const [hasLoadedOnce, setHasLoadedOnce] = useState(false);
  const [error, setError] = useState<Error | null>(null);

  // Guards against a slow earlier request resolving after a newer one.
  const requestId = useRef(0);
  const mounted = useRef(true);
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  // Assigned in an effect, not during render: writing a ref while rendering
  // is unsafe under concurrent rendering.
  const loaderRef = useRef(loader);
  useEffect(() => {
    loaderRef.current = loader;
  });

  const run = useCallback(async () => {
    const id = ++requestId.current;
    setLoading(true);
    setError(null);
    try {
      const result = await loaderRef.current();
      if (!mounted.current || id !== requestId.current) return;
      setData(result);
    } catch (err) {
      if (!mounted.current || id !== requestId.current) return;
      setError(err instanceof Error ? err : new Error('Request failed'));
    } finally {
      if (mounted.current && id === requestId.current) {
        setLoading(false);
        setHasLoadedOnce(true);
      }
    }
  }, []);

  useEffect(() => {
    if (!enabled) return;
    // Kicking off a request is exactly the "synchronize with an external
    // system" case effects exist for. The state updates happen inside `run`,
    // which is the single place this pattern lives in the codebase.
    void run();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [enabled, ...deps]);

  // Derived rather than stored, so a disabled query never needs a setState.
  const effectiveLoading = enabled && isLoading;

  return {
    data,
    isLoading: effectiveLoading,
    isInitialLoading: effectiveLoading && !hasLoadedOnce,
    error,
    refresh: run,
    setData,
  };
}
