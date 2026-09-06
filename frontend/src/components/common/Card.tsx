import React from 'react';

interface CardProps extends React.HTMLAttributes<HTMLDivElement> {
  glass?: boolean;
  hoverEffect?: boolean;
  glow?: boolean;
}

export const Card: React.FC<CardProps> = ({
  children,
  glass = true,
  hoverEffect = true,
  glow = false,
  className = '',
  ...props
}) => {
  return (
    <div
      className={`rounded-xl p-5 border transition-all duration-300 ${
        glass
          ? 'bg-slate-900/60 backdrop-blur-xl border-slate-800/80 text-slate-100 shadow-lg'
          : 'bg-slate-900 border-slate-800 text-slate-100'
      } ${
        hoverEffect ? 'hover:border-slate-700 hover:shadow-brand-500/5 hover:-translate-y-0.5' : ''
      } ${glow ? 'gradient-border' : ''} ${className}`}
      {...props}
    >
      {children}
    </div>
  );
};
