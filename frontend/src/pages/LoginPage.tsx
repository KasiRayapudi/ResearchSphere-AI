import React, { useState } from 'react';
import { NavLink, useNavigate } from 'react-router-dom';
import { Sparkles, ArrowRight, Lock, Mail, Github } from 'lucide-react';
import { Button } from '../components/common/Button';
import { Input } from '../components/common/Input';
import { useAuth } from '../contexts/AuthContext';

export const LoginPage: React.FC = () => {
  const [email, setEmail] = useState('alex.vance@enterprise-ai.io');
  const [password, setPassword] = useState('••••••••••••');
  const [isLoading, setIsLoading] = useState(false);
  const { login } = useAuth();
  const navigate = useNavigate();

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setIsLoading(true);
    await login(email, password);
    setIsLoading(false);
    navigate('/dashboard');
  };

  return (
    <div className="min-h-screen bg-slate-950 flex items-center justify-center p-6 relative overflow-hidden">
      <div className="fixed inset-0 bg-aurora-glow opacity-60 pointer-events-none" />

      <div className="w-full max-w-md rounded-2xl bg-slate-900/90 border border-slate-800 backdrop-blur-xl p-8 shadow-2xl z-10 relative">
        <div className="text-center mb-8 space-y-2">
          <div className="h-12 w-12 rounded-2xl bg-gradient-to-tr from-brand-600 to-indigo-500 flex items-center justify-center shadow-lg shadow-brand-500/30 mx-auto">
            <Sparkles className="h-6 w-6 text-white" />
          </div>
          <h2 className="text-2xl font-bold text-white">Welcome back</h2>
          <p className="text-xs text-slate-400">Sign in to your ResearchSphere AI workspace</p>
        </div>

        <form onSubmit={handleSubmit} className="space-y-4">
          <Input
            label="Work Email"
            type="email"
            placeholder="alex@company.com"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            leftIcon={<Mail className="h-4 w-4" />}
            required
          />

          <div>
            <div className="flex items-center justify-between mb-1">
              <label className="text-xs font-semibold uppercase tracking-wider text-slate-400">Password</label>
              <NavLink to="/forgot-password" className="text-xs text-brand-400 hover:underline">Forgot password?</NavLink>
            </div>
            <Input
              type="password"
              placeholder="••••••••"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              leftIcon={<Lock className="h-4 w-4" />}
              required
            />
          </div>

          <Button type="submit" variant="primary" className="w-full" isLoading={isLoading} icon={<ArrowRight className="h-4 w-4" />}>
            Sign In to Workspace
          </Button>
        </form>

        <div className="my-6 flex items-center gap-3 text-xs text-slate-500">
          <div className="flex-1 h-px bg-slate-800" />
          <span>OR CONTINUE WITH</span>
          <div className="flex-1 h-px bg-slate-800" />
        </div>

        <Button
          variant="outline"
          className="w-full"
          icon={<Github className="h-4 w-4" />}
          onClick={() => handleSubmit({ preventDefault: () => {} } as any)}
        >
          Sign In with GitHub OAuth
        </Button>

        <p className="mt-8 text-center text-xs text-slate-400">
          Don't have an account?{' '}
          <NavLink to="/signup" className="text-brand-400 font-semibold hover:underline">
            Create an enterprise account
          </NavLink>
        </p>
      </div>
    </div>
  );
};
