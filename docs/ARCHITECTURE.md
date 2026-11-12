# 🏗️ ARCHITECTURE.md — Autonomous SRE Agent

## Goal
A local, open-source autonomous agent that receives webhook alerts, queries local runbooks via RAG, investigates Docker logs, and proposes/executes fixes via a LangGraph state machine with Human-in-the-Loop (HITL) approval.

## Tech Stack
- **Routing & State:** LangGraph
- **LLM:** Ollama (qwen2.5-coder:3b)
- **Embeddings:** HuggingFace (all-MiniLM-L6-v2)
- **Vector DB:** ChromaDB (Local persistent)
- **Environment:** Docker Desktop / Python Docker SDK
- **Webhooks:** FastAPI

## Directory Structure
```text
autonomous_sre_agent/
├── README.md                # Quick start
├── CLAUDE.md                # Instructions for Claude Code
├── Dockerfile               # Image for the sre_agent service
├── docker-compose.yml       # Runs sre_agent + dummy_prod_api
├── requirements.txt         # Dependencies
├── .env.example             # Environment variable names (copy to .env)
│
├── docs/
│   ├── ARCHITECTURE.md      # Project blueprint (this file)
│   ├── RULES.md             # Project constraints
│   └── WORKFLOW.md          # How components, graph nodes and endpoints connect
│
├── agent/                   # The SRE Agent Logic
│   └── sre_agent.py         # LangGraph nodes, state graph and Docker SDK helpers
│
├── api/                     # Webhook Listener
│   └── webhook_listener.py  # FastAPI server: /alert, /approve/{id}, /reject/{id}
│
├── scripts/                 # Utilities
│   ├── setup_runbooks.py    # Embeds runbooks into ChromaDB
│   └── trigger_incident.py  # Crashes target_service and fires webhook
│
├── target_service/          # The "broken" microservice we are monitoring
│   ├── app.py               # FastAPI dummy app
│   ├── Dockerfile
│   └── docker-compose.yml   # Standalone compose for the target only
│
├── tests/
│   └── test_sre_agent.py    # Offline pytest suite (Docker mocked)
│
└── data/                    # Runtime state (git-ignored)
    ├── chroma/              # Persistent local vector database
    └── checkpoints.sqlite   # LangGraph SQLite checkpointer
```
