"""Behavioural tests for the HITL approval flow in api/webhook_listener.py (offline)."""
import asyncio
import importlib
import logging
import re
import sqlite3
import threading
from typing import Any, Dict, List
from unittest.mock import MagicMock

import pytest
import requests
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import MemorySaver
from langgraph.checkpoint.sqlite import SqliteSaver
from uvicorn.logging import AccessFormatter

import agent.sre_agent as ag

WEBHOOK_URL = "http://discord.invalid/hook-secret-path"
SERVICE = "svc"


class Env:
    """Bundle of the stubbed graph, listener module, client and captured side effects."""

    def __init__(self, wl: Any, graph: Any, client: TestClient) -> None:
        self.wl = wl
        self.graph = graph
        self.client = client
        self.exec_calls = 0
        self.messages: List[str] = []
        self.resumes: List[str] = []
        self.verify_ok = False
        self.real_resume = wl.resume_agent_workflow

    def state(self, thread_id: str) -> Any:
        return self.graph.get_state(self.wl._thread_config(thread_id))

    def token_from_last_message(self) -> str:
        match = re.search(r"token=([\w-]+)", self.messages[-1])
        assert match, "no token in Discord message"
        return match.group(1)


def make_saver(kind: str) -> Any:
    """Builds the checkpointer under test (production uses SqliteSaver)."""
    if kind == "memory":
        return MemorySaver()
    saver = SqliteSaver(sqlite3.connect(":memory:", check_same_thread=False))
    saver.setup()
    return saver


@pytest.fixture(params=["memory", "sqlite"])
def env(monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest) -> Env:
    monkeypatch.setenv("BASE_URL", "http://dummy.test")
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", WEBHOOK_URL)
    import api.webhook_listener as wl

    holder: Dict[str, Env] = {}

    def fake_execute(state: Dict[str, Any]) -> Dict[str, Any]:
        holder["e"].exec_calls += 1
        return {"status": "action_executed"}

    def fake_verify(state: Dict[str, Any]) -> Dict[str, Any]:
        status = "Resolved" if holder["e"].verify_ok else "Escalated - Restart Failed"
        return {"status": status, "iteration_count": state.get("iteration_count", 0) + 1}

    monkeypatch.setattr(ag, "parse_alert", lambda s: {"service_name": SERVICE, "iteration_count": 0})
    monkeypatch.setattr(ag, "retrieve_context", lambda s: {"runbook_context": "x"})
    monkeypatch.setattr(ag, "investigate_logs", lambda s: {"container_logs": "x"})
    monkeypatch.setattr(ag, "plan_mitigation", lambda s: {"proposed_action": "RESTART svc"})
    monkeypatch.setattr(ag, "check_policy", lambda s: {
        "policy_verdict": "allowed", "policy_reason": "stubbed allow",
        "policy_attempts": 1, "denied_actions": [],
    })
    monkeypatch.setattr(ag, "execute_action", fake_execute)
    monkeypatch.setattr(ag, "verify_recovery", fake_verify)
    monkeypatch.setattr(ag, "escalate_to_human", lambda s: {"status": "Escalated"})
    graph = ag.create_sre_graph(make_saver(request.param))
    monkeypatch.setattr(wl, "sre_graph", graph)

    e = Env(wl, graph, TestClient(wl.app))
    holder["e"] = e

    def fake_post(url: str, json: Any = None, timeout: Any = None) -> MagicMock:
        e.messages.append(json["content"])
        return MagicMock(status_code=204)

    monkeypatch.setattr(wl.requests, "post", fake_post)
    monkeypatch.setattr(wl, "resume_agent_workflow", lambda tid: e.resumes.append(tid))
    return e


def start(env: Env, thread_id: str = "t1") -> str:
    """Runs the workflow to the HITL pause and returns the issued token."""
    env.wl.run_agent_workflow({"service": SERVICE}, thread_id)
    return env.token_from_last_message()


# 1. GET never acts ---------------------------------------------------------

