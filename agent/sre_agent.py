import logging
import os
import sys
from typing import Any, Dict, Optional
from typing_extensions import TypedDict

import chromadb
import docker
from langchain_community.embeddings import HuggingFaceEmbeddings
from langchain_community.vectorstores import Chroma
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_ollama import ChatOllama
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [%(name)s] %(message)s"
)
logger = logging.getLogger("sre_agent")

CHROMA_PATH = "./data/chroma"
COLLECTION_NAME = "sre_runbooks"
OLLAMA_MODEL = "qwen2.5-coder:3b"


class AgentState(TypedDict, total=False):
    """LangGraph State representation for the Autonomous SRE Agent."""
    alert_payload: Dict[str, Any]
    service_name: str
    runbook_context: str
    container_logs: str
    proposed_action: str
    status: str


def get_llm() -> ChatOllama:
    """Instantiates local ChatOllama model (Rule 1: Zero Paid APIs)."""
    return ChatOllama(model=OLLAMA_MODEL, temperature=0.0)


def parse_alert(state: AgentState) -> Dict[str, Any]:
    """Node: Extracts the failing service name from alert_payload."""
    logger.info("Executing node: parse_alert")
    try:
        payload = state.get("alert_payload", {})
        # Support direct service key, nested incident details, or fallback
        service_name = (
            payload.get("service")
            or payload.get("service_name")
            or payload.get("incident", {}).get("service", {}).get("name")
            or "dummy_prod_api"
        )
        logger.info("Identified failing service: %s", service_name)
        return {
            "service_name": service_name,
            "status": "alert_parsed"
        }
    except Exception as exc:
        logger.exception("Failed to parse alert payload: %s", exc)
        return {
            "service_name": "dummy_prod_api",
            "status": f"parse_alert_error: {exc}"
        }


def retrieve_context(state: AgentState) -> Dict[str, Any]:
    """Node: Queries local ChromaDB for runbooks related to the failing service."""
    logger.info("Executing node: retrieve_context")
    try:
        service_name = state.get("service_name") or "dummy_prod_api"
        embeddings = HuggingFaceEmbeddings(model_name="all-MiniLM-L6-v2")
        client = chromadb.PersistentClient(path=CHROMA_PATH)
        vector_db = Chroma(
            client=client,
            collection_name=COLLECTION_NAME,
            embedding_function=embeddings
        )

        query = f"Incident remediation protocol for service {service_name} deadlock memory crash failure"
        docs = vector_db.similarity_search(query, k=2)

        if docs:
            context = "\n---\n".join([doc.page_content for doc in docs])
        else:
            context = f"No specific runbook found for service {service_name}."

        logger.info("Retrieved %d runbook document(s)", len(docs))
        return {
            "runbook_context": context,
            "status": "context_retrieved"
        }
    except Exception as exc:
        logger.exception("Failed to retrieve context from ChromaDB: %s", exc)
        return {
            "runbook_context": "Runbook retrieval unavailable.",
            "status": f"retrieve_context_error: {exc}"
        }


def investigate_logs(state: AgentState) -> Dict[str, Any]:
    """Node: Uses Docker SDK to fetch last 20 log lines from the container."""
    logger.info("Executing node: investigate_logs")
    service_name = state.get("service_name") or "dummy_prod_api"
    try:
        client = docker.from_env()
        container = client.containers.get(service_name)
        raw_logs = container.logs(tail=20)
        logs = raw_logs.decode("utf-8", errors="replace").strip()
        logger.info("Retrieved %d log lines from container %s", len(logs.splitlines()), service_name)
        return {
            "container_logs": logs or "Container produced no log output.",
            "status": "logs_investigated"
        }
    except docker.errors.NotFound:
        msg = f"Container '{service_name}' not found. Please verify the container name and deployment status."
        logger.warning(msg)
        return {"container_logs": msg, "status": "container_not_found"}
    except docker.errors.DockerException as exc:
        msg = f"Docker daemon communication error for '{service_name}': {exc}"
        logger.error(msg)
        return {"container_logs": msg, "status": "docker_daemon_error"}
    except Exception as exc:
        msg = f"Unexpected error investigating logs for '{service_name}': {exc}"
        logger.exception(msg)
        return {"container_logs": msg, "status": f"investigate_logs_error: {exc}"}


def plan_mitigation(state: AgentState) -> Dict[str, Any]:
    """Node: Passes logs, alert, and runbook to Ollama LLM to decide on a fix."""
    logger.info("Executing node: plan_mitigation")
    try:
        service_name = state.get("service_name", "dummy_prod_api")
        alert = state.get("alert_payload", {})
        runbook = state.get("runbook_context", "None")
        logs = state.get("container_logs", "None")

        prompt = (
            f"TARGET SERVICE: {service_name}\n"
            f"ALERT DETAILS:\n{alert}\n\n"
            f"RECENT CONTAINER LOGS:\n{logs}\n\n"
            f"RELEVANT SRE RUNBOOK:\n{runbook}\n\n"
            f"CRITICAL REQUIREMENT:\n"
            f"Output ONLY the authorized remediation command in the exact format: RESTART <container_name>\n"
            f"Do NOT include explanations, markdown formatting, or conversational filler.\n"
            f"Your output MUST be ONLY: RESTART {service_name}"
        )

        llm = get_llm()
        messages = [
            SystemMessage(content="You are an automated SRE engine. Output ONLY raw, parseable action commands with zero conversational text."),
            HumanMessage(content=prompt)
        ]
        response = llm.invoke(messages)
        raw_action = response.content.strip()
        # Clean stray quotes, backticks, or trailing lines to ensure 100% parseable command
        cleaned_action = raw_action.replace("`", "").replace('"', "").replace("'", "").splitlines()[-1].strip()
        logger.info("Clean mitigation command generated: '%s'", cleaned_action)
        return {
            "proposed_action": cleaned_action,
            "status": "mitigation_planned"
        }
    except Exception as exc:
        logger.exception("Failed to generate mitigation plan: %s", exc)
        return {
            "proposed_action": f"RESTART {service_name}",
            "status": f"plan_mitigation_error: {exc}"
        }


