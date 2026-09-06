import React, { useState, useEffect } from 'react';
import {
  Sparkles,
  Users,
  FileText,
  Bookmark,
  Plus,
  Play,
  CheckCircle2,
  Clock,
  Cpu,
  Shield,
  Layers,
  Activity,
  UserCheck,
} from 'lucide-react';
import { Card } from '../components/common/Card';
import { Button } from '../components/common/Button';
import { Badge } from '../components/common/Badge';
import { Tabs } from '../components/common/Tabs';
import { Modal } from '../components/common/Modal';
import { Input } from '../components/common/Input';
import { ApiService } from '../services/api';
import { ResearchSession, AgentStep } from '../types';

export const ResearchWorkspacePage: React.FC = () => {
  const [activeTab, setActiveTab] = useState('canvas');
  const [sessions, setSessions] = useState<ResearchSession[]>([]);
  const [activeSession, setActiveSession] = useState<ResearchSession | null>(null);
  const [newSessionModal, setNewSessionModal] = useState(false);
  const [titleInput, setTitleInput] = useState('');
  const [objectiveInput, setObjectiveInput] = useState('');

  useEffect(() => {
    const fetchSessions = async () => {
      const sess = await ApiService.getResearchSessions();
      setSessions(sess);
      if (sess.length > 0) setActiveSession(sess[0]);
    };
    fetchSessions();
  }, []);

  const handleCreateSession = async () => {
    if (!titleInput || !objectiveInput) return;
    const created = await ApiService.startResearchSession(titleInput, objectiveInput);
    setSessions((prev) => [created, ...prev]);
    setActiveSession(created);
    setNewSessionModal(false);
    setTitleInput('');
    setObjectiveInput('');
  };

  const tabs = [
    { id: 'canvas', label: 'Agent Research Canvas', icon: <Sparkles className="h-4 w-4" /> },
    { id: 'notes', label: 'Saved Notes & Highlights', icon: <Bookmark className="h-4 w-4" /> },
    { id: 'team', label: 'Team Members & RBAC', icon: <Users className="h-4 w-4" /> },
  ];

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
              {sessions.map((s) => (
                <div
                  key={s.id}
                  onClick={() => setActiveSession(s)}
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
            <Button variant="outline" size="sm" icon={<Plus className="h-4 w-4" />}>
              Invite Member
            </Button>
          </div>

          <div className="divide-y divide-slate-800 text-xs">
            {[
              { name: 'Alex Vance', email: 'alex.vance@enterprise-ai.io', role: 'Workspace Owner' },
              { name: 'Dr. Sarah Lin', email: 'sarah.lin@enterprise-ai.io', role: 'Admin & Lead Researcher' },
              { name: 'Marcus Chen', email: 'marcus.chen@enterprise-ai.io', role: 'AI Solution Architect' },
            ].map((m) => (
              <div key={m.email} className="py-3.5 flex items-center justify-between">
                <div>
                  <div className="font-bold text-slate-200">{m.name}</div>
                  <div className="text-[10px] text-slate-500 font-mono">{m.email}</div>
                </div>
                <Badge variant="brand" size="sm">{m.role}</Badge>
              </div>
            ))}
          </div>
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
