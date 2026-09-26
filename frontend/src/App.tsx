import React, { Suspense, lazy } from 'react';
import { BrowserRouter, Navigate, Route, Routes, useLocation } from 'react-router-dom';
import { AuthProvider, useAuth } from './contexts/AuthContext';
import { ThemeProvider } from './contexts/ThemeContext';
import { WorkspaceProvider } from './contexts/WorkspaceContext';
import { RealtimeProvider } from './contexts/RealtimeContext';
import { ToastProvider } from './contexts/ToastContext';
import { ErrorBoundary } from './components/common/ErrorBoundary';
import { FullPageLoader } from './components/common/States';
import { SessionExpiredDialog } from './components/common/SessionExpiredDialog';

import { LandingPage } from './pages/LandingPage';
import { LoginPage } from './pages/LoginPage';
import { SignupPage } from './pages/SignupPage';
import { ForgotPasswordPage } from './pages/ForgotPasswordPage';
import { AppLayout } from './components/layout/AppLayout';

// Route-level code splitting: the authenticated app is the bulk of the bundle
// and is not needed to render the landing or auth pages.
const DashboardPage = lazy(() =>
  import('./pages/DashboardPage').then((m) => ({ default: m.DashboardPage }))
);
const ChatPage = lazy(() => import('./pages/ChatPage').then((m) => ({ default: m.ChatPage })));
const DocumentManagerPage = lazy(() =>
  import('./pages/DocumentManagerPage').then((m) => ({ default: m.DocumentManagerPage }))
);
const ResearchWorkspacePage = lazy(() =>
  import('./pages/ResearchWorkspacePage').then((m) => ({ default: m.ResearchWorkspacePage }))
);
const ReportGeneratorPage = lazy(() =>
  import('./pages/ReportGeneratorPage').then((m) => ({ default: m.ReportGeneratorPage }))
);
const AnalyticsPage = lazy(() =>
  import('./pages/AnalyticsPage').then((m) => ({ default: m.AnalyticsPage }))
);
const MembersPage = lazy(() =>
  import('./pages/MembersPage').then((m) => ({ default: m.MembersPage }))
);
const AdminPanelPage = lazy(() =>
  import('./pages/AdminPanelPage').then((m) => ({ default: m.AdminPanelPage }))
);

/**
 * Blocks unauthenticated access. Waits for the session bootstrap to finish so
 * a page refresh with a valid token does not bounce the user to /login.
 */
const ProtectedRoute: React.FC<{ children: React.ReactNode }> = ({ children }) => {
  const { isAuthenticated, isBootstrapping } = useAuth();
  const location = useLocation();

  if (isBootstrapping) return <FullPageLoader label="Restoring your session" />;
  if (!isAuthenticated) {
    // Remember where they were headed so login can return them there.
    return <Navigate to="/login" replace state={{ from: location.pathname }} />;
  }
  return <>{children}</>;
};

/** Keeps signed-in users out of the login/signup pages. */
const PublicOnlyRoute: React.FC<{ children: React.ReactNode }> = ({ children }) => {
  const { isAuthenticated, isBootstrapping } = useAuth();
  if (isBootstrapping) return <FullPageLoader label="Loading" />;
  if (isAuthenticated) return <Navigate to="/dashboard" replace />;
  return <>{children}</>;
};

const AppRoutes: React.FC = () => (
  <Routes>
    <Route path="/" element={<LandingPage />} />
    <Route
      path="/login"
      element={
        <PublicOnlyRoute>
          <LoginPage />
        </PublicOnlyRoute>
      }
    />
    <Route
      path="/signup"
      element={
        <PublicOnlyRoute>
          <SignupPage />
        </PublicOnlyRoute>
      }
    />
    <Route path="/forgot-password" element={<ForgotPasswordPage />} />

    <Route
      element={
        <ProtectedRoute>
          <AppLayout />
        </ProtectedRoute>
      }
    >
      <Route path="/dashboard" element={<DashboardPage />} />
      <Route path="/chat" element={<ChatPage />} />
      <Route path="/documents" element={<DocumentManagerPage />} />
      <Route path="/workspace" element={<ResearchWorkspacePage />} />
      <Route path="/reports" element={<ReportGeneratorPage />} />
      <Route path="/analytics" element={<AnalyticsPage />} />
      <Route path="/members" element={<MembersPage />} />
      <Route path="/admin" element={<AdminPanelPage />} />
    </Route>

    <Route path="*" element={<Navigate to="/" replace />} />
  </Routes>
);

export const App: React.FC = () => (
  <ErrorBoundary section="Application">
    <ThemeProvider>
      <ToastProvider>
        <AuthProvider>
          <WorkspaceProvider>
            {/* Inside WorkspaceProvider: the socket follows the active workspace. */}
            <RealtimeProvider>
              <BrowserRouter>
                <Suspense fallback={<FullPageLoader label="Loading workspace" />}>
                  <AppRoutes />
                </Suspense>
                <SessionExpiredDialog />
              </BrowserRouter>
            </RealtimeProvider>
          </WorkspaceProvider>
        </AuthProvider>
      </ToastProvider>
    </ThemeProvider>
  </ErrorBoundary>
);
