"""Offline tests for the judge PRE-CHECK block (precheck_description / judge_command / evaluate_policy)."""
from typing import Any, List
from unittest.mock import MagicMock

import pytest

import agent.sre_agent as ag

SERVICE = "svc"
PASSING = "EXEC svc rm -rf /tmp/cache"
BLOCKED = "EXEC svc rm -rf /app"


@pytest.fixture(autouse=True)
def managed_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MANAGED_CONTAINERS", SERVICE)
    monkeypatch.delenv("HOSTNAME", raising=False)
    client = MagicMock()
    client.containers.get.side_effect = lambda t: MagicMock(**{"name": t}, id="f00d" * 16)
    monkeypatch.setattr(ag.docker, "from_env", MagicMock(return_value=client))


class FakeLlm:
    def __init__(self, text: str = "ALLOW: fine") -> None:
        self.text = text
        self.prompts: List[str] = []

    def invoke(self, messages: List[Any]) -> Any:
        self.prompts.append(str(messages[-1].content))
        return MagicMock(content=self.text)


@pytest.fixture
def llm(monkeypatch: pytest.MonkeyPatch) -> FakeLlm:
    fake = FakeLlm()
    monkeypatch.setattr(ag, "get_llm", lambda *a, **k: fake)
    return fake


# 1. precheck_description ----------------------------------------------------

def test_precheck_rm_under_tmp() -> None:
    text = ag.precheck_description(PASSING)
    assert "/tmp/" in text and "POL-004" in text


def test_precheck_read_tool() -> None:
    text = ag.precheck_description("EXEC svc tail -n 50 /var/log/app.log")
    assert "tail" in text and "/var/log" in text and "POL-004" in text


def test_precheck_ps() -> None:
    text = ag.precheck_description("EXEC svc ps aux")
    assert "ps" in text and "POL-004" in text


def test_precheck_ps_mentions_no_paths() -> None:
    text = ag.precheck_description("EXEC svc ps aux")
    assert "/var/log" not in text and "/tmp" not in text and "/app" not in text


@pytest.mark.parametrize("verb,pol", [("RESTART", "POL-001"), ("PAUSE", "POL-002"), ("UNPAUSE", "POL-002")])
def test_precheck_lifecycle(verb: str, pol: str) -> None:
    text = ag.precheck_description(f"{verb} svc")
    assert pol in text and verb in text
    other = "POL-002" if pol == "POL-001" else "POL-001"
    assert other not in text


# 2-3, 5. judge_command ------------------------------------------------------

def test_judge_prompt_with_precheck(llm: FakeLlm) -> None:
    assert ag.hard_block_reason(PASSING, SERVICE) is None
    verdict, _ = ag.judge_command(PASSING, SERVICE, "POL-004 clause")
    assert verdict == "allowed"
    prompt = llm.prompts[0]
    assert "PRE-CHECK RESULT" in prompt
    assert "When unsure, DENY" not in prompt
    block = prompt.split("PRE-CHECK RESULT", 1)[1].split("<<<COMMAND>>>", 1)[0]
    assert "ALLOW: <" in block
    assert "answer ALLOW." not in block
    assert "<<<COMMAND>>>\n" + PASSING + "\n<<<END>>>" in prompt


def test_judge_prompt_hard_blocked_has_no_precheck(llm: FakeLlm) -> None:
    assert ag.hard_block_reason(BLOCKED, SERVICE)
    ag.judge_command(BLOCKED, SERVICE, "POL-004 clause")
    prompt = llm.prompts[0]
    assert "PRE-CHECK RESULT" not in prompt
    assert "When unsure, DENY" in prompt
    assert "<<<COMMAND>>>\n" + BLOCKED + "\n<<<END>>>" in prompt


# 4. evaluate_policy ---------------------------------------------------------

def test_evaluate_policy_hard_block_never_calls_judge_or_llm(monkeypatch: pytest.MonkeyPatch) -> None:
    judge, get_llm, retrieve = MagicMock(), MagicMock(), MagicMock()
    monkeypatch.setattr(ag, "judge_command", judge)
    monkeypatch.setattr(ag, "get_llm", get_llm)
    monkeypatch.setattr(ag, "retrieve_policy", retrieve)
    verdict, reason = ag.evaluate_policy(BLOCKED, SERVICE)
    assert verdict == "denied" and reason.startswith("hard block")
    judge.assert_not_called()
    get_llm.assert_not_called()
    retrieve.assert_not_called()


# Discord wording --------------------------------------------------------------

@pytest.fixture
def wl(monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setenv("BASE_URL", "http://dummy.test")
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "http://discord.invalid/hook-secret-path")
    import api.webhook_listener as module

    return module


def test_discord_content_with_policy_reason(wl: Any) -> None:
    text = wl.build_discord_content("t", "svc", "RESTART svc", "http://a", "http://b", policy_reason="no freeze")
    assert "Static allowlist:" in text
    assert "Policy judge (advisory):" in text
    assert "A human must still review" in text
    assert "no freeze" in text
    assert "\u2705" not in text


def test_discord_content_without_policy_reason(wl: Any) -> None:
    text = wl.build_discord_content("t", "svc", "RESTART svc", "http://a", "http://b")
    for phrase in ("Static allowlist:", "Policy judge", "A human must still review", "\u2705"):
        assert phrase not in text


def test_discord_policy_reason_clipped_and_backticks_neutralised(wl: Any) -> None:
    reason = "x" * 500 + chr(96) + "@everyone"
    text = wl.build_discord_content("t", "svc", "RESTART svc", "http://a", "http://b", policy_reason=reason)
    assert "x" * 201 not in text
    assert "@everyone" not in text
    reason2 = "a" + chr(96) + "b"
    text2 = wl.build_discord_content("t", "svc", "RESTART svc", "http://a", "http://b", policy_reason=reason2)
    assert "a'b" in text2 and "a" + chr(96) + "b" not in text2
