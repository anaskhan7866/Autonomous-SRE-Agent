# Autonomous SRE Agent

A local-only SRE agent. When a service alert arrives, it looks up runbooks in a local ChromaDB, reads the container's Docker logs and asks a local Ollama model for a fix. It then **pauses for human approval** (a Discord link or the CLI) before running the fix through the Docker SDK and checking `/health`.

No paid or external AI APIs are used.

## Quick start
```bash
python -m venv venv && venv/Scripts/activate      # Windows; use venv/bin/activate on Linux/macOS
pip install -r requirements.txt
cp .env.example .env                               # then fill in DISCORD_WEBHOOK_URL and BASE_URL
ollama pull qwen2.5-coder:3b
python scripts/setup_runbooks.py                   # seed runbooks (run from the repo root)
```

Run the full stack:
```bash
docker compose up --build
python scripts/trigger_incident.py
```

Or run the CLI demo with approval in the terminal:
```bash
python agent/sre_agent.py          # add --yes to auto-approve
```
On the host, Ollama defaults to `http://host.docker.internal:11434`. If that name doesn't resolve on your machine, set `OLLAMA_BASE_URL=http://localhost:11434`.

## Tests
```bash
python -m pytest -q
python -m pyflakes agent api scripts target_service tests
```

## Layout
| Path | Purpose |
|---|---|
| `agent/` | LangGraph state machine and Docker SDK helpers |
| `api/` | FastAPI webhook listener (`/alert`, `/approve/{id}`, `/reject/{id}`) |
| `scripts/` | Runbook seeding and incident trigger |
| `target_service/` | Dummy "production" API that the agent monitors |
| `tests/` | Offline pytest suite |
| `docs/` | [Architecture](docs/ARCHITECTURE.md), [rules](docs/RULES.md), [workflow](docs/WORKFLOW.md) |
| `data/` | Runtime ChromaDB and checkpoints (git-ignored) |
