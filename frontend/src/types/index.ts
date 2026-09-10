export interface User {
  id: string;
  name: string;
  email: string;
  role: 'owner' | 'admin' | 'researcher' | 'viewer';
  avatarUrl?: string;
  createdAt: string;
  storageLimitMb: number;
  storageUsedMb: number;
}

export interface Workspace {
  id: string;
  name: string;
  slug: string;
  icon?: string;
  memberCount: number;
  documentCount: number;
  role: 'owner' | 'admin' | 'member';
  createdAt: string;
}

export interface Document {
  id: string;
  title: string;
  fileType: 'pdf' | 'docx' | 'txt' | 'md' | 'csv' | 'pptx';
  fileSizeKb: number;
  status: 'indexed' | 'chunking' | 'embedding' | 'ocr_processing' | 'failed';
  chunkCount: number;
  tags: string[];
  uploadedBy: string;
  uploadedAt: string;
  version: number;
  ocrApplied: boolean;
  folderPath?: string;
}

export interface SourceCitation {
  id: string;
  documentId: string;
  documentTitle: string;
  fileType: string;
  excerpt: string;
  pageNumber?: number;
  confidenceScore: number;
  sectionHeader?: string;
}

export interface ChatMessage {
  id: string;
  role: 'user' | 'assistant' | 'system';
  content: string;
  timestamp: string;
  citations?: SourceCitation[];
  agentSteps?: AgentStep[];
  modelUsed?: string;
  isStreaming?: boolean;
}

export interface AgentStep {
  id: string;
  agentName: 'Planner' | 'Research' | 'Retrieval' | 'Summarizer' | 'Critic' | 'Citation' | 'Report' | 'Memory';
  status: 'queued' | 'running' | 'completed' | 'failed';
  task: string;
  outputSummary?: string;
  executionTimeMs?: number;
  timestamp: string;
}

export interface ResearchSession {
  id: string;
  title: string;
  objective: string;
  workspaceId: string;
  status: 'planning' | 'in_progress' | 'synthesis' | 'completed' | 'archived';
  progressPercentage: number;
  agentSteps: AgentStep[];
  sourcesCount: number;
  createdAt: string;
  updatedAt: string;
}

export interface MCPConnector {
  id: string;
  name: string;
  provider: 'github' | 'gdrive' | 'local' | 'slack' | 'notion' | 'confluence' | 'jira' | 'gmail' | 'teams' | 'dropbox' | 'sharepoint';
  status: 'connected' | 'disconnected' | 'syncing' | 'error';
  lastSyncedAt?: string;
  itemsSyncedCount: number;
  config: Record<string, any>;
  isFutureConnector?: boolean;
}

export interface Report {
  id: string;
  title: string;
  format: 'pdf' | 'markdown' | 'html';
  summary: string;
  objective: string;
  sections: {
    title: string;
    content: string;
  }[];
  generatedAt: string;
  author: string;
  sourceDocumentIds: string[];
}

export interface AnalyticsData {
  questionsAskedTotal: number;
  documentsIndexedTotal: number;
  embeddingsGeneratedTotal: number;
  avgResponseTimeMs: number;
  storageUsageMb: number;
  storageCapacityMb: number;
  dailyQueries: { date: string; queryCount: number; avgLatencyMs: number }[];
  topSources: { sourceName: string; accessCount: number; category: string }[];
  modelUsageBreakdown: { modelName: string; percentage: number }[];
}

export interface SystemHealth {
  postgressStatus: 'healthy' | 'degraded' | 'down';
  qdrantStatus: 'healthy' | 'degraded' | 'down';
  redisStatus: 'healthy' | 'degraded' | 'down';
  fastapiStatus: 'healthy' | 'degraded' | 'down';
  celeryStatus: 'healthy' | 'degraded' | 'down';
  uptimeSeconds: number;
  activeAgentsCount: number;
}

export interface FeatureFlag {
  id: string;
  key: string;
  name: string;
  description: string;
  enabled: boolean;
  targetRole: string;
}

/**
 * Indexing progress for a document, from GET /documents/{id}/status.
 *
 * Ingestion runs in a background worker, so an upload returns `queued` and
 * the document moves through `processing` to one of the terminal states.
 */
export interface DocumentStatus {
  id: string;
  status: 'queued' | 'processing' | 'indexed' | 'failed' | 'pending';
  progress: number;
  chunkCount: number;
  error: string | null;
  updatedAt: string | null;
  startedAt: string | null;
  completedAt: string | null;
}

/** States a document will not move on from without a new request. */
export const TERMINAL_DOCUMENT_STATUSES = ['indexed', 'failed'] as const;

/** A person's role within one workspace. Distinct from the platform role. */
export type WorkspaceRole = 'owner' | 'admin' | 'editor' | 'viewer';

export interface WorkspaceMember {
  id: string;
  userId: string;
  email: string;
  fullName: string | null;
  role: WorkspaceRole;
  joinedAt: string;
  invitedBy: string | null;
}

export type InvitationStatus = 'pending' | 'accepted' | 'revoked' | 'expired';

export interface WorkspaceInvitation {
  id: string;
  email: string;
  role: WorkspaceRole;
  expiresAt: string;
  createdAt: string;
  invitedBy: string | null;
  status: InvitationStatus;
}

/**
 * What each role may do, mirroring backend PERMISSIONS. Used only to decide
 * which controls to render: the server enforces this independently, and a
 * client that got it wrong would be corrected by a 403.
 */
export const ROLE_CAN = {
  manageMembers: (role: WorkspaceRole) => role === 'owner' || role === 'admin',
  transferOwnership: (role: WorkspaceRole) => role === 'owner',
  write: (role: WorkspaceRole) => role !== 'viewer',
} as const;

/**
 * The envelope every list endpoint returns.
 *
 * `total` and `pages` are null on a keyset page: counting the whole set
 * would defeat the point of using a cursor, so the API omits them rather
 * than reporting a number it did not compute.
 */
export interface Paginated<T> {
  items: T[];
  page: number;
  pageSize: number;
  total: number | null;
  pages: number | null;
  hasNext: boolean;
  hasPrevious: boolean;
  nextCursor: string | null;
}

/** Query parameters accepted by every list endpoint. */
export interface PageQuery {
  page?: number;
  pageSize?: number;
  sort?: string;
  order?: 'asc' | 'desc';
  search?: string;
  cursor?: string;
}
