import React, { useState, useEffect } from 'react';
import {
  FileText,
  Upload,
  Search,
  Filter,
  Trash2,
  Eye,
  CheckCircle2,
  Tag,
  Folder,
  Layers,
  Sparkles,
  RefreshCw,
  Plus,
  FileCode,
} from 'lucide-react';
import { Card } from '../components/common/Card';
import { Button } from '../components/common/Button';
import { Input } from '../components/common/Input';
import { Badge } from '../components/common/Badge';
import { Modal } from '../components/common/Modal';
import { ApiService } from '../services/api';
import { Document } from '../types';

export const DocumentManagerPage: React.FC = () => {
  const [documents, setDocuments] = useState<Document[]>([]);
  const [searchQuery, setSearchQuery] = useState('');
  const [selectedTag, setSelectedTag] = useState<string | null>(null);
  const [isUploading, setIsUploading] = useState(false);
  const [uploadModalOpen, setUploadModalOpen] = useState(false);
  const [previewDoc, setPreviewDoc] = useState<Document | null>(null);

  useEffect(() => {
    const fetchDocs = async () => {
      const docs = await ApiService.getDocuments();
      setDocuments(docs);
    };
    fetchDocs();
  }, []);

  const handleFileUpload = async (file: File) => {
    setIsUploading(true);
    const newDoc = await ApiService.uploadDocument(file);
    setDocuments((prev) => [newDoc, ...prev]);
    setIsUploading(false);
    setUploadModalOpen(false);
  };

  const filteredDocs = documents.filter((d) => {
    const matchesSearch = d.title.toLowerCase().includes(searchQuery.toLowerCase()) || d.tags.some(t => t.toLowerCase().includes(searchQuery.toLowerCase()));
    const matchesTag = selectedTag ? d.tags.includes(selectedTag) : true;
    return matchesSearch && matchesTag;
  });

  const allTags = Array.from(new Set(documents.flatMap((d) => d.tags)));

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
              {filteredDocs.map((doc) => (
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
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Card>

      {/* Upload Modal */}
      <Modal
        isOpen={uploadModalOpen}
        onClose={() => setUploadModalOpen(false)}
        title="Upload & Index Enterprise Document"
        description="Supported formats: PDF, DOCX, TXT, MD, CSV, PPTX. Tesseract OCR runs automatically."
      >
        <div className="space-y-6 pt-2">
          <div
            onDragOver={(e) => e.preventDefault()}
            onDrop={(e) => {
              e.preventDefault();
              if (e.dataTransfer.files.length > 0) {
                handleFileUpload(e.dataTransfer.files[0]);
              }
            }}
            className="border-2 border-dashed border-slate-700 hover:border-brand-500 rounded-2xl p-8 text-center bg-slate-950/60 transition-colors cursor-pointer space-y-3"
          >
            <Upload className="h-10 w-10 text-brand-400 mx-auto" />
            <div className="text-sm font-bold text-white">Drag and drop file here, or click to browse</div>
            <p className="text-xs text-slate-400">PDF, DOCX, MD, CSV up to 100MB per file</p>
            <input
              type="file"
              className="hidden"
              id="file-upload-input"
              onChange={(e) => {
                if (e.target.files && e.target.files[0]) handleFileUpload(e.target.files[0]);
              }}
            />
            <Button
              variant="outline"
              size="sm"
              onClick={() => document.getElementById('file-upload-input')?.click()}
              isLoading={isUploading}
            >
              Select File
            </Button>
          </div>
        </div>
      </Modal>

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
