"""Round-3 follow-up tests: diagnostic notes, carried notes, query hardening, planner fallbacks, notify passthrough."""
from types import SimpleNamespace
from typing import Any, Dict, List
from unittest.mock import MagicMock

import pytest
from langgraph.checkpoint.memory import MemorySaver

import agent.sre_agent as ag

SVC = "dummy_prod_api"
NL = chr(10)
DIAG = ag.DIAGNOSTIC_NOTE
VERIF = ag.VERIFICATION_NOTE
TAIL = "tail -n 50 /var/log/app.log"


@pytest.fixture(autouse=True)
def managed_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MANAGED_CONTAINERS", SVC)
    monkeypatch.delenv("HOSTNAME", raising=False)


def _container(exit_code: int = 0, output: bytes = b"") -> MagicMock:
    container = MagicMock()
    container.exec_run.return_value = (exit_code, output)
    return container


# 1. _run_exec ----------------------------------------------------------------------

def test_run_exec_success_adds_flattened_clipped_note() -> None:
    output = ("line one" + NL + "line  two" + NL).encode() + b"y" * 2000
    out = ag._run_exec(_container(0, output), TAIL, "previous logs")
    logs = out["container_logs"]
    assert logs.startswith("previous logs" + NL + f"{DIAG} {TAIL}: line one line two yyy")
    note = logs.splitlines()[-1]
    assert NL not in note
    assert len(note) <= len(f"{DIAG} {TAIL}: ") + 800
    assert out["status"].startswith("action_executed")


def test_run_exec_nonzero_exit_still_adds_note() -> None:
    out = ag._run_exec(_container(2, b"No such file"), TAIL, "prev")
    assert out["status"].startswith("action_failed")
    assert out["container_logs"].endswith(f"{DIAG} {TAIL}: No such file")
    assert out["container_logs"].startswith("prev")


def test_run_exec_timeout_adds_no_note(monkeypatch: pytest.MonkeyPatch) -> None:
    def slow(*_a: Any, **_k: Any) -> Any:
        raise TimeoutError("slow")

    monkeypatch.setattr(ag, "_exec_with_timeout", slow)
    out = ag._run_exec(_container(), TAIL, "prev")
    assert out["status"].startswith("action_failed: timeout")
    assert "container_logs" not in out


def test_run_exec_default_call_works() -> None:
    out = ag._run_exec(_container(0, b"ok"), "df -h")
    assert out["container_logs"] == f"{DIAG} df -h: ok"  # no prev logs, no leading newline


def test_execute_action_exec_diagnostic_carries_previous_logs(monkeypatch: pytest.MonkeyPatch) -> None:
    container = _container(0, b"free space 1G")
    client = MagicMock()
    client.containers.get.return_value = container
    monkeypatch.setattr(ag.docker, "from_env", MagicMock(return_value=client))
    state = {"proposed_action": f"EXEC {SVC} df -h", "service_name": SVC, "container_logs": "old log line"}
    out = ag.execute_action(state)
    assert out["status"].startswith("action_executed")
    assert out["container_logs"].startswith("old log line")
    assert f"{DIAG} df -h: free space 1G" in out["container_logs"]


# 2. investigate_logs carries notes -----------------------------------------------------

def test_investigate_logs_keeps_failure_notes_and_last_two_diagnostics(monkeypatch: pytest.MonkeyPatch) -> None:
    container = MagicMock()
    container.logs.return_value = b"fresh line"
    client = MagicMock()
    client.containers.get.return_value = container
    monkeypatch.setattr(ag.docker, "from_env", MagicMock(return_value=client))
    prev = NL.join([
        "stale", f"{DIAG} d1", f"{VERIF} f1", f"{DIAG} d2", f"{DIAG} d3", f"{VERIF} f2",
    ])
    out = ag.investigate_logs({"service_name": SVC, "container_logs": prev})
    lines = out["container_logs"].splitlines()
    assert lines[0] == "fresh line"
    assert "stale" not in out["container_logs"]
    assert f"{VERIF} f1" in lines and f"{VERIF} f2" in lines
    assert f"{DIAG} d2" in lines and f"{DIAG} d3" in lines
    assert f"{DIAG} d1" not in lines
    assert lines.index(f"{VERIF} f2") > 0


# 3. _build_runbook_query ------------------------------------------------------------------

def test_query_excludes_marker_lines() -> None:
    logs = NL.join(["ERROR real problem", f"{DIAG} tail: ERROR noise", f"{VERIF} ERROR noise2"])
    query = ag._build_runbook_query({"service_name": SVC, "container_logs": logs, "alert_payload": {}})
    assert "real problem" in query
    assert "noise" not in query


