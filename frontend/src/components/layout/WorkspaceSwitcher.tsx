import React, { useEffect, useRef, useState } from 'react';
import { Check, ChevronDown, Cpu, Plus } from 'lucide-react';
import { useWorkspace } from '../../contexts/WorkspaceContext';
import { useToast } from '../../contexts/ToastContext';
import { Modal } from '../common/Modal';
import { Button } from '../common/Button';
import { Input } from '../common/Input';
import { Skeleton } from '../common/States';

/**
 * Real workspace switcher. The sidebar previously rendered a static button
 * with no menu, backed by a hardcoded workspace list.
 */
export const WorkspaceSwitcher: React.FC<{ collapsed?: boolean }> = ({ collapsed = false }) => {
  const { workspaces, activeWorkspace, setActiveWorkspace, createWorkspace, isLoading, error } =
    useWorkspace();
  const toast = useToast();
  const [open, setOpen] = useState(false);
  const [creating, setCreating] = useState(false);
  const [name, setName] = useState('');
  const [busy, setBusy] = useState(false);
  const containerRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const onClickAway = (e: MouseEvent) => {
      if (containerRef.current && !containerRef.current.contains(e.target as Node)) {
        setOpen(false);
      }
    };
    const onEscape = (e: KeyboardEvent) => e.key === 'Escape' && setOpen(false);
    document.addEventListener('mousedown', onClickAway);
    document.addEventListener('keydown', onEscape);
    return () => {
      document.removeEventListener('mousedown', onClickAway);
      document.removeEventListener('keydown', onEscape);
    };
  }, [open]);

  const handleCreate = async () => {
    if (!name.trim()) return;
    setBusy(true);
    try {
      const ws = await createWorkspace(name.trim());
      toast.success('Workspace created', `“${ws.name}” is now active.`);
      setCreating(false);
      setName('');
      setOpen(false);
    } catch (err) {
      toast.fromError(err, 'Could not create workspace');
    } finally {
      setBusy(false);
    }
  };

  if (isLoading && !activeWorkspace) {
    return <Skeleton className={collapsed ? 'h-9 w-9' : 'h-9 w-full'} />;
  }

  const label = activeWorkspace?.name ?? (error ? 'Unavailable' : 'No workspace');
  const initials = (activeWorkspace?.name ?? '??').substring(0, 2).toUpperCase();

  if (collapsed) {
    return (
      <div className="flex justify-center">
        <div
          title={label}
          className="flex h-9 w-9 items-center justify-center rounded-lg border border-slate-800 bg-slate-900 text-xs font-bold text-brand-400"
        >
          {initials}
        </div>
      </div>
    );
  }

  return (
    <div className="relative" ref={containerRef}>
      <button
        onClick={() => setOpen((v) => !v)}
        aria-haspopup="listbox"
        aria-expanded={open}
        className="flex w-full items-center justify-between rounded-lg border border-slate-800 bg-slate-900/80 px-3 py-2 text-xs font-medium text-slate-200 transition-colors hover:border-slate-700"
      >
        <span className="flex items-center gap-2 truncate">
          <Cpu className="h-4 w-4 shrink-0 text-brand-400" />
          <span className="truncate">{label}</span>
        </span>
        <ChevronDown
          className={`h-3.5 w-3.5 shrink-0 text-slate-500 transition-transform ${open ? 'rotate-180' : ''}`}
        />
      </button>

      {open && (
        <div
          role="listbox"
          className="absolute left-0 right-0 top-full z-40 mt-1.5 overflow-hidden rounded-lg border border-slate-800 bg-slate-900 shadow-2xl"
        >
          <div className="max-h-64 overflow-y-auto p-1">
            {workspaces.length === 0 && (
              <p className="px-3 py-2.5 text-xs text-slate-500">No workspaces yet</p>
            )}
            {workspaces.map((ws) => (
              <button
                key={ws.id}
                role="option"
                aria-selected={ws.id === activeWorkspace?.id}
                onClick={() => {
                  setActiveWorkspace(ws);
                  setOpen(false);
                }}
                className="flex w-full items-center justify-between gap-2 rounded-md px-3 py-2 text-left text-xs text-slate-300 transition-colors hover:bg-slate-800"
              >
                <span className="min-w-0 flex-1 truncate">
                  {ws.name}
                  <span className="ml-1.5 text-[10px] text-slate-600">
                    {ws.documentCount} docs
                  </span>
                </span>
                {ws.id === activeWorkspace?.id && (
                  <Check className="h-3.5 w-3.5 shrink-0 text-brand-400" />
                )}
              </button>
            ))}
          </div>
          <button
            onClick={() => {
              setCreating(true);
              setOpen(false);
            }}
            className="flex w-full items-center gap-2 border-t border-slate-800 px-3 py-2.5 text-xs font-medium text-brand-400 transition-colors hover:bg-slate-800/60"
          >
            <Plus className="h-3.5 w-3.5" />
            New workspace
          </button>
        </div>
      )}

      <Modal
        isOpen={creating}
        onClose={() => setCreating(false)}
        title="Create workspace"
        description="Workspaces keep documents, chats and reports separate."
        maxWidth="sm"
      >
        <div className="space-y-4">
          <Input
            label="Workspace name"
            value={name}
            autoFocus
            onChange={(e) => setName(e.target.value)}
            onKeyDown={(e) => e.key === 'Enter' && handleCreate()}
            placeholder="Research Team"
          />
          <div className="flex justify-end gap-2">
            <Button variant="ghost" size="sm" onClick={() => setCreating(false)}>
              Cancel
            </Button>
            <Button
              size="sm"
              onClick={handleCreate}
              isLoading={busy}
              disabled={!name.trim() || busy}
            >
              Create
            </Button>
          </div>
        </div>
      </Modal>
    </div>
  );
};
