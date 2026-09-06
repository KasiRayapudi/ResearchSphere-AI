import React, { useState, useEffect } from 'react';
import {
  Shield,
  Activity,
  Users,
  HardDrive,
  Sliders,
  CheckCircle2,
  AlertTriangle,
  Server,
  Database,
  Cpu,
  Key,
  RefreshCw,
} from 'lucide-react';
import { Card } from '../components/common/Card';
import { Button } from '../components/common/Button';
import { Badge } from '../components/common/Badge';
import { Tabs } from '../components/common/Tabs';
import { ApiService } from '../services/api';
import { ApiError } from '../services/apiClient';
import { useAsyncData } from '../hooks/useAsyncData';
import { useAuth } from '../contexts/AuthContext';
import { EmptyState, ErrorState, ListSkeleton, Skeleton, TableSkeleton } from '../components/common/States';
import { ShieldAlert } from 'lucide-react';
import { SystemHealth, FeatureFlag } from '../types';

export const AdminPanelPage: React.FC = () => {
  const [activeTab, setActiveTab] = useState('health');
  const { isAdmin } = useAuth();

  const healthQuery = useAsyncData(() => ApiService.getSystemHealth(), []);
  const flagsQuery = useAsyncData(() => ApiService.getFeatureFlags(), []);
  const statsQuery = useAsyncData(() => ApiService.getSystemStats(), []);
  const usersQuery = useAsyncData(() => ApiService.getUsers(100, 0), []);

  const health = healthQuery.data;
  const flags = flagsQuery.data ?? [];
  const stats = statsQuery.data;
  const users = usersQuery.data;

  const refreshAll = () => {
    void healthQuery.refresh();
    void flagsQuery.refresh();
    void statsQuery.refresh();
    void usersQuery.refresh();
  };

  const tabs = [
    { id: 'health', label: 'System Health & Infrastructure', icon: <Server className="h-4 w-4" /> },
    { id: 'flags', label: 'Feature Flags & Controls', icon: <Sliders className="h-4 w-4" /> },
    { id: 'users', label: 'Users & Access', icon: <Users className="h-4 w-4" /> },
  ];

  // The backend now enforces admin-only access on every /admin route, so a
  // non-admin gets a real 403 instead of the previous hardcoded payload.
  const accessDenied =
    (healthQuery.error as ApiError | null)?.status === 403 ||
    (statsQuery.error as ApiError | null)?.status === 403;

  if (accessDenied || (!isAdmin && healthQuery.error)) {
    return (
      <EmptyState
        icon={<ShieldAlert className="h-6 w-6" />}
        title="Administrator access required"
        description="Your account does not have the admin role. Ask an administrator to grant it if you need access to this panel."
      />
    );
  }

  if (healthQuery.isInitialLoading) {
    return (
      <div className="space-y-6">
        <Skeleton className="h-8 w-72" />
        <ListSkeleton rows={5} />
      </div>
    );
  }

  if (healthQuery.error) {
    return (
      <ErrorState
        title="Could not load system status"
        message={healthQuery.error.message}
        onRetry={refreshAll}
      />
    );
  }

  return (
    <div className="space-y-8 animate-in fade-in duration-300">
      {/* Header */}
      <div>
        <h1 className="text-2xl font-extrabold text-white tracking-tight flex items-center gap-2">
          Admin Console & Governance <Badge variant="brand">Superadmin Privilege</Badge>
        </h1>
        <p className="text-xs text-slate-400 mt-1">
          Manage system infrastructure services, vector database indexes, user quotas, and global feature toggles.
        </p>
      </div>

      <Tabs tabs={tabs} activeTab={activeTab} onChange={setActiveTab} />

      {activeTab === 'health' && health && (
        <div className="space-y-6">
          <div className="grid grid-cols-1 md:grid-cols-3 lg:grid-cols-5 gap-4">
            {[
              { name: 'PostgreSQL 16', status: health.postgressStatus, icon: Database },
              { name: 'Qdrant Vector DB', status: health.qdrantStatus, icon: Cpu },
              { name: 'Redis Cache', status: health.redisStatus, icon: Server },
              { name: 'FastAPI Gateway', status: health.fastapiStatus, icon: Activity },
              { name: 'Celery Workers', status: health.celeryStatus, icon: RefreshCw },
            ].map((srv) => {
              const Icon = srv.icon;
              return (
                <Card key={srv.name} className="p-4 space-y-3">
                  <div className="flex items-center justify-between">
                    <div className="p-2 rounded-lg bg-slate-800 text-brand-400">
                      <Icon className="h-4 w-4" />
                    </div>
                    <Badge variant="success" size="sm">{srv.status}</Badge>
                  </div>
                  <div>
                    <div className="text-xs font-bold text-slate-100">{srv.name}</div>
                    <div className="text-[10px] text-slate-500 font-mono">Operational • 99.99% SLA</div>
                  </div>
                </Card>
              );
            })}
          </div>

          <Card className="p-6 space-y-4">
            <h3 className="text-sm font-bold text-white uppercase tracking-wider text-slate-400">
              System Telemetry Snapshot
            </h3>
            {/* Real values from /api/v1/admin/health. Uptime, CPU and RAM were
                previously hardcoded, and activeAgentsCount no longer exists. */}
            <div className="grid grid-cols-2 md:grid-cols-4 gap-4 text-xs font-mono bg-slate-950 p-4 rounded-xl border border-slate-800">
              <div>
                <span className="text-slate-500">Uptime:</span>{' '}
                {typeof (health as any).uptimeSeconds === 'number'
                  ? `${Math.floor((health as any).uptimeSeconds / 3600)}h ${Math.floor(
                      ((health as any).uptimeSeconds % 3600) / 60
                    )}m`
                  : '-'}
              </div>
              <div>
                <span className="text-slate-500">Version:</span>{' '}
                {(health as any).version ?? '-'}
              </div>
              <div>
                <span className="text-slate-500">Environment:</span>{' '}
                {(health as any).environment ?? '-'}
              </div>
              <div>
                <span className="text-slate-500">Gemini:</span>{' '}
                {(health as any).geminiStatus ?? '-'}
              </div>
            </div>
          </Card>
        </div>
      )}

      {activeTab === 'flags' && (
        <Card className="p-6 space-y-6">
          <h3 className="text-base font-bold text-white flex items-center gap-2">
            <Sliders className="h-4 w-4 text-brand-400" /> Feature Flags Registry
          </h3>

          <div className="divide-y divide-slate-800 text-xs">
            {flags.map((flag) => (
              <div key={flag.id} className="py-4 flex items-center justify-between gap-4">
                <div className="space-y-1">
                  <div className="flex items-center gap-2">
                    <span className="font-bold text-slate-200">{flag.name}</span>
                    <span className="font-mono text-[10px] text-brand-400 bg-brand-500/10 px-2 py-0.5 rounded border border-brand-500/20">
                      {flag.key}
                    </span>
                  </div>
                  <p className="text-slate-400 text-[11px]">{flag.description}</p>
                </div>

                <div className="flex shrink-0 items-center gap-2">
                  {flag.targetRole && (
                    <span className="font-mono text-[10px] text-slate-600">{flag.targetRole}</span>
                  )}
                  <span
                    className={`rounded-full border px-2 py-0.5 text-[10px] font-semibold ${
                      flag.enabled
                        ? 'border-emerald-500/30 bg-emerald-500/15 text-emerald-300'
                        : 'border-slate-700 bg-slate-800 text-slate-400'
                    }`}
                  >
                    {flag.enabled ? 'Enabled' : 'Disabled'}
                  </span>
                </div>
              </div>
            ))}
          </div>
        </Card>
      )}

      {activeTab === 'users' && (
        <div className="space-y-6">
          {/* Platform statistics - real database counts */}
          <Card className="p-6 space-y-4">
            <div className="flex items-center justify-between">
              <h3 className="text-base font-bold text-white">Platform Statistics</h3>
              <Button variant="ghost" size="sm" onClick={refreshAll} icon={<RefreshCw className="h-3.5 w-3.5" />}>
                Refresh
              </Button>
            </div>

            {statsQuery.isInitialLoading ? (
              <ListSkeleton rows={3} />
            ) : statsQuery.error ? (
              <ErrorState message={statsQuery.error.message} onRetry={statsQuery.refresh} />
            ) : stats ? (
              <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-6">
                {[
                  { label: 'Users', value: stats.users.total, sub: `${stats.users.active} active` },
                  { label: 'Admins', value: stats.users.admins, sub: `+${stats.users.newLast7Days} new / 7d` },
                  { label: 'Workspaces', value: stats.workspaces.total },
                  { label: 'Documents', value: stats.documents.total, sub: `${stats.documents.failed} failed` },
                  { label: 'Chunks', value: stats.documents.chunks, sub: `${stats.documents.storageMb} MB` },
                  { label: 'Messages', value: stats.chat.messages, sub: `${stats.chat.messagesLast24h} / 24h` },
                ].map((item) => (
                  <div key={item.label} className="rounded-lg border border-slate-800 bg-slate-900/50 p-3">
                    <p className="text-[10px] uppercase tracking-wide text-slate-500">{item.label}</p>
                    <p className="mt-1 font-mono text-lg font-bold text-white">{item.value}</p>
                    {item.sub && <p className="text-[10px] text-slate-500">{item.sub}</p>}
                  </div>
                ))}
              </div>
            ) : null}
          </Card>

          {/* Real user roster */}
          <Card className="p-0 overflow-hidden">
            <div className="flex items-center justify-between border-b border-slate-800 px-5 py-4">
              <h3 className="text-base font-bold text-white">User Accounts</h3>
              {users && (
                <span className="font-mono text-[11px] text-slate-500">{users.total} total</span>
              )}
            </div>

            {usersQuery.isInitialLoading ? (
              <div className="p-5"><TableSkeleton rows={5} cols={4} /></div>
            ) : usersQuery.error ? (
              <div className="p-5">
                <ErrorState message={usersQuery.error.message} onRetry={usersQuery.refresh} />
              </div>
            ) : !users || users.items.length === 0 ? (
              <div className="p-5"><EmptyState title="No user accounts found" /></div>
            ) : (
              <div className="overflow-x-auto">
                <table className="w-full text-xs">
                  <thead>
                    <tr className="border-b border-slate-800 text-left text-slate-500">
                      <th className="p-4 font-medium">Name</th>
                      <th className="p-4 font-medium">Email</th>
                      <th className="p-4 font-medium">Role</th>
                      <th className="p-4 font-medium">Status</th>
                      <th className="p-4 font-medium">Joined</th>
                    </tr>
                  </thead>
                  <tbody>
                    {users.items.map((u) => (
                      <tr key={u.id} className="border-b border-slate-800/60 hover:bg-slate-900/40">
                        <td className="p-4 font-medium text-slate-200">{u.name}</td>
                        <td className="p-4 font-mono text-[11px] text-slate-400">{u.email}</td>
                        <td className="p-4">
                          <Badge size="sm" variant={u.role === 'admin' ? 'brand' : 'neutral'}>
                            {u.role}
                          </Badge>
                        </td>
                        <td className="p-4">
                          <Badge size="sm" variant={u.isActive ? 'success' : 'error'}>
                            {u.isActive ? 'active' : 'disabled'}
                          </Badge>
                        </td>
                        <td className="p-4 text-slate-500">
                          {u.createdAt ? new Date(u.createdAt).toLocaleDateString() : '-'}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </Card>

          <Card className="p-5">
            <h3 className="mb-1.5 text-sm font-bold text-white">Audit Logging</h3>
            <p className="text-xs leading-relaxed text-slate-400">
              Structured audit records are emitted to the{' '}
              <span className="font-mono text-slate-300">researchsphere.audit</span> logger for
              logins, uploads, deletions, report generation, admin access and permission failures,
              each correlated by request id. They are written to the process log stream; there is no
              audit-query API yet, so they are not browsable from this panel.
            </p>
          </Card>
        </div>
      )}
    </div>
  );
};
