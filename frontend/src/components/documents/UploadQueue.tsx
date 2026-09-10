import React, { useCallback, useEffect, useRef, useState } from 'react';
import { AlertCircle, CheckCircle2, Copy, FileUp, Loader2, UploadCloud, X } from 'lucide-react';
import { ApiService } from '../../services/api';
import { ApiError } from '../../services/apiClient';
import { Button } from '../common/Button';
import { Document } from '../../types';

/** Mirrors the backend allowlist (ALLOWED_UPLOAD_EXTENSIONS). */
export const ACCEPTED_EXTENSIONS = ['pdf', 'docx', 'txt', 'md', 'markdown', 'csv'];
const MAX_SIZE_MB = 50;

/**
 * `indexing` is the state between a successful upload and the document
 * being searchable. The server accepts the file, queues it and returns
 * immediately; a worker does the extraction and embedding, so the client
 * polls until the document reaches a terminal state.
 */
type ItemStatus = 'queued' | 'uploading' | 'indexing' | 'done' | 'duplicate' | 'error';

interface QueueItem {
  id: string;
  file: File;
  status: ItemStatus;
  /** Upload progress while sending, then indexing progress while queued. */
  progress: number;
  message?: string;
  controller?: AbortController;
  /** Set once the server has accepted the file; the key for polling. */
  documentId?: string;
}

/** How often to ask the server for indexing progress. */
const POLL_INTERVAL_MS = 2500;

/**
 * Stop polling after this long. Indexing that has not finished by now has
 * almost certainly failed in a way the server will report on its own; the
 * server-side reaper marks such documents failed, and continuing to poll a
 * document nobody is watching just burns requests.
 */
const POLL_TIMEOUT_MS = 10 * 60 * 1000;

function describe(status: string, progress: number): string {
  if (status === 'queued') return 'Queued for indexing...';
  if (status === 'processing') {
    if (progress >= 90) return 'Indexing: storing vectors...';
    if (progress >= 70) return 'Indexing: generating embeddings...';
    if (progress >= 35) return 'Indexing: splitting into chunks...';
    if (progress >= 20) return 'Indexing: extracting text...';
    return 'Indexing: starting...';
  }
  return 'Indexing...';
}

interface UploadQueueProps {
  workspaceId?: string;
  folder?: string;
  /** Called for each document the backend accepted (including duplicates). */
  onUploaded: (doc: Document & { duplicate?: boolean }) => void;
}

/** Client-side pre-check so obvious rejects never leave the browser. */
function validate(file: File): string | null {
  const ext = file.name.split('.').pop()?.toLowerCase() ?? '';
  if (!ACCEPTED_EXTENSIONS.includes(ext)) {
    return `.${ext || '?'} files are not supported. Allowed: ${ACCEPTED_EXTENSIONS.join(', ')}`;
  }
  if (file.size === 0) return 'File is empty.';
  if (file.size > MAX_SIZE_MB * 1024 * 1024) {
    return `File is larger than the ${MAX_SIZE_MB} MB limit.`;
  }
  return null;
}

