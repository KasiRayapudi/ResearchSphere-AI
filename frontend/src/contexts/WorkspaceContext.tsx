import React, { createContext, useContext, useState } from 'react';
import { Workspace } from '../types';
import { initialWorkspacesMock } from '../services/mockData';

interface WorkspaceContextType {
  workspaces: Workspace[];
  activeWorkspace: Workspace;
  setActiveWorkspace: (ws: Workspace) => void;
  createWorkspace: (name: string, slug: string) => void;
}

const WorkspaceContext = createContext<WorkspaceContextType | undefined>(undefined);

export const WorkspaceProvider: React.FC<{ children: React.ReactNode }> = ({ children }) => {
  const [workspaces, setWorkspaces] = useState<Workspace[]>(initialWorkspacesMock);
  const [activeWorkspace, setActiveWorkspace] = useState<Workspace>(initialWorkspacesMock[0]);

  const createWorkspace = (name: string, slug: string) => {
    const newWs: Workspace = {
      id: `ws-${Date.now()}`,
      name,
      slug,
      icon: 'Sparkles',
      memberCount: 1,
      documentCount: 0,
      role: 'owner',
      createdAt: new Date().toISOString().split('T')[0],
    };
    setWorkspaces((prev) => [...prev, newWs]);
    setActiveWorkspace(newWs);
  };

  return (
    <WorkspaceContext.Provider value={{ workspaces, activeWorkspace, setActiveWorkspace, createWorkspace }}>
      {children}
    </WorkspaceContext.Provider>
  );
};

export const useWorkspace = () => {
  const context = useContext(WorkspaceContext);
  if (!context) throw new Error('useWorkspace must be used within WorkspaceProvider');
  return context;
};
