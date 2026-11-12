# 🔀 WORKFLOW.md: How the Autonomous SRE Agent Fits Together

This file shows how the parts of the project connect: who calls whom, what data moves between them, and the order things happen during an incident. For the goals and tech stack, see [ARCHITECTURE.md](ARCHITECTURE.md). For the project constraints, see [RULES.md](RULES.md).

---

## 1. Component Map

```mermaid
flowchart LR
    subgraph Host["Host machine"]
        TRIG["scripts/trigger_incident.py"]
        SETUP["scripts/setup_runbooks.py"]
        POLSETUP["scripts/setup_policies.py<br/>(policies/company_policy.md)"]
        OLLAMA["Ollama<br/>qwen2.5-coder:3b<br/>:11434"]
        DATA[("./data<br/>chroma/ (runbooks + company_policies)<br/>+ checkpoints.sqlite")]
        SOCK["/var/run/docker.sock"]
    end

    subgraph Compose["docker-compose.yml"]
        subgraph SRE["sre_agent container :5000"]
            API["api/webhook_listener.py<br/>(FastAPI)"]
            GRAPH["agent/sre_agent.py<br/>(LangGraph sre_graph)"]
        end
        TARGET["dummy_prod_api container :8080<br/>target_service/app.py"]
    end

    DISCORD["Discord webhook"]
    HUMAN(("Human operator"))

    TRIG -- "POST /simulate_crash" --> TARGET
    TRIG -- "POST /alert" --> API
    SETUP -- "embed + write runbooks" --> DATA
    POLSETUP -- "embed + write policy rules" --> DATA
    API -- "imports sre_graph" --> GRAPH
    GRAPH -- "runbook + policy similarity_search" --> DATA
    GRAPH -- "checkpoints per thread_id" --> DATA
    GRAPH -- "ChatOllama.invoke (planner + policy judge)" --> OLLAMA
    GRAPH -- "Docker SDK: logs / restart / exec" --> SOCK
    SOCK -.controls.-> TARGET
    GRAPH -- "GET /health" --> TARGET
    API -- "approval message / policy escalation" --> DISCORD
    DISCORD --> HUMAN
    HUMAN -- "GET /approve|reject/{id}?token= (confirm page)<br/>POST /approve|reject/{id} (decides)" --> API
```

### File responsibilities

| File | Role | Connects to |
|---|---|---|
| [api/webhook_listener.py](../api/webhook_listener.py) | FastAPI entry point (port 5000). Accepts alerts, runs the graph in background tasks, issues a one-time approval token at each pause, sends the Discord approval message, serves confirmation pages (GET) and records the decision (POST). | `agent.sre_agent.sre_graph`, Discord, `.env` |
| [agent/sre_agent.py](../agent/sre_agent.py) | LangGraph state machine: 9 nodes, a company-policy gate (`check_policy`), two routers, a SQLite checkpointer and a pause before `execute_action`. Also has a standalone CLI mode. | ChromaDB (runbooks + policies), Ollama, Docker SDK, target `/health` |
| [policies/company_policy.md](../policies/company_policy.md) | **Sample** company policy (rules POL-001 to POL-007), one `##` section per rule. Replace it with the real policy, then re-run `setup_policies.py`. | Loaded into ChromaDB |
| [scripts/setup_policies.py](../scripts/setup_policies.py) | Seeds the policy into `./data/chroma` (collection `company_policies`), one document per rule. Idempotent. | ChromaDB on disk |
| [target_service/app.py](../target_service/app.py) | The "broken" service. `/health`, `/simulate_crash`, `/recover`. Crash state is kept **in memory**, so a container restart fixes it. | Probed by the agent, crashed by the trigger script |
| [scripts/setup_runbooks.py](../scripts/setup_runbooks.py) | One-time seeding of 2 runbooks into `./data/chroma` (collection `sre_runbooks`). | ChromaDB on disk |
| [scripts/trigger_incident.py](../scripts/trigger_incident.py) | Demo driver: crashes the target, then fires an alert. Uses `localhost` URLs, so it runs on the host. | `:8080/simulate_crash`, `:5000/alert` |
| [tests/test_sre_agent.py](../tests/test_sre_agent.py) | Pytest tests for `execute_action` with Docker mocked. | `agent.sre_agent.execute_action` |
| [docker-compose.yml](../docker-compose.yml) / [Dockerfile](../Dockerfile) | Runs `sre_agent` and `dummy_prod_api`. Mounts `./data` and the Docker socket, and maps `host.docker.internal`. | Host Docker, Ollama |

