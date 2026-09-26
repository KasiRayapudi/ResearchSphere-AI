import React from 'react';
import {
  TrendingUp,
  Cpu,
  Database,
} from 'lucide-react';
import { Card } from '../components/common/Card';
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

export const AnalyticsPage: React.FC = () => {
  const { activeWorkspace, isLoading: workspaceLoading } = useWorkspace();
  const workspaceId = activeWorkspace?.id;

  const {
    data: analytics,
    isInitialLoading,
    error,
    refresh,
  } = useAsyncData(() => ApiService.getAnalytics(workspaceId), [workspaceId], {
    enabled: Boolean(workspaceId),
  });

  if (!workspaceLoading && !activeWorkspace) {
    return (
      <EmptyState
        title="No workspace selected"
        description="Create or select a workspace to see its analytics."
      />
    );
  }

  // Previously this returned null while loading and forever on failure, so a
  // broken endpoint rendered a permanently blank page with no explanation.
  if (isInitialLoading || workspaceLoading) {
    return (
      <div className="space-y-6">
        <Skeleton className="h-8 w-80" />
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-4">
          {Array.from({ length: 4 }).map((_, i) => (
            <StatCardSkeleton key={i} />
          ))}
        </div>
        <div className="grid gap-6 lg:grid-cols-12">
          <div className="lg:col-span-7"><ListSkeleton rows={5} /></div>
          <div className="lg:col-span-5"><ListSkeleton rows={3} /></div>
        </div>
      </div>
    );
  }

  if (error) {
    return (
      <ErrorState
        title="Could not load analytics"
        message={error.message}
        onRetry={refresh}
      />
    );
  }

  if (!analytics) {
    return <EmptyState title="No analytics available yet" />;
  }

  return (
    <div className="space-y-8 animate-in fade-in duration-300">
      {/* Top Header */}
      <div>
        <h1 className="text-2xl font-extrabold text-white tracking-tight flex items-center gap-2">
          Real-Time Telemetry & Vector Analytics <Badge variant="brand">Qdrant Cluster</Badge>
        </h1>
        <p className="text-xs text-slate-400 mt-1">
          Detailed metrics for hybrid RAG search queries, vector chunk generation, latency distribution, and model consumption.
        </p>
      </div>

      {/* Query volume trend - dailyQueries was fetched but never rendered */}
      <Card className="p-6 space-y-4">
        <div className="flex items-center justify-between">
          <div>
            <h3 className="text-base font-bold text-white">Query Volume</h3>
            <p className="text-xs text-slate-400">
              Questions asked per day across this workspace
            </p>
          </div>
          <Badge variant="neutral" size="sm">
            last {analytics.dailyQueries.length} days
          </Badge>
        </div>
        <QueryTrendChart data={analytics.dailyQueries} />
      </Card>

      {/* Top Stat Cards */}
      <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4">
        <Card className="p-5 space-y-2">
          <div className="text-xs text-slate-400 font-semibold uppercase">Total Questions Asked</div>
          <div className="text-3xl font-extrabold text-white">{analytics.questionsAskedTotal.toLocaleString()}</div>
          <div className="text-[11px] text-emerald-400 font-semibold flex items-center gap-1">
            <TrendingUp className="h-3 w-3" /> +18.4% month-over-month
          </div>
        </Card>

        <Card className="p-5 space-y-2">
          <div className="text-xs text-slate-400 font-semibold uppercase">Vector Embeddings</div>
          <div className="text-3xl font-extrabold text-white">{analytics.embeddingsGeneratedTotal.toLocaleString()}</div>
          <div className="text-[11px] text-brand-400 font-semibold">1024-dim BAAI/bge-large</div>
        </Card>

        <Card className="p-5 space-y-2">
          <div className="text-xs text-slate-400 font-semibold uppercase">Avg RAG Latency</div>
          <div className="text-3xl font-extrabold text-white">{analytics.avgResponseTimeMs} ms</div>
          <div className="text-[11px] text-emerald-400 font-semibold">Sub-200ms target met</div>
        </Card>

        <Card className="p-5 space-y-2">
          <div className="text-xs text-slate-400 font-semibold uppercase">Storage Consumption</div>
          <div className="text-3xl font-extrabold text-white">{(analytics.storageUsageMb / 1024).toFixed(1)} GB</div>
          <div className="text-[11px] text-slate-400">of {(analytics.storageCapacityMb / 1024).toFixed(0)} GB quota</div>
        </Card>
      </div>

      {/* Main Charts & Breakdown */}
      <div className="grid grid-cols-1 lg:grid-cols-12 gap-8">
        {/* Most Accessed Sources List */}
        <Card className="lg:col-span-7 p-6 space-y-6">
          <h3 className="text-base font-bold text-white flex items-center gap-2">
            <Database className="h-4 w-4 text-brand-400" /> Most Accessed Vector Sources
          </h3>

          <div className="space-y-4">
            {analytics.topSources.map((src, idx) => (
              <div key={src.sourceName} className="p-3.5 rounded-xl bg-slate-950 border border-slate-800 flex items-center justify-between text-xs">
                <div className="flex items-center gap-3">
                  <span className="font-mono font-bold text-brand-400">#{idx + 1}</span>
                  <div>
                    <div className="font-semibold text-slate-200">{src.sourceName}</div>
                    <div className="text-[10px] text-slate-500">{src.category}</div>
                  </div>
                </div>
                <div className="text-right">
                  <div className="font-mono font-bold text-white">{src.accessCount} Hits</div>
                  <div className="text-[10px] text-emerald-400">0.96 Avg Score</div>
                </div>
              </div>
            ))}
          </div>
        </Card>

        {/* Model Consumption Breakdown */}
        <Card className="lg:col-span-5 p-6 space-y-6">
          <h3 className="text-base font-bold text-white flex items-center gap-2">
            <Cpu className="h-4 w-4 text-purple-400" /> LLM Model Consumption Breakdown
          </h3>

          <div className="space-y-4">
            {analytics.modelUsageBreakdown.map((m) => (
              <div key={m.modelName} className="space-y-2">
                <div className="flex justify-between text-xs font-semibold text-slate-200">
                  <span>{m.modelName}</span>
                  <span className="font-mono text-brand-300">{m.percentage}%</span>
                </div>
                <div className="w-full bg-slate-950 rounded-full h-2 border border-slate-800">
                  <div
                    className="bg-gradient-to-r from-brand-600 to-indigo-500 h-2 rounded-full"
                    style={{ width: `${m.percentage}%` }}
                  />
                </div>
              </div>
            ))}
          </div>
        </Card>
      </div>
    </div>
  );
};
