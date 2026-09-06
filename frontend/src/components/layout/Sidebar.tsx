import React from 'react';
import { NavLink, useLocation, useNavigate } from 'react-router-dom';
import {
  LayoutDashboard,
  MessageSquare,
  FileText,
  Sparkles,
  BarChart3,
  Shield,
  Layers,
  Settings,
  Plus,
  ChevronDown,
  Database,
  Cpu,
  LogOut,
  FolderGit2,
} from 'lucide-react';
import { useAuth } from '../../contexts/AuthContext';
import { WorkspaceSwitcher } from './WorkspaceSwitcher';
import { useToast } from '../../contexts/ToastContext';
import { useWorkspace } from '../../contexts/WorkspaceContext';

interface SidebarProps {
  collapsed: boolean;
  onToggle: () => void;
}

export const Sidebar: React.FC<SidebarProps> = ({ collapsed, onToggle }) => {
  const { user, logout } = useAuth();
  const navigate = useNavigate();
  const toast = useToast();

  // logout is async now (it revokes the session server-side), so route the
  // user to the landing page only once it has settled.
  const handleLogout = async () => {
    await logout();
    toast.success('Signed out', 'Your session has been ended on this device.');
    navigate('/', { replace: true });
  };
  const location = useLocation();
  const { activeWorkspace } = useWorkspace();

  const navItems = [
    { label: 'Dashboard', path: '/dashboard', icon: LayoutDashboard },
    { label: 'AI RAG Chat', path: '/chat', icon: MessageSquare },
    {
      label: 'Document Manager',
      path: '/documents',
      icon: FileText,
      // Real count for the active workspace; was a hardcoded '128'.
      badge: activeWorkspace?.documentCount ? String(activeWorkspace.documentCount) : undefined,
    },
    { label: 'Research Workspace', path: '/workspace', icon: Sparkles },
    { label: 'Report Generator', path: '/reports', icon: Layers },
    { label: 'Analytics Engine', path: '/analytics', icon: BarChart3 },
    { label: 'Admin & System', path: '/admin', icon: Shield },
  ];

  return (
    <aside
      className={`h-screen sticky top-0 bg-slate-950 border-r border-slate-800/80 flex flex-col transition-all duration-300 z-30 ${
        collapsed ? 'w-20' : 'w-64'
      }`}
    >
      {/* Brand Header */}
      <div className="h-16 px-4 flex items-center justify-between border-b border-slate-800/80">
        <NavLink to="/dashboard" className="flex items-center gap-3 overflow-hidden">
          <div className="h-9 w-9 rounded-xl bg-gradient-to-tr from-brand-600 via-indigo-500 to-purple-500 flex items-center justify-center shadow-lg shadow-brand-500/25 shrink-0">
            <Sparkles className="h-5 w-5 text-white" />
          </div>
          {!collapsed && (
            <div className="flex flex-col">
              <span className="font-bold text-base tracking-tight text-white flex items-center gap-1.5">
                ResearchSphere <span className="text-[10px] font-mono font-semibold bg-brand-500/20 text-brand-400 px-1.5 py-0.2 rounded border border-brand-500/30">AI</span>
              </span>
              <span className="text-[10px] text-slate-400 font-mono">Enterprise v2.4</span>
            </div>
          )}
        </NavLink>
      </div>

      {/* Workspace Selector */}
      <div className="p-3 border-b border-slate-800/60">
        <WorkspaceSwitcher collapsed={collapsed} />
      </div>

      {/* Navigation Links */}
      <div className="flex-1 overflow-y-auto p-3 space-y-1">
        {navItems.map((item) => {
          const Icon = item.icon;
          const isActive = location.pathname === item.path;
          return (
            <NavLink
              key={item.path}
              to={item.path}
              className={`flex items-center gap-3 px-3 py-2.5 rounded-lg text-xs font-medium transition-all group relative ${
                isActive
                  ? 'bg-gradient-to-r from-brand-600/20 to-indigo-600/10 text-brand-300 font-semibold border border-brand-500/30 shadow-sm'
                  : 'text-slate-400 hover:text-slate-200 hover:bg-slate-900/60'
              }`}
              title={collapsed ? item.label : undefined}
            >
              <Icon className={`h-4 w-4 shrink-0 transition-colors ${isActive ? 'text-brand-400' : 'text-slate-400 group-hover:text-slate-200'}`} />
              {!collapsed && <span className="truncate flex-1">{item.label}</span>}
              {!collapsed && item.badge && (
                <span className="px-1.5 py-0.5 text-[10px] font-mono font-semibold rounded bg-brand-500/20 text-brand-300 border border-brand-500/30">
                  {item.badge}
                </span>
              )}
              {isActive && (
                <span className="absolute left-0 top-1.5 bottom-1.5 w-1 bg-brand-500 rounded-r-full" />
              )}
            </NavLink>
          );
        })}
      </div>

      {/* Footer User Info */}
      <div className="p-3 border-t border-slate-800/80 bg-slate-950/60">
        {!collapsed ? (
          <div className="flex items-center justify-between">
            <div className="flex items-center gap-2.5 overflow-hidden">
              <img
                src={user?.avatarUrl || 'https://images.unsplash.com/photo-1534528741775-53994a69daeb?auto=format&fit=crop&w=250&q=80'}
                alt="Avatar"
                className="h-8 w-8 rounded-full border border-brand-500/40 object-cover"
              />
              <div className="flex flex-col truncate">
                <span className="text-xs font-semibold text-slate-200 truncate">{user?.name || 'User'}</span>
                <span className="text-[10px] text-slate-500 capitalize">{user?.role}</span>
              </div>
            </div>
            <button
              onClick={handleLogout}
              className="p-1.5 rounded-lg text-slate-400 hover:text-rose-400 hover:bg-slate-900 transition-colors"
              title="Log out"
            >
              <LogOut className="h-4 w-4" />
            </button>
          </div>
        ) : (
          <button
            onClick={handleLogout}
            className="w-full flex justify-center p-2 rounded-lg text-slate-400 hover:text-rose-400 hover:bg-slate-900"
            title="Log out"
          >
            <LogOut className="h-4 w-4" />
          </button>
        )}
      </div>
    </aside>
  );
};
