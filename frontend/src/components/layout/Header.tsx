import React from 'react';
import { Search, Moon, Sun, Bell, Command, PanelLeftClose, PanelLeftOpen, Sparkles } from 'lucide-react';
import { useTheme } from '../../contexts/ThemeContext';
import { useAuth } from '../../contexts/AuthContext';
import { Button } from '../common/Button';

interface HeaderProps {
  sidebarCollapsed: boolean;
  onToggleSidebar: () => void;
  onOpenCommandPalette: () => void;
}

export const Header: React.FC<HeaderProps> = ({
  sidebarCollapsed,
  onToggleSidebar,
  onOpenCommandPalette,
}) => {
  const { theme, toggleTheme } = useTheme();
  const { user } = useAuth();

  return (
    <header className="h-16 sticky top-0 z-20 bg-slate-950/70 backdrop-blur-xl border-b border-slate-800/80 px-6 flex items-center justify-between">
      <div className="flex items-center gap-4">
        <button
          onClick={onToggleSidebar}
          className="p-2 rounded-lg text-slate-400 hover:text-white hover:bg-slate-900 transition-colors"
          title={sidebarCollapsed ? 'Expand Sidebar' : 'Collapse Sidebar'}
        >
          {sidebarCollapsed ? <PanelLeftOpen className="h-5 w-5" /> : <PanelLeftClose className="h-5 w-5" />}
        </button>

        {/* Command Palette Trigger */}
        <button
          onClick={onOpenCommandPalette}
          className="hidden md:flex items-center gap-3 px-3.5 py-1.5 rounded-lg bg-slate-900/90 border border-slate-800 text-slate-400 text-xs hover:border-slate-700 transition-all cursor-pointer w-64"
        >
          <Search className="h-3.5 w-3.5 text-slate-500" />
          <span className="flex-1 text-left">Search docs, agents, models...</span>
          <div className="flex items-center gap-0.5 text-[10px] font-mono text-slate-500 bg-slate-800 px-1.5 py-0.5 rounded">
            <Command className="h-3 w-3" />
            <span>K</span>
          </div>
        </button>
      </div>

      <div className="flex items-center gap-3">
        <Button
          variant="outline"
          size="sm"
          onClick={onOpenCommandPalette}
          icon={<Sparkles className="h-3.5 w-3.5 text-brand-400" />}
          className="hidden sm:inline-flex"
        >
          New Agent Research
        </Button>

        {/* Theme Toggle */}
        <button
          onClick={toggleTheme}
          className="p-2 rounded-lg text-slate-400 hover:text-white hover:bg-slate-900 transition-colors"
          title={`Switch to ${theme === 'dark' ? 'light' : 'dark'} mode`}
        >
          {theme === 'dark' ? <Sun className="h-4 w-4 text-amber-400" /> : <Moon className="h-4 w-4 text-brand-400" />}
        </button>

        {/* Notifications */}
        <div className="relative">
          <button className="p-2 rounded-lg text-slate-400 hover:text-white hover:bg-slate-900 transition-colors relative">
            <Bell className="h-4 w-4" />
            <span className="absolute top-1.5 right-1.5 h-2 w-2 rounded-full bg-brand-500 animate-pulse" />
          </button>
        </div>
      </div>
    </header>
  );
};
