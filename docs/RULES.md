# 📜 RULES.md — Autonomous SRE Agent Constraints

## 1. Zero Paid APIs (Strict Local-Only)
- NEVER use OpenAI, Anthropic, Pinecone, or AWS APIs.
- For LLM reasoning, ONLY use `ChatOllama` pointing to `qwen2.5-coder:3b` or `mistral`.
- For embeddings, ONLY use `HuggingFaceEmbeddings` (`all-MiniLM-L6-v2`).
- For vector storage, ONLY use local `ChromaDB` saving to `./data/chroma`.

## 2. Mandatory Human-In-The-Loop (HITL)
- The agent is manipulating a "production" environment (Docker).
- The agent may READ logs and statuses autonomously.
- The agent MUST NEVER execute a state-changing action (restart, kill, build) without explicit human approval.
- All LangGraph graphs must use `interrupt_before` on execution nodes.

## 3. LangGraph State is the Single Source of Truth
- Do not use global variables for runtime data.
- All context (alert payload, retrieved runbooks, logs, proposed actions) must be stored in the LangGraph `State` TypedDict.
- Every node must return a partial state update dictionary.

## 4. Execution Sandbox
- Use the Python `docker` SDK to interact with the target service.
- Do not use raw `os.system` or `subprocess` for Docker commands unless absolutely necessary.

## 5. Simplicity & Modularity
- One file, one purpose. Keep functions under 50 lines.
- Always include basic error handling (`try/except`) so the LangGraph state doesn't crash on a missing log file.
