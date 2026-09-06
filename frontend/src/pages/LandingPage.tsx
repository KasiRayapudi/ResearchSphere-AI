import React from 'react';
import { Navbar } from '../components/landing/Navbar';
import { HeroSection } from '../components/landing/HeroSection';
import { BentoGrid } from '../components/landing/BentoGrid';
import { WorkflowDiagram } from '../components/landing/WorkflowDiagram';
import { Pricing } from '../components/landing/Pricing';
import { FAQSection } from '../components/landing/FAQSection';
import { Footer } from '../components/landing/Footer';

export const LandingPage: React.FC = () => {
  return (
    <div className="min-h-screen bg-slate-950 text-slate-100 selection:bg-brand-500 selection:text-white">
      <Navbar />
      <main>
        <HeroSection />
        <BentoGrid />
        <WorkflowDiagram />
        <Pricing />
        <FAQSection />
      </main>
      <Footer />
    </div>
  );
};
