import React from 'react';
import { act, fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

import { UploadQueue } from './UploadQueue';
import { ApiService } from '../../services/api';
import { RESYNC, RealtimeEvent, RealtimeStatus } from '../../services/realtime';
import { RealtimeContext } from '../../contexts/realtimeContextValue';
import type { Document } from '../../types';

/**
 * The queue against a realtime context the test controls: the test decides
 * when status events arrive, as the server would. The API is replaced at
 * the service boundary so no network is involved.
 */
function renderQueue(status: RealtimeStatus = 'open') {
  const listeners = new Set<(event: RealtimeEvent) => void>();
  const value = {
    status,
    online: [],
    subscribe: (listener: (event: RealtimeEvent) => void) => {
      listeners.add(listener);
      return () => {
        listeners.delete(listener);
      };
    },
  };
  render(
    <RealtimeContext.Provider value={value}>
      <UploadQueue workspaceId="ws-1" onUploaded={() => undefined} />
    </RealtimeContext.Provider>
  );
  const push = (event: Partial<RealtimeEvent> & { type: string }) =>
    act(() => {
      listeners.forEach((l) =>
        l({ workspace_id: 'ws-1', data: {}, seq: 0, epoch: 'E', ts: '', actor_id: null, ...event })
      );
    });
  const status_ = (seq: number, data: Record<string, unknown>) =>
    push({ type: 'document.status', seq, data: { id: 'd1', ...data } });
  return { push, status: status_ };
}

function acceptUpload(overrides: Partial<Document> = {}) {
  let resolve!: (doc: Document) => void;
  const pending = new Promise<Document>((r) => (resolve = r));
  vi.spyOn(ApiService, 'uploadDocument').mockReturnValue(pending);
  return (doc: Partial<Document> = {}) =>
    act(async () => {
      resolve({ id: 'd1', title: 'notes.txt', status: 'queued', ...overrides, ...doc } as Document);
      await pending;
    });
}

async function drop(name = 'notes.txt') {
  const input = screen.getByTestId('upload-input');
  await act(async () => {
    fireEvent.change(input, { target: { files: [new File(['hello'], name, { type: 'text/plain' })] } });
  });
}

const bar = () => screen.getByRole('progressbar', { name: /Indexing notes.txt/ });

describe('UploadQueue', () => {
  it('never polls', async () => {
    const intervals = vi.spyOn(window, 'setInterval');
    const statusCalls = vi.spyOn(ApiService, 'getDocumentStatus');
    const finish = acceptUpload();
    renderQueue('open');
    await drop();
    await finish();

    expect(bar()).toHaveAttribute('aria-valuenow', '0');
    expect(intervals).not.toHaveBeenCalled();
    expect(statusCalls).not.toHaveBeenCalled();
  });

  it('moves the bar as status events arrive, then finishes', async () => {
    const finish = acceptUpload();
    const { status } = renderQueue();
    await drop();
    await finish();

    status(5, { status: 'processing', progress: 35 });
    expect(bar()).toHaveAttribute('aria-valuenow', '35');
    expect(screen.getByText('Indexing: splitting into chunks...')).toBeInTheDocument();

    status(6, { status: 'processing', progress: 70 });
    expect(bar()).toHaveAttribute('aria-valuenow', '70');

    status(7, { status: 'indexed', progress: 100 });
    expect(screen.queryByRole('progressbar')).not.toBeInTheDocument();
    expect(screen.getByLabelText('notes.txt indexed')).toBeInTheDocument();
  });

  it('a late, out-of-order event does not move the bar backwards', async () => {
    const finish = acceptUpload();
    const { status } = renderQueue();
    await drop();
    await finish();

    status(8, { status: 'processing', progress: 90 });
    status(6, { status: 'processing', progress: 35 });
    expect(bar()).toHaveAttribute('aria-valuenow', '90');
  });

  it('shows a failure with the server’s reason', async () => {
    const finish = acceptUpload();
    const { status } = renderQueue();
    await drop();
    await finish();

    status(9, { status: 'failed', error: 'PDF is encrypted' });
    expect(screen.getByText('PDF is encrypted')).toBeInTheDocument();
  });

  it('applies events that arrived before the upload response', async () => {
    // A fast worker can finish steps before the browser learns the id.
    const finish = acceptUpload();
    const { status } = renderQueue();
    await drop();
    status(3, { status: 'processing', progress: 70 });
    await finish();

    expect(bar()).toHaveAttribute('aria-valuenow', '70');
  });

  it('asks once, over REST, after a resync -- and not before', async () => {
    const statusCalls = vi.spyOn(ApiService, 'getDocumentStatus').mockResolvedValue({
      id: 'd1',
      status: 'processing',
      progress: 90,
      chunkCount: 0,
      error: null,
      updatedAt: null,
      startedAt: null,
      completedAt: null,
    });
    const finish = acceptUpload();
    const { push } = renderQueue();
    await drop();
    await finish();
    expect(statusCalls).not.toHaveBeenCalled();

    await act(async () => {
      push({ type: RESYNC, data: { reason: 'incomplete' } });
    });
    expect(statusCalls).toHaveBeenCalledOnce();
    expect(bar()).toHaveAttribute('aria-valuenow', '90');
  });

  it('asks once when the upload returns while the socket is down', async () => {
    const statusCalls = vi.spyOn(ApiService, 'getDocumentStatus').mockResolvedValue({
      id: 'd1',
      status: 'indexed',
      progress: 100,
      chunkCount: 4,
      error: null,
      updatedAt: null,
      startedAt: null,
      completedAt: null,
    });
    const finish = acceptUpload();
    renderQueue('reconnecting');
    await drop();
    await finish();

    expect(statusCalls).toHaveBeenCalledOnce();
    expect(screen.getByLabelText('notes.txt indexed')).toBeInTheDocument();
  });

  it('ignores status for documents that are not its own', async () => {
    const finish = acceptUpload();
    const { push } = renderQueue();
    await drop();
    await finish();

    push({ type: 'document.status', seq: 4, data: { id: 'someone-else', status: 'indexed' } });
    expect(bar()).toHaveAttribute('aria-valuenow', '0');
  });

  it('an inline-indexed upload is finished immediately', async () => {
    const finish = acceptUpload({ status: 'indexed' });
    renderQueue();
    await drop();
    await finish();
    expect(screen.queryByRole('progressbar')).not.toBeInTheDocument();
  });
});
