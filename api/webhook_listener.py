import html
import json
import logging
import os
import re
import secrets
import sys
import threading
import time
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, quote
import uuid
import warnings

from dotenv import load_dotenv
from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import HTMLResponse
import requests
import uvicorn

# Load environment variables at the top of the script
load_dotenv()

# Suppress library deprecation and unauthenticated HF hub notices
os.environ["HF_HUB_DISABLE_SYMLINKS_WARNING"] = "1"
os.environ["HF_HUB_VERBOSITY"] = "error"
os.environ["TOKENIZERS_PARALLELISM"] = "false"
os.environ["TQDM_DISABLE"] = "1"

warnings.filterwarnings("ignore")

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

from agent.sre_agent import _denied_entry, sre_graph

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [%(name)s] %(message)s"
)
logger = logging.getLogger("webhook_listener")

_TOKEN_QUERY_RE = re.compile(r"([?&]token=)[^&\s\"']*", re.IGNORECASE)


class _RedactTokenFilter(logging.Filter):
    """Redacts the `token` query value from uvicorn access-log records.

    uvicorn logs `'%s - "%s %s HTTP/%s" %d'` with args
    (client_addr, method, full_path, http_version, status_code); full_path carries the query string.
    """

    _sre_redact_token = True  # stable marker; survives module reloads (isinstance does not)

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple):
            record.args = tuple(
                _TOKEN_QUERY_RE.sub(r"\1[REDACTED]", a) if isinstance(a, str) else a
                for a in record.args
            )
        if isinstance(record.msg, str):
            record.msg = _TOKEN_QUERY_RE.sub(r"\1[REDACTED]", record.msg)
        return True


def _install_access_log_filter() -> None:
    """Idempotently attaches the redaction filter to the uvicorn.access logger."""
    access_logger = logging.getLogger("uvicorn.access")
    for old in [f for f in access_logger.filters if getattr(f, "_sre_redact_token", False)]:
        access_logger.removeFilter(old)
    access_logger.addFilter(_RedactTokenFilter())


_install_access_log_filter()

_MAX_FORM_BYTES = 4096
_MAX_ALERT_BYTES = 64 * 1024
_CSP_EXEMPT_PATHS = frozenset({"/docs", "/docs/oauth2-redirect", "/redoc", "/openapi.json"})
_NOTIFY_RETRY_DELAY_SECONDS = 2.0
_SECURITY_HEADERS = {
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": (
        "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; "
        "frame-ancestors 'none'; base-uri 'none'"
    ),
}

# Strictly read environment variables via os.getenv() without hardcoded fallbacks
BASE_URL = os.getenv("BASE_URL")
if not BASE_URL:
    raise ValueError("Missing required environment variable: BASE_URL")
BASE_URL = BASE_URL.strip().rstrip("/")

DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL")
if not DISCORD_WEBHOOK_URL:
    raise ValueError("Missing required environment variable: DISCORD_WEBHOOK_URL")

app = FastAPI(title="Autonomous SRE Agent Webhook Listener", version="1.0.0")

@app.middleware("http")
async def add_security_headers(request: Request, call_next: Any) -> Any:
    """Adds hardening headers to every response (confirm, result and error pages)."""
    response = await call_next(request)
    for name, value in _SECURITY_HEADERS.items():
        if name == "Content-Security-Policy" and request.url.path in _CSP_EXEMPT_PATHS:
            continue  # Swagger UI / ReDoc need inline scripts and CDN assets
        response.headers[name] = value
    return response


# Serialises the read-check-update "claim" of an approval decision.
_decision_lock = threading.Lock()


def _thread_config(thread_id: str) -> Dict[str, Any]:
    """Builds the LangGraph config for a thread."""
    return {"configurable": {"thread_id": thread_id}}


# ---------------------------------------------------------------------------
# HTML / message rendering (presentation only; every dynamic value is escaped)
# ---------------------------------------------------------------------------

