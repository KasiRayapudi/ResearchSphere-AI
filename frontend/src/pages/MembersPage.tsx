import React, { useState } from 'react';
import { Crown, KeyRound, RefreshCw, Trash2, UserPlus, Users, X } from 'lucide-react';
import { ApiService } from '../services/api';
import { ApiError } from '../services/apiClient';
import { Badge } from '../components/common/Badge';
import { Button } from '../components/common/Button';
import { Card } from '../components/common/Card';
import { ConfirmDialog } from '../components/common/ConfirmDialog';
import { EmptyState, ErrorState, ListSkeleton } from '../components/common/States';
import { Input } from '../components/common/Input';
import { Modal } from '../components/common/Modal';
import { InviteDialog } from '../components/workspace/InviteDialog';
import { RoleBadge } from '../components/workspace/RoleBadge';
import { useAuth } from '../contexts/AuthContext';
import { useToast } from '../contexts/ToastContext';
import { useWorkspace } from '../contexts/WorkspaceContext';
import { useAsyncData } from '../hooks/useAsyncData';
import { useRealtime, useRealtimeEvent } from '../hooks/useRealtime';
import { EVENTS, RESYNC } from '../services/realtime';
import { InvitationStatus, ROLE_CAN, WorkspaceMember, WorkspaceRole } from '../types';

const ASSIGNABLE_ROLES: WorkspaceRole[] = ['admin', 'editor', 'viewer'];

const INVITE_STATUS_VARIANT: Record<InvitationStatus, 'success' | 'warning' | 'neutral' | 'error'> =
  {
    pending: 'warning',
    accepted: 'success',
    revoked: 'neutral',
    expired: 'error',
  };

