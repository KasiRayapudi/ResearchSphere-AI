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
import { SystemHealth, FeatureFlag } from '../types';

export const AdminPanelPage: React.FC = () => {
  const [activeTab, setActiveTab] = useState('health');
  const [health, setHealth] = useState<SystemHealth | null>(null);
  const [flags, setFlags] = useState<FeatureFlag[]>([]);

  useEffect(() => {
    const fetchData = async () => {
      const [h, f] = await Promise.all([
        ApiService.getSystemHealth(),
        ApiService.getFeatureFlags(),
      ]);
      setHealth(h);
      setFlags(f);
    };
    fetchData();
  }, []);

  const toggleFlag = (id: string) => {
    setFlags((prev) =>
      prev.map((flag) => (flag.id === id ? { ...flag, enabled: !flag.enabled } : flag))
    );
  };

  const tabs = [
    { id: 'health', label: 'System Health & Infrastructure', icon: <Server className="h-4 w-4" /> },
    { id: 'flags', label: 'Feature Flags & Controls', icon: <Sliders className="h-4 w-4" /> },
    { id: 'users', label: 'User Roles & Audit Logs', icon: <Users className="h-4 w-4" /> },
  ];

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
            <div className="grid grid-cols-2 md:grid-cols-4 gap-4 text-xs font-mono bg-slate-950 p-4 rounded-xl border border-slate-800">
              <div><span className="text-slate-500">Uptime:</span> 16.5 Days</div>
              <div><span className="text-slate-500">Active LangGraph Agents:</span> {health.activeAgentsCount}</div>
              <div><span className="text-slate-500">CPU Usage:</span> 14.2%</div>
              <div><span className="text-slate-500">RAM Allocated:</span> 4.8 GB / 16 GB</div>
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

                <button
                  onClick={() => toggleFlag(flag.id)}
                  className={`w-12 h-6 rounded-full border p-1 flex items-center transition-colors cursor-pointer ${
                    flag.enabled ? 'bg-brand-600 border-brand-500' : 'bg-slate-800 border-slate-700'
                  }`}
                >
                  <div
                    className={`w-4 h-4 rounded-full bg-white transition-transform ${
                      flag.enabled ? 'translate-x-6' : 'translate-x-0'
                    }`}
                  />
                </button>
              </div>
            ))}
          </div>
        </Card>
      )}

      {activeTab === 'users' && (
        <Card className="p-6 space-y-4">
          <h3 className="text-base font-bold text-white">Global User Roster & Audit Controls</h3>
          <div className="p-8 text-center text-xs text-slate-500 font-mono">
            Audit logging enabled. All user actions logged to PostgreSQL audit_logs table.
          </div>
        </Card>
      )}
    </div>
  );
};
