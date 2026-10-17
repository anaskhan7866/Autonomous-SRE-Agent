import logging
import os
import sys
import uuid
from typing import Any, Dict

# Ensure project root is in sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi import BackgroundTasks, FastAPI, status
import uvicorn

from agent.sre_agent import create_sre_graph

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [%(name)s] %(message)s"
)
logger = logging.getLogger("webhook_listener")

app = FastAPI(title="Autonomous SRE Agent Webhook Listener", version="1.0.0")


def run_agent_workflow(alert_payload: Dict[str, Any]):
    """Background task: Executes SRE agent triage graph up to HITL breakpoint."""
    thread_id = str(uuid.uuid4())
    logger.info("Starting background agent triage for alert: %s (thread: %s)", alert_payload, thread_id)
    try:
        sre_graph = create_sre_graph()
        thread_config = {"configurable": {"thread_id": thread_id}}

        # Stream graph until interrupt_before=['execute_action']
        for _ in sre_graph.stream({"alert_payload": alert_payload}, thread_config):
            pass

        state = sre_graph.get_state(thread_config)
        if "execute_action" in state.next:
            proposed = state.values.get("proposed_action", "No action proposed")
            logger.info("HITL Breakpoint reached! Proposed action: '%s'. Waiting for Slack approval.", proposed)
        else:
            logger.info("Agent workflow finished without requiring HITL intervention.")
    except Exception as exc:
        logger.exception("Failed executing background agent triage: %s", exc)


@app.post("/alert", status_code=status.HTTP_202_ACCEPTED)
async def receive_alert(alert: Dict[str, Any], background_tasks: BackgroundTasks):
    """Webhook endpoint receiving PagerDuty-style alert payloads."""
    try:
        service = alert.get("service") or alert.get("service_name") or "unknown_service"
        logger.info("Received incident webhook for service: '%s'", service)
        background_tasks.add_task(run_agent_workflow, alert)
        return {
            "status": "accepted",
            "message": "Incident alert received. SRE Agent triage initiated in background.",
            "service": service
        }
    except Exception as exc:
        logger.exception("Failed to accept alert webhook: %s", exc)
        return {
            "status": "error",
            "message": f"Failed to process alert: {exc}"
        }


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=5000)
