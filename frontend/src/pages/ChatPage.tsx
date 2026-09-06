import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  AlertCircle,
  BookOpen,
  Bot,
  Check,
  ChevronDown,
  Copy,
  FileText,
  MessageSquare,
  RotateCcw,
  Send,
  Sparkles,
  Square,
  User as UserIcon,
} from 'lucide-react';
import { Badge } from '../components/common/Badge';
import { Button } from '../components/common/Button';
import { Modal } from '../components/common/Modal';
import { EmptyState } from '../components/common/States';
import { MarkdownMessage } from '../components/chat/MarkdownMessage';
import { SSEService } from '../services/sseService';
import { ApiService } from '../services/api';
import { useWorkspace } from '../contexts/WorkspaceContext';
import { useToast } from '../contexts/ToastContext';
import { ChatMessage, Document, SourceCitation } from '../types';

interface UiMessage extends ChatMessage {
  /** Set when the request failed, enabling a retry action. */
  error?: string;
  responseTimeMs?: number;
}

const SUGGESTIONS = [
  'Summarise the key findings across my documents',
  'What are the main risks identified?',
  'Compare the approaches described in my sources',
];

export const ChatPage: React.FC = () => {
  const { activeWorkspace } = useWorkspace();
  const toast = useToast();

  const [messages, setMessages] = useState<UiMessage[]>([]);
  const [input, setInput] = useState('');
  const [isStreaming, setStreaming] = useState(false);
  const [selectedCitation, setSelectedCitation] = useState<SourceCitation | null>(null);
  const [copiedId, setCopiedId] = useState<string | null>(null);
  const [documents, setDocuments] = useState<Document[]>([]);
  const [autoScroll, setAutoScroll] = useState(true);

  const abortRef = useRef<AbortController | null>(null);
  const endRef = useRef<HTMLDivElement>(null);
  const scrollRef = useRef<HTMLDivElement>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const lastPromptRef = useRef<string>('');

  // Document titles let citations show a real filename instead of an id.
  useEffect(() => {
    let cancelled = false;
    ApiService.getDocuments(activeWorkspace?.id)
      .then((docs) => !cancelled && setDocuments(docs))
      .catch(() => {
        /* citation titles degrade to the document id; not worth a toast */
      });
    return () => {
      cancelled = true;
    };
  }, [activeWorkspace?.id]);

  const titleFor = useCallback(
    (documentId: string) => documents.find((d) => d.id === documentId)?.title ?? '',
    [documents]
  );

  // Auto-scroll, but stop fighting the user if they scroll up to read.
  useEffect(() => {
    if (autoScroll) endRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [messages, autoScroll]);

  const onScroll = () => {
    const el = scrollRef.current;
    if (!el) return;
    const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 120;
    setAutoScroll(atBottom);
  };

  const stop = useCallback(() => {
    abortRef.current?.abort();
    abortRef.current = null;
    setStreaming(false);
  }, []);

  const runPrompt = useCallback(
    async (prompt: string, replaceAssistantId?: string) => {
      const controller = new AbortController();
      abortRef.current = controller;
      lastPromptRef.current = prompt;
      setStreaming(true);
      setAutoScroll(true);

      const assistantId = replaceAssistantId ?? `a-${Date.now()}`;
      setMessages((prev) => {
        const base = replaceAssistantId
          ? prev.filter((m) => m.id !== replaceAssistantId)
          : prev;
        return [
          ...base,
          {
            id: assistantId,
            role: 'assistant',
            content: '',
            timestamp: new Date().toISOString(),
            isStreaming: true,
            citations: [],
          },
        ];
      });

      await SSEService.streamChat(
        {
          prompt,
          workspaceId: activeWorkspace?.id,
          signal: controller.signal,
        },
        {
          onToken: (token) =>
            setMessages((prev) =>
              prev.map((m) =>
                m.id === assistantId ? { ...m, content: m.content + token } : m
              )
            ),
          onCitations: (citations) =>
            setMessages((prev) =>
              prev.map((m) => (m.id === assistantId ? { ...m, citations } : m))
            ),
          onComplete: ({ citations, responseTimeMs }) =>
            setMessages((prev) =>
              prev.map((m) =>
                m.id === assistantId
                  ? { ...m, isStreaming: false, citations: citations.length ? citations : m.citations, responseTimeMs }
                  : m
              )
            ),
          onError: (err) => {
            setMessages((prev) =>
              prev.map((m) =>
                m.id === assistantId
                  ? { ...m, isStreaming: false, error: err.message }
                  : m
              )
            );
            toast.fromError(err, 'The assistant could not respond');
          },
        }
      );

      setStreaming(false);
      abortRef.current = null;
    },
    [activeWorkspace?.id, toast]
  );

  const send = useCallback(
    (text?: string) => {
      const prompt = (text ?? input).trim();
      if (!prompt || isStreaming) return;
      setMessages((prev) => [
        ...prev,
        {
          id: `u-${Date.now()}`,
          role: 'user',
          content: prompt,
          timestamp: new Date().toISOString(),
        },
      ]);
      setInput('');
      void runPrompt(prompt);
    },
    [input, isStreaming, runPrompt]
  );

  /** Re-run the last prompt, replacing the previous assistant answer. */
  const regenerate = useCallback(
    (assistantId: string) => {
      if (isStreaming) return;
      const idx = messages.findIndex((m) => m.id === assistantId);
      const priorUser = [...messages.slice(0, idx)].reverse().find((m) => m.role === 'user');
      if (!priorUser) return;
      void runPrompt(priorUser.content, assistantId);
    },
    [messages, isStreaming, runPrompt]
  );

  const copyMessage = async (message: UiMessage) => {
    try {
      await navigator.clipboard.writeText(message.content);
      setCopiedId(message.id);
      window.setTimeout(() => setCopiedId(null), 1600);
    } catch {
      toast.warning('Could not copy', 'Clipboard access was blocked by the browser.');
    }
  };

  // Auto-grow the composer up to a sensible ceiling.
  useEffect(() => {
    const el = textareaRef.current;
    if (!el) return;
    el.style.height = 'auto';
    el.style.height = `${Math.min(el.scrollHeight, 180)}px`;
  }, [input]);

  const hasMessages = messages.length > 0;
  const activeCitations = useMemo(
    () => messages.flatMap((m) => m.citations ?? []),
    [messages]
  );

  return (
    <div className="flex h-[calc(100vh-4rem)] flex-col">
      {/* Header */}
      <div className="flex shrink-0 items-center justify-between border-b border-slate-800/70 px-5 py-3">
        <div className="flex items-center gap-3">
          <div className="flex h-9 w-9 items-center justify-center rounded-lg bg-gradient-to-tr from-brand-600 to-indigo-500">
            <Sparkles className="h-4.5 w-4.5 text-white" />
          </div>
          <div>
            <h1 className="text-sm font-semibold text-slate-100">Research Chat</h1>
            <p className="text-[11px] text-slate-500">
              {activeWorkspace ? `Grounded in ${activeWorkspace.name}` : 'No workspace selected'}
            </p>
          </div>
        </div>
        {activeCitations.length > 0 && (
          <Badge variant="brand" size="sm" icon={<BookOpen className="h-3 w-3" />}>
            {activeCitations.length} sources cited
          </Badge>
        )}
      </div>

      {/* Messages */}
      <div ref={scrollRef} onScroll={onScroll} className="flex-1 overflow-y-auto px-5 py-6">
        {!hasMessages ? (
          <div className="mx-auto max-w-2xl pt-10">
            <EmptyState
              icon={<MessageSquare className="h-6 w-6" />}
              title="Ask anything about your documents"
              description="Answers are generated only from documents indexed in this workspace, with citations back to the source."
            />
            <div className="mt-5 grid gap-2 sm:grid-cols-3">
              {SUGGESTIONS.map((s) => (
                <button
                  key={s}
                  onClick={() => send(s)}
                  className="rounded-lg border border-slate-800 bg-slate-900/50 p-3 text-left text-xs text-slate-400 transition-colors hover:border-brand-500/40 hover:text-slate-200"
                >
                  {s}
                </button>
              ))}
            </div>
          </div>
        ) : (
          <div className="mx-auto max-w-3xl space-y-6">
            {messages.map((m) => (
              <div key={m.id} className="flex gap-3">
                <div
                  className={`flex h-8 w-8 shrink-0 items-center justify-center rounded-lg ${
                    m.role === 'user'
                      ? 'bg-slate-800 text-slate-300'
                      : 'bg-gradient-to-tr from-brand-600 to-indigo-500 text-white'
                  }`}
                >
                  {m.role === 'user' ? (
                    <UserIcon className="h-4 w-4" />
                  ) : (
                    <Bot className="h-4 w-4" />
                  )}
                </div>

                <div className="min-w-0 flex-1">
                  <div className="mb-1 flex items-center gap-2">
                    <span className="text-xs font-semibold text-slate-300">
                      {m.role === 'user' ? 'You' : 'ResearchSphere'}
                    </span>
                    <time
                      dateTime={m.timestamp}
                      className="text-[10px] text-slate-600"
                      title={new Date(m.timestamp).toLocaleString()}
                    >
                      {new Date(m.timestamp).toLocaleTimeString([], {
                        hour: '2-digit',
                        minute: '2-digit',
                      })}
                    </time>
                    {typeof m.responseTimeMs === 'number' && m.responseTimeMs > 0 && (
                      <span className="text-[10px] text-slate-600">{m.responseTimeMs} ms</span>
                    )}
                  </div>

                  {m.role === 'user' ? (
                    <p className="whitespace-pre-wrap text-sm leading-relaxed text-slate-300">
                      {m.content}
                    </p>
                  ) : m.error ? (
                    <div className="flex flex-col items-start gap-2 rounded-lg border border-rose-500/25 bg-rose-500/5 p-3">
                      <p className="flex items-center gap-2 text-xs text-rose-300">
                        <AlertCircle className="h-3.5 w-3.5" />
                        {m.error}
                      </p>
                      <Button
                        size="sm"
                        variant="secondary"
                        onClick={() => regenerate(m.id)}
                        icon={<RotateCcw className="h-3 w-3" />}
                      >
                        Retry
                      </Button>
                    </div>
                  ) : (
                    <>
                      <MarkdownMessage content={m.content} />
                      {m.isStreaming && (
                        <span className="ml-0.5 inline-block h-4 w-1.5 animate-pulse bg-brand-400 align-middle" />
                      )}

                      {!m.isStreaming && m.content && (
                        <div className="mt-2 flex items-center gap-1">
                          <button
                            onClick={() => copyMessage(m)}
                            className="flex items-center gap-1 rounded px-1.5 py-1 text-[11px] text-slate-500 transition-colors hover:bg-slate-800 hover:text-slate-300"
                          >
                            {copiedId === m.id ? (
                              <Check className="h-3 w-3 text-emerald-400" />
                            ) : (
                              <Copy className="h-3 w-3" />
                            )}
                            {copiedId === m.id ? 'Copied' : 'Copy'}
                          </button>
                          <button
                            onClick={() => regenerate(m.id)}
                            disabled={isStreaming}
                            className="flex items-center gap-1 rounded px-1.5 py-1 text-[11px] text-slate-500 transition-colors hover:bg-slate-800 hover:text-slate-300 disabled:opacity-40"
                          >
                            <RotateCcw className="h-3 w-3" />
                            Regenerate
                          </button>
                        </div>
                      )}

                      {(m.citations?.length ?? 0) > 0 && (
                        <CitationList
                          citations={m.citations!}
                          titleFor={titleFor}
                          onSelect={setSelectedCitation}
                        />
                      )}
                    </>
                  )}
                </div>
              </div>
            ))}
            <div ref={endRef} />
          </div>
        )}
      </div>

      {/* Composer */}
      <div className="shrink-0 border-t border-slate-800/70 bg-slate-950/60 px-5 py-3">
        <div className="mx-auto max-w-3xl">
          {!autoScroll && hasMessages && (
            <button
              onClick={() => {
                setAutoScroll(true);
                endRef.current?.scrollIntoView({ behavior: 'smooth' });
              }}
              className="mx-auto mb-2 flex items-center gap-1 rounded-full border border-slate-700 bg-slate-900 px-3 py-1 text-[11px] text-slate-400 hover:text-slate-200"
            >
              <ChevronDown className="h-3 w-3" /> Jump to latest
            </button>
          )}
          <div className="flex items-end gap-2 rounded-xl border border-slate-800 bg-slate-900/70 p-2 focus-within:border-brand-500/50">
            <textarea
              ref={textareaRef}
              rows={1}
              value={input}
              onChange={(e) => setInput(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter' && !e.shiftKey) {
                  e.preventDefault();
                  send();
                }
              }}
              placeholder={
                activeWorkspace ? 'Ask about your documents…' : 'Create a workspace to start'
              }
              disabled={!activeWorkspace}
              aria-label="Message"
              className="max-h-[180px] flex-1 resize-none bg-transparent px-2 py-1.5 text-sm text-slate-200 outline-none placeholder:text-slate-600 disabled:opacity-50"
            />
            {isStreaming ? (
              <Button
                size="sm"
                variant="secondary"
                onClick={stop}
                icon={<Square className="h-3 w-3" />}
              >
                Stop
              </Button>
            ) : (
              <Button
                size="sm"
                onClick={() => send()}
                disabled={!input.trim() || !activeWorkspace}
                icon={<Send className="h-3.5 w-3.5" />}
              >
                Send
              </Button>
            )}
          </div>
          <p className="mt-1.5 text-center text-[10px] text-slate-600">
            Answers are grounded in your indexed documents. Verify important results.
          </p>
        </div>
      </div>

      {/* Citation detail */}
      <Modal
        isOpen={Boolean(selectedCitation)}
        onClose={() => setSelectedCitation(null)}
        title="Source excerpt"
        maxWidth="2xl"
      >
        {selectedCitation && (
          <div className="space-y-3">
            <div className="flex flex-wrap items-center gap-2">
              <Badge variant="neutral" size="sm" icon={<FileText className="h-3 w-3" />}>
                {titleFor(selectedCitation.documentId) || selectedCitation.documentId || 'Document'}
              </Badge>
              {selectedCitation.confidenceScore > 0 && (
                <Badge variant="success" size="sm">
                  {(selectedCitation.confidenceScore * 100).toFixed(1)}% match
                </Badge>
              )}
            </div>
            <p className="max-h-[50vh] overflow-y-auto whitespace-pre-wrap rounded-lg border border-slate-800 bg-slate-950/60 p-4 text-xs leading-relaxed text-slate-300">
              {selectedCitation.excerpt}
            </p>
          </div>
        )}
      </Modal>
    </div>
  );
};

