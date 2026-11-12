"""Round-2 offline tests: read-only EXEC allowlist, executor re-check, unapproved-pause handling, graph wiring."""
import time
from typing import Any, Dict, List
from unittest.mock import MagicMock

import pytest
from langgraph.checkpoint.memory import MemorySaver
from fastapi.testclient import TestClient

import agent.sre_agent as ag

SVC = "dummy_prod_api"
WEBHOOK_URL = "http://discord.invalid/hook-secret-path"


@pytest.fixture(autouse=True)
def managed_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MANAGED_CONTAINERS", SVC)
    monkeypatch.delenv("HOSTNAME", raising=False)


def _exec(rest: str) -> str:
    return f"EXEC {SVC} {rest}"


# 1. hard_block_reason: read-only allowlist ------------------------------------

@pytest.mark.parametrize("rest", [
    "cat /etc/shadow", "cat /root/.ssh/id_rsa", "cat /root/.aws/credentials",
    "cat /proc/self/root/run/secrets/x", "cat /proc/1/cmdline", "cat /proc/self/fd/3",
    "cat /app/config.py", "cat /dev/zero", "cat /tmp/../etc/shadow", "cat /tmp/.env",
    "ps eww", "ps auxe", "ps -o pid",
    "du --files0-from=/etc/shadow", "du -s",
    "tail -f /var/log/x", "tail -F /var/log/x",
    "head -c 99999999 /tmp/x", "head -n abc /tmp/x", "head -n 99999 /tmp/x",
    "free -s 1",
    "rm -rf /tmp/x/.", "rm -rf /tmp/./x", "rm -rf //tmp/x",
    "cat", "cat //tmp/x",
    "ls /tmp/a /tmp/b /tmp/c /tmp/d /tmp/e /tmp/f",
    "uptime -p",
])
def test_read_only_allowlist_denies(rest: str) -> None:
    assert ag.hard_block_reason(_exec(rest), SVC)


@pytest.mark.parametrize("rest", [
    "ls", "ls /tmp/cache", "ls -lh /app/logs",
    "cat /var/log/app.log",
    "tail -n 50 /var/log/app.log", "tail -n50 /var/log/app.log", "head -n 5 /tmp/x",
    "ps", "ps -ef", "ps aux",
    "df -h", "df -h /tmp", "du -sh /tmp/cache",
    "free -h", "uptime", "whoami", "id",
    "rm -rf /tmp/cache", "rm -r /tmp/a /tmp/b",
    "tail /app/logs/docker.log", "ls /tmp/dd",
])
def test_read_only_allowlist_allows(rest: str) -> None:
    assert ag.hard_block_reason(_exec(rest), SVC) is None


# 2. execute_action re-check, timeout, truncation -------------------------------

