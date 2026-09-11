import React, { useMemo, useState } from 'react';
import {
  Sparkles,
  Users,
  Bookmark,
  Plus,
  Cpu,
} from 'lucide-react';
import { Card } from '../components/common/Card';
import { Button } from '../components/common/Button';
import { Badge } from '../components/common/Badge';
import { Tabs } from '../components/common/Tabs';
import { Modal } from '../components/common/Modal';
import { Input } from '../components/common/Input';
import { ApiService } from '../services/api';
import { useAsyncData } from '../hooks/useAsyncData';
import { useWorkspace } from '../contexts/WorkspaceContext';
import { useToast } from '../contexts/ToastContext';
import { useAuth } from '../contexts/AuthContext';
import { EmptyState, ErrorState, ListSkeleton, Skeleton } from '../components/common/States';

export const ResearchWorkspacePage: React.FC = () => {
  const [activeTab, setActiveTab] = useState('canvas');
  // Sessions are derived from the query rather than mirrored into local state,
  // which previously required an effect to keep the two in sync.
  const [activeSessionId, setActiveSessionId] = useState<string | null>(null);
  const [newSessionModal, setNewSessionModal] = useState(false);
  const [titleInput, setTitleInput] = useState('');
  const [objectiveInput, setObjectiveInput] = useState('');

  const { activeWorkspace } = useWorkspace();
  const workspaceId = activeWorkspace?.id;
  const toast = useToast();
  const { user } = useAuth();
  const [isCreating, setIsCreating] = useState(false);
  const [search, setSearch] = useState('');

  const { data, isInitialLoading, error, refresh, setData } = useAsyncData(
    () => ApiService.getResearchSessions(workspaceId),
    [workspaceId],
    { enabled: Boolean(workspaceId) }
  );

  const sessions = useMemo(() => data ?? [], [data]);
  const activeSession = useMemo(
    () => sessions.find((item) => item.id === activeSessionId) ?? sessions[0] ?? null,
    [sessions, activeSessionId]
  );

  const visibleSessions = useMemo(() => {
    const q = search.trim().toLowerCase();
    if (!q) return sessions;
    return sessions.filter(
      (item) =>
        item.title.toLowerCase().includes(q) ||
        (item.objective ?? '').toLowerCase().includes(q)
    );
  }, [sessions, search]);

  const handleCreateSession = async () => {
    if (!titleInput || !objectiveInput || isCreating) return;
    setIsCreating(true);
    try {
      const created = await ApiService.startResearchSession(
        titleInput,
        objectiveInput,
        workspaceId
      );
      setData((prev) => [created, ...(prev ?? [])]);
      setActiveSessionId(created.id);
      setNewSessionModal(false);
      setTitleInput('');
      setObjectiveInput('');
      toast.success('Research session complete', created.title + ' is ready to review.');
    } catch (err) {
      toast.fromError(err, 'Could not run the research workflow');
    } finally {
      setIsCreating(false);
    }
  };

  const tabs = [
    { id: 'canvas', label: 'Agent Research Canvas', icon: <Sparkles className="h-4 w-4" /> },
    { id: 'notes', label: 'Saved Notes & Highlights', icon: <Bookmark className="h-4 w-4" /> },
    { id: 'team', label: 'Team Members & RBAC', icon: <Users className="h-4 w-4" /> },
  ];

  if (!activeWorkspace) {
    return (
      <EmptyState
        title="No workspace selected"
        description="Create or select a workspace to run multi-agent research."
      />
    );
  }

  if (isInitialLoading) {
    return (
      <div className="space-y-6">
        <Skeleton className="h-8 w-80" />
        <ListSkeleton rows={5} />
      </div>
    );
  }

  if (error) {
    return (
      <ErrorState
        title="Could not load research sessions"
        message={error.message}
        onRetry={refresh}
      />
    );
  }

  return (
    <div className="space-y-8 animate-in fade-in duration-300">
      {/* Header */}
      <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-4">
        <div>
          <h1 className="text-2xl font-extrabold text-white tracking-tight flex items-center gap-2">
            LangGraph Agent Canvas <Badge variant="brand">8 Active Agents</Badge>
          </h1>
          <p className="text-xs text-slate-400 mt-1">
            Visual subtask graph execution, state persistence checkpoints, and synthesis outputs.
          </p>
        </div>

        <Button
          variant="primary"
          size="sm"
          onClick={() => setNewSessionModal(true)}
          icon={<Plus className="h-4 w-4" />}
        >
          New Agentic Session
        </Button>
      </div>

      <Tabs tabs={tabs} activeTab={activeTab} onChange={setActiveTab} />

      {activeTab === 'canvas' && activeSession && (
        <div className="grid grid-cols-1 lg:grid-cols-12 gap-8">
          {/* Left Session Selector */}
          <Card className="lg:col-span-4 p-5 space-y-4">
            <h3 className="text-sm font-bold text-white uppercase tracking-wider text-slate-400">
              Active Sessions
            </h3>
            <div className="space-y-3">
              <div className="mb-2">
                <label className="sr-only" htmlFor="session-search">Search sessions</label>
                <input
                  id="session-search"
                  value={search}
                  onChange={(e) => setSearch(e.target.value)}
                  placeholder="Search sessions..."
                  className="w-full rounded-lg border border-slate-800 bg-slate-950/60 px-3 py-2 text-xs text-slate-200 outline-none placeholder:text-slate-600 focus:border-brand-500"
                />
              </div>

              {visibleSessions.length === 0 && (
                <p className="px-1 py-6 text-center text-xs text-slate-500">
                  {sessions.length === 0
                    ? 'No research sessions yet.'
                    : 'No sessions match your search.'}
                </p>
              )}

              {visibleSessions.map((s) => (
                <div
                  key={s.id}
                  onClick={() => setActiveSessionId(s.id)}
                  className={`p-3.5 rounded-xl border transition-all cursor-pointer ${
                    activeSession.id === s.id
                      ? 'bg-slate-900 border-brand-500/60 shadow-md'
                      : 'bg-slate-950/40 border-slate-800 hover:border-slate-700'
                  }`}
                >
                  <div className="flex items-center justify-between">
                    <h4 className="text-xs font-bold text-slate-200 truncate">{s.title}</h4>
                    <Badge variant={s.status === 'completed' ? 'success' : 'brand'} size="sm">
                      {s.status}
                    </Badge>
                  </div>
                  <p className="text-[11px] text-slate-400 mt-1 line-clamp-1">{s.objective}</p>
                </div>
              ))}
            </div>
          </Card>

          {/* Right Visual Canvas */}
          <Card className="lg:col-span-8 p-6 space-y-6">
            <div className="flex items-center justify-between border-b border-slate-800 pb-4">
              <div>
                <h2 className="text-lg font-bold text-white">{activeSession.title}</h2>
                <p className="text-xs text-slate-400 mt-0.5">{activeSession.objective}</p>
              </div>
              <span className="text-xs font-mono font-bold text-brand-300 bg-brand-500/10 px-3 py-1 rounded-full border border-brand-500/20">
                Progress: {activeSession.progressPercentage}%
              </span>
            </div>

            {/* Agent Nodes Grid */}
            <div className="space-y-4">
              <h3 className="text-xs font-mono text-slate-400 uppercase font-semibold">
                Execution Graph Nodes Trace:
              </h3>
              <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                {activeSession.agentSteps.map((step) => (
                  <div
                    key={step.id}
                    className="p-4 rounded-xl bg-slate-950 border border-slate-800 hover:border-slate-700 transition-colors space-y-2"
                  >
                    <div className="flex items-center justify-between">
                      <span className="text-xs font-bold text-brand-400 font-mono flex items-center gap-1.5">
                        <Cpu className="h-3.5 w-3.5" /> {step.agentName} Agent
                      </span>
                      <Badge
                        variant={
                          step.status === 'completed'
                            ? 'success'
                            : step.status === 'running'
                            ? 'brand'
                            : 'neutral'
                        }
                        size="sm"
                      >
                        {step.status}
                      </Badge>
                    </div>
                    <p className="text-xs text-slate-300 leading-normal">{step.task}</p>
                    <div className="flex items-center justify-between text-[10px] font-mono text-slate-500 pt-2 border-t border-slate-900">
                      <span>Time: {step.executionTimeMs || 120}ms</span>
                      <span>{step.timestamp}</span>
                    </div>
                  </div>
                ))}
              </div>
            </div>
          </Card>
        </div>
      )}

      {activeTab === 'notes' && (
        <Card className="p-8 text-center space-y-4">
          <Bookmark className="h-10 w-10 text-brand-400 mx-auto" />
          <h3 className="text-lg font-bold text-white">Saved Research Notes & Code Bookmarks</h3>
          <p className="text-xs text-slate-400 max-w-md mx-auto">
            Highlighted citations and code snippets saved across your agent sessions appear here.
          </p>
        </Card>
      )}

      {activeTab === 'team' && (
        <Card className="p-6 space-y-6">
          <div className="flex items-center justify-between">
            <h3 className="text-base font-bold text-white flex items-center gap-2">
              <Users className="h-4 w-4 text-brand-400" /> Team Permissions & RBAC Matrix
            </h3>
            <Button variant="outline" size="sm" disabled icon={<Plus className="h-4 w-4" />}>
              Invite Member
            </Button>
          </div>

          {/* The backend has no workspace-membership model yet, so the only
              real member is the owner. This previously listed three invented
              colleagues as though they were real accounts. */}
          <div className="divide-y divide-slate-800 text-xs">
            {user && (
              <div className="py-3.5 flex items-center justify-between">
                <div>
                  <div className="font-bold text-slate-200">{user.name}</div>
                  <div className="text-[10px] text-slate-500 font-mono">{user.email}</div>
                </div>
                <Badge variant="brand" size="sm">Workspace Owner</Badge>
              </div>
            )}
          </div>
          <p className="pt-3 text-[11px] leading-relaxed text-slate-500">
            Shared workspaces are not available yet. Each workspace currently belongs to a single
            owner, so invitations and member roles are disabled.
          </p>
        </Card>
      )}

      {/* New Session Modal */}
      <Modal
        isOpen={newSessionModal}
        onClose={() => setNewSessionModal(false)}
        title="Start LangGraph Agentic Research Session"
        description="Breaks objectives into Planner, Research, Retrieval, Summarizer, Critic, and Citation subtasks."
      >
        <div className="space-y-4 pt-2">
          <Input
            label="Session Title"
            placeholder="e.g. LangGraph vs AutoGen Benchmarking"
            value={titleInput}
            onChange={(e) => setTitleInput(e.target.value)}
          />
          <Input
            label="Research Objective"
            placeholder="Specify goals, documents to focus on, or target outcomes..."
            value={objectiveInput}
            onChange={(e) => setObjectiveInput(e.target.value)}
          />
          <Button variant="primary" className="w-full mt-4" onClick={handleCreateSession}>
            Launch LangGraph Pipeline
          </Button>
        </div>
      </Modal>
    </div>
  );
};
