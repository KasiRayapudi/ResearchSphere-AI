import React, { useState } from 'react';
import { NavLink, useNavigate } from 'react-router-dom';
import { Sparkles, ArrowRight, Lock, Mail, User } from 'lucide-react';
import { Button } from '../components/common/Button';
import { Input } from '../components/common/Input';
import { useAuth } from '../contexts/AuthContext';
import { useToast } from '../contexts/ToastContext';
import { ApiError } from '../services/apiClient';
import { AlertCircle } from 'lucide-react';

export const SignupPage: React.FC = () => {
  const [name, setName] = useState('Alex Vance');
  const [email, setEmail] = useState('alex.vance@enterprise-ai.io');
  const [password, setPassword] = useState('••••••••••••');
  const [isLoading, setIsLoading] = useState(false);
  const { signup } = useAuth();
  const navigate = useNavigate();

  const [formErrors, setFormErrors] = useState<string[]>([]);
  const toast = useToast();

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (isLoading) return;
    setFormErrors([]);
    setIsLoading(true);
    try {
      await signup(name, email, password);
      navigate('/dashboard', { replace: true });
    } catch (err) {
      // The backend password policy returns every violation, so show them all
      // rather than only the first.
      if (err instanceof ApiError && err.fieldMessages.length > 0) {
        setFormErrors(err.fieldMessages);
      } else {
        setFormErrors([err instanceof Error ? err.message : 'Could not create your account.']);
      }
      toast.fromError(err, 'Could not create your account');
    } finally {
      setIsLoading(false);
    }
  };

  return (
    <div className="min-h-screen bg-slate-950 flex items-center justify-center p-6 relative overflow-hidden">
      <div className="fixed inset-0 bg-aurora-glow opacity-60 pointer-events-none" />

      <div className="w-full max-w-md rounded-2xl bg-slate-900/90 border border-slate-800 backdrop-blur-xl p-8 shadow-2xl z-10 relative">
        <div className="text-center mb-8 space-y-2">
          <div className="h-12 w-12 rounded-2xl bg-gradient-to-tr from-brand-600 to-indigo-500 flex items-center justify-center shadow-lg shadow-brand-500/30 mx-auto">
            <Sparkles className="h-6 w-6 text-white" />
          </div>
          <h2 className="text-2xl font-bold text-white">Create Enterprise Account</h2>
          <p className="text-xs text-slate-400">Start 14-day free trial with full RAG & Agent access</p>
        </div>

        {formErrors.length > 0 && (
          <div
            role="alert"
            className="mb-4 rounded-lg border border-rose-500/30 bg-rose-500/10 p-3 text-xs text-rose-200"
          >
            <div className="flex items-start gap-2">
              <AlertCircle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
              <ul className="space-y-1">
                {formErrors.map((m) => (
                  <li key={m}>{m}</li>
                ))}
              </ul>
            </div>
          </div>
        )}

        <form onSubmit={handleSubmit} className="space-y-4">
          <Input
            label="Full Name"
            type="text"
            placeholder="Alex Vance"
            value={name}
            onChange={(e) => setName(e.target.value)}
            leftIcon={<User className="h-4 w-4" />}
            required
          />

          <Input
            label="Work Email"
            type="email"
            placeholder="alex@company.com"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            leftIcon={<Mail className="h-4 w-4" />}
            required
          />

          <Input
            label="Password"
            type="password"
            placeholder="••••••••••••"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            leftIcon={<Lock className="h-4 w-4" />}
            required
          />

          <Button type="submit" variant="primary" className="w-full" isLoading={isLoading} icon={<ArrowRight className="h-4 w-4" />}>
            Create Account
          </Button>
        </form>

        <p className="mt-8 text-center text-xs text-slate-400">
          Already have an account?{' '}
          <NavLink to="/login" className="text-brand-400 font-semibold hover:underline">
            Sign in
          </NavLink>
        </p>
      </div>
    </div>
  );
};
