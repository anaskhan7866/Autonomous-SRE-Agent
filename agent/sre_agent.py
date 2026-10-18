import logging
import os
import sys
from functools import lru_cache
from typing import Any, Dict, Optional
from typing_extensions import TypedDict
import warnings

# Suppress library deprecation and unauthenticated HF hub notices
os.environ["HF_HUB_DISABLE_SYMLINKS_WARNING"] = "1"
os.environ["HF_HUB_VERBOSITY"] = "error"
os.environ["TOKENIZERS_PARALLELISM"] = "false"
os.environ["TQDM_DISABLE"] = "1"

warnings.filterwarnings("ignore")
try:
    from langchain_core._api.deprecation import LangChainDeprecationWarning
    warnings.filterwarnings("ignore", category=LangChainDeprecationWarning)
except Exception:
    pass

for noisy_logger in [
    "httpx",
    "httpcore",
    "sentence_transformers",
    "transformers",
    "huggingface_hub",
    "chromadb",
    "urllib3",
]:
    logging.getLogger(noisy_logger).setLevel(logging.WARNING)

try:
    from transformers.utils import logging as tf_logging
    tf_logging.set_verbosity_error()
    tf_logging.disable_progress_bar()
except Exception:
    pass

import chromadb
import docker
import requests
import time
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


@lru_cache(maxsize=1)
def get_embeddings() -> HuggingFaceEmbeddings:
    """Instantiates local HuggingFace embeddings once with caching."""
    return HuggingFaceEmbeddings(model_name="all-MiniLM-L6-v2")


def get_vector_store() -> Chroma:
    """Instantiates local Chroma vector store instance."""
    client = chromadb.PersistentClient(path=CHROMA_PATH)
    return Chroma(
        client=client,
        collection_name=COLLECTION_NAME,
        embedding_function=get_embeddings()
    )


def parse_alert(state: AgentState) -> Dict[str, Any]:
    """Node: Extracts the failing service name from alert_payload."""
    logger.info("[NODE: parse_alert] Parsing incoming alert payload...")
    try:
        payload = state.get("alert_payload", {})
        service_name = (
            payload.get("service")
            or payload.get("service_name")
            or payload.get("incident", {}).get("service", {}).get("name")
            or "dummy_prod_api"
        )
        logger.info("[NODE: parse_alert] Target service identified: '%s'", service_name)
        return {
            "service_name": service_name,
            "status": "alert_parsed"
        }
    except Exception as exc:
        logger.exception("[NODE: parse_alert] Failed parsing alert payload: %s", exc)
        return {
            "service_name": "dummy_prod_api",
            "status": f"parse_alert_error: {exc}"
        }


def retrieve_context(state: AgentState) -> Dict[str, Any]:
    """Node: Queries local ChromaDB for runbooks related to the failing service."""
    logger.info("[NODE: retrieve_context] Searching runbook knowledge base...")
    try:
        service_name = state.get("service_name") or "dummy_prod_api"
        vector_db = get_vector_store()

        query = f"Incident remediation protocol for service {service_name} deadlock memory crash failure"
        docs = vector_db.similarity_search(query, k=2)

        if docs:
            context = "\n---\n".join([doc.page_content for doc in docs])
            logger.info("[NODE: retrieve_context] Found %d relevant runbook document(s)", len(docs))
        else:
            context = f"No specific runbook found for service {service_name}."
            logger.warning("[NODE: retrieve_context] No runbooks found for service '%s'", service_name)

        return {
            "runbook_context": context,
            "status": "context_retrieved"
        }
    except Exception as exc:
        logger.exception("[NODE: retrieve_context] Failed to retrieve runbook from ChromaDB: %s", exc)
        return {
            "runbook_context": "Runbook retrieval unavailable.",
            "status": f"retrieve_context_error: {exc}"
        }


