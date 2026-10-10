"""Наблюдаемость (М7, N4/N5): трейс каждого прогона и метрики, посчитанные из трейсов."""

from __future__ import annotations

import time

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command
from prometheus_client import CollectorRegistry, generate_latest

from k8s_incident_agent.graph import Agent, build_graph
from k8s_incident_agent.llm import LLMClient, Usage
from k8s_incident_agent.metrics import TraceCollector, aggregate
from k8s_incident_agent.schemas import Route
from k8s_incident_agent.trace import Tracer, outcome, read_trace
from test_graph import FakeLLM, fake_get_manifest, fake_list_pods, tool_call


def traced_run(config, tmp_path, llm=None, resume="approved"):
    tracer = Tracer(tmp_path / "r1.jsonl", "r1")
    agent = Agent(llm or FakeLLM(), config, tools={"k8s_list_pods": fake_list_pods,
                                                   "k8s_get_manifest": fake_get_manifest},
                  apply_patch=lambda *a, **k: {"status": "applied", "detail": "ok"}, tracer=tracer)
    graph = build_graph(agent).compile(checkpointer=InMemorySaver())
    cfg = {"configurable": {"thread_id": "r1"}}
    graph.invoke({"query": "под api падает", "started": time.time(), "evidence": []}, cfg)
    if graph.get_state(cfg).interrupts:
        tracer.emit("approval", node="approval", decision=resume, wait_seconds=600.0)
        graph.invoke(Command(resume=resume), cfg)
    tracer.finish(graph.get_state(cfg).values)
    return read_trace(tmp_path / "r1.jsonl")


def test_trace_has_every_node_tool_and_outcome(config, tmp_path):
    events = traced_run(config, tmp_path)
    nodes = [e["node"] for e in events if e["event"] == "node"]
    assert nodes[:3] == ["classify", "plan", "diagnose"] and nodes[-1] == "report"
    assert "remember" in nodes
    tools = [(e["tool"], e["status"]) for e in events if e["event"] == "tool"]
    assert tools == [("k8s_list_pods", "ok"), ("k8s_get_manifest", "ok")]
    assert all(e["node"] == "diagnose" for e in events if e["event"] == "tool")
    end = events[-1]
    assert end["event"] == "run_end" and end["outcome"] == "resolved"


def test_denied_tool_call_is_traced(config, tmp_path):
    """Попытка k8s_patch у диагноста — в трейсе status=denied: на этом стоит алерт безопасности."""
    llm = FakeLLM(chat=[{"tool_calls": [tool_call("k8s_patch", kind="Deployment", name="api",
                                                  namespace="payments", patch={})]},
                        {"content": "понял"}], patch=None)
    events = traced_run(config, tmp_path, llm)
    denied = [e for e in events if e["event"] == "tool" and e["status"] == "denied"]
    assert denied and denied[0]["tool"] == "k8s_patch"


class Gateway(LLMClient):
    """Шлюз подменён на уровне HTTP-вызова: проверяется, что пишет в трейс сам клиент."""

    def __init__(self, replies, tracer):
        self.model, self.reasoning_effort, self.tracer = "m", None, tracer
        self.replies = list(replies)

    def _call(self, messages, tools, response_format, model):
        return {"message": {"content": self.replies.pop(0)}, "latency": 2.5, "cost": 0.001,
                "tokens_in": 100, "tokens_out": 20}


def test_llm_calls_and_format_retries_are_traced(tmp_path):
    tracer = Tracer(tmp_path / "r.jsonl", "r")
    tracer.node = "classify"
    llm = Gateway(['{"route": "nope"}', '{"route": "diagnose", "reason": "x"}'], tracer)
    assert llm.structured([{"role": "user", "content": "q"}], Route, Usage()).route == "diagnose"
    events = read_trace(tmp_path / "r.jsonl")
    assert [e["event"] for e in events] == ["llm", "format_retry", "llm"]
    first = events[0]
    assert (first["node"], first["label"], first["tokens_in"], first["status"]) == \
        ("classify", "Route", 100, "ok")
    assert first["messages"][0]["content"] == "q"   # промпт в трейсе — то, что ушло в шлюз


def test_metrics_from_traces(config, tmp_path):
    traced_run(config, tmp_path)
    text = generate_latest(_registry(tmp_path)).decode()
    assert 'agent_runs_total{outcome="resolved",route="diagnose"} 1.0' in text
    assert 'agent_tool_calls_total{status="ok",tool="k8s_list_pods"} 1.0' in text
    assert 'agent_approvals_total{decision="approved"} 1.0' in text
    assert 'agent_memory_search_hits_count 1.0' in text


def test_run_duration_excludes_human_wait():
    """Человек думал над патчем 600 с — в длительность прогона это не входит (OBSERVABILITY §2)."""
    events = [{"event": "node", "node": "classify", "seconds": 2.0},
              {"event": "approval", "decision": "approved", "wait_seconds": 600.0},
              {"event": "node", "node": "execute", "seconds": 5.0},
              {"event": "run_end", "route": "diagnose", "outcome": "resolved", "cost": 0.02,
               "review_iterations": 1}]
    agg = aggregate([events])
    assert agg.observations["agent_run_duration_seconds"][("diagnose",)] == [7.0]


def test_outcomes():
    assert outcome({"route": "out_of_scope"}) == "refused"
    assert outcome({"route": "runbook_qa"}) == "answered"
    assert outcome({"route": "failed"}) == "error"
    assert outcome({"route": "diagnose", "budget_exhausted": True}) == "budget_exceeded"
    assert outcome({"route": "diagnose", "execution_result": {"status": "applied"}}) == "resolved"
    assert outcome({"route": "diagnose"}) == "unresolved"


def _registry(directory):
    registry = CollectorRegistry()
    registry.register(TraceCollector(directory))
    return registry


def test_mutation_series_exists_before_first_attempt(tmp_path):
    """Алерт AgentUnapprovedMutation считает increase(): ряд должен быть с нулём до события."""
    text = generate_latest(_registry(tmp_path)).decode()
    assert 'agent_tool_calls_total{status="denied",tool="k8s_patch"} 0.0' in text


def test_alerts_and_dashboard_use_only_exported_metrics():
    """Контракт М7: опечатка в имени метрики сделала бы алерт или панель вечно пустыми — молча."""
    import re
    from pathlib import Path

    from k8s_incident_agent.metrics import COUNTERS, HISTOGRAMS

    root = Path(__file__).resolve().parent.parent / "observability"
    exported = {f"{name}_total" for name in COUNTERS} | {
        f"{name}{suffix}" for name in HISTOGRAMS for suffix in ("_bucket", "_sum", "_count")}
    for path in (root / "alerts.yml", root / "grafana" / "dashboards" / "agent.json"):
        used = set(re.findall(r"agent_[a-z_]+", path.read_text(encoding="utf-8")))
        assert used and used <= exported, (path.name, used - exported)
