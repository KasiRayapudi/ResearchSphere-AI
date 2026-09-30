import React, { useCallback, useEffect, useMemo, useState } from 'react';
import { REVOKED, RealtimeClient, RealtimeStatus } from '../services/realtime';
import { RealtimeContext, RealtimeContextValue } from './realtimeContextValue';
import { useAuth } from './AuthContext';
import { useWorkspace } from './WorkspaceContext';

/**
 * Owns the one WebSocket for the active workspace.
 *
 * Connects when there is a signed-in user and a workspace, switches when the
 * workspace changes, and closes on sign-out. Screens never touch the socket:
 * they subscribe through `useRealtimeEvent`, so a screen that unmounts simply
 * stops listening and the connection is unaffected.
 */
export const RealtimeProvider: React.FC<{
  children: React.ReactNode;
  /** For tests; the app lets the provider build its own. */
  client?: RealtimeClient;
}> = ({ children, client: injected }) => {
  const { isAuthenticated } = useAuth();
  const { activeWorkspace, refresh: refreshWorkspaces } = useWorkspace();
  const [client] = useState(() => injected ?? new RealtimeClient());
  const [status, setStatus] = useState<RealtimeStatus>(client.status);
  const [online, setOnline] = useState<string[]>(client.online);
  const workspaceId = isAuthenticated ? (activeWorkspace?.id ?? null) : null;

  useEffect(() => client.onStatus(setStatus), [client]);
  useEffect(() => client.onPresence(setOnline), [client]);

  useEffect(() => {
    if (!workspaceId) return;
    client.connect(workspaceId);
    return () => client.disconnect();
  }, [client, workspaceId]);

  // The reason a reconnect is waiting has probably just gone away when the
  // network comes back or the tab is looked at again; skip the backoff.
  useEffect(() => {
    const wake = () => client.reconnectNow();
    const onVisible = () => {
      if (document.visibilityState === 'visible') wake();
    };
    window.addEventListener('online', wake);
    document.addEventListener('visibilitychange', onVisible);
    return () => {
      window.removeEventListener('online', wake);
      document.removeEventListener('visibilitychange', onVisible);
    };
  }, [client]);

  // Access to this workspace was withdrawn while connected -- removed, or
  // the workspace went away. Reloading the list moves the switcher off it.
  useEffect(
    () =>
      client.subscribe((event) => {
        if (event.type === REVOKED) void refreshWorkspaces();
      }),
    [client, refreshWorkspaces]
  );

  // Stable across renders, so subscribers do not churn on every status or
  // presence change.
  const subscribe = useCallback<RealtimeContextValue['subscribe']>(
    (listener) => client.subscribe(listener),
    [client]
  );

  const value = useMemo<RealtimeContextValue>(
    () => ({ status, online, subscribe }),
    [status, online, subscribe]
  );

  return <RealtimeContext.Provider value={value}>{children}</RealtimeContext.Provider>;
};
