"""Граф агента на поддельной модели (М4): маршруты, инварианты, бюджеты, подтверждение.

Модель и кластер подменены: проверяется то, что делает код, а не то, что ответит LLM.
"""

from __future__ import annotations

import json
import time

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from k8s_incident_agent.graph import REFUSAL, Agent, build_graph
from k8s_incident_agent.llm import LLMError
from k8s_incident_agent.schemas import Hypotheses, PatchProposal, Plan, ReviewVerdict, Route
from k8s_incident_agent.tools.schemas import ToolResult

S1_PATCH = {"spec": {"template": {"spec": {"containers": [{"name": "api", "env": [
    {"name": "DB_PASSWORD", "valueFrom": {"secretKeyRef": {"name": "payments-db",
                                                           "key": "password"}}}]}]}}}}


def tool_call(tool: str, /, **args) -> dict:
    return {"id": f"c-{tool}", "type": "function",
            "function": {"name": tool, "arguments": json.dumps(args)}}


class FakeLLM:
    """Ответы по схеме (structured) и очередь ответов диагноста (chat с инструментами)."""

    def __init__(self, route="diagnose", chat=None, hypotheses=None, verdicts=None,
                 patch=S1_PATCH, fail: set[str] | None = None):
        self.route = route
        default = [{"tool_calls": [tool_call("k8s_list_pods", namespace="payments")]},
                   {"tool_calls": [tool_call("k8s_get_manifest", kind="Deployment", name="api",
                                             namespace="payments")]},
                   {"content": "фактов достаточно"}]
        self.chat_replies = list(chat if chat is not None else default)
        self.hypotheses = hypotheses
        self.verdicts = list(verdicts or ["ok"])
        self.patch = patch
        self.fail = fail or set()
        self.tool_turns = 0
        self.target = "api"

    def chat(self, messages, usage, tools=None):
        if "chat" in self.fail:
            raise LLMError("таймаут шлюза")
        if tools:
            self.tool_turns += 1
        return self.chat_replies.pop(0) if self.chat_replies else {"content": "готово"}

    def structured(self, messages, schema, usage, free_form=False):
        if schema.__name__ in self.fail:
            raise LLMError("таймаут шлюза")
        if schema is Route:
            return Route(route=self.route, reason="тест")
        if schema is Plan:
            return Plan(steps=[{"goal": "состояние подов", "tool_hint": "k8s_list_pods"}])
        if schema is Hypotheses:
            return Hypotheses(hypotheses=self.hypotheses or [{
                "id": "h-1", "statement": "нет DB_PASSWORD", "evidence_ids": ["ev-001"],
                "confidence": "high"}])
        if schema is ReviewVerdict:
            verdict = self.verdicts.pop(0) if len(self.verdicts) > 1 else self.verdicts[0]
            return ReviewVerdict(verdict=verdict, gaps=[] if verdict == "ok" else ["логи"])
        if schema is PatchProposal:
            patch = None if self.patch is None else {
                "kind": "Deployment", "name": self.target, "namespace": "payments",
                "patch": self.patch,
                "rationale": "вернуть ссылку на секрет", "evidence_ids": ["ev-001"]}
            return PatchProposal(patch=patch, recommendation="добавить DB_PASSWORD из Secret")
        raise AssertionError(schema)


def fake_list_pods(namespace=None, selector=None, *, store, config):
    evidence = store.add("k8s_list_pods", {"namespace": namespace}, "api CrashLoopBackOff")
    return ToolResult(tool="k8s_list_pods", status="ok", evidence_id=evidence.id,
                      summary=evidence.summary, data={"pods": []})


# Манифест так, как его отдаёт k8s_get_manifest: api без DB_PASSWORD, но Secret payments-db
# виден — на него ссылается DB_USER. Из него граф берёт uid/generation и имена контейнеров.
API_MANIFEST = """kind: Deployment
metadata: {name: api, namespace: payments, uid: u-1, generation: 3}
spec:
  template:
    spec:
      containers:
      - name: api
        env:
        - name: DB_USER
          valueFrom: {secretKeyRef: {name: payments-db, key: username}}
"""


def fake_get_manifest(kind, name, namespace=None, *, store, config):
    evidence = store.add("k8s_get_manifest", {"kind": kind, "name": name, "namespace": namespace},
                         f"манифест {kind} {name}", raw=API_MANIFEST)
    return ToolResult(tool="k8s_get_manifest", status="ok", evidence_id=evidence.id,
                      summary=evidence.summary, data={"manifest_yaml": API_MANIFEST})


def run(llm: FakeLLM, config, resume: str | None = None):
    applied: list = []

    def apply_patch(*args, **kwargs):
        applied.append((*args, kwargs.get("expected")))
        return {"status": "applied", "detail": "ok"}

    agent = Agent(llm, config, tools={"k8s_list_pods": fake_list_pods,
                                      "k8s_get_manifest": fake_get_manifest},
                  apply_patch=apply_patch)
    graph = build_graph(agent).compile(checkpointer=InMemorySaver())
    cfg = {"configurable": {"thread_id": "t"}}
    graph.invoke({"query": "под api падает", "started": time.time(), "evidence": []}, cfg)
    interrupted = bool(graph.get_state(cfg).interrupts)
    if resume is not None and interrupted:
        graph.invoke(Command(resume=resume), cfg)
    return graph.get_state(cfg).values, applied, interrupted


def test_out_of_scope_refuses_without_any_tool_call(config):
    """S6: вежливый отказ, ни одного вызова инструментов кластера."""
    llm = FakeLLM(route="out_of_scope")
    state, applied, _ = run(llm, config)
    assert state["report"] == REFUSAL
    assert llm.tool_turns == 0 and not state.get("evidence") and not applied


