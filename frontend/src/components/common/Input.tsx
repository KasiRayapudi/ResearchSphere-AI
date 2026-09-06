import React from 'react';

interface InputProps extends React.InputHTMLAttributes<HTMLInputElement> {
  label?: string;
  error?: string;
  leftIcon?: React.ReactNode;
  rightIcon?: React.ReactNode;
}

export const Input: React.FC<InputProps> = ({
  label,
  error,
  leftIcon,
  rightIcon,
  className = '',
  id,
  ...props
}) => {
  const inputId = id || (label ? label.toLowerCase().replace(/\s+/g, '-') : undefined);

  return (
    <div className="w-full flex flex-col gap-1.5">
      {label && (
        <label htmlFor={inputId} className="text-xs font-semibold uppercase tracking-wider text-slate-400">
          {label}
        </label>
      )}
      <div className="relative flex items-center">
        {leftIcon && <div className="absolute left-3 text-slate-400 pointer-events-none">{leftIcon}</div>}
        <input
          id={inputId}
          className={`w-full rounded-lg border bg-slate-900/80 px-3 py-2 text-sm text-slate-100 placeholder-slate-500 transition-all duration-200 focus:outline-none focus:ring-2 focus:ring-brand-500/50 ${
            leftIcon ? 'pl-9' : ''
          } ${rightIcon ? 'pr-9' : ''} ${
            error ? 'border-red-500/80 focus:ring-red-500/40' : 'border-slate-800 focus:border-brand-500'
          } ${className}`}
          {...props}
        />
        {rightIcon && <div className="absolute right-3 text-slate-400">{rightIcon}</div>}
      </div>
      {error && <span className="text-xs text-red-400">{error}</span>}
    </div>
  );
};