_PAGE_CSS = """
:root { color-scheme: light dark; --bg:#f4f5f7; --card:#ffffff; --fg:#1a1d21; --muted:#4b5563;
  --border:#d1d5db; --code:#eef0f3; --accent:#374151; --on-accent:#ffffff; --btn2:#1a1d21; }
.approve, .approved { --accent:#166534; --on-accent:#ffffff; }
.reject, .rejected { --accent:#b91c1c; --on-accent:#ffffff; }
.error { --accent:#92400e; --on-accent:#ffffff; }
.info { --accent:#1d4ed8; --on-accent:#ffffff; }
@media (prefers-color-scheme: dark) {
  :root { --bg:#111418; --card:#1c2128; --fg:#e8eaed; --muted:#aab1bb; --border:#3a424d;
    --code:#0d1117; --btn2:#e8eaed; }
  .approve, .approved { --accent:#4ade80; --on-accent:#052e16; }
  .reject, .rejected { --accent:#f87171; --on-accent:#450a0a; }
  .error { --accent:#fbbf24; --on-accent:#3b2002; }
  .info { --accent:#93c5fd; --on-accent:#0b2447; }
}
* { box-sizing: border-box; }
body { margin:0; padding:16px; min-height:100vh; display:flex; align-items:center;
  justify-content:center; background:var(--bg); color:var(--fg);
  font:16px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif; }
main.card { width:100%; max-width:520px; background:var(--card); border:1px solid var(--border);
  border-top:6px solid var(--accent); border-radius:12px; padding:24px; }
h1 { font-size:1.4rem; line-height:1.25; margin:12px 0 16px; }
.badge { display:inline-block; padding:2px 12px; border-radius:999px; background:var(--accent);
  color:var(--on-accent); font-size:.8rem; font-weight:700; letter-spacing:.06em; }
dl { margin:0 0 16px; } dt { color:var(--muted); font-size:.85rem; margin-top:12px; }
dd { margin:2px 0 0; }
code, pre { font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace; font-size:.9rem;
  background:var(--code); border:1px solid var(--border); border-radius:6px; }
code { padding:1px 6px; overflow-wrap:anywhere; }
pre { margin:2px 0 0; padding:10px 12px; white-space:pre-wrap; overflow-wrap:anywhere; }
.note { color:var(--muted); font-size:.9rem; }
.btn { display:block; width:100%; min-height:48px; padding:12px 16px; border-radius:8px;
  font:inherit; font-weight:700; text-align:center; text-decoration:none; cursor:pointer; }
.btn-primary { border:2px solid var(--accent); background:var(--accent); color:var(--on-accent); }
.btn-secondary { margin-top:12px; border:2px solid var(--border); background:transparent;
  color:var(--btn2); }
.btn:focus-visible { outline:3px solid var(--fg); outline-offset:2px; }
"""


def _page_shell(title: str, body_class: str, body: str) -> str:
    """Wraps an already-escaped body fragment in the shared, self-contained page."""
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex">
<title>{html.escape(title)}</title>
<style>{_PAGE_CSS}</style>
</head>
<body>
<main class="card {html.escape(body_class)}">
{body}
</main>
</body>
</html>"""


def render_confirm_page(action: str, thread_id: str, token: str, service: str, proposed_action: str) -> str:
    """Confirmation page with a form that POSTs the decision. action: 'approve' | 'reject'."""
    e_action = html.escape(action)
    form_action = html.escape(f"/{action}/{quote(thread_id, safe='')}")
    label = "Confirm approval" if action == "approve" else "Confirm rejection"
    body = f"""<span class="badge">{e_action.upper()}</span>
<h1>Confirm {e_action} of this fix?</h1>
<dl>
<dt>Failing service</dt><dd><code>{html.escape(service)}</code></dd>
<dt>Proposed action</dt><dd><pre>{html.escape(proposed_action)}</pre></dd>
<dt>Thread ID</dt><dd><code>{html.escape(thread_id)}</code></dd>
</dl>
<p class="note">Nothing runs until you press the button below.</p>
<form method="post" action="{form_action}">
<input type="hidden" name="token" value="{html.escape(token)}">
<button type="submit" class="btn btn-primary">{label}</button>
</form>
<p class="note">To cancel, just close this tab. Nothing runs unless you press the button above.</p>"""
    return _page_shell(f"Confirm {action}", action, body)


def render_result_page(kind: str, title: str, message: str, thread_id: str) -> str:
    """Result/info page. kind: 'approved' | 'rejected' | 'error' | 'info'."""
    body = f"""<span class="badge">{html.escape(kind.upper())}</span>
