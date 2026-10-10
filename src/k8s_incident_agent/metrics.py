"""Метрики Prometheus из трейсов (N5, OBSERVABILITY §3; контракт — .scratch/m7-observability).

Почему из трейсов, а не счётчиками в процессе: CLI живёт ~30 с, Prometheus скрейпит раз в 15 с —
счётчики процесса умирали бы раньше, чем их прочитают (пришлось бы ставить pushgateway). Здесь
каждый скрейп заново агрегирует `data/traces/*.jsonl`: трейс — источник истины, метрика — его
свёртка, и перезапуск `k8s-agent metrics serve` ничего не теряет.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Iterator
from pathlib import Path

from prometheus_client.core import CounterMetricFamily, HistogramMetricFamily, Metric

from k8s_incident_agent.trace import read_trace

MUTATING = ("k8s_patch", "k8s_rollout_restart")   # на них алерт AgentUnapprovedMutation

# Имена, лейблы и бакеты фиксированы контрактом: на них опираются алерты и дашборд.
COUNTERS: dict[str, tuple[str, tuple[str, ...]]] = {
    "agent_runs": ("Прогоны по маршруту и исходу", ("route", "outcome")),
    "agent_llm_calls": ("Вызовы модели", ("model", "node", "status")),
    "agent_llm_cost_usd": ("Стоимость вызовов модели, $", ("model", "node")),
    "agent_llm_tokens": ("Токены модели", ("model", "node", "direction")),
    "agent_structured_output_retries": ("Повторы из-за невалидного structured output",
                                        ("model", "node")),
    "agent_tool_calls": ("Вызовы инструментов; denied — отклонён кодом", ("tool", "status")),
    "agent_evidence_truncated": ("Свидетельства, обрезанные до лимита", ("tool",)),
    "agent_approvals": ("Решения человека по патчу", ("decision",)),
}
HISTOGRAMS: dict[str, tuple[str, tuple[str, ...], tuple[float, ...]]] = {
    "agent_run_duration_seconds": ("Длительность прогона без ожидания человека", ("route",),
                                   (5, 10, 20, 30, 60, 90, 120, 180, 300, 600)),
    "agent_run_cost_usd": ("Стоимость прогона, $", ("route",),
                           (0.005, 0.01, 0.02, 0.05, 0.1, 0.2, 0.5, 1)),
    "agent_llm_latency_seconds": ("Латентность вызова модели", ("model", "node"),
                                  (1, 2, 5, 10, 20, 30, 60, 120)),
    "agent_tool_duration_seconds": ("Длительность вызова инструмента", ("tool",),
                                    (0.1, 0.25, 0.5, 1, 2, 5, 10)),
    "agent_review_iterations": ("Итерации ревью за прогон", (), (1, 2, 3)),
    "agent_memory_search_hits": ("Найдено похожих инцидентов", (), (0, 1, 2, 3)),
}


class Aggregate:
    """Свёртка событий трейсов в значения счётчиков и наблюдения гистограмм."""

    def __init__(self) -> None:
        self.counters: dict[str, dict[tuple, float]] = defaultdict(lambda: defaultdict(float))
        self.observations: dict[str, dict[tuple, list[float]]] = defaultdict(
            lambda: defaultdict(list))
        # Ряд, на котором стоит алерт безопасности, существует с нуля заранее: иначе первая
        # попытка мутации создала бы ряд сразу со значением 1, и increase() её бы не увидел.
        for tool in MUTATING:
            self.counters["agent_tool_calls"][(tool, "denied")] = 0.0

    def run(self, events: list[dict]) -> None:
        """События одного прогона. Длительность — сумма узлов: пауза на подтверждение не в счёт."""
        node_seconds = 0.0
        for e in events:
            kind, node = e.get("event"), e.get("node", "")
            if kind == "node":
                node_seconds += e.get("seconds", 0.0)
            elif kind == "llm":
                model = e.get("model", "")
                self.counters["agent_llm_calls"][(model, node, e.get("status", ""))] += 1
                self.counters["agent_llm_cost_usd"][(model, node)] += e.get("cost") or 0.0
                self.counters["agent_llm_tokens"][(model, node, "in")] += e.get("tokens_in") or 0
                self.counters["agent_llm_tokens"][(model, node, "out")] += e.get("tokens_out") or 0
                self.observations["agent_llm_latency_seconds"][(model, node)].append(
                    e.get("latency", 0.0))
            elif kind == "format_retry":
                self.counters["agent_structured_output_retries"][(e.get("model", ""), node)] += 1
            elif kind == "tool":
                tool = e.get("tool", "")
                self.counters["agent_tool_calls"][(tool, e.get("status", ""))] += 1
                if e.get("truncated"):
                    self.counters["agent_evidence_truncated"][(tool,)] += 1
                if e.get("status") != "denied":
                    self.observations["agent_tool_duration_seconds"][(tool,)].append(
                        e.get("seconds", 0.0))
            elif kind == "approval":
                self.counters["agent_approvals"][(e.get("decision", ""),)] += 1
            elif kind == "memory_search":
                self.observations["agent_memory_search_hits"][()].append(e.get("hits", 0))
            elif kind == "run_end":
                route = e.get("route", "")
                self.counters["agent_runs"][(route, e.get("outcome", ""))] += 1
                self.observations["agent_run_duration_seconds"][(route,)].append(node_seconds)
                self.observations["agent_run_cost_usd"][(route,)].append(e.get("cost", 0.0))
                if route == "diagnose":
                    self.observations["agent_review_iterations"][()].append(
                        e.get("review_iterations", 0))

    def families(self) -> Iterator[Metric]:
        for name, (doc, labels) in COUNTERS.items():
            family = CounterMetricFamily(name, doc, labels=list(labels))
            for values, value in sorted(self.counters[name].items()):
                family.add_metric(list(values), value)
            yield family
        for name, (doc, labels, bounds) in HISTOGRAMS.items():
            family = HistogramMetricFamily(name, doc, labels=list(labels))
            for values, observed in sorted(self.observations[name].items()):
                buckets = [(str(b), float(sum(1 for x in observed if x <= b))) for b in bounds]
                buckets.append(("+Inf", float(len(observed))))
                family.add_metric(list(values), buckets, sum_value=sum(observed))
            yield family


def aggregate(runs: Iterable[list[dict]]) -> Aggregate:
    agg = Aggregate()
    for events in runs:
        agg.run(events)
    return agg


class TraceCollector:
    """Коллектор для prometheus_client: каждый скрейп читает каталог трейсов заново."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory

    def collect(self) -> Iterator[Metric]:
        runs = (read_trace(p) for p in sorted(self.directory.glob("*.jsonl")))
        yield from aggregate(runs).families()
