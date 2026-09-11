import React, { memo, useState } from 'react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
// PrismLight + explicit language registration: the full `Prism` build bundles
// every supported grammar and added ~700 kB to the chat chunk on its own.
import { PrismLight as SyntaxHighlighter } from 'react-syntax-highlighter';
import { oneDark } from 'react-syntax-highlighter/dist/esm/styles/prism';
import bash from 'react-syntax-highlighter/dist/esm/languages/prism/bash';
import json from 'react-syntax-highlighter/dist/esm/languages/prism/json';
import markdown from 'react-syntax-highlighter/dist/esm/languages/prism/markdown';
import python from 'react-syntax-highlighter/dist/esm/languages/prism/python';
import sql from 'react-syntax-highlighter/dist/esm/languages/prism/sql';
import tsx from 'react-syntax-highlighter/dist/esm/languages/prism/tsx';
import typescript from 'react-syntax-highlighter/dist/esm/languages/prism/typescript';
import yaml from 'react-syntax-highlighter/dist/esm/languages/prism/yaml';

([
  ['bash', bash], ['json', json], ['markdown', markdown], ['python', python],
  ['sql', sql], ['tsx', tsx], ['typescript', typescript], ['ts', typescript],
  ['js', tsx], ['javascript', tsx], ['yaml', yaml], ['yml', yaml],
] as const).forEach(([name, lang]) => SyntaxHighlighter.registerLanguage(name, lang));
import { Check, Copy } from 'lucide-react';

/** Code block with a language label and its own copy button. */
const CodeBlock: React.FC<{ language: string; value: string }> = ({ language, value }) => {
  const [copied, setCopied] = useState(false);

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(value);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1600);
    } catch {
      /* clipboard blocked */
    }
  };

  return (
    <div className="group/code relative my-3 overflow-hidden rounded-lg border border-slate-800">
      <div className="flex items-center justify-between border-b border-slate-800 bg-slate-900/80 px-3 py-1.5">
        <span className="font-mono text-[10px] uppercase tracking-wider text-slate-500">
          {language || 'code'}
        </span>
        <button
          onClick={copy}
          aria-label="Copy code"
          className="flex items-center gap-1 rounded px-1.5 py-0.5 text-[10px] text-slate-400 transition-colors hover:bg-slate-800 hover:text-slate-200"
        >
          {copied ? <Check className="h-3 w-3 text-emerald-400" /> : <Copy className="h-3 w-3" />}
          {copied ? 'Copied' : 'Copy'}
        </button>
      </div>
      <SyntaxHighlighter
        language={language || 'text'}
        style={oneDark}
        customStyle={{ margin: 0, background: 'rgb(2 6 23)', fontSize: '12px', padding: '12px' }}
        wrapLongLines
      >
        {value}
      </SyntaxHighlighter>
    </div>
  );
};

/**
 * Renders assistant markdown. Memoised because streaming re-renders the
 * message on every token, and re-parsing markdown each time is the single
 * biggest cost in the chat view.
 */
export const MarkdownMessage = memo<{ content: string }>(({ content }) => (
  <div className="prose-chat text-sm leading-relaxed text-slate-200">
    <ReactMarkdown
      remarkPlugins={[remarkGfm]}
      components={{
        code({ inline, className, children, ...props }: any) {
          const match = /language-(\w+)/.exec(className || '');
          const text = String(children).replace(/\n$/, '');
          if (!inline && (match || text.includes('\n'))) {
            return <CodeBlock language={match?.[1] ?? ''} value={text} />;
          }
          return (
            <code
              className="rounded border border-slate-800 bg-slate-900 px-1.5 py-0.5 font-mono text-[12px] text-brand-300"
              {...props}
            >
              {children}
            </code>
          );
        },
        a({ children, href }) {
          return (
            <a
              href={href}
              target="_blank"
              rel="noopener noreferrer"
              className="text-brand-400 underline underline-offset-2 hover:text-brand-300"
            >
              {children}
            </a>
          );
        },
        h1: ({ children }) => (
          <h1 className="mb-2 mt-4 text-base font-bold text-slate-100">{children}</h1>
        ),
        h2: ({ children }) => (
          <h2 className="mb-2 mt-4 text-sm font-bold text-slate-100">{children}</h2>
        ),
        h3: ({ children }) => (
          <h3 className="mb-1.5 mt-3 text-sm font-semibold text-slate-200">{children}</h3>
        ),
        p: ({ children }) => <p className="mb-2.5 last:mb-0">{children}</p>,
        ul: ({ children }) => (
          <ul className="mb-2.5 ml-4 list-disc space-y-1 marker:text-slate-600">{children}</ul>
        ),
        ol: ({ children }) => (
          <ol className="mb-2.5 ml-4 list-decimal space-y-1 marker:text-slate-600">{children}</ol>
        ),
        blockquote: ({ children }) => (
          <blockquote className="my-2 border-l-2 border-brand-500/50 pl-3 text-slate-400">
            {children}
          </blockquote>
        ),
        table: ({ children }) => (
          <div className="my-3 overflow-x-auto rounded-lg border border-slate-800">
            <table className="w-full text-xs">{children}</table>
          </div>
        ),
        th: ({ children }) => (
          <th className="border-b border-slate-800 bg-slate-900/80 px-3 py-2 text-left font-semibold text-slate-300">
            {children}
          </th>
        ),
        td: ({ children }) => (
          <td className="border-b border-slate-800/60 px-3 py-2 text-slate-400">{children}</td>
        ),
        hr: () => <hr className="my-4 border-slate-800" />,
      }}
    >
      {content}
    </ReactMarkdown>
  </div>
));

MarkdownMessage.displayName = 'MarkdownMessage';