/** Collapsible reference list under an assistant message. */
const CitationList: React.FC<{
  citations: SourceCitation[];
  titleFor: (id: string) => string;
  onSelect: (c: SourceCitation) => void;
}> = ({ citations, titleFor, onSelect }) => {
  const [open, setOpen] = useState(false);

  return (
    <div className="mt-3 rounded-lg border border-slate-800 bg-slate-900/40">
      <button
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        className="flex w-full items-center justify-between px-3 py-2 text-[11px] font-medium text-slate-400 hover:text-slate-200"
      >
        <span className="flex items-center gap-1.5">
          <BookOpen className="h-3 w-3" />
          {citations.length} source{citations.length === 1 ? '' : 's'}
        </span>
        <ChevronDown className={`h-3 w-3 transition-transform ${open ? 'rotate-180' : ''}`} />
      </button>
      {open && (
        <ul className="space-y-1 border-t border-slate-800 p-2">
          {citations.map((c, i) => (
            <li key={c.id}>
              <button
                onClick={() => onSelect(c)}
                className="w-full rounded-md p-2 text-left transition-colors hover:bg-slate-800/60"
              >
                <span className="flex items-center gap-2">
                  <span className="font-mono text-[10px] text-slate-600">[{i + 1}]</span>
                  <span className="truncate text-[11px] font-medium text-slate-300">
                    {titleFor(c.documentId) || c.documentId || 'Source'}
                  </span>
                  {c.confidenceScore > 0 && (
                    <span className="ml-auto shrink-0 font-mono text-[10px] text-emerald-400">
                      {(c.confidenceScore * 100).toFixed(0)}%
                    </span>
                  )}
                </span>
                <span className="mt-1 block truncate text-[10px] text-slate-500">{c.excerpt}</span>
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
};