@pytest.mark.parametrize("action", ["approve", "reject"])
def test_get_renders_form_and_never_acts(env: Env, action: str) -> None:
    token = start(env)
    resp = env.client.get(f"/{action}/t1?token={token}")
    assert resp.status_code == 200
    assert 'method="post"' in resp.text
    assert f'action="/{action}/t1"' in resp.text
    assert re.search(r'<input[^>]*type="hidden"[^>]*name="token"[^>]*value="%s"' % re.escape(token), resp.text)
    assert env.resumes == []
    assert env.exec_calls == 0
    state = env.state("t1")
    assert state.values["hitl_decision"] == "pending"
    assert state.next == ("execute_action",)
    assert state.values["approval_token"] == token


# 2. 403 / 404 ---------------------------------------------------------------

@pytest.mark.parametrize("action", ["approve", "reject"])
def test_get_missing_or_wrong_token_is_403(env: Env, action: str) -> None:
    start(env)
    assert env.client.get(f"/{action}/t1").status_code == 403
    assert env.client.get(f"/{action}/t1?token=bad").status_code == 403


@pytest.mark.parametrize("action", ["approve", "reject"])
def test_post_missing_or_wrong_token_is_403(env: Env, action: str) -> None:
    start(env)
    assert env.client.post(f"/{action}/t1").status_code == 403
    assert env.client.post(f"/{action}/t1", data={"token": "bad"}).status_code == 403
    assert env.resumes == []
    assert env.state("t1").values["hitl_decision"] == "pending"


@pytest.mark.parametrize("action", ["approve", "reject"])
def test_unknown_thread_is_404(env: Env, action: str) -> None:
    token = start(env)
    assert env.client.get(f"/{action}/nope?token={token}").status_code == 404
    assert env.client.post(f"/{action}/nope", data={"token": token}).status_code == 404
    assert env.resumes == []


# 3. approve once ------------------------------------------------------------

def test_post_approve_once_then_409(env: Env) -> None:
    token = start(env)
    first = env.client.post("/approve/t1", data={"token": token})
    assert first.status_code == 200
    assert env.resumes == ["t1"]
    assert env.state("t1").values["hitl_decision"] == "approved"
    second = env.client.post("/approve/t1", data={"token": token})
    assert second.status_code == 409
    assert env.resumes == ["t1"]
    assert env.client.get(f"/approve/t1?token={token}").status_code == 409


# 4. reject ------------------------------------------------------------------

def test_post_reject_never_resumes_and_blocks_approve(env: Env) -> None:
    token = start(env)
    assert env.client.post("/reject/t1", data={"token": token}).status_code == 200
    assert env.resumes == []
    assert env.state("t1").values["hitl_decision"] == "rejected"
    assert env.client.post("/approve/t1", data={"token": token}).status_code == 409
    assert env.resumes == []
    assert env.exec_calls == 0
    assert env.state("t1").next == ("execute_action",)


# 5. concurrency -------------------------------------------------------------

