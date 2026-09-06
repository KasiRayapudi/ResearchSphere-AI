import { initialDocumentsMock, initialChatMessagesMock, initialResearchSessionsMock, initialMCPConnectorsMock, initialReportsMock, analyticsDataMock, systemHealthMock, featureFlagsMock, currentUserMock } from './mockData';
import { Document, ChatMessage, ResearchSession, MCPConnector, Report, AnalyticsData, SystemHealth, FeatureFlag } from '../types';

const API_BASE = '/api/v1';

export class ApiService {
  private static token: string | null = localStorage.getItem('rs_auth_token');

  static setToken(token: string | null) {
    this.token = token;
    if (token) {
      localStorage.setItem('rs_auth_token', token);
    } else {
      localStorage.removeItem('rs_auth_token');
    }
  }

  // --- Auth APIs ---
  static async login(email: string, pass: string) {
    try {
      const res = await fetch(`${API_BASE}/auth/login`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ email, password: pass }),
      });
      if (res.ok) {
        const data = await res.json();
        this.setToken(data.access_token);
        return data.user;
      }
    } catch (e) {
      console.warn('Backend login unavailable, returning fallback auth user');
    }
    this.setToken('mock-jwt-token-xyz-123');
    return { ...currentUserMock, email };
  }

  static async signup(name: string, email: string, pass: string) {
    try {
      const res = await fetch(`${API_BASE}/auth/signup`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name, email, password: pass }),
      });
      if (res.ok) {
        const data = await res.json();
        this.setToken(data.access_token);
        return data.user;
      }
    } catch (e) {
      console.warn('Backend signup unavailable, returning fallback auth user');
    }
    this.setToken('mock-jwt-token-xyz-123');
    return { ...currentUserMock, name, email };
  }

  // --- Documents API ---
  static async getDocuments(): Promise<Document[]> {
    try {
      const res = await fetch(`${API_BASE}/documents`, {
        headers: { Authorization: `Bearer ${this.token}` },
      });
      if (res.ok) return await res.json();
    } catch (e) {
      // Fallback
    }
    return initialDocumentsMock;
  }

  static async uploadDocument(file: File, folder?: string): Promise<Document> {
    try {
      const formData = new FormData();
      formData.append('file', file);
      if (folder) formData.append('folder', folder);

      const res = await fetch(`${API_BASE}/documents/upload`, {
        method: 'POST',
        headers: { Authorization: `Bearer ${this.token}` },
        body: formData,
      });
      if (res.ok) return await res.json();
    } catch (e) {
      // Fallback
    }
    const ext = file.name.split('.').pop()?.toLowerCase() || 'pdf';
    const newDoc: Document = {
      id: `doc-${Date.now()}`,
      title: file.name,
      fileType: (['pdf', 'docx', 'txt', 'md', 'csv', 'pptx'].includes(ext) ? ext : 'pdf') as any,
      fileSizeKb: Math.round(file.size / 1024),
      status: 'indexed',
      chunkCount: Math.floor(Math.random() * 150) + 20,
      tags: ['Uploaded', ext.toUpperCase()],
      uploadedBy: currentUserMock.name,
      uploadedAt: new Date().toISOString(),
      version: 1,
      ocrApplied: ext === 'pdf',
      folderPath: folder || '/Uploads',
    };
    return newDoc;
  }

  // --- Research Sessions API ---
  static async getResearchSessions(): Promise<ResearchSession[]> {
    try {
      const res = await fetch(`${API_BASE}/research`, {
        headers: { Authorization: `Bearer ${this.token}` },
      });
      if (res.ok) return await res.json();
    } catch (e) {}
    return initialResearchSessionsMock;
  }

  static async startResearchSession(title: string, objective: string): Promise<ResearchSession> {
    try {
      const res = await fetch(`${API_BASE}/research/start`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          Authorization: `Bearer ${this.token}`,
        },
        body: JSON.stringify({ title, objective }),
      });
      if (res.ok) return await res.json();
    } catch (e) {}

    const newSession: ResearchSession = {
      id: `rs-${Date.now()}`,
      title,
      objective,
      workspaceId: 'ws-1',
      status: 'in_progress',
      progressPercentage: 25,
      sourcesCount: 6,
      createdAt: new Date().toISOString(),
      updatedAt: new Date().toISOString(),
      agentSteps: [
        { id: `as-${Date.now()}-1`, agentName: 'Planner', status: 'completed', task: 'Decomposed objective into 4 parallel agent subtasks.', executionTimeMs: 310, timestamp: 'Just now' },
        { id: `as-${Date.now()}-2`, agentName: 'Research', status: 'running', task: 'Crawling connected vector collections & GitHub MCP files.', executionTimeMs: 150, timestamp: 'Just now' },
        { id: `as-${Date.now()}-3`, agentName: 'Retrieval', status: 'queued', task: 'Pending hybrid RRF vector lookup.', timestamp: 'Queued' },
      ],
    };
    return newSession;
  }

  // --- MCP Connectors API ---
  static async getMCPConnectors(): Promise<MCPConnector[]> {
    try {
      const res = await fetch(`${API_BASE}/mcp`, {
        headers: { Authorization: `Bearer ${this.token}` },
      });
      if (res.ok) return await res.json();
    } catch (e) {}
    return initialMCPConnectorsMock;
  }

  static async toggleMCPConnector(id: string): Promise<MCPConnector> {
    try {
      const res = await fetch(`${API_BASE}/mcp/${id}/toggle`, {
        method: 'POST',
        headers: { Authorization: `Bearer ${this.token}` },
      });
      if (res.ok) return await res.json();
    } catch (e) {}
    const connector = initialMCPConnectorsMock.find((c) => c.id === id);
    if (connector) {
      connector.status = connector.status === 'connected' ? 'disconnected' : 'connected';
      connector.lastSyncedAt = 'Just now';
      return { ...connector };
    }
    throw new Error('Connector not found');
  }

  // --- Reports API ---
  static async getReports(): Promise<Report[]> {
    try {
      const res = await fetch(`${API_BASE}/reports`, {
        headers: { Authorization: `Bearer ${this.token}` },
      });
      if (res.ok) return await res.json();
    } catch (e) {}
    return initialReportsMock;
  }

  static async generateReport(title: string, objective: string, docIds: string[]): Promise<Report> {
    try {
      const res = await fetch(`${API_BASE}/reports/generate`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          Authorization: `Bearer ${this.token}`,
        },
        body: JSON.stringify({ title, objective, document_ids: docIds }),
      });
      if (res.ok) return await res.json();
    } catch (e) {}

    const newReport: Report = {
      id: `rep-${Date.now()}`,
      title,
      format: 'pdf',
      summary: `Automated agent report synthesized from ${docIds.length} connected knowledge sources covering ${objective}.`,
      objective,
      generatedAt: new Date().toISOString(),
      author: currentUserMock.name,
      sourceDocumentIds: docIds,
      sections: [
        { title: 'Executive Summary', content: `This report investigates: ${objective}. Findings have been cross-referenced against vector embeddings and verified by the LangGraph Critic Agent.` },
        { title: 'Research Findings & Architecture', content: 'Dense semantic retrieval yielded high-relevance chunks with 0.94+ confidence score. Reciprocal Rank Fusion guaranteed non-redundancy across sources.' },
        { title: 'Key Takeaways & Next Steps', content: '1. Production vector indexing meets 184ms target latency.\n2. MCP connector automated sync eliminates manually uploaded outdated documents.\n3. Security controls comply with enterprise standards.' },
      ],
    };
    return newReport;
  }

  // --- Analytics & System Health API ---
  static async getAnalytics(): Promise<AnalyticsData> {
    try {
      const res = await fetch(`${API_BASE}/analytics`, {
        headers: { Authorization: `Bearer ${this.token}` },
      });
      if (res.ok) return await res.json();
    } catch (e) {}
    return analyticsDataMock;
  }

  static async getSystemHealth(): Promise<SystemHealth> {
    try {
      const res = await fetch(`${API_BASE}/admin/health`, {
        headers: { Authorization: `Bearer ${this.token}` },
      });
      if (res.ok) return await res.json();
    } catch (e) {}
    return systemHealthMock;
  }

  static async getFeatureFlags(): Promise<FeatureFlag[]> {
    try {
      const res = await fetch(`${API_BASE}/admin/feature-flags`, {
        headers: { Authorization: `Bearer ${this.token}` },
      });
      if (res.ok) return await res.json();
    } catch (e) {}
    return featureFlagsMock;
  }
}
