# ResearchSphere AI

> **Enterprise AI Workspace powered by RAG, MCP, AI Agents, and LangGraph.**

ResearchSphere AI is an enterprise-grade AI research and knowledge management platform where users can connect multiple knowledge sources, upload documents, chat with their data using zero-hallucination grounded citations, perform deep research using LangGraph multi-agent workflows, and generate executive PDF reports.

---

## Technical Architecture

```
                                  +------------------------------------+
                                  |     React 19 + Vite Frontend       |
                                  | (Tailwind CSS, Framer Motion,      |
                                  |  Lucide Icons, Command Palette)    |
                                  +-----------------+------------------+
                                                    |
                                                    | REST APIs / SSE Streaming
                                                    v
                                  +-----------------+------------------+
                                  |       FastAPI Backend Gateway      |
                                  |    (JWT Auth, Rate Limiting, CORS) |
                                  +--------+-----------------+---------+
                                           |                 |
                   +-----------------------+                 +------------------------+
                   v                                                                  v
    +--------------+---------------+                                   +--------------+---------------+
    |    LangGraph Multi-Agent     |                                   |     RAG Pipeline Engine      |
    |      Research Engine         |                                   |  (Semantic + Hybrid Search,  |
    +--------------+---------------+                                   |   Re-ranking, Citations)     |
                   |                                                   +--------------+---------------+
   +---------------+---------------+                                                  |
   | Planner | Research | Retrieval|                                                  v
   | Summarizer | Critic| Citation |                                   +--------------+---------------+
   | Report  | Memory Agents       |                                   | Vector DB & Cache / Storage  |
   +---------------+---------------+                                   | (Qdrant, PostgreSQL, Redis,  |
                   |                                                   | Supabase Storage Abstraction)|
                   v                                                   +------------------------------+
    +--------------+---------------+
    |  Modular MCP Connectors      |
    | (GitHub, GDrive, Local Files)|
    +------------------------------+
```

---

## Tech Stack Overview

### Frontend
- **Framework**: React 19, TypeScript, Vite
- **Styling**: Tailwind CSS (Glassmorphism, Radial Aurora Gradients, Soft Glow Borders)
- **Animations**: Framer Motion, Micro-interactions, Animated Canvas Visualizers
- **Icons & Navigation**: Lucide Icons, Linear-style Command Palette (`Ctrl+K`), Responsive Sidebar Layout
- **State & Router**: React Router v7, Custom Auth & Theme Contexts

### Backend
- **Framework**: FastAPI (Python 3.11)
- **Security**: JWT Authentication, Bcrypt Password Hashing, CORS Controls
- **ORM & DB**: SQLAlchemy, PostgreSQL 16, SQLite fallback mode
- **Background Tasks**: Celery, Redis Broker

### AI & Vector Stack
- **RAG Pipeline**: Dense Semantic Search (BAAI/bge-large-en-v1.5) + Sparse BM25 + Reciprocal Rank Fusion ($k=60$)
- **Vector Database**: Qdrant Vector Database
- **Agent Orchestration**: LangGraph 0.2 Multi-Agent State Graph (Planner, Research, Retrieval, Summarizer, Critic, Citation, Report, Memory)
- **MCP Protocol**: Modular Model Context Protocol Connectors for **GitHub**, **Google Drive**, and **Local Files** (extensible to Slack, Notion, Jira, Confluence, Teams, Gmail)

---

## Features

- **Landing Page**: Sticky glass navbar, animated neural network hero canvas, bento feature grid, interactive LangGraph agent workflow diagram, pricing tier toggles, and FAQ accordion.
- **RAG Agentic Chat**: Full ChatGPT/Perplexity-style interface with real-time streaming, syntax highlighted markdown code blocks, line-level source citations (`[1]`, `[2]`), right-side **Sources Panel**, document previewer, and model selector.
- **Document Knowledge Store**: Drag-and-drop file upload for PDF, DOCX, TXT, MD, CSV, PPTX files. Automatic Tesseract OCR text extraction, recursive chunking, and tag filtering.
- **Research Workspace**: Visual agent research canvas tracking real-time subtask graph execution, state persistence checkpoints, and saved notes.
- **Executive Report Builder**: Generate PDF, Markdown, and HTML reports complete with executive summaries, technical architecture findings, advantages, limitations, and references.
- **Real-Time Telemetry & Admin Console**: Storage consumption gauges, daily query volume charts, Qdrant vector index telemetry, system health status, and feature flag toggles.

---

## Quickstart Guide

### Option 1: Docker Compose (Recommended)

```bash
# 1. Clone & prepare environment
cp .env.example .env

# 2. Launch full stack container suite
docker-compose up --build
```
Access points:
- **Frontend App**: `http://localhost:3000`
- **FastAPI API & Docs**: `http://localhost:8000/docs`
- **Qdrant Dashboard**: `http://localhost:6333/dashboard`

---

### Option 2: Local Development

#### 1. Backend Setup
```bash
cd backend
python -m venv venv
# On Windows:
venv\Scripts\activate
# Install requirements
pip install -r requirements.txt
# Seed database
python seed_data.py
# Start server
uvicorn main:app --reload --port 8000
```

#### 2. Frontend Setup
```bash
cd frontend
npm install
npm run dev
```
Open `http://localhost:3000` in your browser.

---

## License

Enterprise Proprietary License. Created for ResearchSphere AI.
