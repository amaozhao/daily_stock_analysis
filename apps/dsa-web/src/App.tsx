import type React from 'react';
import { Suspense, lazy, useEffect } from 'react';
import { BrowserRouter as Router, Navigate, Route, Routes, useLocation } from 'react-router-dom';
import { ApiErrorAlert, Loading, Shell } from './components/common';
import { AuthProvider, useAuth } from './contexts/AuthContext';
import { useAgentChatStore } from './stores/agentChatStore';
import './App.css';

const HomePage = lazy(() => import('./pages/HomePage'));
const BacktestPage = lazy(() => import('./pages/BacktestPage'));
const SettingsPage = lazy(() => import('./pages/SettingsPage'));
const LoginPage = lazy(() => import('./pages/LoginPage'));
const NotFoundPage = lazy(() => import('./pages/NotFoundPage'));
const ChatPage = lazy(() => import('./pages/ChatPage'));
const PortfolioPage = lazy(() => import('./pages/PortfolioPage'));

const withPageFallback = (element: React.ReactElement) => (
  <Suspense fallback={<Loading className="min-h-[50vh]" />}>
    {element}
  </Suspense>
);

const AppContent: React.FC = () => {
  const location = useLocation();
  const { authEnabled, loggedIn, isLoading, loadError, refreshStatus } = useAuth();

  useEffect(() => {
    useAgentChatStore.getState().setCurrentRoute(location.pathname);
  }, [location.pathname]);

  if (isLoading) {
    return (
      <div className="flex min-h-screen items-center justify-center bg-base">
        <div className="h-8 w-8 animate-spin rounded-full border-2 border-cyan/20 border-t-cyan" />
      </div>
    );
  }

  if (loadError) {
    return (
      <div className="flex min-h-screen flex-col items-center justify-center gap-4 bg-base px-4">
        <div className="w-full max-w-lg">
          <ApiErrorAlert error={loadError} />
        </div>
        <button
          type="button"
          className="btn-primary"
          onClick={() => void refreshStatus()}
        >
          重试
        </button>
      </div>
    );
  }

  if (authEnabled && !loggedIn) {
    if (location.pathname === '/login') {
      return withPageFallback(<LoginPage />);
    }
    const redirect = encodeURIComponent(location.pathname + location.search);
    return <Navigate to={`/login?redirect=${redirect}`} replace />;
  }

  if (location.pathname === '/login') {
    return <Navigate to="/" replace />;
  }

  return (
    <Routes>
      <Route element={<Shell />}>
        <Route path="/" element={withPageFallback(<HomePage />)} />
        <Route path="/chat" element={withPageFallback(<ChatPage />)} />
        <Route path="/portfolio" element={withPageFallback(<PortfolioPage />)} />
        <Route path="/backtest" element={withPageFallback(<BacktestPage />)} />
        <Route path="/settings" element={withPageFallback(<SettingsPage />)} />
        <Route path="*" element={withPageFallback(<NotFoundPage />)} />
      </Route>
      <Route path="/login" element={withPageFallback(<LoginPage />)} />
    </Routes>
  );
};

const App: React.FC = () => {
  return (
    <Router>
      <AuthProvider>
        <AppContent />
      </AuthProvider>
    </Router>
  );
};

export default App;
