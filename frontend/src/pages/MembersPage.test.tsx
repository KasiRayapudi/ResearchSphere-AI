import React from 'react';
import { act, render, screen, waitFor } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

import { MembersPage } from './MembersPage';
import { ApiService } from '../services/api';
import { RealtimeEvent } from '../services/realtime';
import { RealtimeContext } from '../contexts/realtimeContextValue';
import { ToastProvider } from '../contexts/ToastContext';
import type { WorkspaceMember } from '../types';

const state = vi.hoisted(() => ({
  workspace: {
    activeWorkspace: { id: 'ws-1', name: 'Research' },
    refresh: async () => undefined,
  },
  auth: { user: { id: 'me' } },
}));
vi.mock('../contexts/WorkspaceContext', () => ({ useWorkspace: () => state.workspace }));
vi.mock('../contexts/AuthContext', () => ({ useAuth: () => state.auth }));

const member = (id: string, userId: string, fullName: string, role: WorkspaceMember['role']) => ({
  id,
  userId,
  email: `${userId}@example.com`,
  fullName,
  role,
  joinedAt: '2026-09-11T00:00:00',
  invitedBy: null,
});

async function renderPage(online: string[] = []) {
  const members = vi
    .spyOn(ApiService, 'getMembers')
    .mockResolvedValue([member('m1', 'me', 'Me', 'owner'), member('m2', 'you', 'You', 'editor')]);
  const invitations = vi.spyOn(ApiService, 'getInvitations').mockResolvedValue([]);
  const listeners = new Set<(event: RealtimeEvent) => void>();
  const value = {
    status: 'open' as const,
    online,
    subscribe: (listener: (event: RealtimeEvent) => void) => {
      listeners.add(listener);
      return () => {
        listeners.delete(listener);
      };
    },
  };
  render(
    <ToastProvider>
      <RealtimeContext.Provider value={value}>
        <MembersPage />
      </RealtimeContext.Provider>
    </ToastProvider>
  );
  await screen.findByText('You');
  await waitFor(() => expect(invitations).toHaveBeenCalledTimes(1));
  const push = (type: string, actor: string | null = 'someone-else') =>
    act(() => {
      listeners.forEach((l) =>
        l({ type, seq: 5, data: {}, workspace_id: 'ws-1', epoch: 'E', ts: '', actor_id: actor })
      );
    });
  return { push, members, invitations };
}

describe('MembersPage with live events', () => {
  it('shows who is online', async () => {
    await renderPage(['you']);
    expect(screen.getAllByLabelText('online')).toHaveLength(1);
  });

  it("refetches members when someone else changes them", async () => {
    const { push, members } = await renderPage();
    push('member.added');
    await waitFor(() => expect(members).toHaveBeenCalledTimes(2));
  });

  it('refetches invitations on an invitation event', async () => {
    const { push, invitations } = await renderPage();
    push('invitation.sent');
    await waitFor(() => expect(invitations).toHaveBeenCalledTimes(2));
  });

  it('does not refetch for the echo of its own change', async () => {
    const { push, members } = await renderPage();
    push('member.role_changed', 'me');
    await act(async () => undefined);
    expect(members).toHaveBeenCalledTimes(1);
  });
});
