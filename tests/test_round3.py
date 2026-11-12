"""Round-3 offline tests: runbook query, planner repeat-avoidance, graph order, runbook seeding."""
from types import SimpleNamespace
from typing import Any, Dict, List, Set
from unittest.mock import MagicMock

import pytest
from langgraph.checkpoint.memory import MemorySaver

import agent.sre_agent as ag
from scripts import setup_runbooks as sr

SVC = "dummy_prod_api"
NL = chr(10)


# 1. _build_runbook_query / retrieve_context -------------------------------------

def test_query_includes_service_alert_and_signal_lines() -> None:
    logs = NL.join(["INFO ok", "ERROR db write failed", "INFO ok again", "OSError: No space left on device"])
    state = {"service_name": SVC, "container_logs": logs,
             "alert_payload": {"severity": "CRITICAL", "description": "disk problem"}}
    query = ag._build_runbook_query(state)
    for needle in (SVC, "CRITICAL", "disk problem", "ERROR db write failed", "No space left"):
        assert needle in query
    assert "INFO ok" not in query


def test_query_caps_log_text() -> None:
    logs = NL.join(f"ERROR line {i:03d} " + "x" * 80 for i in range(40))
    query = ag._build_runbook_query({"service_name": SVC, "container_logs": logs, "alert_payload": {}})
    assert len(query) < 500 + 100
    assert "line 039" in query  # newest lines win
    assert "line 000" not in query


def test_query_falls_back_to_last_lines_when_nothing_matches() -> None:
    logs = NL.join(["alpha", "bravo", "charlie"])
    query = ag._build_runbook_query({"service_name": SVC, "container_logs": logs, "alert_payload": {}})
    assert "alpha" in query and "charlie" in query


def test_query_ignores_non_string_alert_fields() -> None:
    alert = {"severity": 5, "description": ["x"], "summary": None, "title": {"a": 1}, "message": "real message"}
    query = ag._build_runbook_query({"service_name": SVC, "alert_payload": alert})
    assert "real message" in query
    assert "['x']" not in query and "None" not in query


def test_query_handles_empty_state() -> None:
    assert SVC in ag._build_runbook_query({})


def test_retrieve_context_uses_k3_and_built_query(monkeypatch: pytest.MonkeyPatch) -> None:
    store = MagicMock()
    store.similarity_search.return_value = [SimpleNamespace(page_content="rb1"), SimpleNamespace(page_content="rb2")]
    monkeypatch.setattr(ag, "get_vector_store", lambda: store)
    state = {"service_name": SVC, "container_logs": "ERROR boom", "alert_payload": {"severity": "HIGH"}}
    out = ag.retrieve_context(state)
    expected = ag._build_runbook_query(state)
    store.similarity_search.assert_called_once_with(expected, k=3)
    assert out["runbook_context"] == "rb1" + NL + "---" + NL + "rb2"


# 2. Graph order --------------------------------------------------------------------

def test_graph_edges_order_and_retry_reentry() -> None:
    graph = ag.create_sre_graph(MemorySaver())
    edges = {(e.source, e.target) for e in graph.get_graph().edges}
    assert ("parse_alert", "investigate_logs") in edges
    assert ("investigate_logs", "retrieve_context") in edges
    assert ("retrieve_context", "plan_mitigation") in edges
    assert ("verify_recovery", "investigate_logs") in edges  # retry re-enters at investigate_logs
    assert ("parse_alert", "retrieve_context") not in edges
    assert ("investigate_logs", "plan_mitigation") not in edges


# 3. plan_mitigation repeat avoidance -------------------------------------------------

class ScriptedLlm:
    def __init__(self, replies: List[str]) -> None:
        self.replies = list(replies)
        self.prompts: List[str] = []

    def invoke(self, messages: List[Any]) -> Any:
        self.prompts.append(messages[-1].content)
        return SimpleNamespace(content=self.replies.pop(0))


def _plan(monkeypatch: pytest.MonkeyPatch, replies: List[str], denied: List[Any]) -> Any:
    llm = ScriptedLlm(replies)
    monkeypatch.setattr(ag, "get_llm", lambda: llm)
    out = ag.plan_mitigation({"service_name": SVC, "denied_actions": denied, "alert_payload": {}})
    return out, llm


def test_plan_no_denials_single_call(monkeypatch: pytest.MonkeyPatch) -> None:
    out, llm = _plan(monkeypatch, [f"RESTART {SVC}"], [])
    assert out["proposed_action"] == f"RESTART {SVC}"
    assert len(llm.prompts) == 1


@pytest.mark.parametrize("denied_entry", [
    {"command": f"EXEC {SVC} ls /tmp", "reason": "no"},
    {"command": f"exec   {SVC}  ls /tmp ", "reason": "no"},
    f"EXEC {SVC} ls /tmp | no",
])
def test_plan_denied_command_triggers_one_reask(monkeypatch: pytest.MonkeyPatch, denied_entry: Any) -> None:
    out, llm = _plan(monkeypatch, [f"EXEC {SVC} ls /tmp", f"PAUSE {SVC}"], [denied_entry])
    assert out["proposed_action"] == f"PAUSE {SVC}"
    assert len(llm.prompts) == 2
    assert "DENIED" in llm.prompts[1] and "DIFFERENT" in llm.prompts[1]
    assert "DIFFERENT" not in llm.prompts[0]


