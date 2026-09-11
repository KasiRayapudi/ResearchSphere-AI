import React, { useState, useEffect } from 'react';
import {
  Download,
  CheckCircle2,
  Plus,
  BookOpen,
} from 'lucide-react';
import { Card } from '../components/common/Card';
import { Button } from '../components/common/Button';
import { Badge } from '../components/common/Badge';
import { Modal } from '../components/common/Modal';
import { Input } from '../components/common/Input';
import { ApiService } from '../services/api';
import { useWorkspace } from '../contexts/WorkspaceContext';
import { useToast } from '../contexts/ToastContext';
import { EmptyState, ErrorState, ListSkeleton, Skeleton } from '../components/common/States';
import { Report, Document } from '../types';

export const ReportGeneratorPage: React.FC = () => {
  const [reports, setReports] = useState<Report[]>([]);
  const [selectedReport, setSelectedReport] = useState<Report | null>(null);
  const [documents, setDocuments] = useState<Document[]>([]);
  const [wizardOpen, setWizardOpen] = useState(false);
  const [title, setTitle] = useState('');
  const [objective, setObjective] = useState('');
  const [selectedDocs, setSelectedDocs] = useState<string[]>([]);
  const [isGenerating, setIsGenerating] = useState(false);
  const [isLoadingData, setIsLoadingData] = useState(true);
  const [loadError, setLoadError] = useState<Error | null>(null);

  const { activeWorkspace } = useWorkspace();
  const workspaceId = activeWorkspace?.id;
  const toast = useToast();

  useEffect(() => {
    let cancelled = false;
    setLoadError(null);
    setIsLoadingData(true);
    Promise.all([ApiService.getReports(workspaceId), ApiService.getDocuments(workspaceId)])
      .then(([reps, docs]) => {
        if (cancelled) return;
        setReports(reps);
        setDocuments(docs);
        if (reps.length > 0) setSelectedReport(reps[0]);
      })
      .catch((err) => {
        if (!cancelled) setLoadError(err instanceof Error ? err : new Error('Failed to load'));
      })
      .finally(() => {
        if (!cancelled) setIsLoadingData(false);
      });
    return () => {
      cancelled = true;
    };
  }, [workspaceId]);

  const handleGenerate = async () => {
    if (!title || !objective || isGenerating) return;
    setIsGenerating(true);
    try {
      // Generation runs the full LangGraph workflow server-side and can take a
      // while, so the wizard stays open showing progress rather than closing
      // optimistically before the report exists.
      const newReport = await ApiService.generateReport(
        title,
        objective,
        selectedDocs,
        workspaceId
      );
      setReports((prev) => [newReport, ...prev]);
      setSelectedReport(newReport);
      setWizardOpen(false);
      setTitle('');
      setObjective('');
      setSelectedDocs([]);
      toast.success('Report generated', newReport.title + ' is ready.');
    } catch (err) {
      toast.fromError(err, 'Report generation failed');
    } finally {
      setIsGenerating(false);
    }
  };

  const exportReport = (report: Report, format: 'markdown' | 'json') => {
    // Client-side export: the backend stores reports but exposes no download
    // endpoint yet, so the file is assembled from the report payload.
    const lines = [
      '# ' + report.title,
      '',
      '**Objective:** ' + report.objective,
      '**Author:** ' + report.author,
      '**Generated:** ' + new Date(report.generatedAt).toLocaleString(),
      '',
    ];
    report.sections.forEach((sec) => {
      lines.push('## ' + sec.title, '', sec.content, '');
    });

    const content = format === 'json' ? JSON.stringify(report, null, 2) : lines.join('\n');
    const blob = new Blob([content], {
      type: format === 'json' ? 'application/json' : 'text/markdown',
    });
    const url = URL.createObjectURL(blob);
    const anchor = document.createElement('a');
    anchor.href = url;
    anchor.download =
      report.title.replace(/[^a-z0-9]+/gi, '-').toLowerCase() +
      (format === 'json' ? '.json' : '.md');
    anchor.click();
    URL.revokeObjectURL(url);
    toast.success('Report exported', 'Downloaded as ' + (format === 'json' ? 'JSON' : 'Markdown'));
  };

  if (!activeWorkspace) {
    return (
      <EmptyState
        title="No workspace selected"
        description="Create or select a workspace to generate reports."
      />
    );
  }

  if (isLoadingData) {
    return (
      <div className="space-y-6">
        <Skeleton className="h-8 w-72" />
        <ListSkeleton rows={5} />
      </div>
    );
  }

  if (loadError) {
    return (
      <ErrorState
        title="Could not load reports"
        message={loadError.message}
        onRetry={() => window.location.reload()}
      />
    );
  }

  return (
    <div className="space-y-8 animate-in fade-in duration-300">
      {/* Header */}
      <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-4">
        <div>
          <h1 className="text-2xl font-extrabold text-white tracking-tight flex items-center gap-2">
            Executive Report Generator <Badge variant="brand">{reports.length} Reports</Badge>
          </h1>
          <p className="text-xs text-slate-400 mt-1">
            Synthesize multi-source agent research into publication-ready PDF, Markdown, and HTML reports.
          </p>
        </div>

        <Button
          variant="primary"
          size="sm"
          onClick={() => setWizardOpen(true)}
          icon={<Plus className="h-4 w-4" />}
        >
          Generate New Report
        </Button>
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-12 gap-8">
        {/* Left Report List */}
        <Card className="lg:col-span-4 p-5 space-y-4">
          <h3 className="text-xs font-mono text-slate-400 font-bold uppercase tracking-wider">
            Generated Reports
          </h3>
          <div className="space-y-3">
            {reports.length === 0 && (
              <p className="px-1 py-6 text-center text-xs text-slate-500">
                No reports yet. Generate one to get started.
              </p>
            )}

            {reports.map((r) => (
              <div
                key={r.id}
                onClick={() => setSelectedReport(r)}
                className={`p-3.5 rounded-xl border transition-all cursor-pointer ${
                  selectedReport?.id === r.id
                    ? 'bg-slate-900 border-brand-500/60 shadow-md'
                    : 'bg-slate-950/40 border-slate-800 hover:border-slate-700'
                }`}
              >
                <div className="flex items-center justify-between">
                  <h4 className="text-xs font-bold text-slate-200 truncate">{r.title}</h4>
                  <Badge variant="brand" size="sm">{r.format.toUpperCase()}</Badge>
                </div>
                <p className="text-[11px] text-slate-400 mt-1 line-clamp-2">{r.summary}</p>
                <div className="text-[10px] text-slate-500 font-mono mt-2">
                  By {r.author} • {new Date(r.generatedAt).toLocaleDateString()}
                </div>
              </div>
            ))}
          </div>
        </Card>

        {/* Right Live Previewer */}
        <Card className="lg:col-span-8 p-8 space-y-6 bg-slate-900/90 border-slate-800">
          {selectedReport ? (
            <div className="space-y-6">
              <div className="flex items-center justify-between border-b border-slate-800 pb-4">
                <div>
                  <h2 className="text-xl font-extrabold text-white">{selectedReport.title}</h2>
                  <span className="text-xs text-slate-400 font-mono">
                    Author: {selectedReport.author} • Format: {selectedReport.format.toUpperCase()}
                  </span>
                </div>

                <div className="flex items-center gap-2">
                  <Button
                    variant="outline"
                    size="sm"
                    onClick={() => exportReport(selectedReport, 'markdown')}
                    icon={<Download className="h-3.5 w-3.5 text-brand-400" />}
                  >
                    Export Markdown
                  </Button>
                </div>
              </div>

              {/* Report Body Sections */}
              <div className="space-y-6 text-sm leading-relaxed text-slate-200 font-sans">
                {selectedReport.sections.map((sec, idx) => (
                  <div key={idx} className="space-y-2">
                    <h3 className="text-base font-bold text-brand-300 flex items-center gap-2 border-b border-slate-800/60 pb-1">
                      <BookOpen className="h-4 w-4 text-brand-400" /> {sec.title}
                    </h3>
                    <p className="text-slate-300 text-xs leading-relaxed whitespace-pre-wrap">{sec.content}</p>
                  </div>
                ))}
              </div>
            </div>
          ) : (
            <div className="p-12 text-center text-xs text-slate-500">Select a report to view live preview.</div>
          )}
        </Card>
      </div>

      {/* New Report Wizard Modal */}
      <Modal
        isOpen={wizardOpen}
        onClose={() => setWizardOpen(false)}
        title="Multi-Document Report Generator Wizard"
        description="Select connected documents to synthesize an executive PDF research document."
      >
        <div className="space-y-4 pt-2">
          <Input
            label="Report Title"
            placeholder="e.g. Enterprise RAG & Vector Storage Benchmark"
            value={title}
            onChange={(e) => setTitle(e.target.value)}
          />
          <Input
            label="Research Objective"
            placeholder="Describe key topics, metrics, or comparison criteria..."
            value={objective}
            onChange={(e) => setObjective(e.target.value)}
          />

          <div>
            <label className="text-xs font-semibold uppercase tracking-wider text-slate-400 block mb-2">
              Select Knowledge Source Documents:
            </label>
            <div className="max-h-40 overflow-y-auto space-y-2 border border-slate-800 rounded-xl p-2 bg-slate-950">
              {documents.map((d) => {
                const checked = selectedDocs.includes(d.id);
                return (
                  <div
                    key={d.id}
                    onClick={() => {
                      if (checked) setSelectedDocs(selectedDocs.filter((id) => id !== d.id));
                      else setSelectedDocs([...selectedDocs, d.id]);
                    }}
                    className={`p-2 rounded-lg text-xs flex items-center justify-between cursor-pointer ${
                      checked ? 'bg-brand-500/20 text-white font-semibold' : 'text-slate-300 hover:bg-slate-900'
                    }`}
                  >
                    <span>{d.title}</span>
                    {checked && <CheckCircle2 className="h-4 w-4 text-brand-400" />}
                  </div>
                );
              })}
            </div>
          </div>

          <Button
            variant="primary"
            className="w-full mt-4"
            isLoading={isGenerating}
            onClick={handleGenerate}
          >
            Synthesize Executive PDF Report
          </Button>
        </div>
      </Modal>
    </div>
  );
};
