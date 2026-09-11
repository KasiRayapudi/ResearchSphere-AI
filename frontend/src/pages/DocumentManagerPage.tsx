import React, { useCallback, useMemo, useRef, useState } from 'react';
import {
  FileText,
  Upload,
  Search,
  Filter,
  Trash2,
  Eye,
} from 'lucide-react';
import { Card } from '../components/common/Card';
import { Button } from '../components/common/Button';
import { Input } from '../components/common/Input';
import { Badge } from '../components/common/Badge';
import { Modal } from '../components/common/Modal';
import { ApiService } from '../services/api';
import { useAsyncData } from '../hooks/useAsyncData';
import { useWorkspace } from '../contexts/WorkspaceContext';
import { useToast } from '../contexts/ToastContext';
import { UploadQueue } from '../components/documents/UploadQueue';
import { EVENTS, RESYNC } from '../services/realtime';
import { useRealtimeEvent } from '../hooks/useRealtime';
import { ConfirmDialog } from '../components/common/ConfirmDialog';
import { EmptyState, ErrorState, ListSkeleton, Skeleton } from '../components/common/States';
import { Document } from '../types';

type SortKey = 'newest' | 'oldest' | 'name' | 'size' | 'chunks';
const PAGE_SIZE = 12;

export const DocumentManagerPage: React.FC = () => {
  const { activeWorkspace, isLoading: workspaceLoading } = useWorkspace();
  const workspaceId = activeWorkspace?.id;
  const toast = useToast();

  const [searchQuery, setSearchQuery] = useState('');
  const [selectedTag, setSelectedTag] = useState<string | null>(null);
  const [statusFilter, setStatusFilter] = useState<string>('all');
  const [sortKey, setSortKey] = useState<SortKey>('newest');
  const [page, setPage] = useState(1);
  const [uploadModalOpen, setUploadModalOpen] = useState(false);
  const [previewDoc, setPreviewDoc] = useState<Document | null>(null);
  const [pendingDelete, setPendingDelete] = useState<Document | null>(null);
  const [isDeleting, setDeleting] = useState(false);

  const {
    data,
    isInitialLoading,
    error,
    refresh,
    setData,
  } = useAsyncData(() => ApiService.getDocuments(workspaceId), [workspaceId], {
    enabled: Boolean(workspaceId),
  });
  const documents = useMemo(() => data ?? [], [data]);

  // Uploads, deletions and indexing progress from anyone in the workspace,
  // applied as they happen. Status is applied per document only when newer
  // than the last one seen: events can arrive out of order, and a late
  // "processing" must not overwrite "indexed".
  const statusSeq = useRef(new Map<string, number>());
  useRealtimeEvent(
    [EVENTS.DOCUMENT_CREATED, EVENTS.DOCUMENT_DELETED, EVENTS.DOCUMENT_STATUS, RESYNC],
    (event) => {
      if (event.type === RESYNC) {
        void refresh();
        return;
      }
      const incoming = event.data as unknown as Document & { id: string };
      if (event.type === EVENTS.DOCUMENT_DELETED) {
        setData((prev) => (prev ?? []).filter((d) => d.id !== incoming.id));
        return;
      }
      if (event.type === EVENTS.DOCUMENT_CREATED) {
        setData((prev) =>
          (prev ?? []).some((d) => d.id === incoming.id) ? prev : [incoming, ...(prev ?? [])]
        );
        return;
      }
      if (event.seq <= (statusSeq.current.get(incoming.id) ?? 0)) return;
      statusSeq.current.set(incoming.id, event.seq);
      setData((prev) =>
        (prev ?? []).map((d) =>
          d.id === incoming.id
            ? { ...d, status: incoming.status, chunkCount: incoming.chunkCount ?? d.chunkCount }
            : d
        )
      );
    }
  );

  const handleUploaded = useCallback(
    (doc: Document & { duplicate?: boolean }) => {
      if (doc.duplicate) {
        toast.info('Already indexed', `“${doc.title}” is already in this workspace.`);
        return;
      }
      setData((prev) => [doc, ...(prev ?? []).filter((d) => d.id !== doc.id)]);
      toast.success('Document indexed', `“${doc.title}” is ready to query.`);
    },
    [setData, toast]
  );

  const confirmDelete = useCallback(async () => {
    if (!pendingDelete) return;
    setDeleting(true);
    try {
      await ApiService.deleteDocument(pendingDelete.id);
      setData((prev) => (prev ?? []).filter((d) => d.id !== pendingDelete.id));
      toast.success('Document deleted', `“${pendingDelete.title}” and its vectors were removed.`);
      setPendingDelete(null);
    } catch (err) {
      toast.fromError(err, 'Could not delete the document');
    } finally {
      setDeleting(false);
    }
  }, [pendingDelete, setData, toast]);

  const filteredDocs = useMemo(() => {
    const q = searchQuery.trim().toLowerCase();
    const rows = documents.filter((d) => {
      const matchesSearch =
        !q ||
        d.title.toLowerCase().includes(q) ||
        (d.tags ?? []).some((t) => t.toLowerCase().includes(q)) ||
        d.fileType.toLowerCase().includes(q);
      const matchesTag = selectedTag ? (d.tags ?? []).includes(selectedTag) : true;
      const matchesStatus = statusFilter === 'all' ? true : d.status === statusFilter;
      return matchesSearch && matchesTag && matchesStatus;
    });

    const sorted = [...rows];
    switch (sortKey) {
      case 'oldest':
        sorted.sort((a, b) => (a.uploadedAt ?? '').localeCompare(b.uploadedAt ?? ''));
        break;
      case 'name':
        sorted.sort((a, b) => a.title.localeCompare(b.title));
        break;
      case 'size':
        sorted.sort((a, b) => b.fileSizeKb - a.fileSizeKb);
        break;
      case 'chunks':
        sorted.sort((a, b) => b.chunkCount - a.chunkCount);
        break;
      default:
        sorted.sort((a, b) => (b.uploadedAt ?? '').localeCompare(a.uploadedAt ?? ''));
    }
    return sorted;
  }, [documents, searchQuery, selectedTag, statusFilter, sortKey]);

  const totalPages = Math.max(1, Math.ceil(filteredDocs.length / PAGE_SIZE));
  // Clamp during render rather than resetting from an effect: when filters
  // shrink the result set, the current page may no longer exist.
  const currentPage = Math.min(page, totalPages);
  const pagedDocs = filteredDocs.slice((currentPage - 1) * PAGE_SIZE, currentPage * PAGE_SIZE);

  const allTags = Array.from(new Set(documents.flatMap((d) => d.tags)));

  if (!workspaceLoading && !activeWorkspace) {
    return (
      <EmptyState
        title="No workspace yet"
        description="Create a workspace from the sidebar before uploading documents."
      />
    );
  }

  if (isInitialLoading || workspaceLoading) {
    return (
      <div className="space-y-6">
        <Skeleton className="h-8 w-72" />
        <Skeleton className="h-16 w-full" />
        <ListSkeleton rows={6} />
      </div>
    );
  }

  if (error) {
    return (
      <ErrorState
        title="Could not load documents"
        message={error.message}
        onRetry={refresh}
      />
    );
  }

  return (
    <div className="space-y-8 animate-in fade-in duration-300">
      {/* Top Header */}
      <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-4">
        <div>
          <h1 className="text-2xl font-extrabold text-white tracking-tight flex items-center gap-2">
            Document Knowledge Store <Badge variant="brand">{documents.length} Files</Badge>
          </h1>
          <p className="text-xs text-slate-400 mt-1">
            Drag-and-drop ingestion with automated OCR, recursive character chunking, and BAAI embeddings.
          </p>
        </div>

        <Button
          variant="primary"
          size="sm"
          onClick={() => setUploadModalOpen(true)}
          icon={<Upload className="h-4 w-4" />}
        >
          Upload & Index Documents
        </Button>
      </div>

      {/* Search & Tag Filter Bar */}
      <Card className="p-4 flex flex-col md:flex-row items-center justify-between gap-4">
        <div className="w-full md:w-96">
          <Input
            placeholder="Search documents by title, content or semantic tag..."
            value={searchQuery}
            onChange={(e) => setSearchQuery(e.target.value)}
            leftIcon={<Search className="h-4 w-4" />}
          />
        </div>

        {/* Tag Filters */}
        <div className="flex flex-wrap items-center gap-2">
          <span className="text-xs text-slate-400 font-semibold flex items-center gap-1">
            <Filter className="h-3.5 w-3.5" /> Filter:
          </span>
          <button
            onClick={() => setSelectedTag(null)}
            className={`px-2.5 py-1 rounded-full text-xs font-mono transition-colors cursor-pointer ${
              selectedTag === null ? 'bg-brand-500 text-white font-bold' : 'bg-slate-800 text-slate-400 hover:text-white'
            }`}
          >
            All Tags
          </button>
          {allTags.map((tag) => (
            <button
              key={tag}
              onClick={() => setSelectedTag(selectedTag === tag ? null : tag)}
              className={`px-2.5 py-1 rounded-full text-xs font-mono transition-colors cursor-pointer ${
                selectedTag === tag ? 'bg-brand-500 text-white font-bold' : 'bg-slate-800 text-slate-400 hover:text-white'
              }`}
            >
              #{tag}
            </button>
          ))}
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <label className="sr-only" htmlFor="doc-status">Filter by status</label>
          <select
            id="doc-status"
            value={statusFilter}
            onChange={(e) => setStatusFilter(e.target.value)}
            className="rounded-lg border border-slate-700 bg-slate-950/60 px-2.5 py-1.5 text-xs text-slate-300 outline-none focus:border-brand-500"
          >
            <option value="all">All statuses</option>
            <option value="indexed">Indexed</option>
            <option value="processing">Processing</option>
            <option value="failed">Failed</option>
          </select>

          <label className="sr-only" htmlFor="doc-sort">Sort documents</label>
          <select
            id="doc-sort"
            value={sortKey}
            onChange={(e) => setSortKey(e.target.value as SortKey)}
            className="rounded-lg border border-slate-700 bg-slate-950/60 px-2.5 py-1.5 text-xs text-slate-300 outline-none focus:border-brand-500"
          >
            <option value="newest">Newest first</option>
            <option value="oldest">Oldest first</option>
            <option value="name">Name A-Z</option>
            <option value="size">Largest first</option>
            <option value="chunks">Most chunks</option>
          </select>
        </div>
      </Card>

      {/* Document Table */}
      <Card className="overflow-hidden p-0">
        <div className="overflow-x-auto">
          <table className="w-full text-left text-xs">
            <thead className="bg-slate-950/80 border-b border-slate-800 text-slate-400 uppercase font-mono font-semibold">
              <tr>
                <th className="p-4">Document Title</th>
                <th className="p-4">Type</th>
                <th className="p-4">Size</th>
                <th className="p-4">Chunks</th>
                <th className="p-4">Status</th>
                <th className="p-4">Tags</th>
                <th className="p-4 text-right">Actions</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-800/60 text-slate-200">
              {pagedDocs.map((doc) => (
                <tr key={doc.id} className="hover:bg-slate-900/60 transition-colors">
                  <td className="p-4 font-semibold text-slate-100 flex items-center gap-3">
                    <div className="p-2 rounded-lg bg-slate-800 text-brand-400 border border-slate-700">
                      <FileText className="h-4 w-4" />
                    </div>
                    <div>
                      <div className="truncate max-w-xs">{doc.title}</div>
                      <div className="text-[10px] text-slate-500 font-mono">
                        {doc.folderPath} • Uploaded by {doc.uploadedBy}
                      </div>
                    </div>
                  </td>
                  <td className="p-4 font-mono uppercase text-slate-400">{doc.fileType}</td>
                  <td className="p-4 font-mono text-slate-400">{(doc.fileSizeKb / 1024).toFixed(1)} MB</td>
                  <td className="p-4 font-mono text-brand-300 font-bold">{doc.chunkCount}</td>
                  <td className="p-4">
                    <Badge variant={doc.status === 'indexed' ? 'success' : 'warning'} size="sm">
                      {doc.status}
                    </Badge>
                  </td>
                  <td className="p-4">
                    <div className="flex flex-wrap gap-1">
                      {doc.tags.map((t) => (
                        <span key={t} className="px-1.5 py-0.5 rounded bg-slate-800 text-[10px] font-mono text-slate-300">
                          #{t}
                        </span>
                      ))}
                    </div>
                  </td>
                  <td className="p-4 text-right">
                    <div className="flex items-center justify-end gap-2">
                      <Button
                        variant="ghost"
                        size="sm"
                        onClick={() => setPreviewDoc(doc)}
                        icon={<Eye className="h-3.5 w-3.5" />}
                      >
                        Inspect
                      </Button>
                      <button
                        onClick={() => setPendingDelete(doc)}
                        aria-label={`Delete ${doc.title}`}
                        title="Delete document"
                        className="rounded-lg p-1.5 text-slate-500 transition-colors hover:bg-slate-800 hover:text-rose-400"
                      >
                        <Trash2 className="h-3.5 w-3.5" />
                      </button>
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>

          {filteredDocs.length === 0 && (
            <div className="p-6">
              <EmptyState
                title={documents.length === 0 ? 'No documents yet' : 'No documents match your filters'}
                description={
                  documents.length === 0
                    ? 'Upload a PDF, DOCX, TXT, Markdown or CSV file to build your knowledge base.'
                    : 'Try a different search term, status or tag.'
                }
                action={
                  documents.length === 0
                    ? { label: 'Upload a document', onClick: () => setUploadModalOpen(true) }
                    : { label: 'Clear filters', onClick: () => {
                        setSearchQuery('');
                        setSelectedTag(null);
                        setStatusFilter('all');
                      } }
                }
              />
            </div>
          )}
        </div>

        {totalPages > 1 && (
          <div className="flex items-center justify-between border-t border-slate-800 px-4 py-3">
            <span className="text-[11px] text-slate-500">
              Showing {(currentPage - 1) * PAGE_SIZE + 1}-
              {Math.min(currentPage * PAGE_SIZE, filteredDocs.length)} of {filteredDocs.length}
            </span>
            <div className="flex items-center gap-2">
              <Button
                variant="ghost"
                size="sm"
                disabled={currentPage === 1}
                onClick={() => setPage(Math.max(1, currentPage - 1))}
              >
                Previous
              </Button>
              <span className="font-mono text-[11px] text-slate-400">
                {currentPage} / {totalPages}
              </span>
              <Button
                variant="ghost"
                size="sm"
                disabled={currentPage === totalPages}
                onClick={() => setPage(Math.min(totalPages, currentPage + 1))}
              >
                Next
              </Button>
            </div>
          </div>
        )}
      </Card>

      {/* Upload Modal */}
      <Modal
        isOpen={uploadModalOpen}
        onClose={() => setUploadModalOpen(false)}
        title="Upload documents"
        description="Files are validated, scanned and de-duplicated before indexing."
        maxWidth="lg"
      >
        <div className="pt-1">
          <UploadQueue
            workspaceId={workspaceId}
            onUploaded={handleUploaded}
          />
        </div>
      </Modal>

      {/* Delete confirmation */}
      <ConfirmDialog
        isOpen={Boolean(pendingDelete)}
        title="Delete this document?"
        description={
          pendingDelete
            ? `“${pendingDelete.title}” and its ${pendingDelete.chunkCount} indexed chunks will be permanently removed. This cannot be undone.`
            : ''
        }
        confirmLabel="Delete document"
        isBusy={isDeleting}
        onConfirm={confirmDelete}
        onCancel={() => setPendingDelete(null)}
      />

      {/* Preview Modal */}
      {previewDoc && (
        <Modal
          isOpen={!!previewDoc}
          onClose={() => setPreviewDoc(null)}
          title={previewDoc.title}
          description={`Indexed Document Metadata & Vector Chunks (${previewDoc.chunkCount} Chunks)`}
        >
          <div className="space-y-4 pt-2">
            <div className="grid grid-cols-2 gap-3 text-xs font-mono bg-slate-950 p-4 rounded-xl border border-slate-800">
              <div><span className="text-slate-500">FileType:</span> {previewDoc.fileType.toUpperCase()}</div>
              <div><span className="text-slate-500">Size:</span> {previewDoc.fileSizeKb} KB</div>
              <div><span className="text-slate-500">Status:</span> {previewDoc.status}</div>
              <div><span className="text-slate-500">OCR Applied:</span> {previewDoc.ocrApplied ? 'Yes' : 'No'}</div>
            </div>
            <div className="p-4 bg-slate-950 rounded-xl border border-slate-800 text-xs text-slate-300 font-mono space-y-2">
              <span className="text-brand-400">// Vector Chunk Sample #001:</span>
              <p className="leading-relaxed">
                "Dense semantic embeddings generated using BAAI/bge-large-en-v1.5. Vector dimensions = 1024. Cosine similarity metric evaluated against Qdrant index."
              </p>
            </div>
          </div>
        </Modal>
      )}
    </div>
  );
};
