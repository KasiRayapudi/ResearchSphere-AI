import { User, Workspace, Document, ChatMessage, ResearchSession, MCPConnector, Report, AnalyticsData, SystemHealth, FeatureFlag } from '../types';

export const currentUserMock: User = {
  id: 'user-001',
  name: 'Alex Vance',
  email: 'alex.vance@enterprise-ai.io',
  role: 'owner',
  avatarUrl: 'https://images.unsplash.com/photo-1534528741775-53994a69daeb?auto=format&fit=crop&w=250&q=80',
  createdAt: '2025-10-15T08:30:00Z',
  storageLimitMb: 50000,
  storageUsedMb: 12840,
};

export const initialWorkspacesMock: Workspace[] = [
  { id: 'ws-1', name: 'Enterprise Research Hub', slug: 'enterprise-hub', icon: 'Sparkles', memberCount: 14, documentCount: 128, role: 'owner', createdAt: '2025-11-01' },
  { id: 'ws-2', name: 'Quantum AI & RAG Lab', slug: 'rag-lab', icon: 'Cpu', memberCount: 6, documentCount: 45, role: 'admin', createdAt: '2025-12-10' },
  { id: 'ws-3', name: 'Product Architecture & Specs', slug: 'product-specs', icon: 'FolderGit2', memberCount: 9, documentCount: 82, role: 'member', createdAt: '2026-01-05' },
];

export const initialDocumentsMock: Document[] = [
  {
    id: 'doc-101',
    title: 'RAG_Architecture_Benchmark_2026.pdf',
    fileType: 'pdf',
    fileSizeKb: 4820,
    status: 'indexed',
    chunkCount: 342,
    tags: ['Architecture', 'RAG', 'VectorDB', 'Benchmark'],
    uploadedBy: 'Alex Vance',
    uploadedAt: '2026-02-10T14:22:00Z',
    version: 2,
    ocrApplied: true,
    folderPath: '/Research/Technical',
  },
  {
    id: 'doc-102',
    title: 'LangGraph_MultiAgent_Workflow_Spec.md',
    fileType: 'md',
    fileSizeKb: 1240,
    status: 'indexed',
    chunkCount: 98,
    tags: ['LangGraph', 'Agents', 'Specification'],
    uploadedBy: 'Dr. Sarah Lin',
    uploadedAt: '2026-02-08T09:15:00Z',
    version: 1,
    ocrApplied: false,
    folderPath: '/Research/Agents',
  },
  {
    id: 'doc-103',
    title: 'Model_Context_Protocol_MCP_Standard.docx',
    fileType: 'docx',
    fileSizeKb: 2890,
    status: 'indexed',
    chunkCount: 184,
    tags: ['MCP', 'Integration', 'Protocol'],
    uploadedBy: 'Marcus Chen',
    uploadedAt: '2026-02-05T16:40:00Z',
    version: 3,
    ocrApplied: false,
    folderPath: '/Integrations',
  },
  {
    id: 'doc-104',
    title: 'Qdrant_Vector_Index_Performance_Analysis.pptx',
    fileType: 'pptx',
    fileSizeKb: 14200,
    status: 'indexed',
    chunkCount: 210,
    tags: ['Qdrant', 'VectorDB', 'Performance'],
    uploadedBy: 'Alex Vance',
    uploadedAt: '2026-02-01T11:05:00Z',
    version: 1,
    ocrApplied: true,
    folderPath: '/Research/Technical',
  },
  {
    id: 'doc-105',
    title: 'Enterprise_AI_Security_Audit_Report.pdf',
    fileType: 'pdf',
    fileSizeKb: 6540,
    status: 'indexed',
    chunkCount: 420,
    tags: ['Security', 'Compliance', 'Audit'],
    uploadedBy: 'Elena Rostova',
    uploadedAt: '2026-01-28T18:00:00Z',
    version: 1,
    ocrApplied: true,
    folderPath: '/Security',
  },
  {
    id: 'doc-106',
    title: 'Sentence_Transformers_Hybrid_ReRanking.csv',
    fileType: 'csv',
    fileSizeKb: 890,
    status: 'chunking',
    chunkCount: 45,
    tags: ['Embeddings', 'Re-Ranking', 'Data'],
    uploadedBy: 'Alex Vance',
    uploadedAt: '2026-02-12T10:11:00Z',
    version: 1,
    ocrApplied: false,
    folderPath: '/Research/Technical',
  },
];

