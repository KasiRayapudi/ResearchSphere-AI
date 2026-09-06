import React from 'react';
import { useNavigate } from 'react-router-dom';
import { Sparkles, ArrowRight, ShieldCheck, Database, Cpu, Layers, GitBranch } from 'lucide-react';
import { Button } from '../common/Button';
import { AnimatedNetwork } from './AnimatedNetwork';

export const HeroSection: React.FC = () => {
  const navigate = useNavigate();

  return (
    <section className="relative pt-32 pb-20 md:pt-40 md:pb-28 overflow-hidden">
      <div className="max-w-7xl mx-auto px-6 grid grid-cols-1 lg:grid-cols-12 gap-12 items-center">
        {/* Left Headline */}
        <div className="lg:col-span-7 flex flex-col items-start gap-6">
          <div className="inline-flex items-center gap-2 px-3.5 py-1.5 rounded-full bg-brand-500/10 border border-brand-500/30 text-brand-300 text-xs font-semibold tracking-wide">
            <Sparkles className="h-3.5 w-3.5 text-brand-400" />
            <span>Next-Gen Enterprise AI Workspace 2.0</span>
          </div>

          <h1 className="text-4xl md:text-6xl font-extrabold tracking-tight text-white leading-tight">
            Autonomous AI Research powered by <span className="gradient-text">Hybrid RAG & LangGraph</span>
          </h1>

          <p className="text-base md:text-lg text-slate-400 font-normal leading-relaxed max-w-2xl">
            Unify your enterprise knowledge. Connect GitHub, Google Drive, and local docs to perform deep agentic research, generate verified PDF reports, and chat with 100% grounded source citations.
          </p>

          <div className="flex flex-wrap items-center gap-4 pt-2">
            <Button
              variant="primary"
              size="lg"
              onClick={() => navigate('/dashboard')}
              icon={<ArrowRight className="h-5 w-5" />}
            >
              Get Started Free
            </Button>
            <Button
              variant="outline"
              size="lg"
              onClick={() => navigate('/chat')}
              icon={<Sparkles className="h-5 w-5 text-brand-400" />}
            >
              Try Interactive RAG Chat
            </Button>
          </div>

          {/* Key Metrics Pill */}
          <div className="grid grid-cols-3 gap-6 pt-6 border-t border-slate-800/80 w-full max-w-xl">
            <div>
              <div className="text-2xl font-bold text-white">99.8%</div>
              <div className="text-xs text-slate-500 font-medium">Grounded Citations</div>
            </div>
            <div>
              <div className="text-2xl font-bold text-white">&lt;180ms</div>
              <div className="text-xs text-slate-500 font-medium">RRF Vector Retrieval</div>
            </div>
            <div>
              <div className="text-2xl font-bold text-white">8 Agents</div>
              <div className="text-xs text-slate-500 font-medium">LangGraph Pipeline</div>
            </div>
          </div>
        </div>

        {/* Right Canvas Network Visualizer */}
        <div className="lg:col-span-5 h-[420px] w-full relative">
          <AnimatedNetwork />
        </div>
      </div>
    </section>
  );
};