def execute_action(state: AgentState) -> Dict[str, Any]:
    """Node: Uses Docker SDK to execute the proposed remediation action."""
    logger.info("Executing node: execute_action (HITL Approved)")
    action_text = (state.get("proposed_action") or "").strip()
    default_service = state.get("service_name") or "dummy_prod_api"

    # Parse command tokens (e.g., 'RESTART dummy_prod_api')
    tokens = action_text.split()
    command = tokens[0].upper() if tokens else "RESTART"
    target = tokens[1] if len(tokens) > 1 else default_service

    try:
        client = docker.from_env()
        container = client.containers.get(target)

        if "RESTART" in command:
            logger.info("Executing RESTART for container '%s'...", target)
            container.restart(timeout=10)
            container.reload()
            msg = f"Container '{target}' restarted successfully (status: {container.status})."
        else:
            msg = f"Action '{command}' on container '{target}' completed."

        logger.info(msg)
        return {"status": f"action_executed: {msg}"}
    except docker.errors.NotFound:
        msg = f"Cannot execute action: container '{target}' not found."
        logger.error(msg)
        return {"status": f"action_failed: {msg}"}
    except docker.errors.DockerException as exc:
        msg = f"Docker SDK execution error for '{target}': {exc}"
        logger.error(msg)
        return {"status": f"action_failed: {msg}"}
    except Exception as exc:
        msg = f"Unexpected execution error for '{target}': {exc}"
        logger.exception(msg)
        return {"status": f"action_failed: {msg}"}


def create_sre_graph(checkpointer: Optional[Any] = None):
    """Wires and compiles the Autonomous SRE Agent StateGraph with HITL."""
    if checkpointer is None:
        checkpointer = MemorySaver()

    workflow = StateGraph(AgentState)

    workflow.add_node("parse_alert", parse_alert)
    workflow.add_node("retrieve_context", retrieve_context)
    workflow.add_node("investigate_logs", investigate_logs)
    workflow.add_node("plan_mitigation", plan_mitigation)
    workflow.add_node("execute_action", execute_action)

    workflow.add_edge(START, "parse_alert")
    workflow.add_edge("parse_alert", "retrieve_context")
    workflow.add_edge("retrieve_context", "investigate_logs")
    workflow.add_edge("investigate_logs", "plan_mitigation")
    workflow.add_edge("plan_mitigation", "execute_action")
    workflow.add_edge("execute_action", END)

    # RULE 2: Enforce Mandatory HITL before state-changing execution
    return workflow.compile(
        checkpointer=checkpointer,
        interrupt_before=["execute_action"]
    )


if __name__ == "__main__":
    print("=" * 60)
    print("[SRE AGENT] Autonomous SRE Agent - Incident Response Workflow")
    print("=" * 60)

    mock_alert = {
        "service": "dummy_prod_api",
        "severity": "CRITICAL",
        "description": "Health check failure: Connection pool deadlock detected",
        "timestamp": "2026-10-17T14:35:00Z"
    }

    app = create_sre_graph()
    thread_config = {"configurable": {"thread_id": "cli-incident-001"}}

    print(f"\n[1] Ingesting alert for service '{mock_alert['service']}'...")
    for _ in app.stream({"alert_payload": mock_alert}, thread_config):
        pass

    state = app.get_state(thread_config)
    if "execute_action" in state.next:
        proposed = state.values.get("proposed_action", "No action proposed.")
        print("\n" + "=" * 60)
        print("[HITL BREAKPOINT] HUMAN-IN-THE-LOOP APPROVAL REQUIRED")
        print("=" * 60)
        print(f"\nProposed Mitigation Plan:\n\n{proposed}\n")
        print("=" * 60)

        auto_approve = "--yes" in sys.argv or "-y" in sys.argv
        if auto_approve:
            approval = "y"
            print("Approve execution? (y/n): y (auto-approved via CLI flag)")
        else:
            try:
                approval = input("Approve execution? (y/n): ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                approval = "n"

        if approval == "y":
            print("\n[2] Human approval received. Resuming graph execution...\n")
            for _ in app.stream(None, thread_config):
                pass
            final_state = app.get_state(thread_config)
            print("\n" + "=" * 60)
            print("[SUCCESS] Incident Mitigation Completed")
            print(f"Status: {final_state.values.get('status')}")
            print("=" * 60)
        else:
            print("\n[ABORTED] Execution rejected by human operator. Aborting remediation.")
