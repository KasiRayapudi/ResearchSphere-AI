import React, { useState } from 'react';
import { AlertTriangle } from 'lucide-react';
import { Modal } from './Modal';
import { Button } from './Button';

interface ConfirmDialogProps {
  isOpen: boolean;
  title: string;
  description?: string;
  confirmLabel?: string;
  cancelLabel?: string;
  variant?: 'danger' | 'primary';
  /** Require typing this exact text before confirming (irreversible actions). */
  confirmPhrase?: string;
  isBusy?: boolean;
  onConfirm: () => void | Promise<void>;
  onCancel: () => void;
}

export const ConfirmDialog: React.FC<ConfirmDialogProps> = ({
  isOpen,
  title,
  description,
  confirmLabel = 'Confirm',
  cancelLabel = 'Cancel',
  variant = 'danger',
  confirmPhrase,
  isBusy = false,
  onConfirm,
  onCancel,
}) => {
  // Rendered only while open, so the confirm-phrase input starts empty on each
  // open without needing to reset state from an effect.
  if (!isOpen) return null;
  return <ConfirmDialogBody {...{ title, description, confirmLabel, cancelLabel, variant, confirmPhrase, isBusy, onConfirm, onCancel }} />;
};

const ConfirmDialogBody: React.FC<Omit<ConfirmDialogProps, 'isOpen'>> = ({
  title,
  description,
  confirmLabel = 'Confirm',
  cancelLabel = 'Cancel',
  variant = 'danger',
  confirmPhrase,
  isBusy = false,
  onConfirm,
  onCancel,
}) => {
  const [typed, setTyped] = useState('');

  const blocked = Boolean(confirmPhrase) && typed.trim() !== confirmPhrase;

  return (
    <Modal isOpen onClose={onCancel} maxWidth="md">
      <div className="flex gap-4">
        <div
          className={`mt-0.5 h-10 w-10 shrink-0 rounded-full ${
            variant === 'danger' ? 'bg-rose-500/15 text-rose-400' : 'bg-brand-500/15 text-brand-400'
          } flex items-center justify-center`}
        >
          <AlertTriangle className="h-5 w-5" />
        </div>
        <div className="min-w-0 flex-1">
          <h3 className="text-base font-semibold text-slate-100">{title}</h3>
          {description && (
            <p className="mt-1.5 text-sm leading-relaxed text-slate-400">{description}</p>
          )}

          {confirmPhrase && (
            <div className="mt-4">
              <label className="text-xs text-slate-400">
                Type <span className="font-mono font-semibold text-slate-200">{confirmPhrase}</span>{' '}
                to confirm
              </label>
              <input
                autoFocus
                value={typed}
                onChange={(e) => setTyped(e.target.value)}
                className="mt-1.5 w-full rounded-lg border border-slate-700 bg-slate-950/60 px-3 py-2 text-sm text-slate-100 outline-none focus:border-brand-500"
              />
            </div>
          )}

          <div className="mt-5 flex justify-end gap-2">
            <Button variant="ghost" size="sm" onClick={onCancel} disabled={isBusy}>
              {cancelLabel}
            </Button>
            <Button
              variant={variant === 'danger' ? 'danger' : 'primary'}
              size="sm"
              onClick={onConfirm}
              isLoading={isBusy}
              disabled={blocked || isBusy}
            >
              {confirmLabel}
            </Button>
          </div>
        </div>
      </div>
    </Modal>
  );
};