---

## 2. LangGraph State Machine

Defined in `create_sre_graph()` ([agent/sre_agent.py:392](../agent/sre_agent.py#L392)).

```mermaid
stateDiagram-v2
    [*] --> parse_alert
    parse_alert --> investigate_logs
    investigate_logs --> retrieve_context
    retrieve_context --> plan_mitigation
    plan_mitigation --> check_policy
    check_policy --> HITL : allowed
    check_policy --> plan_mitigation : replan (denied, policy_attempts < 3)
    check_policy --> escalate_policy_denied : escalate (denied, policy_attempts >= 3)
    escalate_policy_denied --> [*]
    state "⏸ interrupt_before execute_action (HITL)" as HITL
    HITL --> execute_action : human confirms approval (POST + token)
    HITL --> [*] : human confirms rejection (hitl_decision=rejected, thread stays paused)
    execute_action --> verify_recovery
    verify_recovery --> [*] : resolved (HTTP 200)
    verify_recovery --> investigate_logs : retry_investigation (iteration_count < 3)
    verify_recovery --> escalate_to_human : escalated (iteration_count >= 3)
    escalate_to_human --> [*]
```

### Nodes

| Node | Reads from state | Writes to state | Side effects |
|---|---|---|---|
| `parse_alert` | `alert_payload` | `service_name`, `status=alert_parsed`, `iteration_count=0` | none. Looks for the service in `service`, then `service_name`, then `incident.service.name`, else `dummy_prod_api` |
| `investigate_logs` | `service_name`, `iteration_count`, `container_logs` | `container_logs` (last 20 lines plus earlier `[VERIFICATION FAILURE]` notes), `iteration_count += 1`, resets `policy_attempts=0` and `denied_actions=[]` for the new cycle | Docker `container.logs(tail=20)` |
| `retrieve_context` | `service_name`, `alert_payload`, `container_logs` | `runbook_context` | ChromaDB `similarity_search(k=3)`. The query (`_build_runbook_query`) combines the service, alert text (severity, description, summary…, 200 chars each) and the newest ERROR/CRITICAL/WARN/"No space" log lines (500 chars), so the runbook matches the actual incident |
| `plan_mitigation` | `alert_payload`, `runbook_context`, `container_logs`, `denied_actions` | `proposed_action` (the last LLM output line starting with `RESTART`/`PAUSE`/`UNPAUSE`/`EXEC`, quotes and backticks removed; otherwise the last line) | Ollama call. Adds a **POLICY REJECTIONS** section listing earlier denied commands and reasons. If the model still repeats a denied command, it is asked once more ("DENIED … output a DIFFERENT command"); if it repeats again the agent falls back to `RESTART <service>` (unless that was denied too, in which case the repeat is kept and the gate escalates). Whatever it returns still goes through `check_policy` and HITL. Falls back to `RESTART <service>` on error |
| `check_policy` | `proposed_action`, `service_name`, `policy_attempts`, `denied_actions` | `policy_verdict` (`allowed`/`denied`), `policy_reason`, `policy_attempts += 1`, appends `{"command": …, "reason": …}` to `denied_actions` on deny | 1) hard block/allowlist (no LLM), 2) Docker lookup confirming the target is a managed container, 3) ChromaDB `company_policies` search, 4) Ollama judge answering `ALLOW: …` / `DENY: …`. Any error, empty retrieval or unclear verdict denies |
| `escalate_policy_denied` | `denied_actions` | `status=Escalated - Policy Denied` | The listener sends a Discord escalation (no approval link) |
| `execute_action` ⏸ | `proposed_action`, `service_name` | `status=action_executed:…` or `action_failed:…` (output capped at 2000 chars) | Re-runs the hard block first (`action_failed: refused by hard block`), then Docker `RESTART`, `PAUSE`, `UNPAUSE` or `EXEC`. `EXEC` has a 30 s timeout (`action_failed: timeout`; the process inside the container keeps running). `STOP` exists in the executor but is always refused |
| `verify_recovery` | `service_name` | `status=Resolved`, or `Escalated - Restart Failed` with a `[VERIFICATION FAILURE]` note added to `container_logs` | waits 2s, then `GET /health` |
| `escalate_to_human` | `service_name`, `status` | `status=Escalated to Human Operator` | prints a critical banner; the listener sends a Discord escalation (no approval link) |

