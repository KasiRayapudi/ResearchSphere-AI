/**
 * Typed API surface for ResearchSphere.
 *
 * Every method previously fell back to mock data whenever a request failed -
 * including on a 401 - which made a wrong password log you in and hid backend
 * outages behind convincing fake content. Failures now propagate as ApiError
 * so the UI can show a real error state.
 *
 * The static `ApiService` shape is preserved so existing pages keep working.
 */
import { api, tokenStore, ApiError } from './apiClient';
import type {
  AnalyticsData,
  Document,
  DocumentStatus,
  FeatureFlag,
  MCPConnector,
  Report,
  ResearchSession,
  SystemHealth,
  PageQuery,
  Paginated,
  User,
  Workspace,
  WorkspaceInvitation,
  WorkspaceMember,
  WorkspaceRole,
} from '../types';

/**
 * Turn a PageQuery into the query parameters the API expects.
 *
 * Undefined entries are dropped rather than sent as empty strings, which the
 * server would reject as an invalid sort or order.
 */
function pageQuery(
  workspaceId?: string,
  query: PageQuery = {}
): Record<string, string | number | boolean | null | undefined> {
  return {
    workspace_id: workspaceId,
    page: query.page,
    page_size: query.pageSize,
    sort: query.sort,
    order: query.order,
    search: query.search,
    cursor: query.cursor,
  };
}

export interface AuthResult {
  user: User;
  accessToken: string;
  refreshToken?: string;
}

interface RawAuthResponse {
  access_token: string;
  refresh_token?: string;
  user: {
    id: string;
    name: string;
    email: string;
    role: string;
    avatarUrl?: string;
  };
}

function toUser(raw: RawAuthResponse['user']): User {
  return {
    id: raw.id,
    name: raw.name,
    email: raw.email,
    role: (raw.role as User['role']) ?? 'researcher',
    avatarUrl: raw.avatarUrl,
    createdAt: '',
    storageLimitMb: 0,
    storageUsedMb: 0,
  };
}

export class ApiService {
  static setToken(token: string | null, refreshToken?: string | null) {
    tokenStore.set(token, refreshToken);
  }

  static getToken() {
    return tokenStore.getAccess();
  }

  // ------------------------------------------------------------- auth
  static async login(email: string, password: string): Promise<AuthResult> {
    const data = await api.post<RawAuthResponse>(
      '/auth/login',
      { email, password },
      { anonymous: true, retries: 0 }
    );
    tokenStore.set(data.access_token, data.refresh_token ?? null);
    return {
      user: toUser(data.user),
      accessToken: data.access_token,
      refreshToken: data.refresh_token,
    };
  }

  static async signup(name: string, email: string, password: string): Promise<AuthResult> {
    const data = await api.post<RawAuthResponse>(
      '/auth/signup',
      { name, email, password },
      { anonymous: true, retries: 0 }
    );
    tokenStore.set(data.access_token, data.refresh_token ?? null);
    return {
      user: toUser(data.user),
      accessToken: data.access_token,
      refreshToken: data.refresh_token,
    };
  }

  /** Resolve the signed-in user from the stored token (session bootstrap). */
  static async me(): Promise<User> {
    const raw = await api.get<RawAuthResponse['user']>('/auth/me', { retries: 0 });
    return toUser(raw);
  }

  static async logout(): Promise<void> {
    try {
      await api.post('/auth/logout', undefined, { retries: 0 });
    } finally {
      tokenStore.clear();
    }
  }

  static async requestPasswordReset(email: string): Promise<{ message: string }> {
    return api.post('/auth/password-reset/request', { email }, { anonymous: true });
  }

  static async confirmPasswordReset(token: string, newPassword: string) {
    return api.post(
      '/auth/password-reset/confirm',
      { token, new_password: newPassword },
      { anonymous: true, retries: 0 }
    );
  }