<h1>{html.escape(title)}</h1>
<p>{html.escape(message)}</p>
<p class="note">Thread ID: <code>{html.escape(thread_id)}</code></p>"""
    return _page_shell(title, kind, body)


def _clip(text: str, limit: int) -> str:
    """Truncates text for Discord and neutralises backticks that would break inline code."""
    text = "".join(" " if (ch.isspace() or not ch.isprintable()) and ch != " " else ch for ch in text)
    text = text.replace("`", "'")
    return text if len(text) <= limit else text[: limit - 3] + "..."


def build_discord_content(
    thread_id: str,
    service: str,
    proposed_action: str,
    approve_url: str,
    reject_url: str,
    policy_reason: Optional[str] = None,
) -> str:
    """Builds the Discord message text (< 2000 chars; <url> wrapping suppresses link previews)."""
    marker = "… (truncated, full action on the confirmation page)"
    action_limit = 500
    action = _clip(proposed_action, 10**9)
    if len(action) > action_limit:
        action = action[: action_limit - len(marker)] + marker
    policy_line = (
        f"🔎 **Static allowlist:** passed · **Policy judge (advisory):** no objection — "
        f"`{_clip(policy_reason, 200)}`\n"
        f"A human must still review the command before approving.\n"
        if policy_reason
        else ""
    )
    return (
        f"🚨 **[SRE HITL APPROVAL REQUIRED]**\n"
        f"• **Failing Service:** `{_clip(service, 100)}`\n"
        f"• **Proposed Action:** `{action}`\n"
        f"{policy_line}"
        f"• **Thread ID:** `{_clip(thread_id, 100)}`\n\n"
        f"Links open a confirmation page; nothing runs until you press the button there.\n"
        f"👉 [Review & Approve](<{approve_url}>) | ❌ [Review & Reject](<{reject_url}>)"
    )


def build_escalation_content(
    service: str,
    thread_id: str,
    status: str,
    denied_actions: List[Any],
    proposed_action: str,
) -> str:
    """Builds the escalation Discord text: no approval link, nothing runs (< 2000 chars)."""
    svc, tid = _clip(service, 100), _clip(thread_id, 100)
    last = f"• **Last proposed action:** `{_clip(proposed_action, 300)}`"
    if status == "Escalated - Policy Denied":
        lines = [
            "⛔ **SRE agent needs a human: no policy-compliant fix found**",
            f"• **Service:** `{svc}`",
            f"• **Thread:** `{tid}`",
            "",
            "**Commands proposed and rejected by company policy:**",
        ]
        for index, entry in enumerate(denied_actions[:5], 1):
            command, reason = _denied_entry(entry)
            lines.append(f"{index}. `{_clip(command, 120)}`")
            if reason:
                label = "Blocked by rule" if reason.lower().startswith("hard block") else "Policy judge (advisory)"
                lines.append(f"   {label}: `{_clip(reason, 160)}`")
        lines += ["", "No action was taken. Please investigate manually."]
    elif status == "Escalated to Human Operator":
        lines = [
            "🚨 **SRE agent needs a human: remediation failed after 3 attempts**",
            f"• **Service:** `{svc}`",
            f"• **Thread:** `{tid}`",
        ]
        if proposed_action:
            lines.append(last)
        lines += ["", "Verification still failing. Please investigate manually."]
    else:
        lines = [
            "🛑 **SRE agent needs a human**",
            f"• **Service:** `{svc}`",
            f"• **Thread:** `{tid}`",
            f"• **Status:** `{_clip(status, 100)}`",
        ]
        if proposed_action:
            lines.append(last)
        lines += ["", "No action was taken. Please investigate manually."]
    text = "\n".join(lines)
    return text if len(text) <= 1990 else text[:1987] + "..."


def _page(kind: str, title: str, message: str, thread_id: str, code: int) -> HTMLResponse:
    return HTMLResponse(content=render_result_page(kind, title, message, thread_id), status_code=code)


# ---------------------------------------------------------------------------
# Discord notification and approval-token lifecycle
# ---------------------------------------------------------------------------

def _post_discord(content: str, tag: str) -> bool:
    """Posts a message to the Discord webhook (no mentions). Returns True only on a 2xx response."""
    payload = {"content": content, "allowed_mentions": {"parse": []}}
    try:
        response = requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=5)
    except requests.exceptions.RequestException as exc:
        logger.error("[%s] Failed to dispatch Discord webhook: %s", tag, type(exc).__name__)
        return False
    if not 200 <= response.status_code < 300:
        logger.error("[%s] Discord rejected the webhook (status: %d)", tag, response.status_code)
        return False
    logger.info("[%s] Webhook notification dispatched to Discord (status: %d)", tag, response.status_code)
    return True


def notify_discord(
    thread_id: str, service: str, proposed_action: str, token: str, policy_reason: Optional[str] = None
) -> bool:
    """Sends the HITL approval message to Discord. Returns True only on a 2xx response."""
    quoted = quote(thread_id, safe="")
    approve_url = f"{BASE_URL}/approve/{quoted}?token={token}"
    reject_url = f"{BASE_URL}/reject/{quoted}?token={token}"
    # Never log the token-bearing URLs or the webhook URL.
    logger.info(
        "[HITL NOTIFICATION] Approval requested for %r (thread: %r). Action: %r",
        service, thread_id, proposed_action
    )
    content = build_discord_content(
        thread_id, service, proposed_action, approve_url, reject_url, policy_reason=policy_reason
    )
    return _post_discord(content, "HITL NOTIFICATION")


def notify_escalation(thread_id: str, service: str, reason_kind: str, details: Dict[str, Any]) -> bool:
    """Sends an escalation message (NO approval link or token); retries once. Returns final success.

    reason_kind: 'policy_denied' | 'verification_failed'. details: status, denied_actions, proposed_action.
    """
    content = build_escalation_content(
        service,
        thread_id,
        str(details.get("status") or reason_kind),
        list(details.get("denied_actions") or []),
        str(details.get("proposed_action") or ""),
    )
    logger.warning("[ESCALATION] Notifying operators for thread %r (%s)", thread_id, reason_kind)
    if _post_discord(content, "ESCALATION"):
        return True
    time.sleep(_NOTIFY_RETRY_DELAY_SECONDS)
    return _post_discord(content, "ESCALATION")


def _notify_with_retry(thread_id: str, service: str, proposed: str, token: str, policy_reason: str = "") -> bool:
    """Notifies Discord; on failure retries once after a short delay. Returns final success."""
    extra = {"policy_reason": policy_reason} if policy_reason else {}
    if notify_discord(thread_id, service, proposed, token, **extra):
        return True
    logger.warning("[HITL NOTIFICATION] Retrying Discord notification for thread %r", thread_id)
    time.sleep(_NOTIFY_RETRY_DELAY_SECONDS)
    return notify_discord(thread_id, service, proposed, token, **extra)


def _escalate_unapproved(thread_id: str, values: Dict[str, Any], fallback_service: str) -> None:
    """A paused thread whose action never passed the policy gate: no token, no approval link; escalate."""
    verdict = values.get("policy_verdict")
    logger.warning(
        "[HITL BREAKPOINT] Thread %r paused with policy_verdict %r; not issuing an approval token.", thread_id, verdict
    )
    try:
        details = {
            "status": f"Policy verdict is {verdict!r}; approval not offered",
            "denied_actions": values.get("denied_actions") or [],
            "proposed_action": values.get("proposed_action") or "",
        }
        notify_escalation(thread_id, values.get("service_name") or fallback_service, "policy_denied", details)
    except Exception as exc:
        logger.exception("[HITL BREAKPOINT] Could not escalate unapproved thread %r: %s", thread_id, exc)


def pause_and_notify(thread_id: str, fallback_service: str = "dummy_prod_api") -> bool:
    """If paused before execute_action: issue a fresh one-time token, mark pending, notify Discord."""
    config = _thread_config(thread_id)
    state = sre_graph.get_state(config)
    if "execute_action" not in state.next:
        return False
    if state.values.get("policy_verdict") != "allowed":
        _escalate_unapproved(thread_id, state.values, fallback_service)
        return False
    token = secrets.token_urlsafe(32)
    sre_graph.update_state(config, {"approval_token": token, "hitl_decision": "pending"})
    if "execute_action" not in sre_graph.get_state(config).next:
        logger.error("[HITL BREAKPOINT] Graph left the execute_action pause after token update (thread: %r)", thread_id)
        return False
    proposed = state.values.get("proposed_action", "No action proposed")
    service = state.values.get("service_name") or fallback_service
    logger.info("[HITL BREAKPOINT] Interrupted before execute_action. Proposed action: %r. Awaiting approval.", proposed)
    policy_reason = state.values.get("policy_reason") or ""
    if not _notify_with_retry(thread_id, service, proposed, token, policy_reason):
        logger.error(
            "[HITL BREAKPOINT] Operator NOT notified for thread %r. Incident is paused awaiting approval; "
            "re-trigger notification or approve via the CLI.", thread_id
        )
    return True


_ESCALATION_KINDS = {
    "Escalated - Policy Denied": "policy_denied",
    "Escalated to Human Operator": "verification_failed",
}


def notify_if_escalated(thread_id: str, fallback_service: str = "dummy_prod_api") -> bool:
    """If the graph ended in an escalation state, sends an approval-free Discord escalation."""
    try:
        values = sre_graph.get_state(_thread_config(thread_id)).values or {}
        status_val = values.get("status")
        kind = _ESCALATION_KINDS.get(status_val)
        if kind is None:
            return False
        details = {
            "status": status_val,
            "denied_actions": values.get("denied_actions") or [],
            "proposed_action": values.get("proposed_action") or "",
        }
        service = values.get("service_name") or fallback_service
        if not notify_escalation(thread_id, service, kind, details):
            logger.error("[ESCALATION] Operator NOT notified for thread %r.", thread_id)
        return True
    except Exception as exc:
        logger.exception("[ESCALATION] Could not send escalation for thread %r: %s", thread_id, exc)
        return False


def run_agent_workflow(alert_payload: Dict[str, Any], thread_id: str) -> None:
    """Background task: Executes SRE agent triage graph up to HITL breakpoint."""
    logger.info(
        "[WORKFLOW] Initiating background SRE agent triage for alert: %.500r (thread: %r)",
        alert_payload,
        thread_id
    )
    try:
        # Stream graph until interrupt_before=['execute_action']
        for _ in sre_graph.stream({"alert_payload": alert_payload}, _thread_config(thread_id)):
            pass
        fallback = alert_payload.get("service") or "dummy_prod_api"
        if not pause_and_notify(thread_id, fallback) and not notify_if_escalated(thread_id, fallback):
            logger.info("[WORKFLOW] Agent workflow concluded without requiring HITL intervention.")
    except Exception as exc:
        logger.exception("[WORKFLOW] Error executing background agent triage for thread %r: %s", thread_id, exc)


def resume_agent_workflow(thread_id: str) -> None:
    """Background task: Resumes the workflow after approval; re-notifies if paused again (retry)."""
    config = _thread_config(thread_id)
    try:
        values = sre_graph.get_state(config).values or {}
        decision = values.get("hitl_decision")
        verdict = values.get("policy_verdict")
    except Exception as exc:
        logger.exception("[RESUME] Could not read state for thread %r: %s", thread_id, exc)
        return
    if decision != "approved":
        logger.warning("[RESUME] Refusing to resume thread %r: hitl_decision is %r, not 'approved'.", thread_id, decision)
        return
    if verdict != "allowed":
        logger.warning(
            "[RESUME] Refusing to resume thread %r: policy_verdict is %r (paused before the policy gate existed?).",
            thread_id, verdict
        )
        return
    logger.info("[RESUME] Human approval received for thread: %r. Resuming execution...", thread_id)
    try:
        for _ in sre_graph.stream(None, config):
            pass
        if pause_and_notify(thread_id):
            logger.info("[RESUME] Graph paused again for thread: %r. Fresh approval requested.", thread_id)
            return
        notify_if_escalated(thread_id)
        status_val = sre_graph.get_state(config).values.get("status", "Unknown")
        logger.info("[RESUME] Workflow execution finished for thread: %r. Final status: %s", thread_id, status_val)
    except Exception as exc:
        logger.exception("[RESUME] Error resuming agent workflow for thread %r: %s", thread_id, exc)
        _renotify_after_crash(thread_id)


def _renotify_after_crash(thread_id: str) -> None:
    """If a crashed resume left the graph paused before execute_action, issue a fresh approval request."""
    try:
        if pause_and_notify(thread_id):
            logger.warning("[RESUME] Resume failed for thread %r; fresh approval requested.", thread_id)
    except Exception as exc:
        logger.exception("[RESUME] Could not re-notify after failed resume for thread %r: %s", thread_id, exc)


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

async def read_capped_body(request: Request, limit: int) -> bytes:
    """Reads the request body, enforcing `limit` via Content-Length (400/413) and while streaming (413)."""
    declared = request.headers.get("content-length")
    if declared is not None:
        if not (declared.isascii() and declared.isdigit()):
            raise HTTPException(status_code=400, detail="Invalid Content-Length")
        if int(declared) > limit:
            raise HTTPException(status_code=413, detail="Request body too large")
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > limit:
            raise HTTPException(status_code=413, detail="Request body too large")
    return bytes(body)


async def _read_alert_json(request: Request) -> Dict[str, Any]:
    """Reads and validates the alert body: size-capped, valid JSON, top-level object (else 400/413)."""
    raw = await read_capped_body(request, _MAX_ALERT_BYTES)
    try:
        alert = json.loads(raw)
    except (ValueError, RecursionError):
        raise HTTPException(status_code=400, detail="Invalid JSON body")
    if not isinstance(alert, dict):
        raise HTTPException(status_code=400, detail="Alert payload must be a JSON object")
    return alert


_SERVICE_NAME_RE = re.compile(r"[A-Za-z0-9_.-]{1,64}")


def _validate_service_fields(alert: Dict[str, Any]) -> None:
    """Rejects (400, generic error) a present `service`/`service_name` that is not a short safe name."""
    for key in ("service", "service_name"):
        if key in alert:
            value = alert[key]
            if not isinstance(value, str) or not _SERVICE_NAME_RE.fullmatch(value):
                raise HTTPException(status_code=400, detail="Invalid alert payload")


@app.post("/alert", status_code=status.HTTP_202_ACCEPTED)
async def receive_alert(request: Request, background_tasks: BackgroundTasks) -> Dict[str, Any]:
    """Webhook endpoint receiving PagerDuty-style alert payloads (max 64 KB)."""
    alert = await _read_alert_json(request)
    _validate_service_fields(alert)
    try:
        service = alert.get("service") or alert.get("service_name") or "unknown_service"
        thread_id = str(uuid.uuid4())
        logger.info("[WEBHOOK] Received incident webhook for service: %.100r (thread: %r)", service, thread_id)
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
            "message": "Failed to process alert."
        }


async def read_form_body(request: Request) -> Dict[str, str]:
    """Parses an application/x-www-form-urlencoded body without python-multipart."""
    content_type = request.headers.get("content-type", "").split(";")[0].strip().lower()
    if content_type and content_type != "application/x-www-form-urlencoded":
        raise HTTPException(status_code=415, detail="Unsupported media type")
    body = await read_capped_body(request, _MAX_FORM_BYTES)
    try:
        parsed = parse_qs(body.decode("utf-8", errors="replace"), max_num_fields=10)
    except ValueError:
        raise HTTPException(status_code=400, detail="Too many form fields")
    return {key: values[0] for key, values in parsed.items() if values}


def _token_valid(state: Any, token: Optional[str]) -> bool:
    """Constant-time comparison of the supplied token with the stored one."""
    stored = state.values.get("approval_token") if state.values else None
    if not token or not stored:
        return False
    return secrets.compare_digest(token.encode("utf-8"), stored.encode("utf-8"))


def _is_pending(state: Any) -> bool:
    return state.values.get("hitl_decision") == "pending" and "execute_action" in state.next


def _precheck(action: str, method: str, thread_id: str, state: Any, token: Optional[str]) -> Optional[HTMLResponse]:
    """Shared 404 / 403 / 409 checks. Returns an error page or None when the request may proceed."""
    if not state.values:
        return _page("error", "Not Found", "Unknown incident thread.", thread_id, 404)
    if not _token_valid(state, token):
        logger.warning("[HITL %s] Invalid or missing token on %s (thread: %r)", action.upper(), method, thread_id)
        return _page("error", "Forbidden", "Invalid or missing approval token.", thread_id, 403)
    if not _is_pending(state):
        return _page("info", "Already Decided", "Already decided / not awaiting approval.", thread_id, 409)
    if state.values.get("policy_verdict") != "allowed":
        logger.warning("[HITL %s] Refused on %s: policy_verdict is not 'allowed' (thread: %r)", action.upper(), method, thread_id)
        return _page("info", "Not Approvable", "This action has not passed the company policy check.", thread_id, 409)
    return None


def _show_confirmation(action: str, thread_id: str, token: Optional[str]) -> HTMLResponse:
    """GET handler body: renders the confirmation form only; never changes state."""
    state = sre_graph.get_state(_thread_config(thread_id))
    error = _precheck(action, "GET", thread_id, state, token)
    if error is not None:
        return error
    service = state.values.get("service_name") or "unknown_service"
    proposed = state.values.get("proposed_action", "No action proposed")
    return HTMLResponse(content=render_confirm_page(action, thread_id, token or "", service, proposed))


def _claim_decision(action: str, thread_id: str, token: Optional[str]) -> HTMLResponse:
    """POST handler body: atomically validates and records the first decision.

    Returns the response page; status 200 means the decision was recorded.
    """
    decision = "approved" if action == "approve" else "rejected"
    config = _thread_config(thread_id)
    with _decision_lock:
        state = sre_graph.get_state(config)
        error = _precheck(action, "POST", thread_id, state, token)
        if error is not None:
            return error
        sre_graph.update_state(config, {"hitl_decision": decision})
    if decision == "approved":
        return _page("approved", "Mitigation In Progress",
                     "The agent has been authorized to execute the proposed remediation.", thread_id, 200)
    return _page("rejected", "Mitigation Aborted",
                 "The proposed remediation was rejected. The workflow stays paused.", thread_id, 200)


@app.get("/approve/{thread_id}", response_class=HTMLResponse)
def approve_confirm(thread_id: str, token: Optional[str] = None) -> HTMLResponse:
    """GET: confirmation page only (safe for link-preview crawlers)."""
    return _show_confirmation("approve", thread_id, token)


@app.get("/reject/{thread_id}", response_class=HTMLResponse)
def reject_confirm(thread_id: str, token: Optional[str] = None) -> HTMLResponse:
    """GET: confirmation page only (safe for link-preview crawlers)."""
    return _show_confirmation("reject", thread_id, token)


@app.post("/approve/{thread_id}", response_class=HTMLResponse)
def approve_incident(
    thread_id: str, background_tasks: BackgroundTasks, form: Dict[str, str] = Depends(read_form_body)
) -> HTMLResponse:
    """POST: records the approval (first decision wins) and resumes the graph."""
    response = _claim_decision("approve", thread_id, form.get("token"))
    if response.status_code == 200:
        logger.info("[HITL APPROVAL] Approval accepted for thread: %r", thread_id)
        background_tasks.add_task(resume_agent_workflow, thread_id)
    return response


@app.post("/reject/{thread_id}", response_class=HTMLResponse)
def reject_incident(thread_id: str, form: Dict[str, str] = Depends(read_form_body)) -> HTMLResponse:
    """POST: records the rejection (first decision wins). Never resumes the graph."""
    response = _claim_decision("reject", thread_id, form.get("token"))
    if response.status_code == 200:
        logger.warning("[HITL REJECTION] Rejection accepted for thread: %r. State machine left paused.", thread_id)
    return response


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=5000)