**Router** `check_recovery_status`: returns `resolved` when the status is `Resolved`. Otherwise it returns `retry_investigation` while `iteration_count < 3`, then `escalated`. That allows at most **3 remediation attempts**, and each attempt needs its own human approval.

**Router** `route_policy`: `allowed` goes to the HITL pause, `replan` sends the LLM back to `plan_mitigation`, whose prompt then includes a **POLICY REJECTIONS** list (each denied command with its reason, from `format_policy_rejections`) and the instruction that those commands are not allowed and it must give a different command that is safe and allowed, and `escalate` ends the run after 3 denials (`MAX_POLICY_ATTEMPTS`) in one remediation cycle.

### Company-policy gate

The gate runs **before** the human approval request and never replaces it: an allowed command still pauses and needs a confirmed Discord approval.
1. **Hard block / allowlist** (`hard_block_reason()`): always wins over the LLM, and needs no model.
   - Commands longer than 200 characters are denied. Only `A-Z a-z 0-9 _ space . / : = @ % + , -` are allowed, so backslashes, quotes, globs (`* ? [ ]`), `~`, `$`, `{ }`, shell operators, tabs/newlines and non-ASCII are all refused.
   - `STOP` is always denied (POL-003). `RESTART`/`PAUSE`/`UNPAUSE` take exactly one argument, the target.
   - `EXEC` is split with `shlex.split`, exactly as Docker will run it. `argv[0]` must be a bare name from the allowlist, and each tool has its own flag allowlist:

     | Tool | Allowed flags | Paths |
     |---|---|---|
     | `ls` | `-l -a -la -al -lh -h` | optional |
     | `cat` | none | required |
     | `head`, `tail` | `-n N` / `-nN` only (no `-f`, `-F`, `-c`) | required |
     | `ps` | none, or exactly `-ef` / `aux` | none |
     | `df` | `-h` | optional |
     | `du` | `-s -h -sh` | required |
     | `free` | `-h -m` | none |
     | `uptime`, `whoami`, `id` | none | none |
     | `rm` | `-r -f -rf -fr -R -v` | required, strictly below `/tmp/` |

     Read paths must be absolute, contain no `..`, and normalise to `/tmp`, `/var/log` or `/app/logs` (or below). `.env*` and `docker.sock` are refused even there. `rm` paths may not contain `.` or `..` segments. Symlinks inside these folders can't be checked statically, so run the target with a read-only filesystem or a non-root user where possible.
   - **Target**: it must equal the alert's service and be listed in `MANAGED_CONTAINERS` (comma-separated, default `dummy_prod_api`). `sre_agent`, names starting with `/` and the agent's own `HOSTNAME` are always refused. `/alert` rejects service names outside `^[A-Za-z0-9_.-]{1,64}$` with 400.