  static async checkPasswordStrength(password: string, email?: string, name?: string) {
    return api.post<{
      score: number;
      label: string;
      entropyBits: number;
      valid: boolean;
      violations: string[];
    }>('/auth/password/strength', { password, email, name }, { anonymous: true, retries: 0 });
  }

  // ------------------------------------------------------- workspaces
  static async getWorkspaces(): Promise<Workspace[]> {
    const rows = await api.get<any[]>('/workspaces');
    return rows.map((w) => ({
      id: w.id,
      name: w.name,
      slug: w.id,
      icon: 'Sparkles',
      memberCount: w.memberCount ?? 1,
      documentCount: w.documentCount ?? 0,
      role: (w.role as Workspace['role']) ?? 'owner',
      createdAt: w.createdAt,
    }));
  }

  static async createWorkspace(name: string, description?: string): Promise<Workspace> {
    const w = await api.post<any>('/workspaces', { name, description });
    return {
      id: w.id,
      name: w.name,
      slug: w.id,
      icon: 'Sparkles',
      memberCount: w.memberCount ?? 1,
      documentCount: w.documentCount ?? 0,
      role: 'owner',
      createdAt: w.createdAt,
    };
  }

  // -------------------------------------------------------- documents
  /** One page of documents, with the total and cursor. */
  static getDocumentsPage(
    workspaceId?: string,
    query: PageQuery = {}
  ): Promise<Paginated<Document>> {
    return api.get<Paginated<Document>>('/documents', { query: pageQuery(workspaceId, query) });
  }

  static async getDocuments(workspaceId?: string): Promise<Document[]> {
    // The document manager renders a flat list. It takes the first page at
    // the server's maximum; paging it properly is a UI change, and this
    // keeps the screen correct in the meantime rather than silently showing
    // 25 of 5,000.
    const page = await ApiService.getDocumentsPage(workspaceId, { pageSize: 100 });
    return page.items;
  }

  /**
   * Upload a document with progress. Uses XHR because fetch cannot report
   * upload progress. Returns the created document; `duplicate` is set by the
   * backend when identical content already exists in the workspace.
   */
  /** Indexing progress for one document. Polled while an upload finishes. */
  // ----------------------------------------------------- workspace members --

  static getMembersPage(
    workspaceId?: string,
    query: PageQuery = {}
  ): Promise<Paginated<WorkspaceMember>> {
    return api.get<Paginated<WorkspaceMember>>('/workspace/members', {
      query: pageQuery(workspaceId, query),
    });
  }

  static async getMembers(workspaceId?: string): Promise<WorkspaceMember[]> {
    // The members page shows everyone; the server caps this at 100, which is
    // a larger workspace than the flat list is designed for anyway.
    const page = await ApiService.getMembersPage(workspaceId, { pageSize: 100 });
    return page.items;
  }

  static updateMemberRole(
    memberId: string,
    role: WorkspaceRole,
    workspaceId?: string
  ): Promise<WorkspaceMember> {
    return api.patch<WorkspaceMember>(`/workspace/members/${encodeURIComponent(memberId)}`, {
      role,
      workspace_id: workspaceId,
    });
  }

  static removeMember(memberId: string, workspaceId?: string): Promise<{ message: string }> {
    return api.delete<{ message: string }>(
      `/workspace/members/${encodeURIComponent(memberId)}`,
      { query: { workspace_id: workspaceId } }
    );
  }

  static transferOwnership(
    memberId: string,
    workspaceId?: string
  ): Promise<WorkspaceMember[]> {
    return api.post<WorkspaceMember[]>(
      `/workspace/transfer-ownership?member_id=${encodeURIComponent(memberId)}`,
      { role: 'owner', workspace_id: workspaceId }
    );
  }

  static getInvitationsPage(
    workspaceId?: string,
    query: PageQuery = {}
  ): Promise<Paginated<WorkspaceInvitation>> {
    return api.get<Paginated<WorkspaceInvitation>>('/workspace/invitations', {
      query: pageQuery(workspaceId, query),
    });
  }

