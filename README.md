# 🚀 ResearchSphere AI

<div align="center">

### Enterprise AI Research Platform powered by RAG, LangGraph Multi-Agent Systems & Model Context Protocol (MCP)

*Build, research, analyze, and generate knowledge using enterprise-grade AI workflows.*

![Python](https://img.shields.io/badge/Python-3.11+-blue)
![FastAPI](https://img.shields.io/badge/FastAPI-Backend-green)
![React](https://img.shields.io/badge/React-19-blue)
![TypeScript](https://img.shields.io/badge/TypeScript-5.x-blue)
![LangGraph](https://img.shields.io/badge/LangGraph-Multi--Agent-orange)
![Qdrant](https://img.shields.io/badge/Qdrant-VectorDB-red)
![Docker](https://img.shields.io/badge/Docker-Ready-blue)

</div>

---

# 📖 Overview

ResearchSphere AI is a modern enterprise AI research platform that combines **Retrieval-Augmented Generation (RAG)**, **LangGraph Multi-Agent workflows**, and **Model Context Protocol (MCP)** into a unified workspace for intelligent document analysis and knowledge discovery.

The platform enables users to upload documents, build a private knowledge base, connect external data sources, perform AI-assisted research, and generate structured reports with transparent citations.

Designed with a production-first mindset, ResearchSphere AI focuses on scalability, maintainability, security, observability, and an exceptional user experience.

---

# 🎯 Problem Statement

Modern AI assistants struggle with enterprise knowledge workflows because they often:

- Cannot effectively search private document collections
- Produce responses without transparent citations
- Lack coordinated multi-agent reasoning
- Offer limited integration with enterprise knowledge sources
- Provide poor traceability and research workflows

ResearchSphere AI addresses these limitations by combining semantic retrieval, agent orchestration, and external knowledge integration into a single enterprise-ready platform.

---

# ✨ Key Features

## 🤖 AI Research Workspace

- Retrieval-Augmented Generation (RAG)
- Streaming AI responses
- Source citations
- Semantic document search
- Confidence scoring
- Research history

---

## 📚 Intelligent Document Processing

Supports:

- PDF
- DOCX
- TXT
- Markdown

Pipeline:

```
Upload
    ↓
Text Extraction
    ↓
Chunking
    ↓
Embeddings
    ↓
Vector Storage
```

---

## 🧠 Multi-Agent Research Engine

Powered by LangGraph.

Current agents include:

- Planner Agent
- Retriever Agent
- Research Agent
- Critic Agent
- Citation Agent
- Report Agent

Each agent specializes in a dedicated stage of the research workflow.

---

## 🔗 Model Context Protocol (MCP)

External integrations include:

- GitHub
- Google Drive
- Local Files

Designed to support additional enterprise connectors.

---

## 💬 AI Chat

- Streaming responses
- Citation panel
- Source confidence
- Conversation history
- Workspace-aware context

---

## 📄 Report Generator

Generate:

- Research Reports
- Executive Summaries
- Markdown Reports
- Citation References

---

## 📊 Analytics Dashboard

Track:

- Documents Indexed
- Research Sessions
- Reports Generated
- Connected Sources
- Storage Usage
- System Metrics

---

## 🔐 Authentication

- JWT Authentication
- Secure Login
- User Registration
- Password Hashing
- Workspace Initialization

---

# 🏗️ System Architecture

```text
                 User
                   │
                   ▼
         React + TypeScript Frontend
                   │
                   ▼
             FastAPI Backend
                   │
        ┌──────────┴──────────┐
        ▼                     ▼
 Authentication         Research Engine
                              │
      ┌───────────────────────┼───────────────────────┐
      ▼                       ▼                       ▼
 LangGraph              RAG Pipeline          MCP Connectors
 Multi-Agent            Retrieval Engine
      │                       │
      ▼                       ▼
 Google Gemini         Qdrant Vector DB
      │
      ▼
 AI Response + Citations
```

---

# ⚙️ Technology Stack

## Frontend

- React 19
- TypeScript
- Vite
- Tailwind CSS
- Framer Motion
- React Router

## Backend

- FastAPI
- SQLAlchemy
- Pydantic v2
- JWT Authentication
- bcrypt
- PostgreSQL
- SQLite

## AI & ML

- LangChain
- LangGraph
- Google Gemini
- Sentence Transformers
- RecursiveCharacterTextSplitter

## Vector Database

- Qdrant

## Infrastructure

- Docker
- Docker Compose

---

# 📂 Project Structure

```
ResearchSphere-AI/

├── frontend/
├── backend/
├── docs/
├── architecture/
├── diagrams/
├── tests/
├── docker-compose.yml
├── README.md
├── CLAUDE.md
├── LICENSE
└── .env.example
```

---

# 🔄 Research Workflow

```text
Upload Documents
        │
        ▼
Extract Text
        │
        ▼
Chunk Documents
        │
        ▼
Generate Embeddings
        │
        ▼
Store in Qdrant
        │
        ▼
Semantic Retrieval
        │
        ▼
LangGraph Multi-Agent Workflow
        │
        ▼
Gemini Response Generation
        │
        ▼
Streaming Answer
        │
        ▼
Citations & Report Generation
```

---

# 🚀 Getting Started

## Clone Repository

```bash
git clone https://github.com/your-username/ResearchSphere-AI.git

cd ResearchSphere-AI
```

---

## Backend

```bash
cd backend

python -m venv venv

source venv/bin/activate
# Windows
venv\Scripts\activate

pip install -r requirements.txt

# Create the database schema. The application does not create tables:
# it verifies the migration revision at startup and refuses to run
# against a schema it was not built for.
alembic upgrade head

uvicorn main:app --reload
```

If the server refuses to start with a schema error, it names the command to
run. The two it can ask for are different and are not interchangeable:

- `alembic upgrade head` - applies migrations. Use it on an empty database
  or one already managed by Alembic.
- `alembic stamp head` - records the current schema as up to date without
  changing anything. Use it **only** on a database created before Alembic
  was introduced, whose tables already match.

Also useful:

```bash
alembic current -v                    # what the database is at
alembic upgrade head --sql            # print the SQL instead of applying it
make migration m="add widget table"   # autogenerate after a model change
```

Full detail, including how existing deployments migrate:
[`backend/migrations/README.md`](backend/migrations/README.md).

Document ingestion runs in a Celery worker rather than in the upload
request. `docker compose up` starts one; running the API on its own without
Redis falls back to indexing inline and logs a warning each time.

---

## Frontend

```bash
cd frontend

npm install

npm run dev
```

---

## Docker

```bash
docker-compose up --build
```

---

# 🔧 Environment Variables

Create a `.env` file from `.env.example`.

Example:

```env
DATABASE_URL=
JWT_SECRET=
GEMINI_API_KEY=
QDRANT_URL=
REDIS_URL=
```

---

# 📈 Development Status

## ✅ Completed

- Authentication
- Dashboard
- Document Upload
- Document Processing
- Chunking
- Embeddings
- Qdrant Integration
- Streaming Chat
- Citation Panel
- LangGraph Multi-Agent Workflow
- MCP Connectors
- Report Generation
- Analytics Dashboard
- Admin Panel
- Docker Support

---

## 🚧 Current Development

Production Hardening

Current focus:

- Infrastructure
- Security
- Monitoring
- Testing
- CI/CD
- Deployment Readiness

---

# 🛣️ Roadmap

### Phase 1

- Production Infrastructure
- Middleware Hardening
- Logging
- Health Checks

### Phase 2

- Security
- Rate Limiting
- Upload Validation
- Password Policies

### Phase 3

- Monitoring & Observability
- Performance Optimization
- Agent Metrics

### Phase 4

- Automated Testing
- Integration Testing
- End-to-End Testing

### Phase 5

- CI/CD Pipeline
- Production Deployment
- Kubernetes Support

---

# 🤝 Contributing

Contributions, feature requests, and discussions are welcome.

Please open an issue before submitting major changes.

---

# 📄 License

This project is licensed under the MIT License.

---

# ⭐ Support

If you find this project useful, consider giving it a ⭐ on GitHub.

---

<div align="center">

**ResearchSphere AI — Building the Future of Enterprise AI Research**

</div>
