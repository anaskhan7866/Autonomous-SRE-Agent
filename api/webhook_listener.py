import logging
import os
import sys
import uuid
import warnings
from typing import Any, Dict

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

# Ensure project root is in sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import requests
from fastapi import BackgroundTasks, FastAPI, status
from fastapi.responses import HTMLResponse
import uvicorn

from agent.sre_agent import sre_graph

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [%(name)s] %(message)s"
)
logger = logging.getLogger("webhook_listener")

app = FastAPI(title="Autonomous SRE Agent Webhook Listener", version="1.0.0")


def notify_human_operator(thread_id: str, service: str, proposed_action: str):
    """Sends HITL approval notification with clickable action links to Discord/Slack webhook."""
    base_url = os.getenv("BASE_URL", "http://localhost:5000").rstrip("/")
    webhook_url = os.getenv("DISCORD_WEBHOOK_URL") or os.getenv("SLACK_WEBHOOK_URL")

    approve_url = f"{base_url}/approve/{thread_id}"
    reject_url = f"{base_url}/reject/{thread_id}"

    message = (
        f"[SRE HITL APPROVAL REQUIRED]\n"
        f"Failing Service: {service}\n"
        f"Proposed Action: {proposed_action}\n"
        f"Thread ID: {thread_id}\n\n"
        f"Approve: {approve_url}\n"
        f"Reject: {reject_url}"
    )

    logger.info(
        "[HITL NOTIFICATION] Approval requested for '%s' (thread: %s)\nAction: %s\nApprove: %s\nReject: %s",
        service, thread_id, proposed_action, approve_url, reject_url
    )

    if not webhook_url:
        logger.warning("[HITL NOTIFICATION] DISCORD_WEBHOOK_URL not configured. Notification recorded locally.")
        return

    payload = {
        "content": (
            f"🚨 **[SRE HITL APPROVAL REQUIRED]**\n"
            f"• **Failing Service:** `{service}`\n"
            f"• **Proposed Action:** `{proposed_action}`\n"
            f"• **Thread ID:** `{thread_id}`\n\n"
            f"👉 [Approve Mitigation]({approve_url}) | ❌ [Reject Mitigation]({reject_url})"
        ),
        "text": message
    }

    try:
        response = requests.post(webhook_url, json=payload, timeout=5)
        logger.info("[HITL NOTIFICATION] Webhook notification dispatched (status: %d)", response.status_code)
    except requests.exceptions.RequestException as exc:
        logger.error("[HITL NOTIFICATION] Failed to dispatch webhook notification: %s", exc)


def run_agent_workflow(alert_payload: Dict[str, Any], thread_id: str):
    """Background task: Executes SRE agent triage graph up to HITL breakpoint."""
    logger.info(
        "[WORKFLOW] Initiating background SRE agent triage for alert: %s (thread: %s)",
        alert_payload,
        thread_id
    )
    try:
        thread_config = {"configurable": {"thread_id": thread_id}}

        # Stream graph until interrupt_before=['execute_action']
        for _ in sre_graph.stream({"alert_payload": alert_payload}, thread_config):
            pass

        state = sre_graph.get_state(thread_config)
        if "execute_action" in state.next:
            proposed = state.values.get("proposed_action", "No action proposed")
            service = state.values.get("service_name") or alert_payload.get("service") or "dummy_prod_api"
            logger.info(
                "[HITL BREAKPOINT] Interrupted before execute_action. Proposed action: '%s'. Awaiting remote approval.",
                proposed
            )
            notify_human_operator(thread_id, service, proposed)
        else:
            logger.info("[WORKFLOW] Agent workflow concluded without requiring HITL intervention.")
    except Exception as exc:
        logger.exception("[WORKFLOW] Error executing background agent triage for thread %s: %s", thread_id, exc)


def resume_agent_workflow(thread_id: str):
    """Background task: Resumes SRE agent workflow execution after human operator approval."""
    logger.info("[RESUME] Human approval received for thread: %s. Resuming execution...", thread_id)
    try:
        thread_config = {"configurable": {"thread_id": thread_id}}
        for _ in sre_graph.stream(None, thread_config):
            pass

        final_state = sre_graph.get_state(thread_config)
        status_val = final_state.values.get("status", "Unknown")
        logger.info("[RESUME] Workflow execution finished for thread: %s. Final status: %s", thread_id, status_val)
    except Exception as exc:
        logger.exception("[RESUME] Error resuming agent workflow for thread %s: %s", thread_id, exc)


