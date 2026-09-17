import React, { useState, useEffect } from 'react';
import { Search, Sparkles, FileText, Shield, BarChart3, CornerDownLeft } from 'lucide-react';
import { useNavigate } from 'react-router-dom';

interface CommandPaletteProps {
  isOpen: boolean;
  onClose: () => void;
}

export const CommandPalette: React.FC<CommandPaletteProps> = ({ isOpen, onClose }) => {
  const [query, setQuery] = useState('');
  const navigate = useNavigate();

  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key === 'k') {
        e.preventDefault();
        if (isOpen) onClose();
        else {
          // Open
          const evt = new CustomEvent('open-command-palette');
          window.dispatchEvent(evt);
        }
      }
    };
    window.addEventListener('keydown', handleKeyDown);
    return () => window.removeEventListener('keydown', handleKeyDown);
  }, [isOpen, onClose]);

  if (!isOpen) return null;

  const actions = [
    { id: 'act-1', title: 'New RAG Research Session', icon: <Sparkles className="h-4 w-4 text-brand-400" />, path: '/chat', category: 'Actions' },
    { id: 'act-2', title: 'Upload & Index Document', icon: <FileText className="h-4 w-4 text-emerald-400" />, path: '/documents', category: 'Actions' },
    { id: 'act-3', title: 'Generate PDF Report', icon: <FileText className="h-4 w-4 text-amber-400" />, path: '/reports', category: 'Actions' },
    { id: 'nav-1', title: 'Go to Dashboard', icon: <BarChart3 className="h-4 w-4 text-indigo-400" />, path: '/dashboard', category: 'Navigation' },
    { id: 'nav-2', title: 'Go to Document Manager', icon: <FileText className="h-4 w-4 text-slate-400" />, path: '/documents', category: 'Navigation' },
    { id: 'nav-3', title: 'Go to Research Workspace', icon: <Sparkles className="h-4 w-4 text-purple-400" />, path: '/workspace', category: 'Navigation' },
    { id: 'nav-4', title: 'Go to Analytics Engine', icon: <BarChart3 className="h-4 w-4 text-cyan-400" />, path: '/analytics', category: 'Navigation' },
    { id: 'nav-5', title: 'Go to Admin & Security', icon: <Shield className="h-4 w-4 text-rose-400" />, path: '/admin', category: 'Navigation' },
  ];

  const filtered = actions.filter((a) =>
    a.title.toLowerCase().includes(query.toLowerCase()) || a.category.toLowerCase().includes(query.toLowerCase())
  );

  const handleSelect = (path: string) => {
    navigate(path);
    onClose();
  };

  return (
    <div
      className="fixed inset-0 z-50 flex items-start justify-center pt-24 px-4 bg-slate-950/80 backdrop-blur-md"
      onClick={onClose}
    >
      <div
        className="w-full max-w-xl rounded-2xl bg-slate-900 border border-slate-800 text-slate-100 shadow-2xl overflow-hidden animate-in fade-in zoom-in-95 duration-150"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center px-4 border-b border-slate-800">
          <Search className="h-5 w-5 text-slate-400 mr-3" />
          <input
            type="text"
            placeholder="Type a command or search documents (e.g. RAG, LangGraph, Reports)..."
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            className="w-full py-4 text-sm bg-transparent border-none text-slate-100 placeholder-slate-500 focus:outline-none"
            autoFocus
          />
          <kbd className="px-2 py-0.5 text-[10px] font-mono font-semibold text-slate-400 bg-slate-800 border border-slate-700 rounded">
            ESC
          </kbd>
        </div>

        <div className="max-h-80 overflow-y-auto p-2">
          {filtered.length === 0 ? (
            <div className="p-8 text-center text-sm text-slate-500">No matching commands found.</div>
          ) : (
            filtered.map((item) => (
              <button
                key={item.id}
                onClick={() => handleSelect(item.path)}
                className="w-full flex items-center justify-between px-3 py-2.5 rounded-lg text-sm text-slate-300 hover:bg-brand-500/10 hover:text-white transition-colors cursor-pointer group"
              >
                <div className="flex items-center gap-3">
                  <div className="p-1.5 rounded-md bg-slate-800/80 group-hover:bg-brand-500/20">
                    {item.icon}
                  </div>
                  <span>{item.title}</span>
                </div>
                <div className="flex items-center gap-2">
                  <span className="text-[10px] font-mono uppercase text-slate-500 bg-slate-800/60 px-2 py-0.5 rounded">
                    {item.category}
                  </span>
                  <CornerDownLeft className="h-3.5 w-3.5 text-slate-500 opacity-0 group-hover:opacity-100 transition-opacity" />
                </div>
              </button>
            ))
          )}
        </div>

        <div className="px-4 py-2 bg-slate-950/60 border-t border-slate-800/80 flex items-center justify-between text-[11px] text-slate-500">
          <span>Search documents, agent runs & app pages</span>
          <span>
            Press <kbd className="px-1 py-0.5 bg-slate-800 rounded font-mono">⌘K</kbd> to open anytime
          </span>
        </div>
      </div>
    </div>
  );
};
