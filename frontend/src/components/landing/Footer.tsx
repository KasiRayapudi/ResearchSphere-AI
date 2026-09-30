import React from 'react';
import { NavLink } from 'react-router-dom';
import { Sparkles, Github, Twitter, Linkedin } from 'lucide-react';

export const Footer: React.FC = () => {
  return (
    <footer className="bg-slate-950 border-t border-slate-900 py-16 text-slate-400 text-xs">
      <div className="max-w-7xl mx-auto px-6 grid grid-cols-1 md:grid-cols-5 gap-10">
        <div className="md:col-span-2 space-y-4">
          <NavLink to="/" className="flex items-center gap-3">
            <div className="h-8 w-8 rounded-xl bg-gradient-to-tr from-brand-600 to-indigo-500 flex items-center justify-center shadow-lg shadow-brand-500/25">
              <Sparkles className="h-4 w-4 text-white" />
            </div>
            <span className="font-bold text-base tracking-tight text-white">ResearchSphere AI</span>
          </NavLink>
          <p className="text-slate-400 max-w-sm leading-relaxed">
            Enterprise AI Workspace powered by Hybrid RAG, LangGraph multi-agent orchestration, modular MCP connectors, and vector knowledge persistence.
          </p>
          <div className="flex items-center gap-4 text-slate-400">
            <a href="#" className="hover:text-white transition-colors"><Github className="h-4 w-4" /></a>
            <a href="#" className="hover:text-white transition-colors"><Twitter className="h-4 w-4" /></a>
            <a href="#" className="hover:text-white transition-colors"><Linkedin className="h-4 w-4" /></a>
          </div>
        </div>

        <div>
          <h4 className="font-semibold text-slate-200 uppercase tracking-wider mb-4 text-[11px]">Product</h4>
          <ul className="space-y-2.5">
            <li><NavLink to="/chat" className="hover:text-brand-400">AI RAG Chat</NavLink></li>
            <li><NavLink to="/documents" className="hover:text-brand-400">Document Manager</NavLink></li>
            <li><NavLink to="/workspace" className="hover:text-brand-400">Research Workspace</NavLink></li>
            <li><NavLink to="/reports" className="hover:text-brand-400">Report Generator</NavLink></li>
            <li><NavLink to="/analytics" className="hover:text-brand-400">Analytics Engine</NavLink></li>
          </ul>
        </div>

        <div>
          <h4 className="font-semibold text-slate-200 uppercase tracking-wider mb-4 text-[11px]">Architecture</h4>
          <ul className="space-y-2.5">
            <li><a href="#features" className="hover:text-brand-400">Qdrant Vector DB</a></li>
            <li><a href="#workflow" className="hover:text-brand-400">LangGraph 0.2 Graph</a></li>
            <li><a href="#features" className="hover:text-brand-400">MCP Protocol Specs</a></li>
            <li><a href="#workflow" className="hover:text-brand-400">Reciprocal Rank Fusion</a></li>
            <li><a href="#faq" className="hover:text-brand-400">Docker Compose Setup</a></li>
          </ul>
        </div>

        <div>
          <h4 className="font-semibold text-slate-200 uppercase tracking-wider mb-4 text-[11px]">Company</h4>
          <ul className="space-y-2.5">
            <li><a href="#" className="hover:text-brand-400">About Us</a></li>
            <li><a href="#" className="hover:text-brand-400">Security & SOC2</a></li>
            <li><a href="#" className="hover:text-brand-400">Privacy Policy</a></li>
            <li><a href="#" className="hover:text-brand-400">Terms of Service</a></li>
            <li><a href="#" className="hover:text-brand-400">System Status</a></li>
          </ul>
        </div>
      </div>

      <div className="max-w-7xl mx-auto px-6 mt-12 pt-6 border-t border-slate-900 flex flex-col sm:flex-row items-center justify-between gap-4 text-[11px] text-slate-500 font-mono">
        <span>© 2026 ResearchSphere AI, Inc. All rights reserved.</span>
        <span>Built with React 19, Vite, FastAPI, LangGraph & Qdrant</span>
      </div>
    </footer>
  );
};
