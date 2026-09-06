import React, { useState } from 'react';
import { NavLink } from 'react-router-dom';
import { Sparkles, ArrowLeft, Mail, CheckCircle2 } from 'lucide-react';
import { Button } from '../components/common/Button';
import { Input } from '../components/common/Input';

export const ForgotPasswordPage: React.FC = () => {
  const [email, setEmail] = useState('');
  const [submitted, setSubmitted] = useState(false);

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    if (email) setSubmitted(true);
  };

  return (
    <div className="min-h-screen bg-slate-950 flex items-center justify-center p-6 relative overflow-hidden">
      <div className="fixed inset-0 bg-aurora-glow opacity-60 pointer-events-none" />

      <div className="w-full max-w-md rounded-2xl bg-slate-900/90 border border-slate-800 backdrop-blur-xl p-8 shadow-2xl z-10 relative">
        <div className="text-center mb-8 space-y-2">
          <div className="h-12 w-12 rounded-2xl bg-gradient-to-tr from-brand-600 to-indigo-500 flex items-center justify-center shadow-lg shadow-brand-500/30 mx-auto">
            <Sparkles className="h-6 w-6 text-white" />
          </div>
          <h2 className="text-2xl font-bold text-white">Reset Password</h2>
          <p className="text-xs text-slate-400">Enter your email to receive password reset instructions</p>
        </div>

        {submitted ? (
          <div className="p-6 rounded-xl bg-emerald-500/10 border border-emerald-500/20 text-center space-y-3">
            <CheckCircle2 className="h-8 w-8 text-emerald-400 mx-auto" />
            <h3 className="text-sm font-bold text-white">Reset Link Sent!</h3>
            <p className="text-xs text-slate-300">We emailed instructions to <span className="font-mono font-semibold text-emerald-300">{email}</span></p>
            <NavLink to="/login" className="inline-block mt-2 text-xs text-brand-400 font-semibold hover:underline">
              Return to Login
            </NavLink>
          </div>
        ) : (
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
            <Button type="submit" variant="primary" className="w-full">
              Send Reset Email
            </Button>
          </form>
        )}

        <div className="mt-8 text-center">
          <NavLink to="/login" className="inline-flex items-center gap-1.5 text-xs text-slate-400 hover:text-white">
            <ArrowLeft className="h-3.5 w-3.5" /> Back to Sign In
          </NavLink>
        </div>
      </div>
    </div>
  );
};