export const initialChatMessagesMock: ChatMessage[] = [
  {
    id: 'msg-1',
    role: 'user',
    content: 'How does our hybrid RAG engine combine dense semantic vector search with sparse keyword indexing to maintain zero hallucination grounded responses?',
    timestamp: '10:14 AM',
  },
  {
    id: 'msg-2',
    role: 'assistant',
    content: `Our **ResearchSphere AI RAG Engine** utilizes a state-of-the-art **Hybrid Reciprocal Rank Fusion (RRF)** approach combined with cross-encoder re-ranking to guarantee sub-millisecond precision and 100% grounded citations [1].

### 1. Two-Stage Retrieval Pipeline
1. **Dense Vector Search**: Powered by \`Qdrant Vector Database\` using \`BAAI/bge-large-en-v1.5\` embeddings (1024-dim cosine distance) to capture deep semantic intent [1].
2. **Sparse BM25 Keyword Search**: Evaluates exact term frequencies, metadata tags, and code tokens across PostgreSQL full-text indices [2].

### 2. Reciprocal Rank Fusion (RRF) Formula
The fused score for each chunk \(d \in D\) is calculated as:
\[
RRF\_Score(d) = \sum_{m \in M} \frac{1}{k + r_m(d)}
\]
where \(k = 60\) and \(r_m(d)\) represents the rank of document chunk \(d\) in retrieval method \(m\) [1].

### 3. Cross-Encoder Re-ranking & Fact Verification
The top 25 fused candidates are passed through a Sentence-Transformer Cross-Encoder (\`ms-marco-MiniLM-L-6-v2\`). The **Critic Agent** evaluates factual consistency before synthesizing final markdown output with mandatory inline source citations [2][3].`,
    timestamp: '10:15 AM',
    modelUsed: 'Gemini 1.5 Pro + RAG Engine',
    citations: [
      {
        id: 'cit-1',
        documentId: 'doc-101',
        documentTitle: 'RAG_Architecture_Benchmark_2026.pdf',
        fileType: 'pdf',
        excerpt: 'Reciprocal Rank Fusion (RRF) with k=60 achieves a 94.8% Hit@5 score compared to 81.2% for pure vector cosine similarity on complex technical domain schemas.',
        pageNumber: 14,
        confidenceScore: 0.98,
        sectionHeader: '3.2 Hybrid Fusion Evaluation',
      },
      {
        id: 'cit-2',
        documentId: 'doc-102',
        documentTitle: 'LangGraph_MultiAgent_Workflow_Spec.md',
        fileType: 'md',
        excerpt: 'The Critic Agent halts token streaming if confidence score drops below 0.85, triggering an immediate re-retrieval loop across connected MCP data sources.',
        pageNumber: 3,
        confidenceScore: 0.94,
        sectionHeader: 'Critic Node Validation State',
      },
      {
        id: 'cit-3',
        documentId: 'doc-103',
        documentTitle: 'Model_Context_Protocol_MCP_Standard.docx',
        fileType: 'docx',
        excerpt: 'MCP Connectors inject live schema payloads into vector store namespaces with automated metadata hash verification to eliminate stale references.',
        pageNumber: 8,
        confidenceScore: 0.91,
        sectionHeader: 'State Synchronization & Hash Checks',
      },
    ],
    agentSteps: [
      { id: 'st-1', agentName: 'Planner', status: 'completed', task: 'Deconstructed query into hybrid vector & BM25 retrieval sub-tasks.', executionTimeMs: 120, timestamp: '10:14 AM' },
      { id: 'st-2', agentName: 'Retrieval', status: 'completed', task: 'Queried Qdrant collection "enterprise_docs" & PostgreSQL BM25 index.', executionTimeMs: 340, timestamp: '10:14 AM' },
      { id: 'st-3', agentName: 'Critic', status: 'completed', task: 'Verified grounded factual consistency across 3 source chunk citations.', executionTimeMs: 210, timestamp: '10:15 AM' },
    ],
  },
];

