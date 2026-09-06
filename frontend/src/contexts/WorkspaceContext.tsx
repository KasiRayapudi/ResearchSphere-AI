import React, {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
} from 'react';
import { Workspace } from '../types';
import { ApiService } from '../services/api';
import { useAuth } from './AuthContext';

const ACTIVE_WORKSPACE_KEY = 'rs_active_workspace';

interface WorkspaceContextType {
  workspaces: Workspace[];
  /** Null until workspaces load, or when the account has none. */
  activeWorkspace: Workspace | null;
  isLoading: boolean;
  error: string | null;
  setActiveWorkspace: (ws: Workspace) => void;
  createWorkspace: (name: string, description?: string) => Promise<Workspace>;
  refresh: () => Promise<void>;
}

const WorkspaceContext = createContext<WorkspaceContextType | undefined>(undefined);

export const WorkspaceProvider: React.FC<{ children: React.ReactNode }> = ({ children }) => {
  const { isAuthenticated } = useAuth();
  const [workspaces, setWorkspaces] = useState<Workspace[]>([]);
  const [activeWorkspace, setActive] = useState<Workspace | null>(null);
  const [isLoading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const rows = await ApiService.getWorkspaces();
      setWorkspaces(rows);
      setActive((current) => {
        if (current && rows.some((w) => w.id === current.id)) return current;
        let remembered: string | null = null;
        try {
          remembered = localStorage.getItem(ACTIVE_WORKSPACE_KEY);
        } catch {
          /* storage unavailable */
        }
        return rows.find((w) => w.id === remembered) ?? rows[0] ?? null;
      });
    } catch (err) {
      // Surfaced rather than silently replaced with fake workspaces.
      setError(err instanceof Error ? err.message : 'Could not load workspaces');
      setWorkspaces([]);
      setActive(null);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    if (isAuthenticated) void load();
  }, [isAuthenticated, load]);

  // Derive the signed-out view rather than clearing state from an effect,
  // which would cause an extra render pass on every sign-out.
  const visibleWorkspaces = isAuthenticated ? workspaces : [];
  const visibleActive = isAuthenticated ? activeWorkspace : null;

  const setActiveWorkspace = useCallback((ws: Workspace) => {
    setActive(ws);
    try {
      localStorage.setItem(ACTIVE_WORKSPACE_KEY, ws.id);
    } catch {
      /* storage unavailable */
    }
  }, []);

  const createWorkspace = useCallback(
    async (name: string, description?: string) => {
      const created = await ApiService.createWorkspace(name, description);
      setWorkspaces((prev) => [...prev, created]);
      setActiveWorkspace(created);
      return created;
    },
    [setActiveWorkspace]
  );

  const value = useMemo<WorkspaceContextType>(
    () => ({
      workspaces: visibleWorkspaces,
      activeWorkspace: visibleActive,
      isLoading,
      error,
      setActiveWorkspace,
      createWorkspace,
      refresh: load,
    }),
    [visibleWorkspaces, visibleActive, isLoading, error, setActiveWorkspace, createWorkspace, load]
  );

  return <WorkspaceContext.Provider value={value}>{children}</WorkspaceContext.Provider>;
};

export const useWorkspace = () => {
  const context = useContext(WorkspaceContext);
  if (!context) throw new Error('useWorkspace must be used within WorkspaceProvider');
  return context;
};