@app.post("/alert", status_code=status.HTTP_202_ACCEPTED)
async def receive_alert(alert: Dict[str, Any], background_tasks: BackgroundTasks):
    """Webhook endpoint receiving PagerDuty-style alert payloads."""
    try:
        service = alert.get("service") or alert.get("service_name") or "unknown_service"
        thread_id = str(uuid.uuid4())
        logger.info("[WEBHOOK] Received incident webhook for service: '%s' (thread: %s)", service, thread_id)
        background_tasks.add_task(run_agent_workflow, alert, thread_id)
        return {
            "status": "accepted",
            "message": "Incident alert received. SRE Agent triage initiated in background.",
            "service": service,
            "thread_id": thread_id
        }
    except Exception as exc:
        logger.exception("[WEBHOOK] Failed to accept alert webhook: %s", exc)
        return {
            "status": "error",
            "message": f"Failed to process alert: {exc}"
        }


@app.get("/approve/{thread_id}", response_class=HTMLResponse)
async def approve_incident(thread_id: str, background_tasks: BackgroundTasks):
    """Remote HITL approval endpoint triggered via Discord/Slack link."""
    logger.info("[HITL APPROVAL] Remote approval received for thread: %s", thread_id)
    background_tasks.add_task(resume_agent_workflow, thread_id)
    html_content = f"""<!DOCTYPE html>
<html>
<head>
    <title>SRE Mitigation Approved</title>
    <style>
        body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; background: #0f172a; color: #f8fafc; display: flex; justify-content: center; align-items: center; height: 100vh; margin: 0; }}
        .card {{ background: #1e293b; border: 1px solid #334155; padding: 40px; border-radius: 12px; box-shadow: 0 10px 25px rgba(0,0,0,0.5); text-align: center; max-width: 480px; }}
        .badge {{ display: inline-block; background: #16a34a; color: white; padding: 6px 14px; border-radius: 20px; font-weight: bold; font-size: 14px; margin-bottom: 16px; }}
        h1 {{ font-size: 24px; margin: 0 0 12px 0; }}
        p {{ color: #94a3b8; font-size: 15px; line-height: 1.5; }}
        code {{ background: #0f172a; color: #38bdf8; padding: 3px 8px; border-radius: 4px; font-size: 13px; }}
    </style>
</head>
<body>
    <div class="card">
        <div class="badge">APPROVED</div>
        <h1>Mitigation In Progress</h1>
        <p>The Autonomous SRE Agent has been authorized to execute the proposed remediation plan.</p>
        <p>Thread ID: <code>{thread_id}</code></p>
    </div>
</body>
</html>"""
    return HTMLResponse(content=html_content)


@app.get("/reject/{thread_id}", response_class=HTMLResponse)
async def reject_incident(thread_id: str):
    """Remote HITL rejection endpoint triggered via Discord/Slack link."""
    logger.warning("[HITL REJECTION] Remote rejection received for thread: %s. State machine permanently paused.", thread_id)
    html_content = f"""<!DOCTYPE html>
<html>
<head>
    <title>SRE Mitigation Rejected</title>
    <style>
        body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; background: #0f172a; color: #f8fafc; display: flex; justify-content: center; align-items: center; height: 100vh; margin: 0; }}
        .card {{ background: #1e293b; border: 1px solid #334155; padding: 40px; border-radius: 12px; box-shadow: 0 10px 25px rgba(0,0,0,0.5); text-align: center; max-width: 480px; }}
        .badge {{ display: inline-block; background: #dc2626; color: white; padding: 6px 14px; border-radius: 20px; font-weight: bold; font-size: 14px; margin-bottom: 16px; }}
        h1 {{ font-size: 24px; margin: 0 0 12px 0; }}
        p {{ color: #94a3b8; font-size: 15px; line-height: 1.5; }}
        code {{ background: #0f172a; color: #f87171; padding: 3px 8px; border-radius: 4px; font-size: 13px; }}
    </style>
</head>
<body>
    <div class="card">
        <div class="badge">REJECTED</div>
        <h1>Mitigation Aborted</h1>
        <p>The proposed remediation was rejected by human operator. State machine left permanently paused.</p>
        <p>Thread ID: <code>{thread_id}</code></p>
    </div>
</body>
</html>"""
    return HTMLResponse(content=html_content)


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=5000)


