import React from 'react';
import { useNavigate } from 'react-router-dom';
import { LogIn, ShieldAlert } from 'lucide-react';
import { Modal } from './Modal';
import { Button } from './Button';
import { useAuth } from '../../contexts/AuthContext';

/**
 * Shown when a session could not be silently refreshed. Previously the app
 * simply swapped in mock data and carried on, so an expired session was
 * invisible to the user.
 */
export const SessionExpiredDialog: React.FC = () => {
  const { sessionExpired, acknowledgeExpiry } = useAuth();
  const navigate = useNavigate();

  if (!sessionExpired) return null;

  const goToLogin = () => {
    acknowledgeExpiry();
    navigate('/login', { replace: true });
  };

  return (
    <Modal isOpen onClose={goToLogin} maxWidth="sm">
      <div className="flex flex-col items-center gap-4 text-center">
        <div className="flex h-12 w-12 items-center justify-center rounded-full bg-amber-500/15 text-amber-400">
          <ShieldAlert className="h-6 w-6" />
        </div>
        <div>
          <h3 className="text-base font-semibold text-slate-100">Your session has expired</h3>
          <p className="mt-1.5 text-sm leading-relaxed text-slate-400">
            For your security you have been signed out. Sign in again to pick up where you left
            off.
          </p>
        </div>
        <Button
          variant="primary"
          className="w-full"
          onClick={goToLogin}
          icon={<LogIn className="h-4 w-4" />}
        >
          Sign in again
        </Button>
      </div>
    </Modal>
  );
};
