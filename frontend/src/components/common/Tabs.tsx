import React from 'react';

export interface TabItem {
  id: string;
  label: string;
  icon?: React.ReactNode;
  badge?: string | number;
}

interface TabsProps {
  tabs: TabItem[];
  activeTab: string;
  onChange: (id: string) => void;
  className?: string;
}

export const Tabs: React.FC<TabsProps> = ({ tabs, activeTab, onChange, className = '' }) => {
  return (
    <div className={`flex items-center gap-1 border-b border-slate-800 pb-px ${className}`}>
      {tabs.map((tab) => {
        const isActive = tab.id === activeTab;
        return (
          <button
            key={tab.id}
            onClick={() => onChange(tab.id)}
            className={`flex items-center gap-2 px-4 py-2.5 text-sm font-medium transition-all relative cursor-pointer ${
              isActive
                ? 'text-brand-400 font-semibold'
                : 'text-slate-400 hover:text-slate-200 hover:bg-slate-900/50 rounded-t-lg'
            }`}
          >
            {tab.icon && <span className="text-current">{tab.icon}</span>}
            {tab.label}
            {tab.badge !== undefined && (
              <span
                className={`ml-1 px-1.5 py-0.5 text-[10px] font-bold rounded-full ${
                  isActive ? 'bg-brand-500/20 text-brand-300' : 'bg-slate-800 text-slate-400'
                }`}
              >
                {tab.badge}
              </span>
            )}
            {isActive && (
              <span className="absolute bottom-0 left-0 right-0 h-0.5 bg-gradient-to-r from-brand-500 to-indigo-500 rounded-t-full" />
            )}
          </button>
        );
      })}
    </div>
  );
};
