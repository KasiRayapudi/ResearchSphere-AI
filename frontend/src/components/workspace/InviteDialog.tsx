import React, { useState } from 'react';
import { Mail, Send } from 'lucide-react';
import { ApiService } from '../../services/api';
import { ApiError } from '../../services/apiClient';
import { Button } from '../common/Button';
import { Input } from '../common/Input';
import { Modal } from '../common/Modal';
import { WorkspaceInvitation, WorkspaceRole } from '../../types';

interface InviteDialogProps {
  isOpen: boolean;
  onClose: () => void;
  workspaceId?: string;
  onInvited: (invitation: WorkspaceInvitation) => void;
}

/** Owner is absent on purpose: ownership is transferred, never assigned. */
const ASSIGNABLE: { value: WorkspaceRole; label: string; description: string }[] = [
  { value: 'admin', label: 'Admin', description: 'Manage members and all content' },
  { value: 'editor', label: 'Editor', description: 'Upload, chat, and create reports' },
  { value: 'viewer', label: 'Viewer', description: 'Read-only access' },
];

export const InviteDialog: React.FC<InviteDialogProps> = ({
  isOpen,
  onClose,
  workspaceId,
  onInvited,
}) => {
  const [email, setEmail] = useState('');
  const [role, setRole] = useState<WorkspaceRole>('viewer');
  const [isSending, setSending] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const reset = () => {
    setEmail('');
    setRole('viewer');
    setError(null);
  };

  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    setError(null);
    setSending(true);
    try {
      const invitation = await ApiService.inviteMember(email.trim(), role, workspaceId);
      onInvited(invitation);
      reset();
      onClose();
    } catch (err) {
      // The server distinguishes "already a member" from "already invited";
      // both are useful to show verbatim rather than flattened to "failed".
      setError(err instanceof ApiError ? err.message : 'Could not send the invitation.');
    } finally {
      setSending(false);
    }
  };

  return (
    <Modal
      isOpen={isOpen}
      onClose={() => {
        reset();
        onClose();
      }}
      title="Invite to workspace"
      description="They will receive an invitation code by email."
      maxWidth="md"
    >
      <form onSubmit={submit} className="space-y-4">
        <Input
          label="Email address"
          type="email"
          required
          value={email}
          onChange={(e) => setEmail(e.target.value)}
          placeholder="colleague@example.com"
          leftIcon={<Mail className="h-4 w-4" />}
          autoFocus
        />

        <fieldset className="space-y-2">
          <legend className="text-xs font-medium text-slate-400">Role</legend>
          {ASSIGNABLE.map((option) => (
            <label
              key={option.value}
              className={`flex cursor-pointer items-start gap-3 rounded-lg border p-3 transition-colors ${
                role === option.value
                  ? 'border-brand-500/60 bg-brand-500/10'
                  : 'border-slate-800 bg-slate-900/40 hover:border-slate-700'
              }`}
            >
              <input
                type="radio"
                name="role"
                value={option.value}
                checked={role === option.value}
                onChange={() => setRole(option.value)}
                className="mt-0.5 accent-brand-500"
              />
              <span className="min-w-0">
                <span className="block text-sm font-medium text-slate-200">{option.label}</span>
                <span className="block text-[11px] text-slate-500">{option.description}</span>
              </span>
            </label>
          ))}
        </fieldset>

        {error && (
          <p role="alert" className="text-xs text-rose-400">
            {error}
          </p>
        )}

        <div className="flex justify-end gap-2 pt-2">
          <Button
            type="button"
            variant="ghost"
            onClick={() => {
              reset();
              onClose();
            }}
          >
            Cancel
          </Button>
          <Button type="submit" isLoading={isSending} disabled={!email.trim()}>
            <Send className="h-4 w-4" />
            Send invitation
          </Button>
        </div>
      </form>
    </Modal>
  );
};
