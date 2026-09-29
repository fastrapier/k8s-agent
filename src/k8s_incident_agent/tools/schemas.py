"""Схемы аргументов и результата инструментов (ТЗ раздел 6, «реестр со схемой через pydantic»).

Схемы служат двум целям: валидация аргументов из CLI и генерация JSON Schema для
tool calling на этапе М4 (`model_json_schema()`).
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class ToolArgs(BaseModel):
    """База для всех схем: лишние аргументы — ошибка, а не молчаливое игнорирование."""

    model_config = ConfigDict(extra="forbid")


class ListPodsArgs(ToolArgs):
    namespace: str | None = Field(default=None, description="namespace; по умолчанию из конфига")
    selector: str | None = Field(default=None, description="label selector, например app=api")


class DescribeArgs(ToolArgs):
    kind: str = Field(description="вид объекта: Pod, Deployment, Service, ...")
    name: str = Field(description="имя объекта")
    namespace: str | None = Field(default=None)


class EventsArgs(ToolArgs):
    namespace: str | None = Field(default=None)
    object: str | None = Field(
        default=None, description="имя объекта-источника; без него возвращаются все события ns"
    )


class LogsArgs(ToolArgs):
    pod: str = Field(description="имя пода")
    container: str | None = Field(default=None, description="контейнер; по умолчанию первый")
    tail: int | None = Field(default=None, ge=1, description="сколько последних строк")
    previous: bool = Field(default=False, description="лог предыдущего запуска (для CrashLoop)")
    namespace: str | None = Field(default=None)


class RolloutHistoryArgs(ToolArgs):
    deployment: str
    namespace: str | None = Field(default=None)


class GetManifestArgs(ToolArgs):
    kind: str
    name: str
    namespace: str | None = Field(default=None)


class TopArgs(ToolArgs):
    namespace: str | None = Field(default=None)


class ToolResult(BaseModel):
    """Структурированный результат вызова инструмента.

    `status`:
      * `ok` — данные собраны;
      * `degraded` — инструмент отработал, но источник данных недоступен
        (например, в кластере нет metrics-server);
      * `error` — API-сервер отказал или недоступен. Ошибка описана в `error`,
        исключение наружу не летит (docs/ARCHITECTURE.md, раздел 8).
    """

    tool: str
    status: Literal["ok", "degraded", "error"]
    evidence_id: str
    summary: str
    data: Any | None = None
    error: str | None = None
    truncated: bool = False
