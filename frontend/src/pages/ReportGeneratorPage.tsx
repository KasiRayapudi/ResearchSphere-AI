import React, { useState, useEffect } from 'react';
import {
  Layers,
  FileText,
  Download,
  CheckCircle2,
  Plus,
  Sparkles,
  Printer,
  Share2,
  Copy,
  BookOpen,
} from 'lucide-react';
import { Card } from '../components/common/Card';
import { Button } from '../components/common/Button';
import { Badge } from '../components/common/Badge';
import { Modal } from '../components/common/Modal';
import { Input } from '../components/common/Input';
import { ApiService } from '../services/api';
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

  useEffect(() => {
    const fetchData = async () => {
      const [reps, docs] = await Promise.all([
        ApiService.getReports(),
        ApiService.getDocuments(),
      ]);
      setReports(reps);
      setDocuments(docs);
      if (reps.length > 0) setSelectedReport(reps[0]);
    };
    fetchData();
  }, []);

  const handleGenerate = async () => {
    if (!title || !objective) return;
    setIsGenerating(true);
    const newReport = await ApiService.generateReport(title, objective, selectedDocs);
    setReports((prev) => [newReport, ...prev]);
    setSelectedReport(newReport);
    setIsGenerating(false);
    setWizardOpen(false);
    setTitle('');
    setObjective('');
  };

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
                    onClick={() => alert(`Downloading PDF for ${selectedReport.title}`)}
                    icon={<Download className="h-3.5 w-3.5 text-brand-400" />}
                  >
                    Export PDF
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
