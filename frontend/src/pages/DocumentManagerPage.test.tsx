import React from 'react';
import { act, render, screen, waitFor } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

import { DocumentManagerPage } from './DocumentManagerPage';
import { ApiService } from '../services/api';
import { RESYNC, RealtimeEvent } from '../services/realtime';
import { RealtimeContext } from '../contexts/realtimeContextValue';
import { ToastProvider } from '../contexts/ToastContext';
import type { Document } from '../types';

const workspace = vi.hoisted(() => ({
  value: { activeWorkspace: { id: 'ws-1', name: 'Research' }, isLoading: false },
}));
vi.mock('../contexts/WorkspaceContext', () => ({ useWorkspace: () => workspace.value }));

const doc = (id: string, title: string, status: Document['status'] = 'indexed'): Document => ({
  id,
  title,
  fileType: 'txt',
  fileSizeKb: 1,
  status,
  chunkCount: 1,
  tags: [],
  uploadedBy: 'Ann',
  uploadedAt: '2026-09-11T00:00:00',
  version: 1,
  ocrApplied: false,
  folderPath: '/Uploads',
});

async function renderPage() {
  const loads = vi.spyOn(ApiService, 'getDocuments').mockResolvedValue([doc('a', 'Alpha', 'processing')]);
  const listeners = new Set<(event: RealtimeEvent) => void>();
  const value = {
    status: 'open' as const,
    online: [],
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
        <DocumentManagerPage />
      </RealtimeContext.Provider>
    </ToastProvider>
  );
  await screen.findByText('Alpha');
  const push = (type: string, seq: number, data: Record<string, unknown>) =>
    act(() => {
      listeners.forEach((l) =>
        l({ type, seq, data, workspace_id: 'ws-1', epoch: 'E', ts: '', actor_id: 'someone' })
      );
    });
  return { push, loads };
}

describe('DocumentManagerPage with live events', () => {
  it('shows a document someone else uploaded', async () => {
    const { push } = await renderPage();
    push('document.created', 4, { ...doc('b', 'Beta', 'queued') });
    expect(await screen.findByText('Beta')).toBeInTheDocument();
  });

  it('removes a document someone else deleted', async () => {
    const { push } = await renderPage();
    push('document.deleted', 4, { id: 'a' });
    await waitFor(() => expect(screen.queryByText('Alpha')).not.toBeInTheDocument());
  });

  it('updates status in place, and ignores an older update that arrives late', async () => {
    const { push } = await renderPage();
    push('document.status', 7, { id: 'a', status: 'indexed', chunkCount: 12 });
    expect(await screen.findByText('indexed')).toBeInTheDocument();
    expect(screen.getByText('12')).toBeInTheDocument();

    push('document.status', 5, { id: 'a', status: 'processing', chunkCount: 3 });
    expect(screen.getByText('indexed')).toBeInTheDocument();
    expect(screen.queryByText('processing')).not.toBeInTheDocument();
  });

  it('does not duplicate a document it already has', async () => {
    const { push } = await renderPage();
    push('document.created', 4, { ...doc('a', 'Alpha', 'processing') });
    expect(screen.getAllByText('Alpha')).toHaveLength(1);
  });

  it('refetches on a resync', async () => {
    const { push, loads } = await renderPage();
    push(RESYNC, 0, { reason: 'incomplete' });
    await waitFor(() => expect(loads).toHaveBeenCalledTimes(2));
  });
});
