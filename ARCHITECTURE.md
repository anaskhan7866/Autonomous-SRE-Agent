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
├── RULES.md                 # Project constraints
├── ARCHITECTURE.md          # Project blueprint
├── requirements.txt         # Dependencies
│
├── target_service/          # The "broken" microservice we are monitoring
│   ├── app.py               # FastAPI dummy app
│   ├── Dockerfile           
│   └── docker-compose.yml   
│
├── agent/                   # The SRE Agent Logic
│   ├── sre_agent.py         # LangGraph nodes and state graph
│   └── tools.py             # Docker SDK helper functions
│
├── api/                     # Webhook Listener
│   └── webhook_listener.py  # FastAPI server receiving PagerDuty-style alerts
│
├── scripts/                 # Utilities
│   ├── setup_runbooks.py    # Embeds markdown runbooks into ChromaDB
│   └── trigger_incident.py  # Crashes target_service and fires webhook
│
└── data/                    
    └── chroma/              # Persistent local vector database (git-ignored)
```