def test_patch_waits_for_human_and_rejection_applies_nothing(config):
    state, applied, interrupted = run(FakeLLM(), config, resume="rejected")
    assert interrupted
    assert state["approval_status"] == "rejected"
    assert applied == []
    assert "rejected" in state["report"]


def test_approval_applies_exactly_the_proposed_patch(config):
    state, applied, _ = run(FakeLLM(), config, resume="approved")
    # uid/generation — из прочитанного манифеста: исполнитель сверит, что объект тот же.
    assert applied == [("Deployment", "api", "payments", S1_PATCH,
                        {"uid": "u-1", "generation": 3})]
    assert state["execution_result"]["status"] == "applied"


def test_graph_stops_at_interrupt_without_resume(config):
    """Без ответа человека граф стоит на approval: исполнитель не вызван (ISOLATION §10 №7)."""
    state, applied, interrupted = run(FakeLLM(), config)
    assert interrupted and applied == [] and "approval_status" not in state


def test_diagnostician_cannot_call_patch_tool(config):
    """ISOLATION §10 №2: k8s_patch у диагноста не зарегистрирован — отказ, а не вызов."""
    llm = FakeLLM(chat=[{"tool_calls": [tool_call("k8s_patch", kind="Deployment", name="api",
                                                  namespace="payments", patch={})]},
                        {"content": "понял"}], patch=None)
    state, applied, _ = run(llm, config)
    assert not state.get("evidence") and applied == []


def test_tool_budget_is_a_hard_ceiling(config):
    endless = [{"tool_calls": [tool_call("k8s_list_pods", namespace="payments")]}] * 50
    state, _, _ = run(FakeLLM(chat=endless, patch=None), config)
    assert state["tool_calls"] == config.budget.max_tool_calls
    assert state["budget_exhausted"] is True
    assert "бюджет исчерпан" in state["report"]


def test_review_loop_stops_at_ceiling(config):
    """F5: ревьюер может вернуть задачу не больше max_review_iterations раз."""
    llm = FakeLLM(verdicts=["needs_more_facts"], patch=None)
    state, _, _ = run(llm, config)
    assert state["review_iterations"] == config.budget.max_review_iterations


def test_hypothesis_without_known_evidence_is_dropped(config):
    """F4: гипотеза со ссылкой на несуществующее свидетельство не принимается кодом."""
    llm = FakeLLM(hypotheses=[{"id": "h-1", "statement": "выдумка", "evidence_ids": ["ev-999"],
                               "confidence": "high"}], patch=None)
    state, _, _ = run(llm, config)
    assert state["hypotheses"] == []
    assert "Не установлена" in state["report"]


def test_patch_outside_allow_list_is_not_offered(config):
    llm = FakeLLM(patch={"spec": {"template": {"spec": {"hostPID": True}}}})
    state, applied, interrupted = run(llm, config, resume="approved")
    assert not interrupted and applied == []
    assert state["proposed_patch"] is None
    assert "hostPID" in state["recommendation"]


@pytest.mark.parametrize("failing", ["Route", "Plan", "chat", "Hypotheses", "ReviewVerdict",
                                     "PatchProposal"])
def test_gateway_failure_degrades_to_report(config, failing):
    """ARCHITECTURE §8: сбой шлюза в любом узле — отчёт с пометкой, а не исключение."""
    state, applied, _ = run(FakeLLM(fail={failing}), config, resume="rejected")
    assert "модель не ответила" in state["report"]
    assert applied == []


def test_namespace_must_be_explicit(config):
    """Без namespace инструмент не вызывается: иначе тихо подставился бы default."""
    llm = FakeLLM(chat=[{"tool_calls": [tool_call("k8s_list_pods")]},
                        {"tool_calls": [tool_call("k8s_list_pods", namespace="payments")]},
                        {"content": "готово"}], patch=None)
    state, _, _ = run(llm, config)
    assert [e["args"]["namespace"] for e in state["evidence"]] == ["payments"]


def test_failed_attempts_spend_budget(config):
    """Ревью М4: вызов с ошибкой в аргументах тоже тратит бюджет — иначе цикл бесконечен."""
    broken = [{"tool_calls": [tool_call("k8s_list_pods")]}] * 50
    state, _, _ = run(FakeLLM(chat=broken, patch=None), config)
    assert state["tool_calls"] == config.budget.max_tool_calls
    assert state["budget_exhausted"] is True and not state.get("evidence")


@pytest.mark.parametrize(("name", "patch", "reason"), [
    ("checkout", S1_PATCH, "не читался"),
    ("api", {"spec": {"template": {"spec": {"containers": [{"name": "sidecar", "env": [
        {"name": "A", "value": "1"}]}]}}}}, "нет контейнеров"),
    ("api", {"spec": {"template": {"spec": {"containers": [{"name": "api", "env": [
        {"name": "A", "valueFrom": {"secretKeyRef": {"name": "admin-token", "key": "t"}}}]}]}}}},
     "admin-token"),
], ids=["unobserved-object", "unknown-container", "unseen-secret"])
def test_patch_must_be_grounded_in_evidence(config, name, patch, reason):
    """Ревью М4: белого списка мало — патч касается только увиденного объекта и Secret."""
    llm = FakeLLM(patch=patch)
    llm.target = name
    state, applied, interrupted = run(llm, config, resume="approved")
    assert not interrupted and applied == [] and state["proposed_patch"] is None
    assert reason in state["recommendation"]
