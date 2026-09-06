import React, { useState, useEffect } from 'react';
import { useNavigate } from 'react-router-dom';
import {
  FileText,
  Sparkles,
  Layers,
  Share2,
  HardDrive,
  TrendingUp,
  Clock,
  Plus,
  ArrowUpRight,
  Database,
  CheckCircle2,
  Activity,
  Cpu,
} from 'lucide-react';
import { Card } from '../components/common/Card';
import { Button } from '../components/common/Button';
import { Badge } from '../components/common/Badge';
import { ApiService } from '../services/api';
import { Document, ResearchSession, MCPConnector, AnalyticsData } from '../types';

export const DashboardPage: React.FC = () => {
  const [documents, setDocuments] = useState<Document[]>([]);
  const [sessions, setSessions] = useState<ResearchSession[]>([]);
  const [connectors, setConnectors] = useState<MCPConnector[]>([]);
  const [analytics, setAnalytics] = useState<AnalyticsData | null>(null);
  const navigate = useNavigate();

  useEffect(() => {
    const fetchData = async () => {
      const [docs, sess, conn, aly] = await Promise.all([
        ApiService.getDocuments(),
        ApiService.getResearchSessions(),
        ApiService.getMCPConnectors(),
        ApiService.getAnalytics(),
      ]);
      setDocuments(docs);
      setSessions(sess);
      setConnectors(conn);
      setAnalytics(aly);
    };
    fetchData();
  }, []);

  const connectedSourcesCount = connectors.filter((c) => c.status === 'connected').length;

  return (
    <div className="space-y-8 animate-in fade-in duration-300">
      {/* Top Header */}
      <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-4">
        <div>
          <h1 className="text-2xl font-extrabold text-white tracking-tight flex items-center gap-2">
            Workspace Overview <Badge variant="brand">Enterprise Tier</Badge>
          </h1>
          <p className="text-xs text-slate-400 mt-1">
            Real-time telemetry, hybrid vector status, and active multi-agent research sessions.
          </p>
        </div>

        <div className="flex items-center gap-3">
          <Button
            variant="outline"
            size="sm"
            onClick={() => navigate('/documents')}
            icon={<FileText className="h-4 w-4 text-emerald-400" />}
          >
            Upload Document
          </Button>
          <Button
            variant="primary"
            size="sm"
            onClick={() => navigate('/chat')}
            icon={<Sparkles className="h-4 w-4" />}
          >
            New Agent Research
          </Button>
        </div>
      </div>

      {/* 5 Core Stat Cards */}
      <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-5 gap-4">
        <Card className="p-5 flex flex-col justify-between hover:border-brand-500/40">
          <div className="flex items-center justify-between">
            <span className="text-xs font-semibold text-slate-400">Documents Indexed</span>
            <div className="p-2 rounded-lg bg-emerald-500/10 text-emerald-400">
              <FileText className="h-4 w-4" />
            </div>
          </div>
          <div className="mt-4">
            <div className="text-2xl font-extrabold text-white">{documents.length || 128}</div>
            <div className="text-[11px] text-emerald-400 font-semibold flex items-center gap-1 mt-1">
              <TrendingUp className="h-3 w-3" /> +12% this week
            </div>
          </div>
        </Card>

        <Card className="p-5 flex flex-col justify-between hover:border-brand-500/40">
          <div className="flex items-center justify-between">
            <span className="text-xs font-semibold text-slate-400">Research Sessions</span>
            <div className="p-2 rounded-lg bg-purple-500/10 text-purple-400">
              <Sparkles className="h-4 w-4" />
            </div>
          </div>
          <div className="mt-4">
            <div className="text-2xl font-extrabold text-white">{sessions.length || 24}</div>
            <div className="text-[11px] text-purple-400 font-semibold flex items-center gap-1 mt-1">
              <Cpu className="h-3 w-3" /> 8 Agents Active
            </div>
          </div>
        </Card>

        <Card className="p-5 flex flex-col justify-between hover:border-brand-500/40">
          <div className="flex items-center justify-between">
            <span className="text-xs font-semibold text-slate-400">Reports Generated</span>
            <div className="p-2 rounded-lg bg-amber-500/10 text-amber-400">
              <Layers className="h-4 w-4" />
            </div>
          </div>
          <div className="mt-4">
            <div className="text-2xl font-extrabold text-white">18</div>
            <div className="text-[11px] text-slate-400 mt-1">PDF & Markdown</div>
          </div>
        </Card>

        <Card className="p-5 flex flex-col justify-between hover:border-brand-500/40">
          <div className="flex items-center justify-between">
            <span className="text-xs font-semibold text-slate-400">Connected Sources</span>
            <div className="p-2 rounded-lg bg-cyan-500/10 text-cyan-400">
              <Share2 className="h-4 w-4" />
            </div>
          </div>
          <div className="mt-4">
            <div className="text-2xl font-extrabold text-white">{connectedSourcesCount} / 8</div>
            <div className="text-[11px] text-cyan-400 font-semibold mt-1">GitHub, Drive, Local</div>
          </div>
        </Card>

        <Card className="p-5 flex flex-col justify-between hover:border-brand-500/40">
          <div className="flex items-center justify-between">
            <span className="text-xs font-semibold text-slate-400">Storage Used</span>
            <div className="p-2 rounded-lg bg-brand-500/10 text-brand-400">
              <HardDrive className="h-4 w-4" />
            </div>
          </div>
          <div className="mt-4">
            <div className="text-2xl font-extrabold text-white">12.8 GB</div>
            <div className="w-full bg-slate-800 rounded-full h-1.5 mt-2">
              <div className="bg-brand-500 h-1.5 rounded-full w-[25%]" />
            </div>
          </div>
        </Card>
      </div>

      {/* Main Grid: Visual Activity Chart & Connected Sources */}
      <div className="grid grid-cols-1 lg:grid-cols-12 gap-8">
        {/* Visual Daily Activity Chart */}
        <Card className="lg:col-span-8 p-6 space-y-6">
          <div className="flex items-center justify-between">
            <div>
              <h3 className="text-base font-bold text-white flex items-center gap-2">
                <Activity className="h-4 w-4 text-brand-400" /> Daily Query Volume & Hybrid Latency
              </h3>
              <p className="text-xs text-slate-400">Sub-200ms Reciprocal Rank Fusion response time across Qdrant vector store</p>
            </div>
            <span className="px-2.5 py-1 rounded-md bg-emerald-500/10 text-emerald-300 font-mono text-xs border border-emerald-500/20">
              Avg Latency: 184ms
            </span>
          </div>

          {/* Dynamic SVG Visual Chart */}
          <div className="h-56 w-full pt-4">
            <svg className="w-full h-full overflow-visible" viewBox="0 0 600 180">
              {/* Grid Lines */}
              <line x1="0" y1="30" x2="600" y2="30" stroke="rgba(255,255,255,0.05)" strokeDasharray="4 4" />
              <line x1="0" y1="80" x2="600" y2="80" stroke="rgba(255,255,255,0.05)" strokeDasharray="4 4" />
              <line x1="0" y1="130" x2="600" y2="130" stroke="rgba(255,255,255,0.05)" strokeDasharray="4 4" />

              {/* Area Gradient Fill */}
              <defs>
                <linearGradient id="chartGradient" x1="0" y1="0" x2="0" y2="1">
                  <stop offset="0%" stopColor="#6366f1" stopOpacity="0.4" />
                  <stop offset="100%" stopColor="#6366f1" stopOpacity="0.0" />
                </linearGradient>
              </defs>

              <path
                d="M 0 140 Q 100 110, 200 80 T 400 40 T 600 30 L 600 160 L 0 160 Z"
                fill="url(#chartGradient)"
              />

              <path
                d="M 0 140 Q 100 110, 200 80 T 400 40 T 600 30"
                fill="none"
                stroke="#6366f1"
                strokeWidth="3"
              />

              {/* Data points */}
              {[[0, 140], [100, 115], [200, 80], [300, 65], [400, 40], [500, 35], [600, 30]].map(([x, y], idx) => (
                <circle key={idx} cx={x} cy={y} r="4" fill="#6366f1" stroke="#ffffff" strokeWidth="2" />
              ))}
            </svg>

            <div className="flex justify-between text-[11px] font-mono text-slate-500 pt-2 border-t border-slate-800">
              <span>Feb 6</span>
              <span>Feb 7</span>
              <span>Feb 8</span>
              <span>Feb 9</span>
              <span>Feb 10</span>
              <span>Feb 11</span>
              <span>Feb 12 (Today)</span>
            </div>
          </div>
        </Card>

        {/* Connected Sources & Connectors */}
        <Card className="lg:col-span-4 p-6 space-y-6">
          <div className="flex items-center justify-between">
            <h3 className="text-base font-bold text-white flex items-center gap-2">
              <Share2 className="h-4 w-4 text-cyan-400" /> Active MCP Connectors
            </h3>
            <button onClick={() => navigate('/admin')} className="text-xs text-brand-400 hover:underline">Manage</button>
          </div>

          <div className="space-y-3">
            {connectors.slice(0, 4).map((c) => (
              <div key={c.id} className="p-3 rounded-lg bg-slate-950 border border-slate-800 flex items-center justify-between text-xs">
                <div className="flex items-center gap-2.5">
                  <div className={`h-2 w-2 rounded-full ${c.status === 'connected' ? 'bg-emerald-400 animate-ping' : 'bg-slate-600'}`} />
                  <div>
                    <div className="font-semibold text-slate-200">{c.name}</div>
                    <div className="text-[10px] text-slate-500">{c.itemsSyncedCount} items synced</div>
                  </div>
                </div>
                <Badge variant={c.status === 'connected' ? 'success' : 'neutral'} size="sm">
                  {c.status}
                </Badge>
              </div>
            ))}
          </div>
        </Card>
      </div>

      {/* Active LangGraph Research Sessions Feed */}
      <Card className="p-6 space-y-6">
        <div className="flex items-center justify-between">
          <div>
            <h3 className="text-base font-bold text-white flex items-center gap-2">
              <Sparkles className="h-4 w-4 text-purple-400" /> Recent LangGraph Agent Research Sessions
            </h3>
            <p className="text-xs text-slate-400">Autonomous subtask execution graph and synthesis status</p>
          </div>
          <Button variant="ghost" size="sm" onClick={() => navigate('/workspace')} icon={<ArrowUpRight className="h-4 w-4" />}>
            View All Workspaces
          </Button>
        </div>

        <div className="space-y-4">
          {sessions.map((sess) => (
            <div
              key={sess.id}
              onClick={() => navigate('/workspace')}
              className="p-4 rounded-xl bg-slate-950 border border-slate-800 hover:border-slate-700 transition-colors cursor-pointer flex flex-col md:flex-row md:items-center justify-between gap-4"
            >
              <div className="space-y-1 max-w-2xl">
                <div className="flex items-center gap-2">
                  <h4 className="text-sm font-bold text-slate-200">{sess.title}</h4>
                  <Badge variant={sess.status === 'completed' ? 'success' : 'brand'} size="sm">
                    {sess.status}
                  </Badge>
                </div>
                <p className="text-xs text-slate-400 line-clamp-1">{sess.objective}</p>
              </div>

              <div className="flex items-center gap-6 text-xs text-slate-400 shrink-0">
                <div>
                  <span className="text-slate-500">Progress:</span>{' '}
                  <span className="font-mono font-bold text-brand-300">{sess.progressPercentage}%</span>
                </div>
                <div>
                  <span className="text-slate-500">Sources:</span>{' '}
                  <span className="font-mono font-bold text-white">{sess.sourcesCount}</span>
                </div>
                <Button variant="outline" size="sm">Inspect Canvas</Button>
              </div>
            </div>
          ))}
        </div>
      </Card>
    </div>
  );
};