export const initialResearchSessionsMock: ResearchSession[] = [
  {
    id: 'rs-1',
    title: 'Comparative Analysis of LangGraph vs AutoGen for Enterprise Agentic Pipelines',
    objective: 'Evaluate execution graph state persistence, fault tolerance, cyclic agent workflows, and human-in-the-loop validation overhead.',
    workspaceId: 'ws-1',
    status: 'synthesis',
    progressPercentage: 85,
    sourcesCount: 18,
    createdAt: '2026-02-11T16:00:00Z',
    updatedAt: '2026-02-12T09:40:00Z',
    agentSteps: [
      { id: 'as-1', agentName: 'Planner', status: 'completed', task: 'Created 5 sub-tasks: Framework comparison, state persistence, memory scaling, benchmark metrics, security.', executionTimeMs: 450, timestamp: '09:30 AM' },
      { id: 'as-2', agentName: 'Research', status: 'completed', task: 'Crawled 12 connected docs & GitHub MCP repositories for code samples.', executionTimeMs: 1200, timestamp: '09:32 AM' },
      { id: 'as-3', agentName: 'Retrieval', status: 'completed', task: 'Retrieved 34 semantic vector chunks with RRF re-ranking.', executionTimeMs: 620, timestamp: '09:33 AM' },
      { id: 'as-4', agentName: 'Summarizer', status: 'completed', task: 'Synthesized architectural matrix comparing state graph checkpoints.', executionTimeMs: 890, timestamp: '09:35 AM' },
      { id: 'as-5', agentName: 'Critic', status: 'completed', task: 'Fact-checked claims against LangGraph 0.2 API specifications.', executionTimeMs: 310, timestamp: '09:37 AM' },
      { id: 'as-6', agentName: 'Report', status: 'running', task: 'Compiling executive PDF report with interactive diagrams.', executionTimeMs: 500, timestamp: '09:40 AM' },
    ],
  },
  {
    id: 'rs-2',
    title: 'Model Context Protocol (MCP) Integration for Security Compliance Audit',
    objective: 'Map automated SOC2 and ISO27001 data extraction pipelines using GitHub and Google Drive MCP connectors.',
    workspaceId: 'ws-1',
    status: 'completed',
    progressPercentage: 100,
    sourcesCount: 24,
    createdAt: '2026-02-09T10:00:00Z',
    updatedAt: '2026-02-10T15:20:00Z',
    agentSteps: [
      { id: 'as-10', agentName: 'Planner', status: 'completed', task: 'Defined security audit taxonomy and policy check criteria.', executionTimeMs: 380, timestamp: '10:00 AM' },
      { id: 'as-11', agentName: 'Citation', status: 'completed', task: 'Indexed 42 audit references with line-level code links.', executionTimeMs: 740, timestamp: '10:15 AM' },
    ],
  },
];

export const initialMCPConnectorsMock: MCPConnector[] = [
  { id: 'mcp-1', name: 'GitHub Enterprise Connector', provider: 'github', status: 'connected', lastSyncedAt: '12 mins ago', itemsSyncedCount: 342, config: { repo: 'enterprise/ai-core', branch: 'main' } },
  { id: 'mcp-2', name: 'Google Drive Knowledge Sync', provider: 'gdrive', status: 'connected', lastSyncedAt: '1 hour ago', itemsSyncedCount: 128, config: { folderId: '1Fk9_Xm09A1z' } },
  { id: 'mcp-3', name: 'Local Research Filesystem', provider: 'local', status: 'connected', lastSyncedAt: 'Just now', itemsSyncedCount: 56, config: { path: '/data/research' } },
  // Future connectors
  { id: 'mcp-4', name: 'Slack Workspace Connector', provider: 'slack', status: 'disconnected', itemsSyncedCount: 0, config: {}, isFutureConnector: true },
  { id: 'mcp-5', name: 'Notion AI Workspace Sync', provider: 'notion', status: 'disconnected', itemsSyncedCount: 0, config: {}, isFutureConnector: true },
  { id: 'mcp-6', name: 'Atlassian Jira & Confluence', provider: 'confluence', status: 'disconnected', itemsSyncedCount: 0, config: {}, isFutureConnector: true },
  { id: 'mcp-7', name: 'Microsoft Teams & SharePoint', provider: 'sharepoint', status: 'disconnected', itemsSyncedCount: 0, config: {}, isFutureConnector: true },
  { id: 'mcp-8', name: 'Gmail Enterprise Search', provider: 'gmail', status: 'disconnected', itemsSyncedCount: 0, config: {}, isFutureConnector: true },
];

