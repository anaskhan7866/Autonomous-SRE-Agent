# CLAUDE.md: Autonomous SRE Agent

A local-only autonomous SRE agent: a FastAPI webhook receives alerts, a LangGraph state machine fetches runbooks from local ChromaDB, reads Docker logs, asks Ollama for a fix, pauses for human approval (Discord link or CLI), executes the fix via the Docker SDK and verifies `/health`.

## Read first
- @docs/RULES.md: hard project constraints (local-only models, mandatory HITL, state discipline, Docker SDK).
- @docs/WORKFLOW.md: how the components, graph nodes and endpoints connect.
- `docs/ARCHITECTURE.md`: goals, tech stack and directory layout.
- `.claude/rules/development.md`: multi-agent roles, file ownership and pipeline (loaded automatically).

## Commands
- Tests: `python -m pytest -q` (offline; Docker is mocked)
- Lint: `python -m pyflakes agent api scripts target_service tests`
- Seed runbooks + company policy: `python scripts/setup_runbooks.py` and `python scripts/setup_policies.py` (run from the repo root; re-run setup_policies after editing `policies/company_policy.md`)
- Full stack: `docker compose up --build`, then `python scripts/trigger_incident.py`
- CLI demo: `python agent/sre_agent.py [--yes]`

## Development rules
1. **Understand before modifying.** Read the relevant files, their callers and `docs/WORKFLOW.md` before changing code.
2. **Follow existing conventions.** PEP 8, type hints, module `logger` with `[NODE: name]` prefixes, `try/except` in every graph node returning a partial state dict, functions under 50 lines.
3. **Avoid unnecessary dependencies.** Use the stdlib or existing packages first, and justify any addition to `requirements.txt`. Never add paid or external AI APIs.
4. **Never expose secrets.** Don't read, print, log or commit `.env` or its values. Use `.env.example` to learn the variable names. Never put webhook URLs or tokens in code, logs, HTML or messages.
5. **Maintain backward compatibility.** Keep `AgentState` keys, endpoint paths (`/alert`, `/approve/{id}`, `/reject/{id}`), env var names and CLI flags stable unless the user approves a breaking change.
6. **Keep HITL intact.** `interrupt_before=["execute_action"]` must never be removed or bypassed.
7. **Test changes.** Run the tests and linter after every change, add tests for new behaviour, and report results accurately (including failures).
8. **Stay in scope.** Don't modify files unrelated to the task, and don't reformat untouched code.
9. **Review regressions.** Before finishing, check the callers and graph paths affected by the change and state the regression risk.
10. **Git safety.** Commit only when asked. **Never push, merge or open/merge PRs without explicit user approval.**
