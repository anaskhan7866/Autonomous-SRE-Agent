import logging
import os
from typing import Any, Dict, Optional
from typing_extensions import TypedDict

import chromadb
import docker
from langchain_community.embeddings import HuggingFaceEmbeddings
from langchain_community.vectorstores import Chroma
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_ollama import ChatOllama

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
            f"You are an Autonomous SRE Agent.\n\n"
            f"TARGET SERVICE: {service_name}\n"
            f"ALERT DETAILS:\n{alert}\n\n"
            f"RECENT CONTAINER LOGS:\n{logs}\n\n"
            f"RELEVANT SRE RUNBOOK:\n{runbook}\n\n"
            f"Task:\n"
            f"1. Analyze the logs and alert against the authorized runbook.\n"
            f"2. Formulate a specific, authorized mitigation action (e.g. restart container '{service_name}').\n"
            f"3. State the exact proposed action and justification clearly.\n"
            f"Do not invent unauthorized actions outside the runbook protocol."
        )

        llm = get_llm()
        messages = [
            SystemMessage(content="You are an expert SRE responsible for diagnosing failures and proposing safe remediations."),
            HumanMessage(content=prompt)
        ]
        response = llm.invoke(messages)
        proposed_action = response.content.strip()
        logger.info("Mitigation plan formulated successfully.")
        return {
            "proposed_action": proposed_action,
            "status": "mitigation_planned"
        }
    except Exception as exc:
        logger.exception("Failed to generate mitigation plan: %s", exc)
        return {
            "proposed_action": "Unable to formulate plan due to model error.",
            "status": f"plan_mitigation_error: {exc}"
        }
