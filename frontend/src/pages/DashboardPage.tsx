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
import { useAsyncData } from '../hooks/useAsyncData';
import { useWorkspace } from '../contexts/WorkspaceContext';
import { QueryTrendChart } from '../components/analytics/QueryTrendChart';
import {
  EmptyState,
  ErrorState,
  ListSkeleton,
  Skeleton,
  StatCardSkeleton,
} from '../components/common/States';
import { Document, ResearchSession, MCPConnector, AnalyticsData } from '../types';

export const DashboardPage: React.FC = () => {
  const navigate = useNavigate();
  const { activeWorkspace, isLoading: workspaceLoading } = useWorkspace();
  const workspaceId = activeWorkspace?.id;

  // Each panel loads independently so one failing endpoint no longer leaves
  // the whole dashboard blank (and no longer silently renders mock data).
  const documentsQuery = useAsyncData(
    () => ApiService.getDocuments(workspaceId),
    [workspaceId],
    { enabled: Boolean(workspaceId) }
  );
  const sessionsQuery = useAsyncData(
    () => ApiService.getResearchSessions(workspaceId),
    [workspaceId],
    { enabled: Boolean(workspaceId) }
  );
  const connectorsQuery = useAsyncData(
    () => ApiService.getMCPConnectors(workspaceId),
    [workspaceId],
    { enabled: Boolean(workspaceId) }
  );
  const analyticsQuery = useAsyncData(
    () => ApiService.getAnalytics(workspaceId),
    [workspaceId],
    { enabled: Boolean(workspaceId) }
  );
  const reportsQuery = useAsyncData(
    () => ApiService.getReports(workspaceId),
    [workspaceId],
    { enabled: Boolean(workspaceId) }
  );
  const healthQuery = useAsyncData(() => ApiService.getHealth(), []);

  const documents = documentsQuery.data ?? [];
  const sessions = sessionsQuery.data ?? [];
  const connectors = connectorsQuery.data ?? [];
  const reports = reportsQuery.data ?? [];
  const analytics = analyticsQuery.data;
  const health = healthQuery.data as
    | { status?: string; checks?: Record<string, string>; uptime_seconds?: number; version?: string }
    | null;

  const isLoading =
    workspaceLoading ||
    documentsQuery.isInitialLoading ||
    analyticsQuery.isInitialLoading;

  const loadError = documentsQuery.error ?? analyticsQuery.error;

  const refreshAll = () => {
    void documentsQuery.refresh();
    void sessionsQuery.refresh();
    void connectorsQuery.refresh();
    void reportsQuery.refresh();
    void analyticsQuery.refresh();
    void healthQuery.refresh();
  };

  const connectedSourcesCount = connectors.filter((c) => c.status === 'connected').length;

  // Recent activity, newest first.
  const recentUploads = [...documents]
    .sort((a, b) => (b.uploadedAt ?? '').localeCompare(a.uploadedAt ?? ''))
    .slice(0, 5);

  if (!workspaceLoading && !activeWorkspace) {
    return (
      <EmptyState
        icon={<FileText className="h-6 w-6" />}
        title="No workspace yet"
        description="Create a workspace from the sidebar to start uploading documents and running research."
      />
    );
  }

  if (isLoading) {
    return (
      <div className="space-y-6">
        <Skeleton className="h-8 w-64" />
        <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-5">
          {Array.from({ length: 5 }).map((_, i) => (
            <StatCardSkeleton key={i} />
          ))}
        </div>
        <div className="grid gap-6 lg:grid-cols-12">
          <div className="lg:col-span-8">
            <ListSkeleton rows={4} />
          </div>
          <div className="lg:col-span-4">
            <ListSkeleton rows={3} />
          </div>
        </div>
      </div>
    );
  }

  if (loadError) {
    return (
      <ErrorState
        title="Could not load your dashboard"
        message={loadError.message}
        onRetry={refreshAll}
      />
    );
  }

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
            <div className="text-2xl font-extrabold text-white">{documents.length}</div>
            <div className="text-[11px] text-slate-400 mt-1">
              {analytics ? `${analytics.embeddingsGeneratedTotal} chunks indexed` : '\u2014'}
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
            <div className="text-2xl font-extrabold text-white">{sessions.length}</div>
            <div className="text-[11px] text-slate-400 flex items-center gap-1 mt-1">
              <Cpu className="h-3 w-3" /> LangGraph workflow
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
            <div className="text-2xl font-extrabold text-white">{reports.length}</div>
            <div className="text-[11px] text-slate-400 mt-1">Markdown export</div>
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
            <div className="text-2xl font-extrabold text-white">{connectedSourcesCount} / {connectors.length}</div>
            <div className="text-[11px] text-slate-400 mt-1">
              {connectors.length === 0 ? 'None configured' : 'MCP connectors'}
            </div>
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
            <div className="text-2xl font-extrabold text-white">
              {analytics ? `${analytics.storageUsageMb.toFixed(1)} MB` : '\u2014'}
            </div>
            <div className="w-full bg-slate-800 rounded-full h-1.5 mt-2">
              <div
                className="bg-brand-500 h-1.5 rounded-full transition-all"
                style={{
                  width: `${
                    analytics
                      ? Math.min(
                          100,
                          (analytics.storageUsageMb /
                            Math.max(1, analytics.storageCapacityMb)) *
                            100
                        )
                      : 0
                  }%`,
                }}
              />
            </div>
          </div>
        </Card>
      </div>

      {/* Main Grid: Visual Activity Chart & Connected Sources */}
      <div className="grid grid-cols-1 lg:grid-cols-12 gap-8">
        {/* Real daily query volume. This was a fixed decorative Bezier path
            with a hardcoded "184ms" latency badge. */}
        <Card className="lg:col-span-8 p-6 space-y-6">
          <div className="flex items-center justify-between">
            <div>
              <h3 className="text-base font-bold text-white flex items-center gap-2">
                <Activity className="h-4 w-4 text-brand-400" /> Daily Query Volume
              </h3>
              <p className="text-xs text-slate-400">
                Questions asked per day in this workspace
              </p>
            </div>
            {analytics && analytics.avgResponseTimeMs > 0 && (
              <span className="rounded-md border border-emerald-500/20 bg-emerald-500/10 px-2.5 py-1 font-mono text-xs text-emerald-300">
                Avg latency: {analytics.avgResponseTimeMs}ms
              </span>
            )}
          </div>

          {analyticsQuery.isInitialLoading ? (
            <ListSkeleton rows={3} />
          ) : analytics ? (
            <QueryTrendChart data={analytics.dailyQueries} height={200} />
          ) : (
            <EmptyState title="No query activity yet" />
          )}
        </Card>

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

      {/* Recent uploads + live system status */}
      <div className="grid gap-6 lg:grid-cols-12">
        <Card className="lg:col-span-7 p-6 space-y-4">
          <div className="flex items-center justify-between">
            <h3 className="text-base font-bold text-white flex items-center gap-2">
              <FileText className="h-4 w-4 text-emerald-400" /> Recent Uploads
            </h3>
            <Button variant="ghost" size="sm" onClick={() => navigate('/documents')}>
              View all
            </Button>
          </div>

          {recentUploads.length === 0 ? (
            <EmptyState
              title="No documents yet"
              description="Upload a PDF, DOCX, TXT, Markdown or CSV file to build your knowledge base."
              action={{ label: 'Upload a document', onClick: () => navigate('/documents') }}
            />
          ) : (
            <ul className="space-y-2">
              {recentUploads.map((doc) => (
                <li
                  key={doc.id}
                  className="flex items-center gap-3 rounded-lg border border-slate-800/60 bg-slate-900/40 p-3"
                >
                  <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg bg-slate-800 text-[10px] font-bold uppercase text-slate-400">
                    {doc.fileType}
                  </div>
                  <div className="min-w-0 flex-1">
                    <p className="truncate text-xs font-medium text-slate-200">{doc.title}</p>
                    <p className="text-[10px] text-slate-500">
                      {doc.chunkCount} chunks · {Math.max(1, Math.round(doc.fileSizeKb))} KB
                    </p>
                  </div>
                  <Badge
                    size="sm"
                    variant={
                      doc.status === 'indexed'
                        ? 'success'
                        : doc.status === 'failed'
                          ? 'error'
                          : 'warning'
                    }
                  >
                    {doc.status}
                  </Badge>
                </li>
              ))}
            </ul>
          )}
        </Card>

        <Card className="lg:col-span-5 p-6 space-y-4">
          <h3 className="text-base font-bold text-white flex items-center gap-2">
            <Database className="h-4 w-4 text-brand-400" /> System Status
          </h3>

          {healthQuery.isInitialLoading ? (
            <ListSkeleton rows={3} />
          ) : healthQuery.error ? (
            <ErrorState
              title="Health unavailable"
              message={healthQuery.error.message}
              onRetry={healthQuery.refresh}
            />
          ) : (
            <>
              <ul className="space-y-2">
                {Object.entries(health?.checks ?? {}).map(([name, status]) => {
                  const healthy = status === 'ok' || status === 'configured';
                  const optional = status === 'disabled' || status === 'unavailable';
                  return (
                    <li
                      key={name}
                      className="flex items-center justify-between rounded-lg border border-slate-800/60 bg-slate-900/40 px-3 py-2"
                    >
                      <span className="text-xs capitalize text-slate-300">{name}</span>
                      <span className="flex items-center gap-1.5">
                        <span
                          className={`h-1.5 w-1.5 rounded-full ${
                            healthy
                              ? 'bg-emerald-400'
                              : optional
                                ? 'bg-slate-500'
                                : 'bg-rose-400'
                          }`}
                        />
                        <span
                          className={`font-mono text-[10px] ${
                            healthy
                              ? 'text-emerald-400'
                              : optional
                                ? 'text-slate-500'
                                : 'text-rose-400'
                          }`}
                        >
                          {status}
                        </span>
                      </span>
                    </li>
                  );
                })}
              </ul>
              {health?.uptime_seconds !== undefined && (
                <p className="text-[10px] text-slate-500">
                  Uptime {Math.floor(health.uptime_seconds / 3600)}h{' '}
                  {Math.floor((health.uptime_seconds % 3600) / 60)}m
                  {health.version ? ` · v${health.version}` : ''}
                </p>
              )}
            </>
          )}

          {analytics && (
            <div className="border-t border-slate-800 pt-3">
              <div className="mb-1.5 flex items-center justify-between text-[11px]">
                <span className="text-slate-400">Storage used</span>
                <span className="font-mono text-slate-300">
                  {analytics.storageUsageMb.toFixed(1)} / {analytics.storageCapacityMb} MB
                </span>
              </div>
              <div className="h-1.5 overflow-hidden rounded-full bg-slate-800">
                <div
                  className="h-full rounded-full bg-gradient-to-r from-brand-500 to-indigo-500"
                  style={{
                    width: `${Math.min(
                      100,
                      (analytics.storageUsageMb / Math.max(1, analytics.storageCapacityMb)) * 100
                    )}%`,
                  }}
                />
              </div>
            </div>
          )}
        </Card>
      </div>
    </div>
  );
};