export const UploadQueue: React.FC<UploadQueueProps> = ({
  workspaceId,
  folder,
  onUploaded,
}) => {
  const [items, setItems] = useState<QueueItem[]>([]);
  const [isDragging, setDragging] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);

  const update = useCallback((id: string, patch: Partial<QueueItem>) => {
    setItems((prev) => prev.map((it) => (it.id === id ? { ...it, ...patch } : it)));
  }, []);

  const uploadOne = useCallback(
    async (item: QueueItem) => {
      const controller = new AbortController();
      update(item.id, { status: 'uploading', progress: 0, controller });
      try {
        const doc = await ApiService.uploadDocument(item.file, {
          folder,
          workspaceId,
          signal: controller.signal,
          onProgress: (p) => update(item.id, { progress: p }),
        });
        // The backend returns 200 with duplicate:true when identical content
        // already exists, so this is a normal outcome, not an error.
        if ((doc as any)?.duplicate) {
          update(item.id, {
            status: 'duplicate',
            progress: 100,
            message: 'Already in this workspace - not re-indexed.',
          });
        } else if (doc.status === 'indexed') {
          // A deployment without a background worker indexes inline, so the
          // document can already be finished by the time this returns.
          update(item.id, { status: 'done', progress: 100 });
        } else {
          // Accepted and queued. Indexing progress is polled from here.
          update(item.id, {
            status: 'indexing',
            progress: 0,
            documentId: doc.id,
            message: describe(doc.status, 0),
          });
        }
        onUploaded(doc);
      } catch (err) {
        update(item.id, {
          status: 'error',
          message:
            err instanceof ApiError
              ? err.message
              : err instanceof Error
                ? err.message
                : 'Upload failed',
        });
      }
    },
    [folder, workspaceId, onUploaded, update]
  );

  const enqueue = useCallback(
    (files: FileList | File[]) => {
      const next: QueueItem[] = Array.from(files).map((file) => {
        const problem = validate(file);
        return {
          id: `${file.name}-${file.size}-${Date.now()}-${Math.random().toString(36).slice(2, 6)}`,
          file,
          status: problem ? 'error' : 'queued',
          progress: 0,
          message: problem ?? undefined,
        };
      });
      setItems((prev) => [...prev, ...next]);
      // Sequential upload keeps progress readable and avoids hammering the
      // upload rate-limit tier (20/min on the backend).
      void (async () => {
        for (const item of next) {
          if (item.status === 'queued') await uploadOne(item);
        }
      })();
    },
    [uploadOne]
  );

  // Poll every document that is still indexing. One timer for the whole
  // queue rather than one per file: the number of requests then depends on
  // how long indexing takes, not on how many files were dropped at once.
  const indexingIds = items
    .filter((i) => i.status === 'indexing' && i.documentId)
    .map((i) => i.documentId as string)
    .join(',');

  useEffect(() => {
    if (!indexingIds) return;
    let cancelled = false;
    const startedAt = Date.now();

    const tick = async () => {
      const ids = indexingIds.split(',').filter(Boolean);
      await Promise.all(
        ids.map(async (documentId) => {
          try {
            const status = await ApiService.getDocumentStatus(documentId);
            if (cancelled) return;
            setItems((prev) =>
              prev.map((it) => {
                if (it.documentId !== documentId || it.status !== 'indexing') return it;
                if (status.status === 'indexed') {
                  return { ...it, status: 'done', progress: 100, message: undefined };
                }
                if (status.status === 'failed') {
                  return {
                    ...it,
                    status: 'error',
                    message: status.error ?? 'Indexing failed.',
                  };
                }
                return {
                  ...it,
                  progress: status.progress,
                  message: describe(status.status, status.progress),
                };
              })
            );
          } catch {
            // A single failed poll is not meaningful: the next tick retries,
            // and the timeout below stops this eventually.
          }
        })
      );

      if (!cancelled && Date.now() - startedAt > POLL_TIMEOUT_MS) {
        setItems((prev) =>
          prev.map((it) =>
            it.status === 'indexing'
              ? {
                  ...it,
                  status: 'error',
                  message: 'Still indexing after 10 minutes. Check the document list later.',
                }
              : it
          )
        );
      }
    };

    void tick();
    const timer = window.setInterval(() => void tick(), POLL_INTERVAL_MS);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [indexingIds]);

  const cancel = (item: QueueItem) => {
    item.controller?.abort();
    setItems((prev) => prev.filter((i) => i.id !== item.id));
  };

  const isActive = (s: ItemStatus) => s === 'uploading' || s === 'queued' || s === 'indexing';

  const clearFinished = () => setItems((prev) => prev.filter((i) => isActive(i.status)));

  const uploadingCount = items.filter(
    (i) => i.status === 'uploading' || i.status === 'queued'
  ).length;
  const indexingCount = items.filter((i) => i.status === 'indexing').length;

  return (
    <div className="space-y-3">
      <div
        onDragOver={(e) => {
          e.preventDefault();
          setDragging(true);
        }}
        onDragLeave={() => setDragging(false)}
        onDrop={(e) => {
          e.preventDefault();
          setDragging(false);
          if (e.dataTransfer.files?.length) enqueue(e.dataTransfer.files);
        }}
        onClick={() => inputRef.current?.click()}
        onKeyDown={(e) => {
          if (e.key === 'Enter' || e.key === ' ') {
            e.preventDefault();
            inputRef.current?.click();
          }
        }}
        role="button"
        tabIndex={0}
        aria-label="Upload documents by clicking or dropping files"
        className={`flex cursor-pointer flex-col items-center justify-center gap-2 rounded-xl border-2 border-dashed p-8 text-center transition-colors focus:outline-none focus:ring-2 focus:ring-brand-500/50 ${
          isDragging
            ? 'border-brand-500 bg-brand-500/10'
            : 'border-slate-700 bg-slate-900/40 hover:border-slate-600'
        }`}
      >
        <UploadCloud
          className={`h-8 w-8 ${isDragging ? 'text-brand-400' : 'text-slate-500'}`}
        />
        <div>
          <p className="text-sm font-medium text-slate-200">
            {isDragging ? 'Drop to upload' : 'Drag files here or click to browse'}
          </p>
          <p className="mt-1 text-[11px] text-slate-500">
            {ACCEPTED_EXTENSIONS.join(', ').toUpperCase()} · up to {MAX_SIZE_MB} MB each
          </p>
        </div>
        <input
          ref={inputRef}
          type="file"
          multiple
          hidden
          accept={ACCEPTED_EXTENSIONS.map((e) => `.${e}`).join(',')}
          onChange={(e) => {
            if (e.target.files?.length) enqueue(e.target.files);
            e.target.value = '';
          }}
        />
      </div>

      {items.length > 0 && (
        <div className="rounded-xl border border-slate-800 bg-slate-900/40">
          <div className="flex items-center justify-between border-b border-slate-800 px-3 py-2">
            <span className="text-[11px] font-medium text-slate-400">
              {uploadingCount > 0
                ? `Uploading ${uploadingCount} file(s)`
                : indexingCount > 0
                  ? `Indexing ${indexingCount} file(s)`
                  : 'Upload queue'}
            </span>
            <Button variant="ghost" size="sm" onClick={clearFinished}>
              Clear finished
            </Button>
          </div>
          <ul className="max-h-64 space-y-1 overflow-y-auto p-2">
            {items.map((item) => (
              <li key={item.id} className="rounded-lg bg-slate-900/60 p-2.5">
                <div className="flex items-center gap-2.5">
                  <span className="shrink-0">
                    {item.status === 'uploading' && (
                      <Loader2 className="h-4 w-4 animate-spin text-brand-400" />
                    )}
                    {item.status === 'queued' && <FileUp className="h-4 w-4 text-slate-500" />}
                    {item.status === 'indexing' && (
                      <Loader2 className="h-4 w-4 animate-spin text-amber-400" />
                    )}
                    {item.status === 'done' && (
                      <CheckCircle2 className="h-4 w-4 text-emerald-400" />
                    )}
                    {item.status === 'duplicate' && <Copy className="h-4 w-4 text-amber-400" />}
                    {item.status === 'error' && <AlertCircle className="h-4 w-4 text-rose-400" />}
                  </span>
                  <div className="min-w-0 flex-1">
                    <p className="truncate text-xs text-slate-200">{item.file.name}</p>
                    {item.message && (
                      <p
                        className={`text-[10px] ${
                          item.status === 'error' ? 'text-rose-400' : 'text-amber-400'
                        }`}
                      >
                        {item.message}
                      </p>
                    )}
                  </div>
                  <span className="shrink-0 font-mono text-[10px] text-slate-500">
                    {(item.file.size / 1024).toFixed(0)} KB
                  </span>
                  {item.status === 'uploading' && (
                    <button
                      onClick={() => cancel(item)}
                      aria-label={`Cancel upload of ${item.file.name}`}
                      className="shrink-0 rounded p-1 text-slate-500 hover:bg-slate-800 hover:text-rose-400"
                    >
                      <X className="h-3 w-3" />
                    </button>
                  )}
                </div>
                {(item.status === 'uploading' || item.status === 'indexing') && (
                  <div
                    className="mt-2 h-1 overflow-hidden rounded-full bg-slate-800"
                    role="progressbar"
                    aria-label={
                      item.status === 'indexing'
                        ? `Indexing ${item.file.name}`
                        : `Uploading ${item.file.name}`
                    }
                    aria-valuenow={item.progress}
                    aria-valuemin={0}
                    aria-valuemax={100}
                  >
                    <div
                      className={`h-full rounded-full transition-all duration-200 ${
                        item.status === 'indexing' ? 'bg-amber-500' : 'bg-brand-500'
                      }`}
                      style={{ width: `${item.progress}%` }}
                    />
                  </div>
                )}
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
};
