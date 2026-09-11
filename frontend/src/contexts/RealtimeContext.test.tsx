import React from 'react';
import { act, render, screen } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { RealtimeProvider } from './RealtimeContext';
import { useRealtime } from '../hooks/useRealtime';
import { REVOKED, RealtimeClient, RealtimeEvent, RealtimeStatus } from '../services/realtime';

/**
 * The provider's job is lifecycle: connect while signed in with a workspace,
 * follow workspace switches, close on sign-out and unmount. Auth and
 * workspace state are supplied directly; the client is a recorder, since the
 * client's own behaviour is covered in services/realtime.test.ts.
 */
const state = vi.hoisted(() => ({
  auth: { isAuthenticated: true },
  workspace: {
    activeWorkspace: { id: 'ws-1' } as { id: string } | null,
    refresh: vi.fn(async () => undefined),
  },
}));

vi.mock('./AuthContext', () => ({ useAuth: () => state.auth }));
vi.mock('./WorkspaceContext', () => ({ useWorkspace: () => state.workspace }));

function recorder() {
  const listeners = new Set<(event: RealtimeEvent) => void>();
  const statusListeners = new Set<(status: RealtimeStatus) => void>();
  const presenceListeners = new Set<(online: string[]) => void>();
  const client = {
    status: 'idle' as RealtimeStatus,
    online: [] as string[],
    connect: vi.fn(),
    disconnect: vi.fn(),
    reconnectNow: vi.fn(),
    subscribe: vi.fn((listener: (event: RealtimeEvent) => void) => {
      listeners.add(listener);
      return () => {
        listeners.delete(listener);
      };
    }),
    onStatus: (listener: (status: RealtimeStatus) => void) => {
      statusListeners.add(listener);
      return () => {
        statusListeners.delete(listener);
      };
    },
    onPresence: (listener: (online: string[]) => void) => {
      presenceListeners.add(listener);
      return () => {
        presenceListeners.delete(listener);
      };
    },
  };
  return {
    client,
    listeners,
    emit: (type: string) =>
      act(() => {
        listeners.forEach((l) =>
          l({ type, workspace_id: 'ws-1', data: {}, seq: 0, epoch: '', ts: '', actor_id: null })
        );
      }),
    setStatus: (status: RealtimeStatus) =>
      act(() => statusListeners.forEach((l) => l(status))),
    setOnline: (online: string[]) => act(() => presenceListeners.forEach((l) => l(online))),
  };
}

const Consumer: React.FC = () => {
  const { status, online } = useRealtime();
  return (
    <p>
      {status}:{online.join(',')}
    </p>
  );
};

function mount(rec: ReturnType<typeof recorder>) {
  const tree = () => (
    <RealtimeProvider client={rec.client as unknown as RealtimeClient}>
      <Consumer />
    </RealtimeProvider>
  );
  const view = render(tree());
  return { ...view, refresh: () => view.rerender(tree()) };
}

beforeEach(() => {
  state.auth = { isAuthenticated: true };
  state.workspace.activeWorkspace = { id: 'ws-1' };
  state.workspace.refresh.mockClear();
});

describe('RealtimeProvider', () => {
  it('connects to the active workspace', () => {
    const rec = recorder();
    mount(rec);
    expect(rec.client.connect).toHaveBeenCalledWith('ws-1');
  });

  it('follows a workspace switch', () => {
    const rec = recorder();
    const view = mount(rec);
    state.workspace.activeWorkspace = { id: 'ws-2' };
    view.refresh();
    expect(rec.client.disconnect).toHaveBeenCalledTimes(1);
    expect(rec.client.connect).toHaveBeenLastCalledWith('ws-2');
  });

  it('closes the socket on sign-out and does not reconnect', () => {
    const rec = recorder();
    const view = mount(rec);
    state.auth = { isAuthenticated: false };
    view.refresh();
    expect(rec.client.disconnect).toHaveBeenCalledTimes(1);
    expect(rec.client.connect).toHaveBeenCalledTimes(1);
  });

  it('does not connect without a workspace', () => {
    const rec = recorder();
    state.workspace.activeWorkspace = null;
    mount(rec);
    expect(rec.client.connect).not.toHaveBeenCalled();
  });

  it('closes the socket and drops its listeners on unmount', () => {
    const rec = recorder();
    const view = mount(rec);
    view.unmount();
    expect(rec.client.disconnect).toHaveBeenCalledTimes(1);
    expect(rec.listeners.size).toBe(0);
  });

  it('skips the backoff when the network comes back', () => {
    const rec = recorder();
    mount(rec);
    act(() => {
      window.dispatchEvent(new Event('online'));
    });
    expect(rec.client.reconnectNow).toHaveBeenCalledOnce();
  });

  it('reloads the workspace list when access is withdrawn', () => {
    const rec = recorder();
    mount(rec);
    rec.emit(REVOKED);
    expect(state.workspace.refresh).toHaveBeenCalledOnce();
  });

  it('passes status and presence to consumers', () => {
    const rec = recorder();
    mount(rec);
    rec.setStatus('open');
    rec.setOnline(['u1', 'u2']);
    expect(screen.getByText('open:u1,u2')).toBeInTheDocument();
  });
});
