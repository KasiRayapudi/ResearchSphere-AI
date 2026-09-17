/**
 * Server-sent-event client for the RAG chat stream.
 *
 * The previous implementation invented the agent trace on setTimeout and
 * appended a hardcoded citation, while the real `__SOURCES_JSON__` payload the
 * backend streams was parsed by nobody. Everything here now comes from the
 * server; when the backend sends no sources, none are shown.
 */
import { SourceCitation } from '../types';
import { ApiError, refreshSession, tokenStore } from './apiClient';

export interface ChatStreamCallbacks {
  onToken?: (token: string) => void;
  onCitations?: (citations: SourceCitation[]) => void;
  onComplete?: (result: { content: string; citations: SourceCitation[]; responseTimeMs: number }) => void;
  onError?: (err: Error) => void;
}

export interface ChatStreamRequest {
  prompt: string;
  workspaceId?: string;
  sessionId?: string;
  model?: string;
  signal?: AbortSignal;
}

interface RawSource {
  chunk_id?: string;
  document_id?: string;
  chunk_db_id?: string;
  content?: string;
  score?: number;
  chunk_index?: number;
}

/** Map a backend retrieval chunk onto the citation shape the UI renders. */
function toCitation(raw: RawSource, index: number): SourceCitation {
  const content = raw.content ?? '';
  return {
    id: raw.chunk_id ?? `src-${index}`,
    documentId: raw.document_id ?? '',
    // The retrieval payload carries no filename; the chat panel resolves the
    // real title from the document list when it can.
    documentTitle: '',
    fileType: '',
    excerpt: content.length > 400 ? `${content.slice(0, 400)}…` : content,
    pageNumber: undefined,
    confidenceScore: typeof raw.score === 'number' ? raw.score : 0,
    sectionHeader: undefined,
  };
}

export class SSEService {
  static async streamChat(
    request: ChatStreamRequest,
    callbacks: ChatStreamCallbacks
  ): Promise<void> {
    const { prompt, workspaceId, sessionId, model, signal } = request;

    // Declared outside the try so a deliberate stop can still report the text
    // that was already streamed instead of discarding it.
    let content = '';
    let citations: SourceCitation[] = [];
    let responseTimeMs = 0;

    const send = (token: string | null) =>
      fetch('/api/v1/chat/stream', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          ...(token ? { Authorization: `Bearer ${token}` } : {}),
        },
        body: JSON.stringify({
          prompt,
          workspace_id: workspaceId,
          session_id: sessionId,
          ...(model ? { model } : {}),
        }),
        signal,
      });

    try {
      let response = await send(tokenStore.getAccess());

      // The stream is a long-lived request; refresh once and retry on 401 so a
      // token expiring mid-session does not surface as a failed message.
      if (response.status === 401) {
        const refreshed = await refreshSession();
        if (refreshed) response = await send(tokenStore.getAccess());
      }

      if (!response.ok) {
        let message = `Chat request failed (${response.status})`;
        try {
          const body = await response.json();
          message = body?.error?.message ?? body?.detail ?? message;
        } catch {
          /* non-JSON error */
        }
        throw new ApiError(message, { status: response.status });
      }

      const reader = response.body?.getReader();
      if (!reader) throw new Error('Streaming is not supported in this browser.');

      const decoder = new TextDecoder('utf-8');
      let buffer = '';

      const handleData = (payload: string) => {
        if (payload === '[DONE]') return;
        let parsed: any;
        try {
          parsed = JSON.parse(payload);
        } catch {
          // Non-JSON frame: treat as raw text so nothing is silently dropped.
          content += payload;
          callbacks.onToken?.(payload);
          return;
        }

        if (parsed.error) {
          throw new Error(parsed.error);
        }

        if (typeof parsed.text === 'string') {
          // The backend appends its source marker to the text stream; strip it
          // so the marker never renders in the message body.
          const markerStart = parsed.text.indexOf('__SOURCES_JSON__');
          if (markerStart === -1) {
            content += parsed.text;
            callbacks.onToken?.(parsed.text);
            return;
          }

          const before = parsed.text.slice(0, markerStart);
          if (before) {
            content += before;
            callbacks.onToken?.(before);
          }
          const meta = parsed.text
            .slice(markerStart + '__SOURCES_JSON__'.length)
            .replace('__END_SOURCES__', '')
            .trim();
          try {
            const decoded = JSON.parse(meta);
            citations = (decoded.__sources__ ?? []).map(toCitation);
            responseTimeMs = decoded.__response_time_ms__ ?? 0;
            if (citations.length) callbacks.onCitations?.(citations);
          } catch {
            /* malformed metadata must not break the message */
          }
        }
      };

       
      while (true) {
        const { value, done } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });

        const frames = buffer.split('\n\n');
        buffer = frames.pop() ?? '';
        for (const frame of frames) {
          for (const line of frame.split('\n')) {
            if (line.startsWith('data: ')) handleData(line.slice(6).trim());
          }
        }
      }

      // Trailing frame without a terminating blank line.
      if (buffer.startsWith('data: ')) handleData(buffer.slice(6).trim());

      callbacks.onComplete?.({ content, citations, responseTimeMs });
    } catch (err) {
      if ((err as Error)?.name === 'AbortError') {
        // Deliberate stop: keep whatever was streamed before the stop.
        callbacks.onComplete?.({ content, citations, responseTimeMs });
        return;
      }
      callbacks.onError?.(err instanceof Error ? err : new Error(String(err)));
    }
  }
}