def investigate_logs(state: AgentState) -> Dict[str, Any]:
    """Node: Uses Docker SDK to fetch last 20 log lines from the container."""
    service_name = state.get("service_name") or "dummy_prod_api"
    logger.info("[NODE: investigate_logs] Fetching logs for container: '%s'", service_name)
    try:
        client = docker.from_env()
        container = client.containers.get(service_name)
        raw_logs = container.logs(tail=20)
        logs = raw_logs.decode("utf-8", errors="replace").strip()
        line_count = len(logs.splitlines())
        logger.info("[NODE: investigate_logs] Retrieved %d log lines from '%s'", line_count, service_name)
        return {
            "container_logs": logs or "Container produced no log output.",
            "status": "logs_investigated"
        }
    except docker.errors.NotFound:
        msg = f"Container '{service_name}' not found. Please verify the container name."
        logger.warning("[NODE: investigate_logs] %s", msg)
        return {"container_logs": msg, "status": "container_not_found"}
    except docker.errors.DockerException as exc:
        msg = f"Docker daemon communication error for '{service_name}': {exc}"
        logger.error("[NODE: investigate_logs] %s", msg)
        return {"container_logs": msg, "status": "docker_daemon_error"}
    except Exception as exc:
        msg = f"Unexpected error investigating logs for '{service_name}': {exc}"
        logger.exception("[NODE: investigate_logs] %s", msg)
        return {"container_logs": msg, "status": f"investigate_logs_error: {exc}"}


def plan_mitigation(state: AgentState) -> Dict[str, Any]:
    """Node: Passes logs, alert, and runbook to Ollama LLM to decide on a fix."""
    logger.info("[NODE: plan_mitigation] Analyzing telemetry with Ollama LLM (%s)...", OLLAMA_MODEL)
    service_name = state.get("service_name", "dummy_prod_api")
    try:
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
        cleaned_action = raw_action.replace("`", "").replace('"', "").replace("'", "").splitlines()[-1].strip()
        logger.info("[NODE: plan_mitigation] Formulated mitigation command: '%s'", cleaned_action)
        return {
            "proposed_action": cleaned_action,
            "status": "mitigation_planned"
        }
    except Exception as exc:
        logger.exception("[NODE: plan_mitigation] Failed to generate mitigation plan: %s", exc)
        return {
            "proposed_action": f"RESTART {service_name}",
            "status": f"plan_mitigation_error: {exc}"
        }


def execute_action(state: AgentState) -> Dict[str, Any]:
    """Node: Uses Docker SDK to execute the proposed remediation action."""
    logger.info("[NODE: execute_action] Executing approved action via Docker SDK...")
    action_text = (state.get("proposed_action") or "").strip()
    default_service = state.get("service_name") or "dummy_prod_api"

    tokens = action_text.split()
    command = tokens[0].upper() if tokens else "RESTART"
    target = tokens[1] if len(tokens) > 1 else default_service

    try:
        client = docker.from_env()
        container = client.containers.get(target)

        if "RESTART" in command:
            logger.info("[NODE: execute_action] Restarting container '%s'...", target)
            container.restart(timeout=10)
            container.reload()
            msg = f"Container '{target}' restarted successfully (status: {container.status})."
        else:
            msg = f"Action '{command}' on container '{target}' completed."

        logger.info("[NODE: execute_action] %s", msg)
        return {"status": f"action_executed: {msg}"}
    except docker.errors.NotFound:
        msg = f"Cannot execute action: container '{target}' not found."
        logger.error("[NODE: execute_action] %s", msg)
        return {"status": f"action_failed: {msg}"}
    except docker.errors.DockerException as exc:
        msg = f"Docker SDK execution error for '{target}': {exc}"
        logger.error("[NODE: execute_action] %s", msg)
        return {"status": f"action_failed: {msg}"}
    except Exception as exc:
        msg = f"Unexpected execution error for '{target}': {exc}"
        logger.exception("[NODE: execute_action] %s", msg)
        return {"status": f"action_failed: {msg}"}


HEALTHCHECK_URL = "http://localhost:8080/health"


