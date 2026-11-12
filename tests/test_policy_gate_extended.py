"""Extended offline tests: hard-block rules, target verification, verdict parsing, helpers, Discord formats."""
from types import SimpleNamespace
from typing import Any, List, Tuple
from unittest.mock import MagicMock

import docker
import pytest

import agent.sre_agent as ag

SERVICE = "svc"
BACKTICK = chr(96)
BACKSLASH = chr(92)
NL = chr(10)


def _boom(*_a: Any, **_k: Any) -> Any:
    raise AssertionError("must not be called")


def _client(name: str = "svc", cid: str = "f00dfeedcafe") -> MagicMock:
    container = MagicMock()
    container.name = name
    container.id = cid
    client = MagicMock()
    client.containers.get.return_value = container
    return client


@pytest.fixture(autouse=True)
def managed_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MANAGED_CONTAINERS", SERVICE)
    monkeypatch.delenv("HOSTNAME", raising=False)
    monkeypatch.setattr(ag.docker, "from_env", MagicMock(return_value=_client()))


@pytest.fixture
def wl(monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setenv("BASE_URL", "http://dummy.test")
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "http://discord.invalid/hook-secret-path")
    import api.webhook_listener as module

    return module


# 1. hard_block_reason ---------------------------------------------------------

@pytest.mark.parametrize("command", [
    "EXEC svc r" + BACKSLASH + "m -rf /",
    "EXEC svc rm -rf " + BACKSLASH + "/",
    "EXEC svc rm -rf .",
    "EXEC svc rm -rf /./",
    "EXEC svc rm -rf /tmp",
    "EXEC svc rm -rf /tmp/../etc",
    "EXEC svc rm -rf /usr/local",
    "EXEC svc /bin/rm -rf /tmp/x",
    "EXEC svc find / -delete",
    "EXEC svc python3 -c x",
    "EXEC svc sh -xc x",
    "EXEC svc truncate -s0 /app/app.py",
    "EXEC svc cat /proc/1/environ",
    "EXEC svc cat .env",
    "EXEC svc cat /run/secrets/x",
    "EXEC svc cat /var/run/docker.sock",
    "EXEC svc rm -rf /tmp/cache/*",
    "EXEC svc ls /tmp/" + "a" * 200,
    "EXEC svc ls" + chr(9) + "/tmp",
    "EXEC svc ls /tmp/caf" + chr(233),
    "STOP svc",
    "RESTART other",
    "RESTART sre_agent",
])
def test_hard_block_extended_denials(command: str) -> None:
    assert ag.hard_block_reason(command, SERVICE)


@pytest.mark.parametrize("command", [
    "RESTART svc", "PAUSE svc", "EXEC svc ls /tmp/cache", "EXEC svc rm -rf /tmp/cache", "EXEC svc df -h",
])
def test_hard_block_extended_allowed(command: str) -> None:
    assert ag.hard_block_reason(command, SERVICE) is None