2. **Managed-target check**: the Docker SDK resolves the target, and the real container name must be managed (so an alias or ID prefix can't sneak in). Any Docker error denies.
3. **RAG**: the policy rules are fetched from ChromaDB (`company_policies`) with a query built from the verb and program (e.g. `EXEC rm`). The deny rules POL-005, 006, 007 and 003 are always placed first.
4. **LLM judge**: Ollama gets the rules and the command, wrapped in `<<<COMMAND>>> … <<<END>>>` and marked as untrusted data. It must answer with one line, `ALLOW: <reason>` or `DENY: <reason>`; surrounding `**` or backticks are tolerated. Anything else is a deny, including a second line or the word ALLOW/DENY appearing again in the reason. The reason is advisory and is shown to the approver as such.
   - **PRE-CHECK block**: when step 1 passed, the prompt also gets a fixed line built by code (never by the LLM) saying why the command is in the allowlist, for example "rm of a path strictly under /tmp/ (POL-004)", "RESTART … (POL-001)" or "PAUSE/UNPAUSE … (POL-002)". The judge is told to deny only if a retrieved clause specifically forbids the action, citing its ID. Without this, the small local judge (`qwen2.5-coder:3b`) wrongly denied allowed commands such as `rm -rf /tmp/cache`. Keep the policy IDs in `precheck_description` in sync with `policies/company_policy.md`.
   - **What actually enforces safety**: the static allowlist (step 1), the managed-target check (step 2) and the human approval. With the small local model, the judge rarely denies a command that passed step 1; in testing it allowed one even with an injected change-freeze clause. Treat it as a best-effort second opinion. Rules that must be enforced, such as a change freeze, belong in code, not only in the policy text.

Three more layers stop a thread that never passed the gate (e.g. a checkpoint from before the gate existed): `pause_and_notify` sends no approval link unless `policy_verdict == "allowed"` (it sends an escalation instead), the approve/reject endpoints answer 409 "Not Approvable" without recording anything, and `resume_agent_workflow` refuses to resume.

Seed the policy once per environment with `python scripts/setup_policies.py`. Without it, retrieval is empty and every command is denied.

### State (`AgentState`)

`alert_payload`, `service_name`, `runbook_context`, `container_logs`, `proposed_action`, `status`, `iteration_count`, `approval_token`, `hitl_decision`, `policy_verdict`, `policy_reason`, `policy_attempts`, `denied_actions`. The SQLite checkpointer (`./data/checkpoints.sqlite`) saves state per `thread_id`, which is what lets the graph pause and resume across HTTP requests.

### How a failed attempt reaches the next prompt

`verify_recovery` adds a `[VERIFICATION FAILURE]` line to `container_logs`. `investigate_logs` fetches fresh logs and keeps those lines. `retrieve_context` re-queries with the fresh logs, and `plan_mitigation`'s **PAST ATTEMPTS** instruction tells the LLM not to repeat the same action.

---

## 3. End-to-End Incident Flow (Remote HITL via Discord)

```mermaid
sequenceDiagram
    autonumber
    participant T as trigger_incident.py
    participant S as dummy_prod_api :8080
    participant A as webhook_listener :5000
    participant G as sre_graph (LangGraph)
    participant C as ChromaDB
    participant O as Ollama
    participant D as Discord
    participant H as Human

    T->>S: POST /simulate_crash
    S-->>T: 500 (is_crashed=True, CRITICAL logs)
    T->>A: POST /alert {service, severity, issue}
    A-->>T: 202 {thread_id}
    A->>G: BackgroundTask run_agent_workflow(alert, thread_id)
    G->>G: parse_alert
    G->>S: investigate_logs (docker logs tail=20)
    G->>C: retrieve_context (query = alert + error log lines)
    G->>O: plan_mitigation (prompt)
    O-->>G: "RESTART dummy_prod_api"
    loop check_policy (up to 3 attempts per cycle)
        G->>G: hard block / allowlist + managed-target check
        G->>C: retrieve company_policies rules
        G->>O: judge: ALLOW or DENY
        alt DENY and attempts < 3
            G->>O: plan_mitigation again with POLICY REJECTIONS ("not allowed, give something safe")
        else DENY on 3rd attempt
            G->>G: escalate_policy_denied → END
            A->>D: escalation message (denied commands, no approval link)
        end
    end
    Note over G: ⏸ interrupt_before execute_action<br/>state checkpointed to SQLite
    A->>G: pause_and_notify: update_state(approval_token, hitl_decision=pending)
    A->>D: notify_discord(thread_id, service, action, token)
    D->>H: message with Review & approve / Review & reject links (?token=…)
    H->>A: GET /approve/{thread_id}?token=… (or /reject)
    A-->>H: confirmation page. Nothing runs yet.
    alt Confirm approval
        H->>A: POST /approve/{thread_id} (form: token)
        A->>G: claim decision under lock → hitl_decision=approved
        A->>G: BackgroundTask resume_agent_workflow → stream(None)
        G->>S: execute_action (container.restart)
        G->>S: verify_recovery GET /health
        S-->>G: 200 → status=Resolved → END
        Note over A,G: If verification fails and the graph pauses again,<br/>pause_and_notify issues a NEW token and notifies Discord again.
    else Confirm rejection
        H->>A: POST /reject/{thread_id} (form: token)
        Note over G: hitl_decision=rejected. Thread stays paused; later approvals get 409.
    end
```

**Approval endpoint rules.** A GET only renders a confirmation page, so link-preview crawlers (Discord fetches links as soon as a message is posted) can't trigger an action. Only a POST with the current token decides.

| Situation | Response |
|---|---|
| Unknown `thread_id` | 404 |
| Missing or wrong token (including an old token from an earlier pause) | 403 |
| Already decided, the thread isn't paused before `execute_action`, or its command didn't pass the policy gate | 409 |
| Valid GET | 200 confirmation page (no state change) |
| Valid first POST | 200; approve resumes the graph, reject records `hitl_decision=rejected` |

---

## 4. Entry Points and Run Modes

| Mode | Command | Approval method |
|---|---|---|
| **Full stack (Docker)** | `docker compose up --build`, then `python scripts/trigger_incident.py` from the host | Discord links to `BASE_URL/approve/{id}` |
| **Listener on host** | `python api/webhook_listener.py` (target running via `target_service/docker-compose.yml`) | Discord links |
| **CLI demo** | `python agent/sre_agent.py` (add `--yes` or `-y` to auto-approve) | Terminal `y/n` prompt, repeated for each retry |
| **Tests** | `pytest` | n/a (Docker mocked) |

**Prerequisites (in order):**
1. Ollama running with `qwen2.5-coder:3b` pulled.
2. `python scripts/setup_runbooks.py` and `python scripts/setup_policies.py` run from the project root, which seed `./data/chroma` (runbooks and company policy). Without the policy, every command is denied.
3. `.env` copied from [.env.example](../.env.example) with `DISCORD_WEBHOOK_URL` and `BASE_URL` set. The listener refuses to start without both.
4. For phone or remote approvals, `BASE_URL` must be a public tunnel (ngrok or localhost.run) to port 5000.

### Environment-dependent addresses

| Setting | Inside the container | On the host |
|---|---|---|
| Ollama (`OLLAMA_BASE_URL`) | `http://host.docker.internal:11434` (set in compose) | same default, so set `OLLAMA_BASE_URL=http://localhost:11434` if needed |
| Health check (`get_healthcheck_url`) | `http://dummy_prod_api:8080/health` (detected via `/.dockerenv`) | `http://localhost:8080/health` |
| Override | `HEALTHCHECK_URL` env var wins in both cases | |

---

## 5. Known Gaps and Inconsistencies (from the scan)

These are things I noticed while tracing the flow. None of them have been changed.

1. ~~**Retries 2 and 3 never notify Discord.**~~ Fixed: `resume_agent_workflow` calls `pause_and_notify`, which issues a new token and sends a new Discord message whenever the graph pauses again.
2. ~~**Reject does not record anything.**~~ Fixed: a confirmed reject sets `hitl_decision=rejected`, and any later approve gets 409.
3. ~~**The approve and reject endpoints have no authentication or one-time use.**~~ Fixed: GET only shows a confirmation page (a live test showed Discord's link-preview crawler hitting both links within a second of posting), POST requires a per-pause `secrets.token_urlsafe(32)` token, and the first decision is final (atomic under a lock). The listener redacts `token=` from uvicorn's access log, caps approval form bodies at 4 KB and `/alert` bodies at 64 KB, and sends `no-store`, `no-referrer`, `X-Frame-Options: DENY` and `nosniff` on every response, plus a strict CSP on everything except FastAPI's `/docs`, `/redoc` and `/openapi.json`. Remaining limits: the token sits in the URL query, so it can still appear in tunnel or proxy logs and browser history; it's stored in plaintext in the local SQLite checkpoint; the decision lock is per process, so run the listener as a single uvicorn worker. A thread whose resume crashes while still paused gets a fresh token and notification. The agent also retries a failed Discord notification once, then logs an "Operator NOT notified" error.
4. **`EXEC` runs LLM-generated shell commands** inside the container. Mitigated: the company-policy gate (hard-block list, then RAG policy, then LLM judge) runs before the HITL pause, and human approval is still required. The allowlist means `EXEC` can only run read-only diagnostics or `rm` below `/tmp/`. Remaining limit: the judge is a small local model and, for commands that pass the allowlist, effectively always answers ALLOW, so a mistaken ALLOW still needs a human to catch it. Policy-only rules (e.g. a change freeze) are not enforced unless they are added in code.
5. ~~**`ARCHITECTURE.md` lists `agent/tools.py`**, which doesn't exist.~~ Fixed: the directory tree now matches the repo. The Docker helpers live inside `sre_agent.py`.
6. ~~**The Dockerfile name doesn't match.**~~ Fixed: `dockerfile` was renamed to `Dockerfile`, which matches the reference in `docker-compose.yml`.
7. **Importing `agent.sre_agent` has side effects.** It opens `./data/checkpoints.sqlite` and compiles `sre_graph`, relative to the current directory, so even running tests creates `./data/`.
8. **Paths are relative to where you run from.** `./data/chroma` must be the same directory for `setup_runbooks.py` and the agent, so always run from the project root, or rely on the compose volume.
9. **Test coverage only covers `execute_action`.** There are no tests for the router, `parse_alert`, `verify_recovery` or the listener endpoints.
