"""Реестр инструментов (ARCHITECTURE §4, «Реестр инструментов со схемой через pydantic»).

На этапе М0–М1 в реестре только диагностический контур. Инструменты записи
(`k8s_patch`, `k8s_rollout_restart`) появятся отдельным реестром на этапе М4 и
в этот словарь не попадут: доступность инструмента — это часть границы L3.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from k8s_incident_agent.tools import readonly
from k8s_incident_agent.tools.schemas import (
    DescribeArgs,
    EventsArgs,
    GetManifestArgs,
    ListPodsArgs,
    LogsArgs,
    RolloutHistoryArgs,
    ToolArgs,
    ToolResult,
    TopArgs,
)


@dataclass(frozen=True)
class ToolSpec:
    """Инструмент: имя, функция, схема аргументов, описание для промпта."""

    name: str
    func: Callable[..., ToolResult]
    args_model: type[ToolArgs]
    description: str

    def json_schema(self) -> dict[str, Any]:
        """JSON Schema аргументов — то, что уйдёт в tool calling на этапе М4."""
        return self.args_model.model_json_schema()


READONLY_TOOLS: dict[str, ToolSpec] = {
    spec.name: spec
    for spec in (
        ToolSpec(
            "k8s_list_pods",
            readonly.k8s_list_pods,
            ListPodsArgs,
            "Список подов namespace с состоянием контейнеров и причинами отказа.",
        ),
        ToolSpec(
            "k8s_describe",
            readonly.k8s_describe,
            DescribeArgs,
            "Сводка по объекту: метаданные, статус, условия, связанные события.",
        ),
        ToolSpec(
            "k8s_events",
            readonly.k8s_events,
            EventsArgs,
            "События namespace, при необходимости по конкретному объекту.",
        ),
        ToolSpec(
            "k8s_logs",
            readonly.k8s_logs,
            LogsArgs,
            "Логи пода; previous=true — лог предыдущего запуска.",
        ),
        ToolSpec(
            "k8s_rollout_history",
            readonly.k8s_rollout_history,
            RolloutHistoryArgs,
            "История релизов Deployment: ревизии, образы, change-cause.",
        ),
        ToolSpec(
            "k8s_get_manifest",
            readonly.k8s_get_manifest,
            GetManifestArgs,
            "Манифест объекта с маскированием секретов.",
        ),
        ToolSpec(
            "k8s_top",
            readonly.k8s_top,
            TopArgs,
            "Потребление CPU и памяти подами (требует metrics-server).",
        ),
    )
}

__all__ = ["READONLY_TOOLS", "ToolSpec", "ToolResult"]