def test_hard_block_target_prefix_of_hostname_denied(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MANAGED_CONTAINERS", "svc,abc")
    monkeypatch.setenv("HOSTNAME", "abcdef123456")
    reason = ag.hard_block_reason("RESTART abc", "abc")
    assert reason and "own container" in reason


# 2. _verify_managed_target ----------------------------------------------------

def test_verify_managed_target_ok() -> None:
    assert ag._verify_managed_target("RESTART svc") is None


def test_verify_managed_target_alias_to_unmanaged_denied(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ag.docker, "from_env", MagicMock(return_value=_client(name="other")))
    reason = ag._verify_managed_target("RESTART svc")
    assert reason and "other" in reason


@pytest.mark.parametrize("exc", [docker.errors.NotFound("nope"), docker.errors.DockerException("daemon down")])
def test_verify_managed_target_docker_errors_denied(monkeypatch: pytest.MonkeyPatch, exc: Exception) -> None:
    client = MagicMock()
    client.containers.get.side_effect = exc
    monkeypatch.setattr(ag.docker, "from_env", MagicMock(return_value=client))
    assert ag._verify_managed_target("RESTART svc")


def test_verify_managed_target_id_matching_hostname_denied(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ag.docker, "from_env", MagicMock(return_value=_client(cid="abcdef1234567890")))
    monkeypatch.setenv("HOSTNAME", "abcdef123456")
    assert ag._verify_managed_target("RESTART svc")


def test_evaluate_policy_unresolvable_target_denied_before_judge(monkeypatch: pytest.MonkeyPatch) -> None:
    client = MagicMock()
    client.containers.get.side_effect = docker.errors.NotFound("nope")
    monkeypatch.setattr(ag.docker, "from_env", MagicMock(return_value=client))
    monkeypatch.setattr(ag, "retrieve_policy", _boom)
    monkeypatch.setattr(ag, "judge_command", _boom)
    assert ag.evaluate_policy("RESTART svc", SERVICE)[0] == "denied"


# 3. parse_judge_verdict / judge_command -----------------------------------------

@pytest.mark.parametrize("text, expected", [
    ("**ALLOW**: ok", ("allowed", "ok")),
    ("```" + NL + "ALLOW: ok" + NL + "```", ("allowed", "ok")),
    (BACKTICK + "DENY: no" + BACKTICK, ("denied", "no")),
])
def test_parse_judge_verdict_tolerates_markup(text: str, expected: Tuple[str, str]) -> None:
    assert ag.parse_judge_verdict(text) == expected


@pytest.mark.parametrize("text", [
    "ALLOW: ok" + NL + "extra", "ALLOW: ok DENY", "garbage ~!", "```", "```" + NL + "```",
])
def test_parse_judge_verdict_strict_denials(text: str) -> None:
    assert ag.parse_judge_verdict(text)[0] == "denied"


def test_judge_command_prompt_delimiters_and_clause_order(monkeypatch: pytest.MonkeyPatch) -> None:
    prompts: List[str] = []

    class Llm:
        def invoke(self, messages: List[Any]) -> Any:
            prompts.append(messages[-1].content)
            return SimpleNamespace(content="ALLOW: ok")

    monkeypatch.setattr(ag, "get_llm", lambda: Llm())
    ids = ["POL-001", "POL-003", "POL-007", "POL-005", "POL-005", "POL-006", "POL-002"]
    docs = [SimpleNamespace(page_content=f"clause {i}", metadata={"policy_id": i}) for i in ids]
    assert ag.judge_command("EXEC svc ls /tmp", SERVICE, ag.build_clauses(docs)) == ("allowed", "ok")
    prompt = prompts[0]
    assert "<<<COMMAND>>>" + NL + "EXEC svc ls /tmp" + NL + "<<<END>>>" in prompt
    order = ("POL-005", "POL-006", "POL-007", "POL-003", "POL-001", "POL-002")
    positions = [prompt.index(f"clause {i}") for i in order]
    assert positions == sorted(positions)
    assert prompt.count("clause POL-005") == 1


# 4. helpers ---------------------------------------------------------------------

def test_policy_query_uses_verb_and_program_only() -> None:
    assert ag._policy_query("EXEC svc rm -rf /tmp/x") == "EXEC rm"
    assert ag._policy_query("RESTART svc") == "RESTART"
    assert ag._policy_query("") == ""


def test_pick_command() -> None:
    assert ag._pick_command("think" + NL + "RESTART a" + NL + "EXEC a ls" + NL + "thanks") == "EXEC a ls"
    assert ag._pick_command("hello" + NL + "world") == "world"
    assert ag._pick_command("") == ""
    assert ag._pick_command(" " + NL + " ") == ""
    assert ag._pick_command(BACKTICK + "RESTART svc" + BACKTICK) == "RESTART svc"


def test_denied_entry_handles_dict_and_legacy_string() -> None:
    assert ag._denied_entry({"command": "a", "reason": "r"}) == ("a", "r")
    assert ag._denied_entry("a | r") == ("a", "r")
    assert ag._denied_entry("only-cmd") == ("only-cmd", "")
    text = ag.format_policy_rejections(["legacy-cmd | legacy-why"])
    assert "legacy-cmd" in text and "legacy-why" in text


def test_escalate_policy_denied_swallows_exceptions() -> None:
    class Bad(dict):
        def get(self, *_a: Any, **_k: Any) -> Any:
            raise RuntimeError("boom")

    out = ag.escalate_policy_denied(Bad())  # type: ignore[arg-type]
    assert out == {"status": ag.POLICY_ESCALATED_STATUS}


# 5. Discord message formats -------------------------------------------------------

def test_discord_content_policy_line_is_advisory_code_span(wl: Any) -> None:
    reason = "fine " + BACKTICK + "x" + BACKTICK + " @everyone" + NL + "next"
    text = wl.build_discord_content("t", "svc", "RESTART svc", "http://a", "http://b", policy_reason=reason)
    line = next(ln for ln in text.splitlines() if "advisory" in ln.lower())
    assert "fine" in line and "@everyone" in line and "next" in line
    assert line.count(BACKTICK) == 2 and line.rstrip().endswith(BACKTICK)


def test_escalation_content_dict_and_legacy_entries(wl: Any) -> None:
    denied = [{"command": "STOP svc", "reason": "stop forbidden"}, "RESTART other | not managed"]
    text = wl.build_escalation_content("svc", "t1", "Escalated - Policy Denied", denied, "")
    for needle in ("STOP svc", "stop forbidden", "RESTART other", "not managed", "advisory"):
        assert needle in text
    assert "/approve" not in text and "token=" not in text


def test_escalation_reason_stays_inside_code_span(wl: Any) -> None:
    reason = "bad " + BACKTICK + "tick" + NL + "@everyone ping"
    denied = [{"command": "EXEC svc cat x", "reason": reason}]
    text = wl.build_escalation_content("svc", "t1", "Escalated - Policy Denied", denied, "")
    line = next(ln for ln in text.splitlines() if "advisory" in ln.lower())
    assert "tick" in line and "@everyone" in line and "ping" in line
    assert line.count(BACKTICK) == 2 and line.rstrip().endswith(BACKTICK)
    empty = wl.build_escalation_content(
        "svc", "t1", "Escalated - Policy Denied", [{"command": "STOP svc", "reason": ""}], "")
    assert "advisory" not in empty.lower()
