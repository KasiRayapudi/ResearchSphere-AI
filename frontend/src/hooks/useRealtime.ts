import { useContext, useEffect, useRef } from 'react';
import { RealtimeContext, RealtimeContextValue } from '../contexts/realtimeContextValue';
import type { RealtimeEvent } from '../services/realtime';

/** Connection status, presence and the raw subscribe for the active workspace. */
export function useRealtime(): RealtimeContextValue {
  return useContext(RealtimeContext);
}

/**
 * Run `handler` for every event of the given types.
 *
 * The handler may be a fresh closure on every render -- it is read through a
 * ref -- so only a change in `types` resubscribes. That keeps a screen from
 * dropping events in the gap between unsubscribing and subscribing again.
 */
export function useRealtimeEvent(
  types: readonly string[],
  handler: (event: RealtimeEvent) => void
): void {
  const { subscribe } = useContext(RealtimeContext);
  const handlerRef = useRef(handler);
  useEffect(() => {
    handlerRef.current = handler;
  });

  const key = types.join('|');
  useEffect(() => {
    const wanted = new Set(key.split('|'));
    return subscribe((event) => {
      if (wanted.has(event.type)) handlerRef.current(event);
    });
  }, [subscribe, key]);
}