@pytest.mark.parametrize("action", ["approve", "reject"])
def test_concurrent_posts_exactly_one_wins(env: Env, action: str) -> None:
    token = start(env)
    n = 12
    codes: List[int] = []
    barrier = threading.Barrier(n)

    def worker() -> None:
        barrier.wait()
        codes.append(env.client.post(f"/{action}/t1", data={"token": token}).status_code)

    threads = [threading.Thread(target=worker) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(codes) == n
    assert codes.count(200) == 1
    assert codes.count(409) == n - 1
    assert len(env.resumes) == (1 if action == "approve" else 0)


# 6/7. retry pause, interrupt survives update_state ---------------------------

def test_pause_and_notify_keeps_interrupt(env: Env) -> None:
    token = start(env)
    state = env.state("t1")
    assert state.next == ("execute_action",)
    assert state.values["approval_token"] == token
    assert state.values["hitl_decision"] == "pending"
    assert len(env.messages) == 1


def test_retry_pause_issues_new_token_and_invalidates_old(env: Env) -> None:
    old = start(env)
    assert env.client.post("/approve/t1", data={"token": old}).status_code == 200
    assert env.exec_calls == 0
    # Run the real resume (the fixture replaced it with a recorder).
    env.real_resume("t1")
    assert env.exec_calls == 1
    state = env.state("t1")
    assert state.next == ("execute_action",)
    assert state.values["hitl_decision"] == "pending"
    new = state.values["approval_token"]
    assert new and new != old
    assert len(env.messages) == 2
    assert env.token_from_last_message() == new
    assert env.client.post("/approve/t1", data={"token": old}).status_code == 403
    assert env.client.get(f"/approve/t1?token={old}").status_code == 403
    assert env.client.get(f"/approve/t1?token={new}").status_code == 200


def test_resume_without_pause_does_not_notify(env: Env) -> None:
    env.verify_ok = True
    token = start(env)
    env.client.post("/approve/t1", data={"token": token})
    env.real_resume("t1")
    assert env.state("t1").next == ()
    assert len(env.messages) == 1


# 8. notify_discord ----------------------------------------------------------

@pytest.mark.parametrize("code,expected", [(204, True), (200, True), (404, False), (500, False)])
def test_notify_discord_status(env: Env, monkeypatch: pytest.MonkeyPatch, code: int, expected: bool) -> None:
    monkeypatch.setattr(env.wl.requests, "post", lambda *a, **k: MagicMock(status_code=code))
    assert env.wl.notify_discord("t", "s", "a", "tok") is expected


def test_notify_discord_request_exception(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*a: Any, **k: Any) -> None:
        raise requests.exceptions.ConnectionError(f"cannot reach {WEBHOOK_URL}")

    monkeypatch.setattr(env.wl.requests, "post", boom)
    assert env.wl.notify_discord("t", "s", "a", "tok") is False


@pytest.mark.parametrize("mode", ["204", "404", "500", "exception"])
def test_notify_discord_never_logs_webhook_or_token(
    env: Env, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, mode: str
) -> None:
    secret_token = "SECRET-TOKEN-abc123"

    def fake(*a: Any, **k: Any) -> MagicMock:
        if mode == "exception":
            raise requests.exceptions.Timeout("timeout")
        return MagicMock(status_code=int(mode))

    monkeypatch.setattr(env.wl.requests, "post", fake)
    with caplog.at_level(logging.DEBUG):
        env.wl.notify_discord("t", "s", "a", secret_token)
    assert caplog.records
    for record in caplog.records:
        text = record.getMessage()
        assert WEBHOOK_URL not in text
        assert "hook-secret-path" not in text
        assert secret_token not in text


def test_workflow_logs_never_contain_token(env: Env, caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.DEBUG):
        token = start(env)
        env.client.get(f"/approve/t1?token={token}")
        env.client.post("/approve/t1", data={"token": "wrong"})
        env.client.post("/approve/t1", data={"token": token})
    # Only the listener's own logger: httpx (used by TestClient) logs request URLs itself.
    records = [r for r in caplog.records if r.name == "webhook_listener"]
    assert records
    for record in records:
        assert token not in record.getMessage()
        assert WEBHOOK_URL not in record.getMessage()


# 9. escaping ----------------------------------------------------------------

def test_confirm_page_escapes_dynamic_values(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    evil = "<script>x</script>"
    monkeypatch.setattr(ag, "parse_alert", lambda s: {"service_name": evil, "iteration_count": 0})
    monkeypatch.setattr(ag, "plan_mitigation", lambda s: {"proposed_action": '"><img src=x onerror=1>'})
    env.graph = ag.create_sre_graph(MemorySaver())
    monkeypatch.setattr(env.wl, "sre_graph", env.graph)
    token = start(env)
    resp = env.client.get(f"/approve/t1?token={token}")
    assert resp.status_code == 200
    assert evil not in resp.text
    assert "&lt;script&gt;x&lt;/script&gt;" in resp.text
    assert "<img src=x" not in resp.text


def test_render_confirm_page_escapes_token_and_thread(env: Env) -> None:
    page = env.wl.render_confirm_page("approve", 't"><b>', 'to"><i>', "<s>", "<u>")
    for raw in ("<b>", "<i>", "<s>", "<u>"):
        assert raw not in page


# 10. HITL interrupt ---------------------------------------------------------

def test_compiled_graph_interrupts_before_execute_action(env: Env) -> None:
    assert "execute_action" in env.graph.interrupt_before_nodes
    env.wl.run_agent_workflow({"service": SERVICE}, "t9")
    assert env.exec_calls == 0
    assert env.state("t9").next == ("execute_action",)


# 11. access-log redaction ---------------------------------------------------

ACCESS_ARGS = ("1.2.3.4:5", "GET", "/approve/x?token=ABC&y=1", "1.1", 200)


def access_record() -> logging.LogRecord:
    return logging.LogRecord(
        "uvicorn.access", logging.INFO, __file__, 1,
        '%s - "%s %s HTTP/%s" %d', ACCESS_ARGS, None,
    )


def count_redact_filters() -> int:
    filters = logging.getLogger("uvicorn.access").filters
    return len([f for f in filters if type(f).__name__ == "_RedactTokenFilter"])


def test_redact_filter_scrubs_token_and_keeps_other_params(env: Env) -> None:
    record = access_record()
    assert env.wl._RedactTokenFilter().filter(record) is True
    text = record.getMessage()
    assert "ABC" not in text
    assert "[REDACTED]" in text
    assert "y=1" in text


def test_redact_filter_with_uvicorn_access_formatter(env: Env) -> None:
    record = access_record()
    env.wl._RedactTokenFilter().filter(record)
    formatter = AccessFormatter('%(client_addr)s - "%(request_line)s" %(status_code)s', use_colors=False)
    out = formatter.format(record)
    assert "ABC" not in out
    assert "[REDACTED]" in out
    assert "y=1" in out


def test_redact_filter_installed_on_uvicorn_access_logger(env: Env) -> None:
    assert count_redact_filters() >= 1


def test_redact_filter_installed_once_after_reload(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BASE_URL", "http://dummy.test")
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", WEBHOOK_URL)
    import api.webhook_listener as wl

    importlib.reload(wl)
    importlib.reload(wl)
    assert count_redact_filters() == 1


# 12. body limits ------------------------------------------------------------

FORM_HEADERS = {"Content-Type": "application/x-www-form-urlencoded"}


@pytest.mark.parametrize("action", ["approve", "reject"])
def test_post_content_length_over_limit_is_413(env: Env, action: str) -> None:
    token = start(env)
    resp = env.client.post(f"/{action}/t1", content=b"a" * 5000, headers=FORM_HEADERS)
    assert resp.status_code == 413
    assert env.resumes == []
    assert env.state("t1").values["hitl_decision"] == "pending"
    assert env.state("t1").values["approval_token"] == token


@pytest.mark.parametrize("action", ["approve", "reject"])
def test_post_chunked_oversized_body_is_413(env: Env, action: str) -> None:
    token = start(env)

    def body() -> Any:
        yield f"token={token}&pad=".encode()
        for _ in range(10):
            yield b"a" * 1000

    resp = env.client.post(f"/{action}/t1", content=body(), headers=FORM_HEADERS)
    assert resp.status_code == 413
    assert env.resumes == []
    assert env.state("t1").values["hitl_decision"] == "pending"


@pytest.mark.parametrize("action", ["approve", "reject"])
def test_post_wrong_content_type_is_415(env: Env, action: str) -> None:
    token = start(env)
    resp = env.client.post(f"/{action}/t1", content=f"token={token}".encode(), headers={"Content-Type": "text/plain"})
    assert resp.status_code == 415
    assert env.resumes == []
    assert env.state("t1").values["hitl_decision"] == "pending"


@pytest.mark.parametrize("action", ["approve", "reject"])
def test_post_too_many_form_fields_is_400(env: Env, action: str) -> None:
    token = start(env)
    data = {f"f{i}": "x" for i in range(11)}
    data["token"] = token
    resp = env.client.post(f"/{action}/t1", data=data)
    assert resp.status_code == 400
    assert env.resumes == []
    assert env.state("t1").values["hitl_decision"] == "pending"


def test_post_small_valid_urlencoded_still_works(env: Env) -> None:
    token = start(env)
    resp = env.client.post("/approve/t1", content=f"token={token}".encode(), headers=FORM_HEADERS)
    assert resp.status_code == 200
    assert env.resumes == ["t1"]
    assert env.state("t1").values["hitl_decision"] == "approved"


# 13. security headers -------------------------------------------------------

def assert_secure_headers(resp: Any) -> None:
    assert resp.headers["referrer-policy"] == "no-referrer"
    assert resp.headers["cache-control"] == "no-store"
    assert resp.headers["x-frame-options"] == "DENY"
    assert resp.headers["x-content-type-options"] == "nosniff"
    csp = resp.headers["content-security-policy"]
    assert "default-src 'none'" in csp
    assert "frame-ancestors 'none'" in csp


def test_security_headers_on_all_response_kinds(env: Env) -> None:
    token = start(env)
    responses = {
        200: env.client.get(f"/approve/t1?token={token}"),
        403: env.client.get("/approve/t1?token=bad"),
        404: env.client.get(f"/approve/nope?token={token}"),
        413: env.client.post("/approve/t1", content=b"a" * 5000, headers=FORM_HEADERS),
    }
    post_ok = env.client.post("/reject/t1", data={"token": token})
    responses[409] = env.client.post("/reject/t1", data={"token": token})
    for code, resp in responses.items():
        assert resp.status_code == code
        assert_secure_headers(resp)
    assert post_ok.status_code == 200
    assert_secure_headers(post_ok)


# 14. Discord payload --------------------------------------------------------

def test_notify_discord_disables_mentions(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    sent: List[Any] = []

    def capture(url: str, json: Any = None, timeout: Any = None) -> MagicMock:
        sent.append(json)
        return MagicMock(status_code=204)

    monkeypatch.setattr(env.wl.requests, "post", capture)
    assert env.wl.notify_discord("t", "@everyone", "a", "tok") is True
    assert sent[0]["allowed_mentions"] == {"parse": []}


# 15. notification retry -----------------------------------------------------

def test_notify_failure_twice_logs_not_notified_without_token(
    env: Env, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(env.wl, "_NOTIFY_RETRY_DELAY_SECONDS", 0)
    calls: List[int] = []

    def fail(*a: Any, **k: Any) -> MagicMock:
        calls.append(1)
        return MagicMock(status_code=500)

    monkeypatch.setattr(env.wl.requests, "post", fail)
    with caplog.at_level(logging.DEBUG):
        env.wl.run_agent_workflow({"service": SERVICE}, "t1")
    assert len(calls) == 2
    token = env.state("t1").values["approval_token"]
    assert env.state("t1").next == ("execute_action",)
    assert any("Operator NOT notified" in r.getMessage() for r in caplog.records)
    for record in caplog.records:
        assert token not in record.getMessage()


def test_notify_fail_then_succeed_has_no_not_notified_error(
    env: Env, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(env.wl, "_NOTIFY_RETRY_DELAY_SECONDS", 0)
    codes = [500, 204]
    calls: List[int] = []

    def flaky(url: str, json: Any = None, timeout: Any = None) -> MagicMock:
        calls.append(1)
        return MagicMock(status_code=codes[len(calls) - 1])

    monkeypatch.setattr(env.wl.requests, "post", flaky)
    with caplog.at_level(logging.DEBUG):
        env.wl.run_agent_workflow({"service": SERVICE}, "t1")
    assert len(calls) == 2
    assert not any("Operator NOT notified" in r.getMessage() for r in caplog.records)
    # The first failed attempt logs its own ERROR; only the final "NOT notified" error is forbidden.
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR and "NOT notified" in r.getMessage()]


# 16. resume gate ------------------------------------------------------------

def spy_stream(env: Env, monkeypatch: pytest.MonkeyPatch) -> List[Any]:
    calls: List[Any] = []

    def fake_stream(*a: Any, **k: Any) -> Any:
        calls.append(a)
        return iter(())

    monkeypatch.setattr(env.graph, "stream", fake_stream)
    return calls


@pytest.mark.parametrize("decision", ["pending", "rejected", "missing"])
def test_resume_gate_refuses_non_approved(
    env: Env, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, decision: str
) -> None:
    start(env)
    thread = "t1"
    if decision == "missing":
        thread = "never-started"
    elif decision == "rejected":
        env.graph.update_state(env.wl._thread_config(thread), {"hitl_decision": "rejected"})
    calls = spy_stream(env, monkeypatch)
    with caplog.at_level(logging.DEBUG):
        env.real_resume(thread)
    assert calls == []
    assert env.exec_calls == 0
    assert any(r.levelno == logging.WARNING and r.name == "webhook_listener" for r in caplog.records)


# 17. resume crash -----------------------------------------------------------

def test_resume_crash_issues_fresh_token(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    old = start(env)
    assert env.client.post("/approve/t1", data={"token": old}).status_code == 200

    def boom(*a: Any, **k: Any) -> None:
        raise RuntimeError("stream exploded")

    monkeypatch.setattr(env.graph, "stream", boom)
    env.real_resume("t1")
    assert len(env.messages) == 2
    new = env.token_from_last_message()
    assert new != old
    state = env.state("t1")
    assert state.values["hitl_decision"] == "pending"
    assert state.values["approval_token"] == new
    assert env.client.post("/approve/t1", data={"token": old}).status_code == 403
    assert env.client.get(f"/approve/t1?token={new}").status_code == 200


def test_resume_crash_with_failing_renotify_does_not_propagate(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    old = start(env)
    env.client.post("/approve/t1", data={"token": old})

    def boom(*a: Any, **k: Any) -> None:
        raise RuntimeError("stream exploded")

    def boom2(*a: Any, **k: Any) -> None:
        raise RuntimeError("renotify exploded")

    monkeypatch.setattr(env.graph, "stream", boom)
    monkeypatch.setattr(env.wl, "pause_and_notify", boom2)
    env.real_resume("t1")
    assert len(env.messages) == 1


# 18. /alert error path ------------------------------------------------------

def test_alert_error_response_is_generic(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom() -> None:
        raise RuntimeError("secret-internal-detail")

    monkeypatch.setattr(env.wl.uuid, "uuid4", boom)
    resp = env.client.post("/alert", json={"service": SERVICE})
    assert resp.json()["status"] == "error"
    assert "secret-internal-detail" not in resp.text
    assert "RuntimeError" not in resp.text


# 19. log injection ----------------------------------------------------------

def test_newline_in_thread_id_or_service_not_raw_in_logs(env: Env, caplog: pytest.LogCaptureFixture) -> None:
    start(env, "t1\nFORGED")  # a real thread whose id contains a newline
    with caplog.at_level(logging.DEBUG):
        env.client.get("/approve/t1%0aFORGED?token=bad")
        env.client.get("/approve/t1?token=bad%0aFORGED")
        resp = env.client.post("/alert", json={"service": "a\nFORGED-LINE"})
    assert resp.status_code == 400  # newline service name is rejected before anything is logged
    records = [r for r in caplog.records if r.name == "webhook_listener"]
    assert any("FORGED" in r.getMessage() for r in records)  # thread_id is logged (escaped), not dropped
    for record in records:
        assert "\n" not in record.getMessage()
        assert "\r" not in record.getMessage()


# 20. raw ASGI Content-Length handling, /alert validation, CSP exemptions ------

def asgi_post(wl: Any, path: str, content_length: bytes, body: bytes = b"a=1") -> int:
    """Calls the ASGI app directly so a non-ASCII Content-Length header can be sent as raw bytes."""
    status_codes: List[int] = []
    scope = {
        "type": "http", "method": "POST", "path": path, "query_string": b"",
        "headers": [(b"content-length", content_length), (b"content-type", b"application/x-www-form-urlencoded")],
        "http_version": "1.1", "scheme": "http", "server": ("t", 80), "client": ("c", 1), "root_path": "",
    }

    async def receive() -> Dict[str, Any]:
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message: Dict[str, Any]) -> None:
        if message["type"] == "http.response.start":
            status_codes.append(message["status"])

    asyncio.run(wl.app(scope, receive, send))
    return status_codes[0]


@pytest.mark.parametrize("path", ["/approve/x", "/reject/x", "/alert"])
def test_non_ascii_content_length_is_400(env: Env, path: str) -> None:
    assert asgi_post(env.wl, path, "²".encode("latin-1")) == 400


def test_form_content_length_over_4096_is_413_via_asgi(env: Env) -> None:
    assert asgi_post(env.wl, "/approve/x", b"4097") == 413


def test_alert_declared_content_length_over_64kb_is_413(env: Env) -> None:
    assert asgi_post(env.wl, "/alert", str(64 * 1024 + 1).encode()) == 413


def test_alert_oversized_body_is_413_and_not_scheduled(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: List[Any] = []
    monkeypatch.setattr(env.wl, "run_agent_workflow", lambda *a: calls.append(a))
    resp = env.client.post("/alert", content=b'{"a":"' + b"x" * 70000 + b'"}')
    assert resp.status_code == 413
    assert calls == []


def test_alert_streamed_body_over_64kb_without_content_length_is_413(env: Env) -> None:
    def body() -> Any:
        yield b'{"a":"'
        for _ in range(70):
            yield b"x" * 1000
        yield b'"}'

    resp = env.client.post("/alert", content=body())
    assert "content-length" not in resp.request.headers
    assert resp.status_code == 413


@pytest.mark.parametrize("raw", [b"{bad", b"[1]", b'"just a string"', b"", b"null", b"42"])
def test_alert_non_object_or_invalid_json_is_400(env: Env, monkeypatch: pytest.MonkeyPatch, raw: bytes) -> None:
    calls: List[Any] = []
    monkeypatch.setattr(env.wl, "run_agent_workflow", lambda *a: calls.append(a))
    assert env.client.post("/alert", content=raw).status_code == 400
    assert calls == []


def test_alert_valid_dict_is_202_with_exact_keys(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: List[Any] = []
    monkeypatch.setattr(env.wl, "run_agent_workflow", lambda *a: calls.append(a))
    resp = env.client.post("/alert", json={"service": "s"})
    assert resp.status_code == 202
    body = resp.json()
    assert sorted(body) == ["message", "service", "status", "thread_id"]
    assert body["status"] == "accepted"
    assert body["service"] == "s"
    assert calls == [({"service": "s"}, body["thread_id"])]


@pytest.mark.parametrize("path", ["/docs", "/redoc", "/openapi.json"])
def test_docs_paths_exempt_from_csp_but_keep_other_headers(env: Env, path: str) -> None:
    resp = env.client.get(path)
    assert resp.status_code == 200
    assert "content-security-policy" not in resp.headers
    assert resp.headers["x-frame-options"] == "DENY"
    assert resp.headers["cache-control"] == "no-store"
    assert resp.headers["referrer-policy"] == "no-referrer"
    assert resp.headers["x-content-type-options"] == "nosniff"


def test_non_docs_pages_keep_strict_csp(env: Env) -> None:
    assert_secure_headers(env.client.get("/approve/x?token=bad"))
    assert_secure_headers(env.client.get("/approve/x?token=bad", follow_redirects=False))
    assert_secure_headers(env.client.post("/alert", content=b"{bad"))


# 21. huge payload logging ---------------------------------------------------

def test_run_agent_workflow_logs_truncated_payload(env: Env, caplog: pytest.LogCaptureFixture) -> None:
    payload = {"service": SERVICE, "pad": "x" * 200000}
    with caplog.at_level(logging.DEBUG):
        env.wl.run_agent_workflow(payload, "t1")
    records = [r for r in caplog.records if r.name == "webhook_listener" and "Initiating" in r.getMessage()]
    assert records
    assert len(records[0].getMessage()) <= 620
    assert all(len(r.getMessage()) < 5000 for r in caplog.records if r.name == "webhook_listener")


# 21. /alert service-name validation, resume policy guard --------------------

@pytest.mark.parametrize("payload", [
    {"service": 5}, {"service": "a b"}, {"service": "x" * 65}, {"service": None},
    {"service_name": "bad name"}, {"service": ""},
])
def test_alert_invalid_service_is_400_and_not_scheduled(
    env: Env, monkeypatch: pytest.MonkeyPatch, payload: Dict[str, Any]
) -> None:
    calls: List[Any] = []
    monkeypatch.setattr(env.wl, "run_agent_workflow", lambda *a: calls.append(a))
    assert env.client.post("/alert", json=payload).status_code == 400
    assert calls == []


@pytest.mark.parametrize("name", ["dummy_prod_api", "a.b-c_1", "x" * 64])
def test_alert_valid_service_is_202(env: Env, monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    monkeypatch.setattr(env.wl, "run_agent_workflow", lambda *a: None)
    assert env.client.post("/alert", json={"service": name}).status_code == 202


@pytest.mark.parametrize("verdict", ["denied", None])
def test_resume_refuses_unless_policy_allowed(env: Env, verdict: Any) -> None:
    start(env)
    env.graph.update_state(env.wl._thread_config("t1"), {"hitl_decision": "approved", "policy_verdict": verdict})
    before = env.state("t1").next
    sent = len(env.messages)
    env.real_resume("t1")
    assert len(env.messages) == sent  # no new Discord message
    assert env.exec_calls == 0
    assert env.state("t1").next == before  # the graph was not advanced