@pytest.fixture
def docker_mock(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    container = MagicMock()
    container.status = "running"
    client = MagicMock()
    client.containers.get.return_value = container
    monkeypatch.setattr(ag.docker, "from_env", MagicMock(return_value=client))
    return container


@pytest.mark.parametrize("action", [
    f"STOP {SVC}", f"EXEC {SVC} cat /etc/shadow", f"EXEC {SVC} ls /root", "", "   ",
])
def test_execute_action_refuses_blocked_actions(docker_mock: MagicMock, action: str) -> None:
    out = ag.execute_action({"proposed_action": action, "service_name": SVC})
    assert out["status"].startswith("action_failed: refused by hard block")
    for method in (docker_mock.stop, docker_mock.restart, docker_mock.pause, docker_mock.exec_run):
        method.assert_not_called()


def test_exec_with_timeout_raises_on_slow_command() -> None:
    container = MagicMock()
    container.exec_run.side_effect = lambda cmd: time.sleep(1) or (0, b"")
    with pytest.raises(TimeoutError):
        ag._exec_with_timeout(container, "ls /tmp", timeout=0.1)  # type: ignore[arg-type]


def test_execute_action_node_reports_timeout(docker_mock: MagicMock, monkeypatch: pytest.MonkeyPatch) -> None:
    docker_mock.exec_run.side_effect = lambda cmd: time.sleep(1) or (0, b"")
    original = ag._exec_with_timeout
    monkeypatch.setattr(ag, "_exec_with_timeout", lambda c, cmd: original(c, cmd, timeout=0.1))  # type: ignore[arg-type]
    out = ag.execute_action({"proposed_action": f"EXEC {SVC} ls /tmp", "service_name": SVC})
    assert out["status"].startswith("action_failed: timeout")


def test_execute_action_truncates_long_output(docker_mock: MagicMock) -> None:
    docker_mock.exec_run.return_value = (0, b"x" * 5000)
    out = ag.execute_action({"proposed_action": f"EXEC {SVC} ls /tmp", "service_name": SVC})
    assert out["status"].startswith("action_executed")
    assert out["status"].endswith("... [truncated]")
    assert len(out["status"]) < 2000 + 200


# 3. state helpers / graph wiring --------------------------------------------------

def test_policy_cycle_reset_returns_distinct_lists() -> None:
    first, second = ag._policy_cycle_reset(), ag._policy_cycle_reset()
    assert first == {"policy_attempts": 0, "denied_actions": []}
    assert first["denied_actions"] is not second["denied_actions"]
    first["denied_actions"].append("x")
    assert ag._policy_cycle_reset()["denied_actions"] == []


def test_create_sre_graph_wiring() -> None:
    graph = ag.create_sre_graph(MemorySaver())
    assert "execute_action" in graph.interrupt_before_nodes
    assert {"check_policy", "escalate_policy_denied", "execute_action"} <= set(graph.nodes)


# 4. Listener: pause with a non-allowed verdict ----------------------------------------

class DeniedEnv:
    def __init__(self, wl: Any, graph: Any) -> None:
        self.wl = wl
        self.graph = graph
        self.client = TestClient(wl.app)
        self.payloads: List[Dict[str, Any]] = []
        self.config = wl._thread_config("t1")

    @property
    def texts(self) -> List[str]:
        return [p["content"] for p in self.payloads]

    def values(self) -> Dict[str, Any]:
        return self.graph.get_state(self.config).values


@pytest.fixture
def denied(monkeypatch: pytest.MonkeyPatch) -> DeniedEnv:
    """A thread paused before execute_action although the policy verdict is 'denied' (legacy/forged state)."""
    monkeypatch.setenv("BASE_URL", "http://dummy.test")
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", WEBHOOK_URL)
    import api.webhook_listener as wl

    monkeypatch.setattr(ag, "parse_alert", lambda s: {"service_name": "svc", "iteration_count": 0})
    monkeypatch.setattr(ag, "retrieve_context", lambda s: {"runbook_context": "x"})
    monkeypatch.setattr(ag, "investigate_logs", lambda s: {"container_logs": "x"})
    monkeypatch.setattr(ag, "plan_mitigation", lambda s: {"proposed_action": "RESTART svc"})
    monkeypatch.setattr(ag, "check_policy", lambda s: {
        "policy_verdict": "denied", "policy_reason": "nope", "policy_attempts": 1,
        "denied_actions": [{"command": "RESTART svc", "reason": "nope"}],
    })
    monkeypatch.setattr(ag, "route_policy", lambda s: "allowed")  # force the pause despite the verdict
    graph = ag.create_sre_graph(MemorySaver())
    monkeypatch.setattr(wl, "sre_graph", graph)
    monkeypatch.setattr(wl.time, "sleep", lambda s: None)
    env = DeniedEnv(wl, graph)

    def fake_post(url: str, json: Any = None, timeout: Any = None) -> MagicMock:
        env.payloads.append(json)
        return MagicMock(status_code=204)

    monkeypatch.setattr(wl.requests, "post", fake_post)
    for _ in graph.stream({"alert_payload": {"service": "svc"}}, env.config):
        pass
    assert graph.get_state(env.config).next == ("execute_action",)
    return env


def test_pause_and_notify_with_denied_verdict_escalates_without_token(
    denied: DeniedEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: List[Any] = []
    real = denied.wl.notify_escalation

    def spy(*args: Any, **kwargs: Any) -> bool:
        calls.append(args)
        return real(*args, **kwargs)

    monkeypatch.setattr(denied.wl, "notify_escalation", spy)
    assert denied.wl.pause_and_notify("t1") is False
    assert len(calls) == 1
    assert not denied.values().get("approval_token")
    assert denied.values().get("hitl_decision") != "pending"
    assert denied.payloads
    for text in denied.texts:
        assert "/approve" not in text and "/reject" not in text and "token=" not in text


def _arm(denied: DeniedEnv) -> None:
    denied.graph.update_state(denied.config, {"approval_token": "tok123", "hitl_decision": "pending"})
    assert denied.graph.get_state(denied.config).next == ("execute_action",)


@pytest.mark.parametrize("path", ["/approve/t1", "/reject/t1"])
def test_post_not_allowed_verdict_is_409_and_decision_untouched(denied: DeniedEnv, path: str) -> None:
    _arm(denied)
    resp = denied.client.post(path, data={"token": "tok123"})
    assert resp.status_code == 409
    assert denied.values()["hitl_decision"] == "pending"


@pytest.mark.parametrize("path", ["/approve/t1", "/reject/t1"])
def test_get_not_allowed_verdict_is_409_with_valid_token(denied: DeniedEnv, path: str) -> None:
    _arm(denied)
    resp = denied.client.get(path + "?token=tok123")
    assert resp.status_code == 409
    assert "<form" not in resp.text
    assert denied.values()["hitl_decision"] == "pending"
