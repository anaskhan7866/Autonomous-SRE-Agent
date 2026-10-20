---
name: sre-docker-executor
description: Guides the agent in writing and executing local Python code to safely manage Docker containers (RESTART, STOP, PAUSE, EXEC) via the docker SDK.
---

# SRE Docker Executor Skill

This skill guides the agent in safely managing and executing remediation commands against Docker containers using the Python `docker` SDK within autonomous incident triage systems (such as LangGraph-based SRE agents).

## Core Principles

1. **Strict Command Formatting**: LLMs must be constrained to output unambiguous, parseable commands.
2. **Container State Validation**: State changes must be verified post-execution via `container.reload()`.
3. **Execution Feedback**: Shell commands inside containers must capture exit codes and standard output/error to provide full observability back into the state graph.
4. **Resilient Error Handling**: Explicitly catch Docker SDK exceptions (`NotFound`, `DockerException`) to prevent unhandled node crashes.

---

## Supported Commands & Syntax

Command  | Target Format                       | SDK Action                    | Verification
:------- | :---------------------------------- | :---------------------------- | :-------------------------------
`RESTART`| `RESTART <container_name>`          | `container.restart(timeout)`  | `container.reload()` -> status
`STOP`   | `STOP <container_name>`             | `container.stop(timeout)`     | `container.reload()` -> status
`PAUSE`  | `PAUSE <container_name>`            | `container.pause()`           | `container.reload()` -> status
`UNPAUSE`| `UNPAUSE <container_name>`          | `container.unpause()`         | `container.reload()` -> status
`EXEC`   | `EXEC <container_name> <command>`   | `container.exec_run(command)` | Exit code & output inspection

---

## 1. LLM Mitigation Planning Prompt Pattern

When prompting the LLM in the planning node, enforce strict command syntax to prevent markdown formatting or conversational filler:

```python
prompt = (
    f"TARGET SERVICE: {service_name}\n"
    f"ALERT DETAILS:\n{alert}\n\n"
    f"RECENT CONTAINER LOGS:\n{logs}\n\n"
    f"RELEVANT SRE RUNBOOK:\n{runbook}\n\n"
    f"PAST ATTEMPTS: If the logs below contain a [VERIFICATION FAILURE], "
    f"do NOT propose the exact same action again. Suggest an alternative authorized action.\n\n"
    f"CRITICAL REQUIREMENT:\n"
    f"1. Read the RELEVANT SRE RUNBOOK to determine the correct action.\n"
    f"2. You are restricted to the following Docker commands: RESTART, STOP, PAUSE, UNPAUSE, and EXEC.\n"
    f"3. For EXEC commands, output ONLY the raw command in the exact format: EXEC <container_name> <shell_command>\n"
    f"4. For all other actions, output ONLY the raw command in the exact format: <COMMAND> <container_name>\n"
    f"5. Do NOT include explanations, markdown formatting, or conversational filler.\n"
    f"Example valid outputs:\n"
    f"RESTART {service_name}\n"
    f"EXEC {service_name} rm -rf /tmp/cache/*\n"
)
```

---

## 2. Python Docker SDK Execution Node Pattern

Implement the execution node using `docker.from_env()`, handling both lifecycle commands and internal shell `EXEC` commands:

```python
import docker
import logging

logger = logging.getLogger("sre_docker_executor")

def execute_docker_remediation(proposed_action: str, default_service: str) -> dict:
    """Executes validated Docker lifecycle and shell remediation actions."""
    tokens = proposed_action.strip().split()
    if not tokens:
        return {"status": "action_failed: Empty action received."}

    command = tokens[0].upper()
    target = tokens[1] if len(tokens) > 1 else default_service

    try:
        client = docker.from_env()
        container = client.containers.get(target)

        logger.info("Executing '%s' on container '%s'...", command, target)

        if command == "RESTART":
            container.restart(timeout=10)
        elif command == "STOP":
            container.stop(timeout=10)
        elif command == "PAUSE":
            container.pause()
        elif command == "UNPAUSE":
            container.unpause()
        elif command == "EXEC":
            if len(tokens) < 3:
                raise ValueError("EXEC command requires a shell command to execute.")
            shell_command = " ".join(tokens[2:])
            logger.info("Running inside container '%s': %s", target, shell_command)
            exit_code, output = container.exec_run(shell_command)

            decoded_output = output.decode("utf-8", errors="replace").strip()
            if exit_code != 0:
                msg = f"EXEC command failed with exit code {exit_code}. Output: {decoded_output}"
                logger.error("%s", msg)
                return {"status": f"action_failed: {msg}"}
            else:
                msg = f"EXEC command successful. Output: {decoded_output}"
                logger.info("%s", msg)
                return {"status": f"action_executed: {msg}"}
        else:
            msg = f"Command '{command}' is not supported by the SRE execution engine."
            logger.error("%s", msg)
            return {"status": f"action_failed: {msg}"}

        # For non-EXEC lifecycle actions, refresh container state to verify
        if command != "EXEC":
            container.reload()
            msg = f"Action '{command}' completed. Container '{target}' is now in status: {container.status}."
            logger.info("%s", msg)

        return {"status": f"action_executed: {msg}"}

    except docker.errors.NotFound:
        msg = f"Cannot execute action: container '{target}' not found."
        logger.error("%s", msg)
        return {"status": f"action_failed: {msg}"}
    except docker.errors.DockerException as exc:
        msg = f"Docker SDK execution error for '{target}': {exc}"
        logger.error("%s", msg)
        return {"status": f"action_failed: {msg}"}
    except Exception as exc:
        msg = f"Unexpected execution error for '{target}': {exc}"
        logger.exception("%s", msg)
        return {"status": f"action_failed: {msg}"}
```

---

## 3. Feedback Loop Integration

- **Output Ingestion**: The returned dictionary (`{"status": f"action_executed: {msg}"}`) is merged directly into the LangGraph `AgentState`.
- **Health Verification**: Subsequent nodes (e.g., `verify_recovery`) should query the target's `/health` endpoint or inspect container status to evaluate whether the remediation succeeded before concluding or escalating.