  static async getInvitations(workspaceId?: string): Promise<WorkspaceInvitation[]> {
    const page = await ApiService.getInvitationsPage(workspaceId, { pageSize: 100 });
    return page.items;
  }

  static inviteMember(
    email: string,
    role: WorkspaceRole,
    workspaceId?: string
  ): Promise<WorkspaceInvitation> {
    return api.post<WorkspaceInvitation>('/workspace/invite', {
      email,
      role,
      workspace_id: workspaceId,
    });
  }

  static acceptInvitation(
    token: string
  ): Promise<{ message: string; workspaceId: string; workspaceName: string; role: string }> {
    return api.post('/workspace/invite/accept', { token });
  }

  static revokeInvitation(
    invitationId: string,
    workspaceId?: string
  ): Promise<{ message: string }> {
    return api.post('/workspace/invite/revoke', {
      invitation_id: invitationId,
      workspace_id: workspaceId,
    });
  }

  static resendInvitation(
    invitationId: string,
    workspaceId?: string
  ): Promise<WorkspaceInvitation> {
    return api.post<WorkspaceInvitation>('/workspace/invite/resend', {
      invitation_id: invitationId,
      workspace_id: workspaceId,
    });
  }

  static getDocumentStatus(id: string): Promise<DocumentStatus> {
    return api.get<DocumentStatus>(`/documents/${encodeURIComponent(id)}/status`, {
      // Polled repeatedly; a failed poll is retried by the next tick, so
      // there is no value in retrying inside a single one.
      retries: 0,
    });
  }

  static uploadDocument(
    file: File,
    opts: {
      folder?: string;
      workspaceId?: string;
      onProgress?: (percent: number) => void;
      signal?: AbortSignal;
    } = {}
  ): Promise<Document & { duplicate?: boolean; message?: string }> {
    const { folder, workspaceId, onProgress, signal } = opts;
    return new Promise((resolve, reject) => {
      const form = new FormData();
      form.append('file', file);
      if (folder) form.append('folder', folder);
      if (workspaceId) form.append('workspace_id', workspaceId);

      const xhr = new XMLHttpRequest();
      xhr.open('POST', `/api/v1/documents/upload`);
      const token = tokenStore.getAccess();
      if (token) xhr.setRequestHeader('Authorization', `Bearer ${token}`);

      xhr.upload.onprogress = (e) => {
        if (e.lengthComputable && onProgress) {
          onProgress(Math.round((e.loaded / e.total) * 100));
        }
      };

      xhr.onload = () => {
        let payload: any = null;
        try {
          payload = JSON.parse(xhr.responseText);
        } catch {
          /* ignore */
        }
        if (xhr.status >= 200 && xhr.status < 300) {
          resolve(payload);
        } else {
          const env = payload?.error;
          reject(
            new ApiError(env?.message || payload?.detail || 'Upload failed', {
              status: xhr.status,
              code: env?.code || `HTTP_${xhr.status}`,
              requestId: env?.request_id,
              details: env?.details ?? [],
            })
          );
        }
      };
      xhr.onerror = () =>
        reject(
          new ApiError('Cannot reach the server. Check your connection.', {
            isNetworkError: true,
            code: 'NETWORK_ERROR',
          })
        );
      xhr.onabort = () =>
        reject(new ApiError('Upload cancelled.', { code: 'UPLOAD_CANCELLED' }));

      signal?.addEventListener('abort', () => xhr.abort());
      xhr.send(form);
    });
  }

  static async deleteDocument(id: string): Promise<void> {
    await api.delete(`/documents/${encodeURIComponent(id)}`);
  }

  // ------------------------------------------------------------- chat
  static getChatSessionsPage(workspaceId?: string, query: PageQuery = {}) {
    return api.get<
      Paginated<{ id: string; title: string; workspaceId: string; createdAt: string }>
    >('/chat/sessions', { query: pageQuery(workspaceId, query) });
  }

  static async getChatSessions(workspaceId?: string) {
    const page = await ApiService.getChatSessionsPage(workspaceId, { pageSize: 100 });
    return page.items;
  }

