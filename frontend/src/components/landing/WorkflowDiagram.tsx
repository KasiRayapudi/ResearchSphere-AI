import React, { useState } from 'react';
import { ArrowRight, CheckCircle2, Sparkles, Database, ShieldCheck, FileText } from 'lucide-react';
import { Card } from '../common/Card';

export const WorkflowDiagram: React.FC = () => {
  const [activeStep, setActiveStep] = useState(0);

  const steps = [
    { title: '1. Goal & Context Ingestion', desc: 'User submits research prompt or uploads documents across PDF, DOCX, MD, and CSV files.', details: 'Tesseract OCR runs in background workers to ingest scanned figures and tables into clean markdown chunks.' },
    { title: '2. Multi-Agent Planning', desc: 'Planner Agent breaks target goal into parallel sub-goals and assigns tasks to specialist nodes.', details: 'LangGraph state graph initializes checkpointer state to guarantee zero context loss across multi-turn sessions.' },
    { title: '3. Hybrid RAG Retrieval', desc: 'Dense vector cosine lookup in Qdrant + Sparse BM25 full-text matching in PostgreSQL.', details: 'Reciprocal Rank Fusion (k=60) merges sparse and dense rankings to construct top 25 candidate chunks.' },
    { title: '4. Critic Fact-Checking', desc: 'Critic Agent checks synthesized text against source chunks for 100% grounded consistency.', details: 'If confidence drops below 0.85, an automatic retry loop triggers re-retrieval across connected MCP connectors.' },
    { title: '5. Grounded Citation Output', desc: 'Response is streamed with clickable line-level source citations and auto-generated PDF report.', details: 'End users can inspect original source pages, download structured reports, or ask follow-up questions.' },
  ];

  return (
    <section id="workflow" className="py-24 relative bg-slate-950/60 border-y border-slate-900">
      <div className="max-w-7xl mx-auto px-6">
        <div className="text-center max-w-3xl mx-auto mb-16 space-y-4">
          <span className="px-3 py-1 rounded-full bg-purple-500/10 border border-purple-500/30 text-purple-300 text-xs font-mono font-semibold">
            Interactive LangGraph Workflow
          </span>
          <h2 className="text-3xl md:text-5xl font-extrabold text-white tracking-tight">
            How ResearchSphere AI <span className="gradient-text">Executes Agentic Workflows</span>
          </h2>
        </div>

        <div className="grid grid-cols-1 lg:grid-cols-12 gap-8 items-center">
          {/* Step Selector List */}
          <div className="lg:col-span-5 space-y-3">
            {steps.map((s, idx) => (
              <div
                key={s.title}
                onClick={() => setActiveStep(idx)}
                className={`p-4 rounded-xl border transition-all cursor-pointer ${
                  activeStep === idx
                    ? 'bg-slate-900 border-brand-500/60 shadow-lg shadow-brand-500/10'
                    : 'bg-slate-950/40 border-slate-800 hover:border-slate-700'
                }`}
              >
                <div className="flex items-center justify-between">
                  <h4 className={`font-bold text-sm ${activeStep === idx ? 'text-brand-300' : 'text-slate-200'}`}>
                    {s.title}
                  </h4>
                  {activeStep === idx && <CheckCircle2 className="h-4 w-4 text-brand-400" />}
                </div>
                <p className="text-xs text-slate-400 mt-1">{s.desc}</p>
              </div>
            ))}
          </div>

          {/* Detailed Visualization */}
          <div className="lg:col-span-7">
            <Card className="p-8 border-brand-500/30 bg-slate-900/90 relative overflow-hidden min-h-[340px] flex flex-col justify-between">
              <div className="space-y-4">
                <div className="flex items-center justify-between border-b border-slate-800 pb-4">
                  <span className="text-xs font-mono text-brand-400 font-bold uppercase tracking-wider">
                    Node Stage {activeStep + 1} / 5
                  </span>
                  <span className="px-2.5 py-0.5 rounded-full bg-emerald-500/10 text-emerald-300 text-[11px] font-mono border border-emerald-500/20">
                    State Checkpoint Ready
                  </span>
                </div>
                <h3 className="text-2xl font-bold text-white">{steps[activeStep].title}</h3>
                <p className="text-slate-300 text-sm leading-relaxed">{steps[activeStep].desc}</p>
                <div className="p-4 rounded-lg bg-slate-950 border border-slate-800 text-xs font-mono text-slate-400 leading-normal">
                  <span className="text-purple-400">// Execution Detail:</span> {steps[activeStep].details}
                </div>
              </div>
              <div className="mt-6 flex items-center justify-between text-xs text-slate-500 pt-4 border-t border-slate-800">
                <span>LangGraph 0.2 Engine</span>
                <span className="text-brand-400 font-mono">Status: Stream Ready</span>
              </div>
            </Card>
          </div>
        </div>
      </div>
    </section>
  );
};
