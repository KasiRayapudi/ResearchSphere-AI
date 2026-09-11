import { createContext } from 'react';
import type { RealtimeEvent, RealtimeStatus } from '../services/realtime';

export interface RealtimeContextValue {
  status: RealtimeStatus;
  /** User ids present in the active workspace, as the server reports them. */
  online: string[];
  subscribe: (listener: (event: RealtimeEvent) => void) => () => void;
}

/**
 * What a component sees with no provider above it: never connected, nothing
 * to hear. Lets components render in isolation and in tests without having
 * to special-case a missing socket.
 */
export const DISCONNECTED: RealtimeContextValue = {
  status: 'idle',
  online: [],
  subscribe: () => () => undefined,
};

/**
 * Kept apart from the provider component so that file exports only a
 * component, which is what React Fast Refresh needs to hot-reload it.
 */
export const RealtimeContext = createContext<RealtimeContextValue>(DISCONNECTED);
