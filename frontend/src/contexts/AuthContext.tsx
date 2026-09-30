import React, {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
} from 'react';
import { User } from '../types';
import { ApiService } from '../services/api';
import { onSessionExpired, refreshSession, tokenStore } from '../services/apiClient';

interface AuthContextType {
  user: User | null;
  /** True only once a real token has been validated against /auth/me. */
  isAuthenticated: boolean;
  /** True while the stored session is being validated on first load. */
  isBootstrapping: boolean;
  /** Set when the session expired and could not be silently refreshed. */
  sessionExpired: boolean;
  login: (email: string, pass: string) => Promise<void>;
  signup: (name: string, email: string, pass: string) => Promise<void>;
  logout: () => Promise<void>;
  /** Clear the expiry dialog after the user acknowledges it. */
  acknowledgeExpiry: () => void;
  isAdmin: boolean;
}

const AuthContext = createContext<AuthContextType | undefined>(undefined);

/** Refresh proactively so a long-lived tab does not hit a 401 mid-action. */
const SILENT_REFRESH_INTERVAL_MS = 10 * 60 * 1000;

export const AuthProvider: React.FC<{ children: React.ReactNode }> = ({ children }) => {
  // Starts null, not a mock user: previously the app booted "authenticated"
  // and ProtectedRoute never blocked anything.
  const [user, setUser] = useState<User | null>(null);
  const [isBootstrapping, setBootstrapping] = useState(true);
  const [sessionExpired, setSessionExpired] = useState(false);
  const refreshTimer = useRef<number | null>(null);

  // --- restore an existing session on load -------------------------------
  useEffect(() => {
    let cancelled = false;

    (async () => {
      if (!tokenStore.hasSession()) {
        if (!cancelled) setBootstrapping(false);
        return;
      }
      try {
        const me = await ApiService.me();
        if (!cancelled) setUser(me);
      } catch {
        // Token invalid/expired and refresh already attempted by the client.
        tokenStore.clear();
        if (!cancelled) setUser(null);
      } finally {
        if (!cancelled) setBootstrapping(false);
      }
    })();

    return () => {
      cancelled = true;
    };
  }, []);

  // --- react to a hard session expiry from any in-flight request ---------
  useEffect(
    () =>
      onSessionExpired(() => {
        setUser((current) => {
          // Only surface the dialog to someone who was actually signed in.
          if (current) setSessionExpired(true);
          return null;
        });
      }),
    []
  );

  // --- silent background refresh ----------------------------------------
  useEffect(() => {
    if (!user) return;
    const tick = async () => {
      if (!tokenStore.getRefresh()) return;
      await refreshSession();
    };
    refreshTimer.current = window.setInterval(tick, SILENT_REFRESH_INTERVAL_MS);
    // Also refresh when a backgrounded tab becomes visible again.
    const onVisible = () => {
      if (document.visibilityState === 'visible') void tick();
    };
    document.addEventListener('visibilitychange', onVisible);
    return () => {
      if (refreshTimer.current) window.clearInterval(refreshTimer.current);
      document.removeEventListener('visibilitychange', onVisible);
    };
  }, [user]);

  const login = useCallback(async (email: string, pass: string) => {
    const result = await ApiService.login(email, pass);
    setUser(result.user);
    setSessionExpired(false);
  }, []);

  const signup = useCallback(async (name: string, email: string, pass: string) => {
    const result = await ApiService.signup(name, email, pass);
    setUser(result.user);
    setSessionExpired(false);
  }, []);

  const logout = useCallback(async () => {
    try {
      await ApiService.logout();
    } catch {
      // A failed logout call must still clear local state.
    } finally {
      tokenStore.clear();
      setUser(null);
      setSessionExpired(false);
    }
  }, []);

  const value = useMemo<AuthContextType>(
    () => ({
      user,
      isAuthenticated: Boolean(user),
      isBootstrapping,
      sessionExpired,
      login,
      signup,
      logout,
      acknowledgeExpiry: () => setSessionExpired(false),
      isAdmin: (user?.role ?? '') === 'admin' || (user?.role ?? '') === 'owner',
    }),
    [user, isBootstrapping, sessionExpired, login, signup, logout]
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
};

export const useAuth = () => {
  const context = useContext(AuthContext);
  if (!context) throw new Error('useAuth must be used within AuthProvider');
  return context;
};