  static async createChatSession(workspaceId?: string) {
    return api.post<{ id: string; title: string }>(
      `/chat/sessions${workspaceId ? `?workspace_id=${encodeURIComponent(workspaceId)}` : ''}`
    );
  }

  // --------------------------------------------------------- research
  static getResearchSessionsPage(
    workspaceId?: string,
    query: PageQuery = {}
  ): Promise<Paginated<ResearchSession>> {
    return api.get<Paginated<ResearchSession>>('/research', {
      query: pageQuery(workspaceId, query),
    });
  }

  static async getResearchSessions(workspaceId?: string): Promise<ResearchSession[]> {
    const page = await ApiService.getResearchSessionsPage(workspaceId, { pageSize: 100 });
    return page.items;
  }

  static async startResearchSession(
    title: string,
    objective: string,
    workspaceId?: string
  ): Promise<ResearchSession> {
    return api.post<ResearchSession>('/research/start', {
      title,
      objective,
      workspace_id: workspaceId,
    });
  }

  // ----------------------------------------------------------- reports
  static getReportsPage(
    workspaceId?: string,
    query: PageQuery = {}
  ): Promise<Paginated<Report>> {
    return api.get<Paginated<Report>>('/reports', { query: pageQuery(workspaceId, query) });
  }

  static async getReports(workspaceId?: string): Promise<Report[]> {
    const page = await ApiService.getReportsPage(workspaceId, { pageSize: 100 });
    return page.items;
  }

  static async generateReport(
    title: string,
    objective: string,
    documentIds: string[],
    workspaceId?: string
  ): Promise<Report> {
    return api.post<Report>('/reports/generate', {
      title,
      objective,
      document_ids: documentIds,
      workspace_id: workspaceId,
    });
  }

  // -------------------------------------------------------------- mcp
  static async getMCPConnectors(workspaceId?: string): Promise<MCPConnector[]> {
    return api.get<MCPConnector[]>('/mcp', { query: { workspace_id: workspaceId } });
  }

  static async toggleMCPConnector(id: string): Promise<MCPConnector> {
    return api.post<MCPConnector>(`/mcp/${encodeURIComponent(id)}/toggle`);
  }

  // -------------------------------------------------------- analytics
  static async getAnalytics(workspaceId?: string): Promise<AnalyticsData> {
    return api.get<AnalyticsData>('/analytics', { query: { workspace_id: workspaceId } });
  }

  // ------------------------------------------------------------ admin
  static async getSystemHealth(): Promise<SystemHealth & Record<string, any>> {
    return api.get<SystemHealth>('/admin/health');
  }

  static async getSystemStats() {
    return api.get<{
      users: { total: number; active: number; admins: number; newLast7Days: number };
      workspaces: { total: number };
      documents: {
        total: number; indexed: number; failed: number; processing: number;
        chunks: number; storageBytes: number; storageMb: number;
      };
      chat: { sessions: number; messages: number; messagesLast24h: number };
      reports: { total: number };
      connectors: { total: number; active: number };
      generatedAt: string;
    }>('/admin/stats');
  }

  static async getUsers(limit = 50, offset = 0) {
    return api.get<{
      total: number;
      limit: number;
      offset: number;
      items: Array<{
        id: string; email: string; name: string;
        role: string; isActive: boolean; createdAt: string | null;
      }>;
    }>('/admin/users', { query: { limit, offset } });
  }

  static async getFeatureFlags(): Promise<FeatureFlag[]> {
    const rows = await api.get<any[]>('/admin/feature-flags');
    return rows.map((f) => ({
      id: f.id,
      key: f.key,
      name: f.name,
      description: f.description,
      enabled: f.enabled,
      targetRole: f.source ?? '',
    }));
  }

  // ----------------------------------------------------------- health
  static async getHealth() {
    const res = await fetch('/api/health');
    return res.json();
  }
}

export { ApiError };