def test_query_cuts_huge_line_to_200() -> None:
    logs = "ERROR " + "z" * 5000
    query = ag._build_runbook_query({"service_name": SVC, "container_logs": logs, "alert_payload": {}})
    assert query.count("z") <= 200
    assert len(query) < 500


def test_query_long_middle_line_does_not_stop_shorter_lines() -> None:
    long_line = "ERROR " + "m" * 190
    logs = NL.join(["ERROR first short", long_line, long_line, long_line, "ERROR last short"])
    query = ag._build_runbook_query({"service_name": SVC, "container_logs": logs, "alert_payload": {}})
    assert "last short" in query
    assert "first short" in query  # fits after skipping lines that exceeded the budget


# 4. planner ---------------------------------------------------------------------------------

class ScriptedLlm:
    def __init__(self, replies: List[Any]) -> None:
        self.replies = list(replies)
        self.prompts: List[str] = []

    def invoke(self, messages: List[Any]) -> Any:
        self.prompts.append(messages[-1].content)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return SimpleNamespace(content=reply)


def test_avoid_denied_repeat_clips_denied_action_in_prompt(monkeypatch: pytest.MonkeyPatch) -> None:
    huge = f"EXEC {SVC} ls /tmp/" + "a" * 1000
    llm = ScriptedLlm([f"PAUSE {SVC}"])
    monkeypatch.setattr(ag, "get_llm", lambda: llm)
    state = {"denied_actions": [{"command": huge, "reason": "no"}]}
    assert ag._avoid_denied_repeat(huge, "BASE", state, SVC) == f"PAUSE {SVC}"  # type: ignore[arg-type]
    assert llm.prompts[0].count("a") - "BASE".count("a") <= 200 + len("DENIED by company policy DIFFERENT allowed")


def test_plan_llm_error_falls_back_to_restart(monkeypatch: pytest.MonkeyPatch) -> None:
    llm = ScriptedLlm([RuntimeError("ollama down")])
    monkeypatch.setattr(ag, "get_llm", lambda: llm)
    out = ag.plan_mitigation({"service_name": SVC, "alert_payload": {}, "proposed_action": f"PAUSE {SVC}"})
    assert out["proposed_action"] == f"RESTART {SVC}"
    assert out["status"].startswith("plan_mitigation_error")


def test_plan_llm_error_keeps_existing_action_when_restart_denied(monkeypatch: pytest.MonkeyPatch) -> None:
    llm = ScriptedLlm([RuntimeError("ollama down")])
    monkeypatch.setattr(ag, "get_llm", lambda: llm)
    state = {
        "service_name": SVC, "alert_payload": {}, "proposed_action": f"PAUSE {SVC}",
        "denied_actions": [{"command": f"RESTART {SVC}", "reason": "no"}],
    }
    out = ag.plan_mitigation(state)  # type: ignore[arg-type]
    assert out["proposed_action"] == f"PAUSE {SVC}"
    assert out["status"].startswith("plan_mitigation_error")


# 5. pause_and_notify passes the policy reason through --------------------------------------

def test_pause_and_notify_allowed_passes_policy_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BASE_URL", "http://dummy.test")
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "http://discord.invalid/hook-secret-path")
    import api.webhook_listener as wl

    monkeypatch.setattr(ag, "parse_alert", lambda s: {"service_name": "svc", "iteration_count": 0})
    monkeypatch.setattr(ag, "retrieve_context", lambda s: {"runbook_context": "x"})
    monkeypatch.setattr(ag, "investigate_logs", lambda s: {"container_logs": "x"})
    monkeypatch.setattr(ag, "plan_mitigation", lambda s: {"proposed_action": "RESTART svc"})
    monkeypatch.setattr(ag, "check_policy", lambda s: {
        "policy_verdict": "allowed", "policy_reason": "judge says fine",
        "policy_attempts": 1, "denied_actions": [],
    })
    graph = ag.create_sre_graph(MemorySaver())
    monkeypatch.setattr(wl, "sre_graph", graph)
    config = wl._thread_config("t1")
    for _ in graph.stream({"alert_payload": {"service": "svc"}}, config):
        pass
    calls: List[Dict[str, Any]] = []

    def recorder(*args: Any, **kwargs: Any) -> bool:
        calls.append({"args": args, "kwargs": kwargs})
        return True

    monkeypatch.setattr(wl, "notify_discord", recorder)
    assert wl.pause_and_notify("t1") is True
    assert len(calls) == 1
    assert calls[0]["kwargs"] == {"policy_reason": "judge says fine"}
    assert calls[0]["args"][2] == "RESTART svc"