export const MembersPage: React.FC = () => {
  const { activeWorkspace } = useWorkspace();
  const { user } = useAuth();
  const toast = useToast();
  const workspaceId = activeWorkspace?.id;

  const [isInviteOpen, setInviteOpen] = useState(false);
  const [isAcceptOpen, setAcceptOpen] = useState(false);
  const [removing, setRemoving] = useState<WorkspaceMember | null>(null);
  const [transferring, setTransferring] = useState<WorkspaceMember | null>(null);
  const [busyId, setBusyId] = useState<string | null>(null);

  const members = useAsyncData(() => ApiService.getMembers(workspaceId), [workspaceId], {
    enabled: Boolean(workspaceId),
  });

  // My own role decides which controls appear. The server enforces the same
  // rules, so a client that guessed wrong gets a 403 rather than an effect.
  const myRole: WorkspaceRole =
    (members.data?.find((m) => m.userId === user?.id)?.role as WorkspaceRole) ?? 'viewer';
  const canManage = ROLE_CAN.manageMembers(myRole);

  const invitations = useAsyncData(
    () => ApiService.getInvitations(workspaceId),
    [workspaceId, canManage],
    { enabled: Boolean(workspaceId) && canManage }
  );

  const pending = (invitations.data ?? []).filter((i) => i.status === 'pending');

  // Changes made by other people arrive over the workspace socket. The lists
  // are refetched rather than patched: invitation events deliberately carry
  // only an id, so the server re-applies authorization when the list is
  // read. My own changes are skipped -- withBusy has already refreshed.
  const { online } = useRealtime();
  useRealtimeEvent(
    [
      EVENTS.MEMBER_ADDED,
      EVENTS.MEMBER_REMOVED,
      EVENTS.MEMBER_ROLE_CHANGED,
      EVENTS.INVITATION_SENT,
      EVENTS.INVITATION_REVOKED,
      EVENTS.INVITATION_ACCEPTED,
      RESYNC,
    ],
    (event) => {
      if (event.type === RESYNC) {
        void members.refresh();
        if (canManage) void invitations.refresh();
        return;
      }
      if (event.actor_id && event.actor_id === user?.id) return;
      if (event.type.startsWith('invitation.')) {
        if (canManage) void invitations.refresh();
        return;
      }
      void members.refresh();
    }
  );

  const withBusy = async (id: string, action: () => Promise<unknown>, success: string) => {
    setBusyId(id);
    try {
      await action();
      toast.success(success);
      await members.refresh();
      if (canManage) await invitations.refresh();
    } catch (err) {
      toast.error(err instanceof ApiError ? err.message : 'That did not work.');
    } finally {
      setBusyId(null);
    }
  };

  const changeRole = (member: WorkspaceMember, role: WorkspaceRole) =>
    withBusy(
      member.id,
      () => ApiService.updateMemberRole(member.id, role, workspaceId),
      `${member.fullName ?? member.email} is now ${role}.`
    );

  return (
    <div className="space-y-6 p-6">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="flex items-center gap-2 text-xl font-semibold text-slate-100">
            <Users className="h-5 w-5 text-brand-400" />
            Members
          </h1>
          <p className="mt-1 text-xs text-slate-500">
            Who can reach {activeWorkspace?.name ?? 'this workspace'}, and what they may do.
          </p>
        </div>
        <div className="flex gap-2">
          <Button variant="ghost" onClick={() => setAcceptOpen(true)}>
            <KeyRound className="h-4 w-4" />
            Enter invitation code
          </Button>
          {canManage && (
            <Button onClick={() => setInviteOpen(true)}>
              <UserPlus className="h-4 w-4" />
              Invite
            </Button>
          )}
        </div>
      </div>

      {/* ------------------------------------------------------- members -- */}
      <Card>
        {members.isInitialLoading ? (
          <ListSkeleton rows={3} />
        ) : members.error ? (
          <ErrorState
            title="Could not load members"
            message={members.error.message}
            onRetry={members.refresh}
          />
        ) : !members.data?.length ? (
          <EmptyState
            icon={<Users className="h-8 w-8" />}
            title="No members yet"
            description="Invite a colleague to share this workspace."
          />
        ) : (
          <ul className="divide-y divide-slate-800">
            {members.data.map((member) => {
              const isMe = member.userId === user?.id;
              const isOwner = member.role === 'owner';
              // An admin may not act on another admin: only the owner can.
              const isPeerAdmin = member.role === 'admin' && myRole !== 'owner';
              const mayEdit = canManage && !isOwner && !isPeerAdmin;

              return (
                <li
                  key={member.id}
                  className="flex flex-wrap items-center gap-3 px-4 py-3 first:pt-2 last:pb-2"
                >
                  <div className="min-w-0 flex-1">
                    <p className="truncate text-sm text-slate-200">
                      {member.fullName ?? member.email}
                      {online.includes(member.userId) && (
                        <span
                          className="ml-2 inline-block h-2 w-2 rounded-full bg-emerald-400 align-middle"
                          title="Online now"
                          aria-label="online"
                        />
                      )}
                      {isMe && <span className="ml-2 text-[11px] text-slate-500">(you)</span>}
                    </p>
                    <p className="truncate text-[11px] text-slate-500">
                      {member.email}
                      {member.invitedBy && ` · invited by ${member.invitedBy}`}
                    </p>
                  </div>

                  {mayEdit ? (
                    <label className="sr-only" htmlFor={`role-${member.id}`}>
                      Role for {member.email}
                    </label>
                  ) : null}
                  {mayEdit ? (
                    <select
                      id={`role-${member.id}`}
                      value={member.role}
                      disabled={busyId === member.id}
                      onChange={(e) => changeRole(member, e.target.value as WorkspaceRole)}
                      className="rounded-lg border border-slate-700 bg-slate-900 px-2 py-1 text-xs text-slate-200 focus:border-brand-500 focus:outline-none focus:ring-1 focus:ring-brand-500/40 disabled:opacity-50"
                    >
                      {ASSIGNABLE_ROLES.map((role) => (
                        <option key={role} value={role}>
                          {role}
                        </option>
                      ))}
                    </select>
                  ) : (
                    <RoleBadge role={member.role} />
                  )}

                  {ROLE_CAN.transferOwnership(myRole) && !isOwner && (
                    <button
                      onClick={() => setTransferring(member)}
                      aria-label={`Transfer ownership to ${member.email}`}
                      title="Transfer ownership"
                      className="rounded p-1.5 text-slate-500 hover:bg-slate-800 hover:text-brand-400"
                    >
                      <Crown className="h-4 w-4" />
                    </button>
                  )}

                  {(mayEdit || isMe) && !isOwner && (
                    <button
                      onClick={() => setRemoving(member)}
                      aria-label={isMe ? 'Leave this workspace' : `Remove ${member.email}`}
                      title={isMe ? 'Leave workspace' : 'Remove member'}
                      className="rounded p-1.5 text-slate-500 hover:bg-slate-800 hover:text-rose-400"
                    >
                      <Trash2 className="h-4 w-4" />
                    </button>
                  )}
                </li>
              );
            })}
          </ul>
        )}
      </Card>

      {/* --------------------------------------------------- invitations -- */}
      {canManage && (
        <Card>
          <div className="flex items-center justify-between border-b border-slate-800 px-4 py-2.5">
            <h2 className="text-sm font-medium text-slate-300">
              Pending invitations
              {pending.length > 0 && (
                <span className="ml-2 text-[11px] text-slate-500">{pending.length}</span>
              )}
            </h2>
          </div>

          {invitations.isInitialLoading ? (
            <ListSkeleton rows={2} />
          ) : invitations.error ? (
            <ErrorState
              title="Could not load invitations"
              message={invitations.error.message}
              onRetry={invitations.refresh}
            />
          ) : pending.length === 0 ? (
            <p className="px-4 py-6 text-center text-xs text-slate-500">
              No invitations are waiting to be accepted.
            </p>
          ) : (
            <ul className="divide-y divide-slate-800">
              {pending.map((invitation) => (
                <li key={invitation.id} className="flex flex-wrap items-center gap-3 px-4 py-3">
                  <div className="min-w-0 flex-1">
                    <p className="truncate text-sm text-slate-200">{invitation.email}</p>
                    <p className="text-[11px] text-slate-500">
                      Expires {new Date(invitation.expiresAt).toLocaleDateString()}
                      {invitation.invitedBy && ` · invited by ${invitation.invitedBy}`}
                    </p>
                  </div>
                  <RoleBadge role={invitation.role} />
                  <Badge variant={INVITE_STATUS_VARIANT[invitation.status]} size="sm">
                    {invitation.status}
                  </Badge>
                  <button
                    onClick={() =>
                      withBusy(
                        invitation.id,
                        () => ApiService.resendInvitation(invitation.id, workspaceId),
                        'A new invitation code has been sent.'
                      )
                    }
                    disabled={busyId === invitation.id}
                    aria-label={`Resend the invitation to ${invitation.email}`}
                    title="Resend (issues a new code)"
                    className="rounded p-1.5 text-slate-500 hover:bg-slate-800 hover:text-brand-400 disabled:opacity-50"
                  >
                    <RefreshCw className="h-4 w-4" />
                  </button>
                  <button
                    onClick={() =>
                      withBusy(
                        invitation.id,
                        () => ApiService.revokeInvitation(invitation.id, workspaceId),
                        'Invitation revoked.'
                      )
                    }
                    disabled={busyId === invitation.id}
                    aria-label={`Revoke the invitation to ${invitation.email}`}
                    title="Revoke"
                    className="rounded p-1.5 text-slate-500 hover:bg-slate-800 hover:text-rose-400 disabled:opacity-50"
                  >
                    <X className="h-4 w-4" />
                  </button>
                </li>
              ))}
            </ul>
          )}
        </Card>
      )}

      <InviteDialog
        isOpen={isInviteOpen}
        onClose={() => setInviteOpen(false)}
        workspaceId={workspaceId}
        onInvited={() => {
          toast.success('Invitation sent.');
          void invitations.refresh();
        }}
      />

      <AcceptInvitationDialog
        isOpen={isAcceptOpen}
        onClose={() => setAcceptOpen(false)}
        onAccepted={() => {
          void members.refresh();
        }}
      />

      <ConfirmDialog
        isOpen={Boolean(removing)}
        onCancel={() => setRemoving(null)}
        onConfirm={async () => {
          if (!removing) return;
          const member = removing;
          setRemoving(null);
          await withBusy(
            member.id,
            () => ApiService.removeMember(member.id, workspaceId),
            member.userId === user?.id ? 'You have left the workspace.' : 'Member removed.'
          );
        }}
        title={removing?.userId === user?.id ? 'Leave this workspace?' : 'Remove this member?'}
        description={
          removing?.userId === user?.id
            ? 'You will lose access to its documents, chats and reports until someone invites you back.'
            : `${removing?.fullName ?? removing?.email} will lose access immediately.`
        }
        confirmLabel={removing?.userId === user?.id ? 'Leave' : 'Remove'}
        variant="danger"
      />

      <ConfirmDialog
        isOpen={Boolean(transferring)}
        onCancel={() => setTransferring(null)}
        onConfirm={async () => {
          if (!transferring) return;
          const member = transferring;
          setTransferring(null);
          await withBusy(
            member.id,
            () => ApiService.transferOwnership(member.id, workspaceId),
            `${member.fullName ?? member.email} now owns this workspace.`
          );
        }}
        title="Transfer ownership?"
        description={`${transferring?.fullName ?? transferring?.email} will become the owner, and you will become an admin. Only they will be able to transfer it back.`}
        confirmLabel="Transfer"
        variant="danger"
      />
    </div>
  );
};

