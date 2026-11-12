# Development Workflow Rules (Multi-Agent)

## Roles and file ownership
Only one agent may write to a file in a given task. These ownership rules prevent conflicting edits.

| Agent | Model | May edit | Read-only? |
|---|---|---|---|
| Team lead (main session) | opus | Coordination files: `CLAUDE.md`, `.claude/**`, docs (`docs/**`, `README.md`) | no |
| backend-developer | sonnet | `agent/`, `api/` (logic), `scripts/`, `target_service/`, `policies/`, `requirements.txt`, Docker files, `.env.example` | no |
| frontend-developer | sonnet | `web/`, `frontend/`, and only the HTML/Discord presentation strings in `api/webhook_listener.py` | no |
| qa-engineer | sonnet | `tests/`, `conftest.py`, `pytest.ini` | no |
| security-reviewer | sonnet | nothing | yes |
| code-reviewer | opus | nothing | yes |

- If a task needs a file another agent owns, the team lead hands that part of the work to the owner.
- Two writing agents must never be assigned the same file at the same time. When `api/webhook_listener.py` needs both logic and presentation changes, do them one after the other: backend first, then frontend.
- No agent edits `.env`. All agents read configuration from `.env.example`.
- Exception to `.claude/**` ownership: each writing agent may edit only its own `.claude/notes/<agent-name>.md`. These are compaction-proof progress notes; see `.claude/notes/README.md`.

## Context management
- Auto-compaction windows: the lead uses 200,000 (`autoCompactWindow` in `.claude/settings.json`). backend-developer and frontend-developer use 150,000, and qa-engineer, security-reviewer and code-reviewer use 120,000 (`autoCompactWindow` in each agent's frontmatter; it can only lower the session window).
- The `PreCompact` hook tells the summary to keep: task, progress, files modified, decisions, errors, tests run and next steps. The `SessionStart` (`compact`) hook re-injects the agent's notes and `git status`.
- Prefer 3–5 specialists per feature. Resume the same agent with SendMessage for fix rounds instead of spawning a new one, and avoid nested subagents.

## Standard pipeline
1. **Plan** (lead): read `docs/WORKFLOW.md`, split the work by file ownership, and write the task list.
2. **Implement**: backend-developer and/or frontend-developer, in parallel only when their files don't overlap.
3. **Test**: qa-engineer adds or updates tests and runs `python -m pytest -q`.
4. **Review**: code-reviewer and security-reviewer, in parallel (both read-only).
5. **Fix loop**: the lead sends each finding to the owning agent, then repeats steps 3–4.
6. **Hand-off** (lead): summarise the changes to the user. Commit only when the user asks. Never push or merge without explicit approval.

## Definition of done
- `python -m pytest -q` passes. Report the exact counts.
- `python -m pyflakes agent api scripts target_service tests` is clean.
- The HITL interrupt before `execute_action` is still in place.
- `docs/WORKFLOW.md` is updated if the graph, endpoints or run modes changed.
