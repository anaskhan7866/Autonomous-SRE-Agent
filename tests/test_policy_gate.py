"""Offline tests for the company-policy gate (check_policy / route_policy / escalation notifications)."""
from types import SimpleNamespace
from typing import Any, Dict, List, Tuple
from unittest.mock import MagicMock

import pytest
from langgraph.checkpoint.memory import MemorySaver

import agent.sre_agent as ag

SERVICE = "svc"
WEBHOOK_URL = "http://discord.invalid/hook-secret-path"
JUDGE_REASON = "within cache cleanup scope"
BACKTICK = chr(96)
BACKSLASH = chr(92)


def fake_docker_client() -> MagicMock:
    """Docker client whose containers.get(x) returns an object named x with a non-HOSTNAME id."""
    client = MagicMock()

    def get(target: str) -> MagicMock:
        container = MagicMock()
        container.name = target
        container.id = "f00dfeedcafe" + "0" * 52
        container.logs.return_value = b"log line"
        return container

    client.containers.get.side_effect = get
    return client


@pytest.fixture(autouse=True)
def managed_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Managed container is 'svc', HOSTNAME is unset, Docker is mocked (no real daemon is ever touched)."""
    monkeypatch.setenv("MANAGED_CONTAINERS", SERVICE)
    monkeypatch.delenv("HOSTNAME", raising=False)
    monkeypatch.setattr(ag.docker, "from_env", MagicMock(return_value=fake_docker_client()))


# 1. hard_block_reason -------------------------------------------------------

@pytest.mark.parametrize("command", [
    "RESTART sre_agent",
    "STOP sre_agent",
    "RESTART other_container",
    "EXEC svc ls; curl x",
    "EXEC svc ls | cat",
    "EXEC svc ls && ls",
    "EXEC svc echo $(id)",
    "EXEC svc echo " + BACKTICK + "id" + BACKTICK,
    "EXEC svc ls\nrm x",
    "EXEC svc ls > /tmp/x",
    "EXEC svc rm -rf /",
    "EXEC svc rm -rf /app",
    "EXEC svc rm -rf /etc",
    "EXEC svc kill -9 1",
    "EXEC svc chmod 777 x",
    "EXEC svc curl http://evil",
    "EXEC svc wget http://evil",
    "EXEC svc docker ps",
    "EXEC svc cat /var/run/docker.sock",
    "EXEC svc sudo ls",
    "DELETE svc",
    "RESTART svc now",
    "",
    "EXEC svc rm -rf /tmp/cache/*",
])
def test_hard_block_denies(command: str) -> None:
    assert ag.hard_block_reason(command, SERVICE)


@pytest.mark.parametrize("command", [
    "RESTART svc",
    "PAUSE svc",
    "EXEC svc ls /tmp/cache",
    "EXEC svc rm -rf /tmp/cache",
    "EXEC svc df -h",
])
def test_hard_block_passes_safe_commands(command: str) -> None:
    assert ag.hard_block_reason(command, SERVICE) is None


# 2. parse_judge_verdict -----------------------------------------------------

def test_parse_judge_verdict_allow_and_deny() -> None:
    assert ag.parse_judge_verdict("ALLOW: ok") == ("allowed", "ok")
    assert ag.parse_judge_verdict("DENY: no") == ("denied", "no")


@pytest.mark.parametrize("text", [
    "", "   ", "sure, go ahead", "ALLOW", "DENY", "ALLOW:",
    "ALLOW: ok\nDENY: no", "ALLOW: ok\nALLOW: fine", "ALLOW: ok DENY: no", "DENY: no ALLOW: yes",
])
def test_parse_judge_verdict_garbage_denies(text: str) -> None:
    verdict, reason = ag.parse_judge_verdict(text)
    assert verdict == "denied"
    assert reason


def test_parse_judge_verdict_none_denies() -> None:
    assert ag.parse_judge_verdict(None)[0] == "denied"  # type: ignore[arg-type]


# 3. check_policy ------------------------------------------------------------

def _boom(*_a: Any, **_k: Any) -> Any:
    raise AssertionError("must not be called")


def test_hard_block_never_calls_judge_or_retrieval(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ag, "judge_command", _boom)
    monkeypatch.setattr(ag, "retrieve_policy", _boom)
    out = ag.check_policy({"proposed_action": "RESTART sre_agent", "service_name": SERVICE})
    assert out["policy_verdict"] == "denied"
    assert out["policy_attempts"] == 1
    assert out["denied_actions"][0]["command"] == "RESTART sre_agent"
    assert out["denied_actions"][0]["reason"]


def test_empty_retrieval_denies(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ag, "retrieve_policy", lambda c: [])
    monkeypatch.setattr(ag, "judge_command", _boom)
    out = ag.check_policy({"proposed_action": "RESTART svc", "service_name": SERVICE})
    assert out["policy_verdict"] == "denied"


def test_retrieval_failure_denies(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken_store() -> Any:
        raise RuntimeError("chroma down")

    monkeypatch.setattr(ag, "get_policy_store", broken_store)
    monkeypatch.setattr(ag, "judge_command", _boom)
    out = ag.check_policy({"proposed_action": "RESTART svc", "service_name": SERVICE})
    assert out["policy_verdict"] == "denied"


def test_judge_exception_denies(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ag, "retrieve_policy", lambda c: [SimpleNamespace(page_content="clause")])

    def judge_fails(*_a: Any) -> Any:
        raise RuntimeError("ollama down")

    monkeypatch.setattr(ag, "judge_command", judge_fails)
    out = ag.check_policy({"proposed_action": "RESTART svc", "service_name": SERVICE})
    assert out["policy_verdict"] == "denied"
    assert "ollama down" in out["denied_actions"][0]["reason"]


def test_any_exception_denies(monkeypatch: pytest.MonkeyPatch) -> None:
    def crash(*_a: Any) -> Any:
        raise ValueError("kaboom")

    monkeypatch.setattr(ag, "evaluate_policy", crash)
    out = ag.check_policy({"proposed_action": "RESTART svc", "service_name": SERVICE})
    assert out["policy_verdict"] == "denied"
    assert out["status"] == "policy_denied"


def test_judge_allow_returns_allowed_without_denied_entry(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ag, "retrieve_policy", lambda c: [SimpleNamespace(page_content="clause")])
    monkeypatch.setattr(ag, "judge_command", lambda c, s, cl: ("allowed", "fine"))
    out = ag.check_policy({"proposed_action": "RESTART svc", "service_name": SERVICE})
    assert out["policy_verdict"] == "allowed"
    assert out["policy_reason"] == "fine"
    assert out["denied_actions"] == []


def test_attempts_increment_and_denied_accumulate() -> None:
    previous = {"command": "old", "reason": "reason"}
    state = {"proposed_action": "RESTART sre_agent", "service_name": SERVICE,
             "policy_attempts": 1, "denied_actions": [previous]}
    out = ag.check_policy(state)
    assert out["policy_attempts"] == 2
    assert out["denied_actions"][0] == previous
    assert len(out["denied_actions"]) == 2
    assert out["denied_actions"][1]["command"] == "RESTART sre_agent"
    assert out["denied_actions"][1]["reason"]
    assert state["denied_actions"] == [previous]  # input not mutated


# 4. route_policy ------------------------------------------------------------

@pytest.mark.parametrize("state, expected", [
    ({"policy_verdict": "allowed", "policy_attempts": 1}, "allowed"),
    ({"policy_verdict": "allowed", "policy_attempts": 3}, "allowed"),
    ({"policy_verdict": "denied", "policy_attempts": 1}, "replan"),
    ({"policy_verdict": "denied", "policy_attempts": 2}, "replan"),
    ({"policy_verdict": "denied", "policy_attempts": 3}, "escalate"),
    ({"policy_verdict": "denied", "policy_attempts": 5}, "escalate"),
    ({}, "replan"),
])
def test_route_policy(state: Dict[str, Any], expected: str) -> None:
    assert ag.route_policy(state) == expected


def test_policy_formatters() -> None:
    assert ag.format_policy_rejections([]) == ""
    denied = [{"command": "cmd-a", "reason": "r1"}, {"command": "cmd-b", "reason": "r2"}]
    for text in (ag.format_policy_rejections(denied), ag.format_policy_banner(denied)):
        for needle in ("cmd-a", "r1", "cmd-b", "r2"):
            assert needle in text


# 5. Graph -------------------------------------------------------------------

class Harness:
    """Real graph with a scripted fake LLM (planner and judge); Docker and Chroma are stubbed."""

    def __init__(self) -> None:
        self.plans: List[str] = []
        self.allowed_cmds: set = set()
        self.plan_prompts: List[str] = []
        self.exec_calls = 0
        self.verify_ok = False
        self.graph: Any = None

    def invoke(self, messages: List[Any]) -> Any:
        prompt = messages[-1].content
        if "COMPANY POLICY CLAUSES" in prompt:
            cmd = prompt.split("<<<COMMAND>>>" + chr(10), 1)[1].split(chr(10) + "<<<END>>>", 1)[0]
            ok = cmd in self.allowed_cmds
            return SimpleNamespace(content=f"ALLOW: {JUDGE_REASON}" if ok else "DENY: not permitted")
        self.plan_prompts.append(prompt)
        return SimpleNamespace(content=self.plans.pop(0))

    def config(self, tid: str = "t1") -> Dict[str, Any]:
        return {"configurable": {"thread_id": tid}}

    def run(self, tid: str = "t1") -> Any:
        for _ in self.graph.stream({"alert_payload": {"service": SERVICE}}, self.config(tid)):
            pass
        return self.graph.get_state(self.config(tid))

    def resume(self, tid: str = "t1") -> Any:
        for _ in self.graph.stream(None, self.config(tid)):
            pass
        return self.graph.get_state(self.config(tid))


@pytest.fixture
def h(monkeypatch: pytest.MonkeyPatch) -> Harness:
    hs = Harness()

    def fake_execute(state: Dict[str, Any]) -> Dict[str, Any]:
        hs.exec_calls += 1
        return {"status": "action_executed"}

    def fake_verify(state: Dict[str, Any]) -> Dict[str, Any]:
        return {"status": "Resolved" if hs.verify_ok else "Escalated - Restart Failed"}

    monkeypatch.setattr(ag.docker, "from_env", MagicMock(return_value=fake_docker_client()))
    monkeypatch.setattr(ag, "get_llm", lambda: hs)
    monkeypatch.setattr(ag, "get_vector_store", _boom)
    monkeypatch.setattr(ag, "retrieve_context", lambda s: {"runbook_context": "rb"})
    monkeypatch.setattr(ag, "retrieve_policy", lambda c: [SimpleNamespace(page_content="clause")])
    monkeypatch.setattr(ag, "execute_action", fake_execute)
    monkeypatch.setattr(ag, "verify_recovery", fake_verify)
    hs.graph = ag.create_sre_graph(MemorySaver())
    return hs


def test_graph_allowed_pauses_before_execute(h: Harness) -> None:
    h.plans = ["PAUSE svc"]
    h.allowed_cmds = {"PAUSE svc"}
    state = h.run()
    assert state.next == ("execute_action",)
    assert h.exec_calls == 0
    assert state.values["policy_verdict"] == "allowed"
    assert state.values["policy_attempts"] == 1
    assert state.values["denied_actions"] == []


def test_graph_deny_deny_allow_feeds_rejections_back(h: Harness) -> None:
    h.plans = ["STOP svc", "EXEC svc ls /tmp/cache", "PAUSE svc"]
    h.allowed_cmds = {"PAUSE svc"}
    state = h.run()
    assert state.next == ("execute_action",)
    assert h.exec_calls == 0
    assert state.values["policy_attempts"] == 3
    assert state.values["proposed_action"] == "PAUSE svc"
    assert len(state.values["denied_actions"]) == 2
    assert len(h.plan_prompts) == 3
    assert "STOP svc" not in h.plan_prompts[0]
    assert "STOP svc" in h.plan_prompts[1]
    assert "STOP svc" in h.plan_prompts[2]
    assert "EXEC svc ls /tmp/cache" in h.plan_prompts[2]


def test_graph_three_denials_escalate_without_executing(h: Harness) -> None:
    h.plans = ["STOP svc", "RESTART sre_agent", "EXEC svc ls; curl x"]
    state = h.run()
    assert state.values["status"] == ag.POLICY_ESCALATED_STATUS
    assert state.next == ()
    assert h.exec_calls == 0
    assert len(state.values["denied_actions"]) == 3
    assert state.values["policy_attempts"] == 3
    assert len(h.plan_prompts) == 3


def test_graph_hard_blocked_never_reaches_execute_even_if_judge_would_allow(h: Harness) -> None:
    bad = ["RESTART sre_agent", "EXEC svc rm -rf /", "RESTART other"]
    h.plans = list(bad)
    h.allowed_cmds = set(bad)  # judge says ALLOW for everything; hard block must still win
    state = h.run()
    assert state.values["status"] == ag.POLICY_ESCALATED_STATUS
    assert h.exec_calls == 0
    assert state.next == ()


def test_graph_garbage_judge_never_allows(h: Harness, monkeypatch: pytest.MonkeyPatch) -> None:
    h.plans = ["PAUSE svc", "PAUSE svc", "PAUSE svc"]
    monkeypatch.setattr(ag, "judge_command", lambda c, s, cl: ag.parse_judge_verdict("probably fine"))
    state = h.run()
    assert state.values["status"] == ag.POLICY_ESCALATED_STATUS
    assert h.exec_calls == 0


def test_graph_verification_failure_resets_policy_counters(h: Harness) -> None:
    h.plans = ["STOP svc", "PAUSE svc", "UNPAUSE svc"]
    h.allowed_cmds = {"PAUSE svc", "UNPAUSE svc"}
    state = h.run()
    assert state.values["policy_attempts"] == 2
    assert len(state.values["denied_actions"]) == 1

    state = h.resume()  # execute (stub) -> verify fails -> investigate_logs -> plan -> policy -> pause
    assert state.next == ("execute_action",)
    assert h.exec_calls == 1
    assert state.values["proposed_action"] == "UNPAUSE svc"
    assert state.values["policy_attempts"] == 1
    assert state.values["denied_actions"] == []
    assert "POLICY REJECTIONS" not in h.plan_prompts[-1]
    assert "STOP svc" not in h.plan_prompts[-1]


# 6. Node error paths --------------------------------------------------------

def test_nodes_return_dicts_on_bad_state() -> None:
    assert ag.check_policy({})["policy_verdict"] == "denied"
    assert ag.escalate_policy_denied({})["status"] == ag.POLICY_ESCALATED_STATUS


# 7. Listener ----------------------------------------------------------------

class Listener:
    def __init__(self, wl: Any, hs: Harness) -> None:
        self.wl = wl
        self.h = hs
        self.payloads: List[Dict[str, Any]] = []
        self.statuses: List[int] = []

    @property
    def messages(self) -> List[str]:
        return [p["content"] for p in self.payloads]


@pytest.fixture
def lst(h: Harness, monkeypatch: pytest.MonkeyPatch) -> Listener:
    monkeypatch.setenv("BASE_URL", "http://dummy.test")
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", WEBHOOK_URL)
    import api.webhook_listener as wl

    monkeypatch.setattr(wl, "sre_graph", h.graph)
    monkeypatch.setattr(wl.time, "sleep", lambda s: None)
    ls = Listener(wl, h)

    def fake_post(url: str, json: Any = None, timeout: Any = None) -> MagicMock:
        ls.payloads.append(json)
        code = ls.statuses.pop(0) if ls.statuses else 204
        return MagicMock(status_code=code)

    monkeypatch.setattr(wl.requests, "post", fake_post)
    return ls


def _assert_no_approval(text: str) -> None:
    assert "/approve" not in text
    assert "/reject" not in text
    assert "token=" not in text


def test_policy_denied_run_sends_one_escalation_message(lst: Listener) -> None:
    lst.h.plans = ["STOP svc", "RESTART sre_agent", "EXEC svc ls; curl x"]
    lst.wl.run_agent_workflow({"service": SERVICE}, "t1")
    assert len(lst.payloads) == 1
    _assert_no_approval(lst.messages[0])
    assert lst.payloads[0]["allowed_mentions"] == {"parse": []}
    assert len(lst.messages[0]) < 2000
    assert lst.h.exec_calls == 0


def test_verification_escalation_end_sends_escalation(lst: Listener) -> None:
    lst.h.plans = ["PAUSE svc"] * 3
    lst.h.allowed_cmds = {"PAUSE svc"}
    lst.wl.run_agent_workflow({"service": SERVICE}, "t1")
    for _ in range(3):
        lst.h.graph.update_state(lst.h.config(), {"hitl_decision": "approved"})
        lst.wl.resume_agent_workflow("t1")
    state = lst.h.graph.get_state(lst.h.config())
    assert state.values["status"] == "Escalated to Human Operator"
    assert len(lst.payloads) == 4  # 3 approval requests + 1 escalation
    _assert_no_approval(lst.messages[-1])
    assert lst.payloads[-1]["allowed_mentions"] == {"parse": []}
    assert "token=" in lst.messages[0]


def test_notify_escalation_retries_once_on_500(lst: Listener) -> None:
    lst.statuses = [500, 204]
    assert lst.wl.notify_escalation("t", "svc", "policy_denied", {"status": "x"}) is True
    assert len(lst.payloads) == 2
    lst.payloads.clear()
    lst.statuses = [500, 500, 204]
    assert lst.wl.notify_escalation("t", "svc", "policy_denied", {"status": "x"}) is False
    assert len(lst.payloads) == 2


def test_notify_escalation_has_no_approval_link(lst: Listener) -> None:
    lst.wl.notify_escalation("t", "svc", "verification_failed", {"status": "Escalated to Human Operator"})
    _assert_no_approval(lst.messages[0])


def test_build_escalation_content_bounded(lst: Listener) -> None:
    denied = [{"command": f"EXEC svc {'x' * 400}", "reason": "reason " * 100} for _ in range(50)]
    text = lst.wl.build_escalation_content("s" * 500, "t" * 500, "Escalated - Policy Denied", denied, "p" * 900)
    assert len(text) < 2000
    _assert_no_approval(text)


def test_approval_message_contains_policy_reason(lst: Listener) -> None:
    lst.h.plans = ["PAUSE svc"]
    lst.h.allowed_cmds = {"PAUSE svc"}
    lst.wl.run_agent_workflow({"service": SERVICE}, "t1")
    assert len(lst.payloads) == 1
    assert JUDGE_REASON in lst.messages[0]
    assert "token=" in lst.messages[0]


def test_notify_discord_four_positional_args_without_policy_reason(
    lst: Listener, monkeypatch: pytest.MonkeyPatch
) -> None:
    lst.h.plans = ["PAUSE svc"]
    lst.h.allowed_cmds = {"PAUSE svc"}
    lst.wl.run_agent_workflow({"service": SERVICE}, "t0")  # reach a paused state
    lst.h.graph.update_state(lst.h.config("t0"), {"policy_reason": ""})
    calls: List[Tuple[Tuple[Any, ...], Dict[str, Any]]] = []

    def recorder(*args: Any, **kwargs: Any) -> bool:
        calls.append((args, kwargs))
        return True

    monkeypatch.setattr(lst.wl, "notify_discord", recorder)
    assert lst.wl.pause_and_notify("t0") is True
    assert len(calls) == 1
    assert len(calls[0][0]) == 4
    assert calls[0][1] == {}
