"""Трейс прогона (N5, docs/OBSERVABILITY.md §2): каждый вызов модели и инструмента — строка JSONL.

Файл `data/traces/<run_id>.jsonl`, run_id = thread_id инцидента. Трейс — источник истины: метрики
(`metrics.py`) считаются из этих файлов, `k8s-agent trace` показывает их как временную шкалу.

Промпты и ответы модели пишутся как есть: это данные уже после маскирования — ровно то, что ушло в
шлюз (ISOLATION §6). Значение Secret сюда попасть не может, раз его не видела модель.
"""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


class Tracer:
    """Пишет события одного прогона. `node` — текущий узел графа: его выставляет обёртка узла,
    и вызовы модели и инструментов внутри узла получают его как лейбл."""

    def __init__(self, path: Path | None, run_id: str) -> None:
        self.path = path
        self.run_id = run_id
        self.node = ""
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)

    def emit(self, event: str, **fields: Any) -> None:
        if self.path is None:
            return
        record = {"ts": datetime.now(UTC).isoformat(timespec="milliseconds"), "run": self.run_id,
                  "event": event, "node": fields.pop("node", self.node), **fields}
        with self.path.open("a", encoding="utf-8") as out:
            out.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")

    def node_wrapper(self, name: str, fn):
        """Узел графа с замером: длительность узла — основа длительности прогона без ожидания
        человека (подтверждение — это пауза между узлами, а не время узла)."""
        def run(state):
            self.node = name
            started = time.monotonic()
            result = fn(state)
            self.emit("node", node=name, seconds=round(time.monotonic() - started, 3))
            return result
        return run

    def finish(self, state: dict) -> None:
        """Итог прогона: из него считаются agent_runs_total, длительность и стоимость."""
        self.emit("run_end", node="", route=state.get("route", "failed"), outcome=outcome(state),
                  cost=round(state.get("cost", 0.0), 6), llm_calls=state.get("llm_calls", 0),
                  tool_calls=state.get("tool_calls", 0),
                  review_iterations=state.get("review_iterations", 0),
                  incident_id=state.get("incident_id"))


NULL_TRACER = Tracer(None, "")


def outcome(state: dict) -> str:
    """Исход прогона для метрик (OBSERVABILITY §3)."""
    route = state.get("route", "failed")
    if route == "out_of_scope":
        return "refused"
    if route == "runbook_qa":
        return "answered"
    if route != "diagnose":
        return "error"
    if (state.get("execution_result") or {}).get("status") == "applied":
        return "resolved"
    if state.get("budget_exhausted"):
        return "budget_exceeded"
    return "unresolved"


def read_trace(path: Path) -> list[dict]:
    """События одного файла; битая строка (прогон убит посреди записи) пропускается."""
    events = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return events