export const initialReportsMock: Report[] = [
  {
    id: 'rep-101',
    title: 'Enterprise RAG & Multi-Agent Architecture Benchmark',
    format: 'pdf',
    summary: 'Comprehensive analysis of hybrid vector retrieval, Reciprocal Rank Fusion, and LangGraph agent workflow orchestration for enterprise knowledge bases.',
    objective: 'Evaluate production latency, accuracy, and fault tolerance across 100,000 indexed technical documents.',
    generatedAt: '2026-02-11T18:30:00Z',
    author: 'Alex Vance',
    sourceDocumentIds: ['doc-101', 'doc-102', 'doc-103'],
    sections: [
      { title: 'Executive Summary', content: 'This report details the implementation of ResearchSphere AI enterprise RAG pipeline, incorporating dense BAAI/bge-large embeddings, Qdrant vector storage, BM25 sparse keyword indices, and LangGraph multi-agent synthesis.' },
      { title: 'Problem Statement', content: 'Traditional single-prompt LLM wrappers suffer from token limit constraints, high hallucination rates, and lack of real-time grounding in updated corporate files.' },
      { title: 'Research Findings & Architecture', content: 'Our two-stage retrieval with Reciprocal Rank Fusion (k=60) increased retrieval Hit@5 precision by 16.4 percentage points over pure cosine vector search.' },
      { title: 'Advantages & Limitations', content: 'Advantages: Zero hallucination citations, sub-200ms hybrid search latency, modular MCP integrations. Limitations: OCR ingestion of non-standard scanned blueprints requires GPU worker pools.' },
      { title: 'References & Future Work', content: '1. LangGraph State Graph Specifications 2026\n2. Qdrant Vector Indexing Whitepaper\n3. Model Context Protocol Standard RFC-08' },
    ],
  },
];

export const analyticsDataMock: AnalyticsData = {
  questionsAskedTotal: 14820,
  documentsIndexedTotal: 1280,
  embeddingsGeneratedTotal: 492000,
  avgResponseTimeMs: 184,
  storageUsageMb: 12840,
  storageCapacityMb: 50000,
  dailyQueries: [
    { date: 'Feb 6', queryCount: 1240, avgLatencyMs: 210 },
    { date: 'Feb 7', queryCount: 1450, avgLatencyMs: 195 },
    { date: 'Feb 8', queryCount: 1890, avgLatencyMs: 180 },
    { date: 'Feb 9', queryCount: 2100, avgLatencyMs: 175 },
    { date: 'Feb 10', queryCount: 2450, avgLatencyMs: 168 },
    { date: 'Feb 11', queryCount: 2900, avgLatencyMs: 185 },
    { date: 'Feb 12', queryCount: 2790, avgLatencyMs: 184 },
  ],
  topSources: [
    { sourceName: 'RAG_Architecture_Benchmark_2026.pdf', accessCount: 1420, category: 'Technical PDF' },
    { sourceName: 'LangGraph_MultiAgent_Workflow_Spec.md', accessCount: 980, category: 'Markdown Spec' },
    { sourceName: 'Model_Context_Protocol_MCP_Standard.docx', accessCount: 760, category: 'Word Doc' },
    { sourceName: 'GitHub: enterprise/ai-core', accessCount: 650, category: 'MCP Connector' },
    { sourceName: 'Qdrant_Vector_Index_Performance.pptx', accessCount: 490, category: 'Presentation' },
  ],
  modelUsageBreakdown: [
    { modelName: 'Gemini 1.5 Pro', percentage: 55 },
    { modelName: 'GPT-4o (OpenAI Compatible)', percentage: 30 },
    { modelName: 'Local Sentence Transformers', percentage: 15 },
  ],
};

export const systemHealthMock: SystemHealth = {
  postgressStatus: 'healthy',
  qdrantStatus: 'healthy',
  redisStatus: 'healthy',
  fastapiStatus: 'healthy',
  celeryStatus: 'healthy',
  uptimeSeconds: 1428900, // ~16.5 days
  activeAgentsCount: 8,
};

export const featureFlagsMock: FeatureFlag[] = [
  { id: 'ff-1', key: 'mcp_gdrive_sync', name: 'Google Drive Live MCP Sync', description: 'Enable background folder watching and instant chunk updates for Google Docs.', enabled: true, targetRole: 'All Roles' },
  { id: 'ff-2', key: 'deep_critic_verifier', name: 'Deep Critic Fact Verifier', description: 'Run secondary cross-encoder validation before streaming model tokens.', enabled: true, targetRole: 'Pro & Enterprise' },
  { id: 'ff-3', key: 'voice_input_stream', name: 'Voice Input Streaming', description: 'Enable WebSpeech API audio input for natural voice queries.', enabled: true, targetRole: 'Beta Testers' },
  { id: 'ff-4', key: 'auto_pdf_ocr_ingestion', name: 'Automated Tesseract OCR Ingestion', description: 'Extract text from scanned PDF tables and image figures during chunking.', enabled: true, targetRole: 'All Roles' },
];
