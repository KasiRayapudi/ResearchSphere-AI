import React, { useState, useEffect } from 'react';
import { NavLink, useNavigate } from 'react-router-dom';
import { Sparkles, ArrowRight, Menu, X } from 'lucide-react';
import { Button } from '../common/Button';

export const Navbar: React.FC = () => {
  const [scrolled, setScrolled] = useState(false);
  const [mobileOpen, setMobileOpen] = useState(false);
  const navigate = useNavigate();

  useEffect(() => {
    const handleScroll = () => setScrolled(window.scrollY > 20);
    window.addEventListener('scroll', handleScroll);
    return () => window.removeEventListener('scroll', handleScroll);
  }, []);

  return (
    <nav
      className={`fixed top-0 left-0 right-0 z-50 transition-all duration-300 ${
        scrolled ? 'bg-slate-950/80 backdrop-blur-xl border-b border-slate-800/80 py-3' : 'bg-transparent py-5'
      }`}
    >
      <div className="max-w-7xl mx-auto px-6 flex items-center justify-between">
        <NavLink to="/" className="flex items-center gap-3">
          <div className="h-9 w-9 rounded-xl bg-gradient-to-tr from-brand-600 via-indigo-500 to-purple-500 flex items-center justify-center shadow-lg shadow-brand-500/30">
            <Sparkles className="h-5 w-5 text-white" />
          </div>
          <span className="font-extrabold text-lg tracking-tight text-white flex items-center gap-2">
            ResearchSphere <span className="text-xs font-mono font-bold bg-brand-500/20 text-brand-400 px-2 py-0.5 rounded border border-brand-500/30">AI</span>
          </span>
        </NavLink>

        <div className="hidden md:flex items-center gap-8 text-sm font-medium text-slate-300">
          <a href="#features" className="hover:text-brand-400 transition-colors">Features</a>
          <a href="#architecture" className="hover:text-brand-400 transition-colors">Architecture</a>
          <a href="#workflow" className="hover:text-brand-400 transition-colors">LangGraph Agents</a>
          <a href="#pricing" className="hover:text-brand-400 transition-colors">Pricing</a>
          <a href="#faq" className="hover:text-brand-400 transition-colors">FAQ</a>
        </div>

        <div className="hidden md:flex items-center gap-3">
          <Button variant="ghost" size="sm" onClick={() => navigate('/login')}>
            Sign In
          </Button>
          <Button
            variant="primary"
            size="sm"
            onClick={() => navigate('/dashboard')}
            icon={<ArrowRight className="h-4 w-4" />}
          >
            Launch Platform
          </Button>
        </div>

        <button
          onClick={() => setMobileOpen(!mobileOpen)}
          className="md:hidden p-2 text-slate-400 hover:text-white"
        >
          {mobileOpen ? <X className="h-6 w-6" /> : <Menu className="h-6 w-6" />}
        </button>
      </div>

      {mobileOpen && (
        <div className="md:hidden bg-slate-950 border-b border-slate-800 px-6 py-4 flex flex-col gap-4 text-slate-300 font-medium">
          <a href="#features" onClick={() => setMobileOpen(false)}>Features</a>
          <a href="#architecture" onClick={() => setMobileOpen(false)}>Architecture</a>
          <a href="#workflow" onClick={() => setMobileOpen(false)}>Agents</a>
          <a href="#pricing" onClick={() => setMobileOpen(false)}>Pricing</a>
          <div className="flex flex-col gap-2 pt-2 border-t border-slate-800">
            <Button variant="outline" onClick={() => navigate('/login')}>Sign In</Button>
            <Button variant="primary" onClick={() => navigate('/dashboard')}>Launch Platform</Button>
          </div>
        </div>
      )}
    </nav>
  );
};
