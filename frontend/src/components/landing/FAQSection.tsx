import React, { useState } from 'react';
import { ChevronDown, HelpCircle } from 'lucide-react';

export const FAQSection: React.FC = () => {
  const [openIndex, setOpenIndex] = useState<number | null>(0);

  const faqs = [
    {
      q: 'How does ResearchSphere AI eliminate LLM hallucinations?',
      a: 'We use a two-stage Hybrid Reciprocal Rank Fusion (RRF) retrieval engine with Qdrant vector database and sparse BM25 indexing. Before final response streaming, a LangGraph Critic Agent checks every generated sentence against retrieved source chunks to ensure 100% grounded citations.',
    },
    {
      q: 'What document formats are supported for document indexing?',
      a: 'Our chunking pipeline supports PDF, DOCX, TXT, Markdown, CSV, and PowerPoint (PPTX). Embedded Tesseract OCR automatically ingests scanned blueprints, tables, and images.',
    },
    {
      q: 'How do Model Context Protocol (MCP) connectors work?',
      a: 'MCP connectors run lightweight synchronization tasks to mirror live state from GitHub repositories, Google Drive folders, and local directory watchdogs directly into Qdrant vector collections with zero manual upload required.',
    },
    {
      q: 'Can I deploy ResearchSphere AI on-premise with Docker?',
      a: 'Yes! The full stack is dockerized with Docker Compose orchestrating FastAPI, React 19, PostgreSQL 16, Qdrant Vector DB, Redis, and Celery background workers.',
    },
    {
      q: 'What AI LLM models can I connect to ResearchSphere AI?',
      a: 'Out of the box, we support Google Gemini 1.5 Pro, OpenAI GPT-4o, Anthropic Claude 3.5 Sonnet, and any OpenAI-compatible API host (e.g. vLLM or Ollama).',
    },
  ];

  return (
    <section id="faq" className="py-24 relative bg-slate-950/60 border-t border-slate-900">
      <div className="max-w-4xl mx-auto px-6">
        <div className="text-center mb-16 space-y-4">
          <span className="px-3 py-1 rounded-full bg-slate-800 border border-slate-700 text-slate-300 text-xs font-mono font-semibold">
            Frequently Asked Questions
          </span>
          <h2 className="text-3xl md:text-4xl font-extrabold text-white tracking-tight">
            Everything You Need to Know About <span className="gradient-text">ResearchSphere AI</span>
          </h2>
        </div>

        <div className="space-y-4">
          {faqs.map((faq, idx) => (
            <div
              key={faq.q}
              className="rounded-xl border border-slate-800 bg-slate-900/80 overflow-hidden transition-colors"
            >
              <button
                onClick={() => setOpenIndex(openIndex === idx ? null : idx)}
                className="w-full px-6 py-4 flex items-center justify-between text-left text-sm font-semibold text-slate-200 hover:text-white cursor-pointer"
              >
                <span>{faq.q}</span>
                <ChevronDown
                  className={`h-4 w-4 text-slate-400 transition-transform ${
                    openIndex === idx ? 'rotate-180 text-brand-400' : ''
                  }`}
                />
              </button>
              {openIndex === idx && (
                <div className="px-6 pb-4 text-xs text-slate-400 leading-relaxed border-t border-slate-800/60 pt-3">
                  {faq.a}
                </div>
              )}
            </div>
          ))}
        </div>
      </div>
    </section>
  );
};
