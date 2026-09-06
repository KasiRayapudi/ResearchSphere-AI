import React, { useState, useRef, useEffect } from 'react';
import {
  Send,
  Sparkles,
  Paperclip,
  Mic,
  Copy,
  Check,
  RotateCcw,
  Square,
  FileText,
  ChevronRight,
  Database,
  Cpu,
  Layers,
  ExternalLink,
  Bot,
  User as UserIcon,
  BookOpen,
} from 'lucide-react';
import { Card } from '../components/common/Card';
import { Button } from '../components/common/Button';
import { Badge } from '../components/common/Badge';
import { Modal } from '../components/common/Modal';
import { initialChatMessagesMock } from '../services/mockData';
import { SSEService } from '../services/sseService';
import { ChatMessage, SourceCitation, AgentStep } from '../types';

export const ChatPage: React.FC = () => {
  const [messages, setMessages] = useState<ChatMessage[]>(initialChatMessagesMock);
  const [inputPrompt, setInputPrompt] = useState('');
  const [isStreaming, setIsStreaming] = useState(false);
  const [selectedCitation, setSelectedCitation] = useState<SourceCitation | null>(null);
  const [activeModel, setActiveModel] = useState('Gemini 1.5 Pro + RAG Engine');
  const [copiedMsgId, setCopiedMsgId] = useState<string | null>(null);
  const [voiceActive, setVoiceActive] = useState(false);
  const messagesEndRef = useRef<HTMLDivElement | null>(null);

  const scrollToBottom = () => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  };

  useEffect(() => {
    scrollToBottom();
  }, [messages, isStreaming]);

  const handleSend = (textToSend?: string) => {
    const prompt = textToSend || inputPrompt;
    if (!prompt.trim() || isStreaming) return;

    const userMsg: ChatMessage = {
      id: `usr-${Date.now()}`,
      role: 'user',
      content: prompt,
      timestamp: new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }),
    };

    setMessages((prev) => [...prev, userMsg]);
    setInputPrompt('');
    setIsStreaming(true);

    // Temp assistant message for streaming
    const assistantId = `ast-${Date.now()}`;
    const initialAssistantMsg: ChatMessage = {
      id: assistantId,
      role: 'assistant',
      content: '',
      timestamp: new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }),
      citations: [],
      agentSteps: [],
      modelUsed: activeModel,
      isStreaming: true,
    };

    setMessages((prev) => [...prev, initialAssistantMsg]);

    SSEService.streamChat(prompt, {
      onAgentStep: (step: AgentStep) => {
        setMessages((prev) =>
          prev.map((msg) =>
            msg.id === assistantId
              ? { ...msg, agentSteps: [...(msg.agentSteps || []), step] }
              : msg
          )
        );
      },
      onCitationReceived: (cit: SourceCitation) => {
        setMessages((prev) =>
          prev.map((msg) =>
            msg.id === assistantId
              ? { ...msg, citations: [...(msg.citations || []), cit] }
              : msg
          )
        );
      },
      onToken: (token: string) => {
        setMessages((prev) =>
          prev.map((msg) =>
            msg.id === assistantId ? { ...msg, content: msg.content + token } : msg
          )
        );
      },
      onComplete: (fullMsg: ChatMessage) => {
        setMessages((prev) =>
          prev.map((msg) => (msg.id === assistantId ? { ...fullMsg, id: assistantId } : msg))
        );
        setIsStreaming(false);
      },
    });
  };

  const handleCopy = (id: string, text: string) => {
    navigator.clipboard.writeText(text);
    setCopiedMsgId(id);
    setTimeout(() => setCopiedMsgId(null), 2000);
  };

  const suggestedQueries = [
    'How does our hybrid RAG engine combine dense semantic vector search with sparse keyword indexing?',
    'Summarize key findings from LangGraph_MultiAgent_Workflow_Spec.md for enterprise scalability.',
    'Compare Model Context Protocol (MCP) security schema against ISO27001 audit controls.',
  ];

  return (
    <div className="h-[calc(100vh-7rem)] flex gap-6 overflow-hidden">
      {/* Left Chat Main Area */}
      <div className="flex-1 flex flex-col min-w-0 bg-slate-900/60 backdrop-blur-xl border border-slate-800 rounded-2xl overflow-hidden shadow-2xl">
        {/* Header Bar */}
        <div className="h-14 px-6 border-b border-slate-800/80 flex items-center justify-between bg-slate-950/60">
          <div className="flex items-center gap-3">
            <div className="p-1.5 rounded-lg bg-brand-500/10 text-brand-400 border border-brand-500/20">
              <Bot className="h-4 w-4" />
            </div>
            <div>
              <h2 className="text-sm font-bold text-white flex items-center gap-2">
                RAG Agentic Chat <Badge variant="brand" size="sm">Gemini 1.5 Pro</Badge>
              </h2>
              <span className="text-[10px] text-slate-400 font-mono">128 Documents Indexed • Qdrant Hybrid RRF</span>
            </div>
          </div>

          <div className="flex items-center gap-2">
            <select
              value={activeModel}
              onChange={(e) => setActiveModel(e.target.value)}
              className="bg-slate-900 border border-slate-800 text-slate-200 text-xs rounded-lg px-2.5 py-1.5 font-medium focus:outline-none"
            >
              <option value="Gemini 1.5 Pro + RAG Engine">Gemini 1.5 Pro (Recommended)</option>
              <option value="GPT-4o (OpenAI Compatible)">GPT-4o (OpenAI)</option>
              <option value="Claude 3.5 Sonnet">Claude 3.5 Sonnet</option>
              <option value="SentenceTransformers Local">SentenceTransformers Local</option>
            </select>
          </div>
        </div>

        {/* Message Feed */}
        <div className="flex-1 overflow-y-auto p-6 space-y-6">
          {messages.map((msg) => (
            <div key={msg.id} className={`flex gap-4 ${msg.role === 'user' ? 'justify-end' : 'justify-start'}`}>
              {msg.role === 'assistant' && (
                <div className="h-8 w-8 rounded-xl bg-gradient-to-tr from-brand-600 to-indigo-600 flex items-center justify-center text-white shrink-0 shadow-md">
                  <Sparkles className="h-4 w-4" />
                </div>
              )}

              <div className={`max-w-3xl space-y-3 ${msg.role === 'user' ? 'items-end' : 'items-start'}`}>
                {/* Agent Steps Badge Trace */}
                {msg.agentSteps && msg.agentSteps.length > 0 && (
                  <div className="flex flex-wrap gap-1.5 mb-2">
                    {msg.agentSteps.map((st) => (
                      <span
                        key={st.id}
                        className="px-2 py-0.5 rounded-full bg-slate-950 border border-slate-800 text-[10px] font-mono text-purple-300 flex items-center gap-1"
                      >
                        <span className="h-1.5 w-1.5 rounded-full bg-purple-400 animate-ping" />
                        {st.agentName}: {st.task} ({st.executionTimeMs}ms)
                      </span>
                    ))}
                  </div>
                )}

                <div
                  className={`p-5 rounded-2xl text-sm leading-relaxed ${
                    msg.role === 'user'
                      ? 'bg-brand-600 text-white rounded-br-none shadow-lg shadow-brand-500/20'
                      : 'bg-slate-950/80 border border-slate-800 text-slate-100 rounded-bl-none'
                  }`}
                >
                  <div className="whitespace-pre-wrap font-sans">{msg.content || (msg.isStreaming ? 'Synthesizing response from Qdrant vector chunks...' : '')}</div>
                </div>

                {/* Inline Citations List */}
                {msg.citations && msg.citations.length > 0 && (
                  <div className="pt-2 flex flex-wrap gap-2">
                    <span className="text-[11px] font-semibold text-slate-400 uppercase tracking-wider">Sources:</span>
                    {msg.citations.map((cit, idx) => (
                      <button
                        key={cit.id}
                        onClick={() => setSelectedCitation(cit)}
                        className="px-2.5 py-1 rounded-lg bg-slate-950 border border-slate-800 hover:border-brand-500/60 text-xs font-mono text-brand-300 flex items-center gap-1.5 cursor-pointer transition-colors"
                      >
                        <FileText className="h-3 w-3 text-brand-400" />
                        <span>[{idx + 1}] {cit.documentTitle}</span>
                      </button>
                    ))}
                  </div>
                )}

                {/* Actions bar */}
                {msg.role === 'assistant' && (
                  <div className="flex items-center gap-3 text-xs text-slate-500 pt-1">
                    <span>{msg.timestamp}</span>
                    <span>•</span>
                    <button
                      onClick={() => handleCopy(msg.id, msg.content)}
                      className="hover:text-slate-300 flex items-center gap-1 cursor-pointer"
                    >
                      {copiedMsgId === msg.id ? <Check className="h-3.5 w-3.5 text-emerald-400" /> : <Copy className="h-3.5 w-3.5" />}
                      <span>{copiedMsgId === msg.id ? 'Copied' : 'Copy'}</span>
                    </button>
                    <button onClick={() => handleSend(messages[messages.length - 2]?.content)} className="hover:text-slate-300 flex items-center gap-1 cursor-pointer">
                      <RotateCcw className="h-3.5 w-3.5" />
                      <span>Regenerate</span>
                    </button>
                  </div>
                )}
              </div>

              {msg.role === 'user' && (
                <div className="h-8 w-8 rounded-xl bg-slate-800 border border-slate-700 flex items-center justify-center text-slate-300 shrink-0">
                  <UserIcon className="h-4 w-4" />
                </div>
              )}
            </div>
          ))}
          <div ref={messagesEndRef} />
        </div>

        {/* Suggested Queries */}
        {messages.length <= 2 && (
          <div className="px-6 py-2 flex flex-wrap gap-2 bg-slate-950/40 border-t border-slate-800/40">
            {suggestedQueries.map((q) => (
              <button
                key={q}
                onClick={() => handleSend(q)}
                className="text-xs px-3 py-1.5 rounded-lg bg-slate-900 border border-slate-800 text-slate-300 hover:border-brand-500/50 hover:text-white transition-colors cursor-pointer text-left"
              >
                {q}
              </button>
            ))}
          </div>
        )}

        {/* Input Box */}
        <div className="p-4 bg-slate-950/80 border-t border-slate-800">
          <form
            onSubmit={(e) => {
              e.preventDefault();
              handleSend();
            }}
            className="flex items-center gap-3 bg-slate-900 border border-slate-800 rounded-xl px-4 py-2 focus-within:border-brand-500 transition-colors"
          >
            <button type="button" className="text-slate-400 hover:text-white p-1">
              <Paperclip className="h-4 w-4" />
            </button>
            <input
              type="text"
              placeholder="Ask research question or specify sub-task for LangGraph Agents..."
              value={inputPrompt}
              onChange={(e) => setInputPrompt(e.target.value)}
              className="flex-1 bg-transparent border-none text-sm text-slate-100 placeholder-slate-500 focus:outline-none"
              disabled={isStreaming}
            />
            <button
              type="button"
              onClick={() => setVoiceActive(!voiceActive)}
              className={`p-1.5 rounded-lg transition-colors ${
                voiceActive ? 'bg-red-500/20 text-red-400 animate-pulse' : 'text-slate-400 hover:text-white'
              }`}
              title="Voice Input (WebSpeech Placeholder)"
            >
              <Mic className="h-4 w-4" />
            </button>
            <Button
              type="submit"
              variant="primary"
              size="sm"
              isLoading={isStreaming}
              icon={<Send className="h-4 w-4" />}
            />
          </form>
        </div>
      </div>

      {/* Right Sources Side Drawer */}
      <div className="w-80 bg-slate-900/60 backdrop-blur-xl border border-slate-800 rounded-2xl p-5 flex flex-col hidden lg:flex space-y-4">
        <div className="flex items-center justify-between border-b border-slate-800 pb-3">
          <h3 className="text-sm font-bold text-white flex items-center gap-2">
            <BookOpen className="h-4 w-4 text-brand-400" /> Sources Panel
          </h3>
          <span className="text-[10px] font-mono text-slate-400">Grounded Citations</span>
        </div>

        {selectedCitation ? (
          <div className="space-y-4 animate-in fade-in duration-200">
            <div className="p-3 rounded-xl bg-brand-500/10 border border-brand-500/30 space-y-2">
              <div className="flex items-center justify-between">
                <Badge variant="brand" size="sm">{selectedCitation.fileType.toUpperCase()}</Badge>
                <span className="text-[10px] font-mono text-emerald-400 font-bold">
                  Score: {(selectedCitation.confidenceScore * 100).toFixed(0)}%
                </span>
              </div>
              <h4 className="text-xs font-bold text-white">{selectedCitation.documentTitle}</h4>
              <span className="text-[10px] text-slate-400 font-mono">Page {selectedCitation.pageNumber || 1} • {selectedCitation.sectionHeader}</span>
            </div>

            <div className="p-4 rounded-xl bg-slate-950 border border-slate-800 text-xs text-slate-300 space-y-2 leading-relaxed">
              <span className="text-brand-400 font-mono font-semibold">// Chunk Excerpt:</span>
              <p className="italic">"{selectedCitation.excerpt}"</p>
            </div>

            <Button
              variant="outline"
              size="sm"
              className="w-full"
              icon={<ExternalLink className="h-3.5 w-3.5" />}
              onClick={() => alert(`Opening full preview for ${selectedCitation.documentTitle}`)}
            >
              Open Document Preview
            </Button>
          </div>
        ) : (
          <div className="p-8 text-center text-xs text-slate-500 space-y-2">
            <Database className="h-8 w-8 text-slate-700 mx-auto" />
            <p>Click any citation tag [1] in AI response to inspect vector source chunk and confidence score.</p>
          </div>
        )}
      </div>
    </div>
  );
};
