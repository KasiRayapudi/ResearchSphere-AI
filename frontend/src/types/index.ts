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