/**
 * Accepting an invitation.
 *
 * The code arrives by email rather than as a link: the API has no reliable
 * way to know what URL this app is served from, so it sends a value to paste
 * instead of guessing one and producing broken emails.
 */
const AcceptInvitationDialog: React.FC<{
  isOpen: boolean;
  onClose: () => void;
  onAccepted: () => void;
}> = ({ isOpen, onClose, onAccepted }) => {
  const toast = useToast();
  const { refresh: refreshWorkspaces } = useWorkspace();
  const [token, setToken] = useState('');
  const [isBusy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    setError(null);
    setBusy(true);
    try {
      const result = await ApiService.acceptInvitation(token.trim());
      toast.success(`You have joined ${result.workspaceName} as ${result.role}.`);
      setToken('');
      onAccepted();
      // The new workspace has to appear in the switcher straight away.
      await refreshWorkspaces?.();
      onClose();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'That code could not be used.');
    } finally {
      setBusy(false);
    }
  };

  return (
    <Modal
      isOpen={isOpen}
      onClose={() => {
        setToken('');
        setError(null);
        onClose();
      }}
      title="Accept an invitation"
      description="Paste the code from your invitation email."
      maxWidth="md"
    >
      <form onSubmit={submit} className="space-y-4">
        <Input
          label="Invitation code"
          required
          value={token}
          onChange={(e) => setToken(e.target.value)}
          placeholder="Paste the code here"
          leftIcon={<KeyRound className="h-4 w-4" />}
          autoFocus
        />
        {error && (
          <p role="alert" className="text-xs text-rose-400">
            {error}
          </p>
        )}
        <div className="flex justify-end gap-2">
          <Button type="button" variant="ghost" onClick={onClose}>
            Cancel
          </Button>
          <Button type="submit" isLoading={isBusy} disabled={!token.trim()}>
            Join workspace
          </Button>
        </div>
      </form>
    </Modal>
  );
};