def test_plan_repeat_on_second_call_falls_back_to_restart(monkeypatch: pytest.MonkeyPatch) -> None:
    denied = [{"command": f"EXEC {SVC} ls /tmp", "reason": "no"}]
    out, llm = _plan(monkeypatch, [f"EXEC {SVC} ls /tmp", f"EXEC {SVC} ls /tmp"], denied)
    assert out["proposed_action"] == f"RESTART {SVC}"
    assert len(llm.prompts) == 2


def test_plan_keeps_repeat_when_restart_also_denied(monkeypatch: pytest.MonkeyPatch) -> None:
    denied = [{"command": f"EXEC {SVC} ls /tmp", "reason": "no"}, {"command": f"RESTART {SVC}", "reason": "no"}]
    out, llm = _plan(monkeypatch, [f"EXEC {SVC} ls /tmp", f"EXEC {SVC} ls /tmp"], denied)
    assert out["proposed_action"] == f"EXEC {SVC} ls /tmp"
    assert len(llm.prompts) == 2  # exactly two calls, no loop


def test_normalize_command() -> None:
    assert ag._normalize_command("  restart   svc ") == "RESTART svc"
    assert ag._normalize_command("") == ""


# 4. setup_runbooks -----------------------------------------------------------------

class FakeStore:
    def __init__(self, ids: List[str]) -> None:
        self.docs: Dict[str, str] = {i: "old" for i in ids}
        self.deleted: List[str] = []

    def get(self) -> Dict[str, Any]:
        return {"ids": list(self.docs)}

    def delete(self, ids: List[str]) -> None:
        self.deleted.extend(ids)
        for i in ids:
            self.docs.pop(i, None)

    def add_texts(self, texts: List[str], metadatas: List[Dict[str, Any]], ids: List[str]) -> None:
        assert len(texts) == len(metadatas) == len(ids)
        for i, t in zip(ids, texts):
            assert i not in self.docs, "duplicate id would be rejected / overwritten"
            self.docs[i] = t


def _expected_ids() -> Set[str]:
    return {f"runbook_{d['id']}" for d in sr.RUNBOOK_DOCUMENTS}


def test_runbooks_have_four_unique_stable_ids() -> None:
    assert len(sr.RUNBOOK_DOCUMENTS) == 4
    assert len(_expected_ids()) == 4


def test_inject_ids_stable_regardless_of_order(monkeypatch: pytest.MonkeyPatch) -> None:
    first = FakeStore([])
    sr.inject_runbooks(first)  # type: ignore[arg-type]
    monkeypatch.setattr(sr, "RUNBOOK_DOCUMENTS", list(reversed(sr.RUNBOOK_DOCUMENTS)))
    second = FakeStore([])
    sr.inject_runbooks(second)  # type: ignore[arg-type]
    assert first.docs == second.docs
    assert set(first.docs) == _expected_ids()


def test_inject_deletes_stale_runbook_ids_but_not_foreign_ids() -> None:
    store = FakeStore(["runbook_old_removed", "runbook_0", "other_collection_doc"])
    sr.inject_runbooks(store)  # type: ignore[arg-type]
    assert "runbook_old_removed" in store.deleted and "runbook_0" in store.deleted
    assert "other_collection_doc" not in store.deleted
    assert store.docs["other_collection_doc"] == "old"
    assert set(store.docs) == _expected_ids() | {"other_collection_doc"}


def test_inject_is_idempotent() -> None:
    store = FakeStore([])
    sr.inject_runbooks(store)  # type: ignore[arg-type]
    snapshot = dict(store.docs)
    sr.inject_runbooks(store)  # type: ignore[arg-type]
    sr.inject_runbooks(store)  # type: ignore[arg-type]
    assert store.docs == snapshot
    assert len(store.docs) == 4


def _doc(doc_id: str) -> str:
    return next(d["content"] for d in sr.RUNBOOK_DOCUMENTS if d["id"] == doc_id)


def test_disk_runbook_mentions_cache_cleanup_command() -> None:
    assert "rm -rf /tmp/cache" in _doc("dummy_prod_api_disk_cache_full")


def test_dummy_prod_api_runbook_commands_pass_hard_block(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MANAGED_CONTAINERS", raising=False)
    monkeypatch.delenv("HOSTNAME", raising=False)
    import re

    pattern = re.compile(r"(EXEC dummy_prod_api [\w./= -]+?(?=\s+or\s|\.(?:\s|$)|$)|RESTART dummy_prod_api)")
    checked = 0
    for doc in sr.RUNBOOK_DOCUMENTS:
        if doc["metadata"]["service"] != SVC:
            continue
        for match in pattern.findall(doc["content"]):
            command = match.strip().rstrip(".")
            assert ag.hard_block_reason(command, SVC) is None, command
            checked += 1
    assert checked >= 4
