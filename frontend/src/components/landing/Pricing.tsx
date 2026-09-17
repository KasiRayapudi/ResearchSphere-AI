import React, { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { Check } from 'lucide-react';
import { Button } from '../common/Button';
import { Card } from '../common/Card';

export const Pricing: React.FC = () => {
  const [annual, setAnnual] = useState(true);
  const navigate = useNavigate();

  const plans = [
    {
      name: 'Starter Researcher',
      price: annual ? '$19' : '$29',
      desc: 'Ideal for individual researchers and small AI experiments.',
      features: ['Up to 50 Documents Indexed', 'Hybrid RAG Search (Qdrant)', 'Basic Gemini 1.5 Model', 'GitHub MCP Connector', 'Community Support'],
      cta: 'Start Free Trial',
      highlighted: false,
    },
    {
      name: 'Pro Team Workspace',
      price: annual ? '$79' : '$99',
      desc: 'For growing research teams requiring multi-agent workflows and PDF reports.',
      features: ['Up to 1,000 Documents Indexed', '8 LangGraph Specialist Agents', 'Gemini Pro & GPT-4o Support', 'GitHub + Google Drive MCP', 'Executive PDF Report Export', '24/7 Priority Support'],
      cta: 'Get Started Pro',
      highlighted: true,
    },
    {
      name: 'Enterprise Scale',
      price: 'Custom',
      desc: 'Dedicated vector infrastructure, SLA guarantees, and custom MCP connectors.',
      features: ['Unlimited Documents & Storage', 'Custom Private LangGraph Nodes', 'Local Filesystem & Slack MCP', 'SOC2 Compliance & SSO Audit', 'Dedicated Solution Engineer', '99.99% Uptime Guarantee'],
      cta: 'Contact Sales',
      highlighted: false,
    },
  ];

  return (
    <section id="pricing" className="py-24 relative">
      <div className="max-w-7xl mx-auto px-6">
        <div className="text-center max-w-3xl mx-auto mb-16 space-y-4">
          <span className="px-3 py-1 rounded-full bg-brand-500/10 border border-brand-500/30 text-brand-300 text-xs font-mono font-semibold">
            Flexible Enterprise Pricing
          </span>
          <h2 className="text-3xl md:text-5xl font-extrabold text-white tracking-tight">
            Transparent Plans for <span className="gradient-text">Teams of All Sizes</span>
          </h2>
          <div className="flex items-center justify-center gap-3 pt-4">
            <span className={`text-xs font-semibold ${!annual ? 'text-white' : 'text-slate-400'}`}>Monthly</span>
            <button
              onClick={() => setAnnual(!annual)}
              className="w-12 h-6 rounded-full bg-slate-800 border border-slate-700 p-1 flex items-center transition-colors cursor-pointer"
            >
              <div
                className={`w-4 h-4 rounded-full bg-brand-500 transition-transform ${
                  annual ? 'translate-x-6' : 'translate-x-0'
                }`}
              />
            </button>
            <span className={`text-xs font-semibold ${annual ? 'text-white' : 'text-slate-400'}`}>
              Annual <span className="text-emerald-400 font-mono text-[10px] bg-emerald-500/10 px-1.5 py-0.5 rounded border border-emerald-500/20">Save 20%</span>
            </span>
          </div>
        </div>

        <div className="grid grid-cols-1 md:grid-cols-3 gap-8">
          {plans.map((p) => (
            <Card
              key={p.name}
              className={`p-8 flex flex-col justify-between relative ${
                p.highlighted ? 'border-brand-500 bg-slate-900 shadow-2xl shadow-brand-500/15' : 'bg-slate-950/60'
              }`}
            >
              {p.highlighted && (
                <div className="absolute -top-3 left-1/2 -translate-x-1/2 px-3 py-0.5 rounded-full bg-gradient-to-r from-brand-500 to-indigo-500 text-white text-[10px] font-bold tracking-wider uppercase shadow-md">
                  Most Popular
                </div>
              )}
              <div className="space-y-6">
                <div>
                  <h3 className="text-xl font-bold text-white">{p.name}</h3>
                  <p className="text-xs text-slate-400 mt-1">{p.desc}</p>
                </div>
                <div className="flex items-baseline gap-1">
                  <span className="text-4xl font-extrabold text-white">{p.price}</span>
                  {p.price !== 'Custom' && <span className="text-xs text-slate-400">/ month</span>}
                </div>
                <ul className="space-y-3 pt-4 border-t border-slate-800 text-xs text-slate-300">
                  {p.features.map((f) => (
                    <li key={f} className="flex items-center gap-2.5">
                      <Check className="h-4 w-4 text-brand-400 shrink-0" />
                      <span>{f}</span>
                    </li>
                  ))}
                </ul>
              </div>

              <Button
                variant={p.highlighted ? 'primary' : 'outline'}
                className="w-full mt-8"
                onClick={() => navigate('/dashboard')}
              >
                {p.cta}
              </Button>
            </Card>
          ))}
        </div>
      </div>
    </section>
  );
};