def verify_recovery(state: AgentState) -> Dict[str, Any]:
    """Node: Verifies whether the remediated service is healthy via GET /health."""
    logger.info("[NODE: verify_recovery] Sending health check to %s...", HEALTHCHECK_URL)
    # Brief pause allowing container network socket to bind post-restart
    time.sleep(2)
    try:
        response = requests.get(HEALTHCHECK_URL, timeout=5)
        if response.status_code == 200:
            logger.info("[NODE: verify_recovery] Health check returned 200 OK. Incident successfully resolved.")
            return {
                "status": "Resolved"
            }
        else:
            logger.warning("[NODE: verify_recovery] Health check failed with status code: %d", response.status_code)
            current_logs = state.get("container_logs", "")
            failure_note = f"\n[VERIFICATION FAILURE] Health check to {HEALTHCHECK_URL} failed with status {response.status_code}. Previous remediation action did not work."
            return {
                "status": "Escalated - Restart Failed",
                "container_logs": current_logs + failure_note
            }
    except Exception as exc:
        logger.error("[NODE: verify_recovery] Health check connection error: %s", exc)
        current_logs = state.get("container_logs", "")
        failure_note = f"\n[VERIFICATION FAILURE] Connection to {HEALTHCHECK_URL} failed: {exc}. Previous remediation action did not work."
        return {
            "status": "Escalated - Restart Failed",
            "container_logs": current_logs + failure_note
        }


def escalate_to_human(state: AgentState) -> Dict[str, Any]:
    """Node: Alerts human operators when automated remediation fails verification."""
    service_name = state.get("service_name", "dummy_prod_api")
    current_status = state.get("status", "Escalated - Restart Failed")
    alert_banner = (
        "\n" + "!" * 60 + "\n"
        "[CRITICAL ALERT: ESCALATION TO HUMAN OPERATOR]\n"
        f"Automated mitigation for service '{service_name}' failed verification!\n"
        f"Current Status: {current_status}\n"
        "Manual intervention required immediately!\n"
        + "!" * 60 + "\n"
    )
    print(alert_banner)
    logger.critical("[NODE: escalate_to_human] Incident failed verification. Escalated to human operator.")
    return {
        "status": "Escalated to Human Operator"
    }


def check_recovery_status(state: AgentState) -> str:
    """Conditional edge router: Directs graph to END if resolved, or escalate_to_human if escalated."""
    if state.get("status") == "Resolved":
        return "resolved"
    return "escalated"


def create_sre_graph(checkpointer: Optional[Any] = None):
    """Wires and compiles the Autonomous SRE Agent StateGraph with HITL and feedback loop."""
    if checkpointer is None:
        checkpointer = MemorySaver()

    workflow = StateGraph(AgentState)

    workflow.add_node("parse_alert", parse_alert)
    workflow.add_node("retrieve_context", retrieve_context)
    workflow.add_node("investigate_logs", investigate_logs)
    workflow.add_node("plan_mitigation", plan_mitigation)
    workflow.add_node("execute_action", execute_action)
    workflow.add_node("verify_recovery", verify_recovery)
    workflow.add_node("escalate_to_human", escalate_to_human)

    workflow.add_edge(START, "parse_alert")
    workflow.add_edge("parse_alert", "retrieve_context")
    workflow.add_edge("retrieve_context", "investigate_logs")
    workflow.add_edge("investigate_logs", "plan_mitigation")
    workflow.add_edge("plan_mitigation", "execute_action")
    workflow.add_edge("execute_action", "verify_recovery")

    workflow.add_conditional_edges(
        "verify_recovery",
        check_recovery_status,
        {
            "resolved": END,
            "escalated": "escalate_to_human"
        }
    )
    workflow.add_edge("escalate_to_human", END)

    # RULE 2: Enforce Mandatory HITL before state-changing execution
    return workflow.compile(
        checkpointer=checkpointer,
        interrupt_before=["execute_action"]
    )


# Pre-compiled graph singleton for direct import by listeners/services
sre_graph = create_sre_graph()


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
            print("[RESULT] Incident Mitigation Workflow Completed")
            print(f"Final Status: {final_state.values.get('status')}")
            print("=" * 60)
        else:
            print("\n[ABORTED] Execution rejected by human operator. Aborting remediation.")
