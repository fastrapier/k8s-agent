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
from k8s_incident_agent.memory import MemoryStore
from k8s_incident_agent.schemas import (
    Hypotheses,
    IncidentRecord,
    PatchProposal,
    Plan,
    ReviewVerdict,
    Route,
)
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
                 patch=S1_PATCH, fail: set[str] | None = None, similar=None):
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
        self.similar = similar or []
        self.tool_turns = 0
        self.target = "api"

    def chat(self, messages, usage, tools=None):
        if "chat" in self.fail:
            raise LLMError("таймаут шлюза")
        if tools:
            self.tool_turns += 1
        return self.chat_replies.pop(0) if self.chat_replies else {"content": "готово"}

    def structured(self, messages, schema, usage, free_form=False, **_):
        if schema.__name__ in self.fail:
            raise LLMError("таймаут шлюза")
        if schema is Route:
            return Route(route=self.route, reason="тест")
        if schema is Plan:
            return Plan(steps=[{"goal": "состояние подов", "tool_hint": "k8s_list_pods"}])
        if schema is Hypotheses:
            return Hypotheses(hypotheses=self.hypotheses or [{
                "id": "h-1", "statement": "нет DB_PASSWORD", "evidence_ids": ["ev-001"],
                "confidence": "high"}], similar=self.similar)
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


def run(llm: FakeLLM, config, resume: str | None = None, memory: MemoryStore | None = None):
    applied: list = []

    def apply_patch(*args, **kwargs):
        applied.append((*args, kwargs.get("expected")))
        return {"status": "applied", "detail": "ok"}

    agent = Agent(llm, config, tools={"k8s_list_pods": fake_list_pods,
                                      "k8s_get_manifest": fake_get_manifest},
                  apply_patch=apply_patch, memory=memory)
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


# --- Память (М6: F8, S5) ---------------------------------------------------------------------

PAST = IncidentRecord(
    namespace="payments", kinds=["Deployment"], objects=["payments/api"],
    symptoms="под api падает в CrashLoopBackOff", root_cause="у api пропала переменная DB_PASSWORD",
    fix="вернули env DB_PASSWORD из secretKeyRef", fix_applied=True, outcome="resolved",
    evidence_ids=["ev-003"])


def test_applied_fix_is_remembered_and_reported(config):
    """F8: после применённого и проверенного фикса — запись resolved, id в отчёте."""
    memory = MemoryStore()
    state, _, _ = run(FakeLLM(), config, resume="approved", memory=memory)
    stored = memory.get(state["incident_id"])
    assert stored.outcome == "resolved" and stored.objects == ["payments/api"]
    assert "DB_PASSWORD" in stored.fix and "secretKeyRef" in stored.fix
    assert state["incident_id"] in state["report"]


def test_rejected_fix_is_stored_but_not_searchable(config):
    memory = MemoryStore()
    state, _, _ = run(FakeLLM(), config, resume="rejected", memory=memory)
    assert memory.get(state["incident_id"]).outcome == "unresolved"
    assert memory.search("под api падает") == []


def test_repeated_incident_supersedes_previous_record(config):
    memory = MemoryStore()
    first, _, _ = run(FakeLLM(), config, resume="approved", memory=memory)
    second, _, _ = run(FakeLLM(), config, resume="approved", memory=memory)
    assert [p.id for p in memory.search("под api падает")] == [second["incident_id"]]
    assert first["incident_id"] != second["incident_id"]


def test_similar_past_incident_reaches_report(config):
    """S5: прошлый случай найден, аналитик отметил класс и отличие, ссылка — в отчёте."""
    memory = MemoryStore()
    past_id = memory.write(PAST, created_at="2026-09-14T10:00:00+00:00")
    llm = FakeLLM(similar=[
        {"incident_id": past_id, "same_class": True, "difference": "сейчас другой релиз"},
        {"incident_id": "inc-1999-01-01-001", "same_class": True, "difference": "выдумка"}])
    state, _, _ = run(llm, config, resume="rejected", memory=memory)
    assert [s["id"] for s in state["similar_incidents"]] == [past_id]   # выдуманный id отброшен
    assert past_id in state["report"] and "сейчас другой релиз" in state["report"]
    assert "inc-1999-01-01-001" not in state["report"]


def test_memory_search_is_filtered_by_incident_namespace(config):
    memory = MemoryStore()
    memory.write(PAST.model_copy(update={"namespace": "billing", "objects": ["billing/api"]}))
    llm = FakeLLM(similar=[{"incident_id": "inc-x", "same_class": True, "difference": "-"}])
    state, _, _ = run(llm, config, resume="rejected", memory=memory)
    assert state["similar_incidents"] == []
    assert "похожих инцидентов нет" in state["report"]


def test_model_sees_namespace_as_required(config):
    """Evals М8: схема «namespace необязателен» вела модель к 15 вызовам с namespace: null."""
    agent = Agent(FakeLLM(), config, tools={"k8s_list_pods": fake_list_pods,
                                            "k8s_get_manifest": fake_get_manifest})
    for schema in agent.tool_schemas:
        params = schema["function"]["parameters"]
        assert "namespace" in params["required"], schema["function"]["name"]
        assert params["properties"]["namespace"]["type"] == "string"


def test_quoted_namespace_is_unquoted(config):
    """Evals М8: namespace «"payments"» в кавычках давал «сервиса нет» — кавычки снимаются."""
    llm = FakeLLM(chat=[{"tool_calls": [tool_call("k8s_list_pods", namespace='"payments"')]},
                        {"content": "готово"}], patch=None)
    state, _, _ = run(llm, config)
    assert [e["args"]["namespace"] for e in state["evidence"]] == ["payments"]


def test_past_fix_of_same_class_reaches_propose(config):
    """S5: прошлый фикс того же класса — подсказка автору патча; другой класс не подсказывается."""
    memory = MemoryStore()
    same = memory.write(PAST, created_at="2026-09-14T10:00:00+00:00")
    other = memory.write(PAST.model_copy(update={"objects": ["payments/checkout"],
                                                 "fix": "limits.memory 256Mi"}))
    llm = FakeLLM(similar=[{"incident_id": same, "same_class": True, "difference": "-"},
                           {"incident_id": other, "same_class": False, "difference": "OOM"}])
    seen = []
    original = llm.structured

    def spy(messages, schema, usage, free_form=False, **kw):
        if schema is PatchProposal:
            seen.append(messages[-1]["content"])
        return original(messages, schema, usage, free_form, **kw)

    llm.structured = spy
    run(llm, config, resume="rejected", memory=memory)
    assert "secretKeyRef" in seen[0] and "256Mi" not in seen[0]


def test_unjudged_memory_candidate_is_still_reported(config):
    """Репетиция демо: модель опустила оценку кандидатов — найденный случай всё равно в отчёте."""
    memory = MemoryStore()
    past_id = memory.write(PAST, created_at="2026-09-14T10:00:00+00:00")
    state, _, _ = run(FakeLLM(similar=[]), config, resume="rejected", memory=memory)
    assert [(s["id"], s["same_class"]) for s in state["similar_incidents"]] == [(past_id, None)]
    assert past_id in state["report"] and "не оценил" in state["report"]
