"""Схемы структурированных ответов модели (ARCHITECTURE §5, MEMORY §3.1).

Это контракт между узлами графа (М4) и моделью: ответ модели валидируется pydantic, и
невалидный ответ — повтор с текстом ошибки, а не догадка. На этапе М3 те же схемы служат
блоку B сравнения моделей: проверяется, насколько стабильно модель в них попадает.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class Strict(BaseModel):
    """Лишние поля — ошибка: модель не должна дописывать то, чего схема не просила."""

    model_config = ConfigDict(extra="forbid")


class Hypothesis(Strict):
    """Гипотеза аналитика. Без ссылок на свидетельства гипотеза не принимается (F4)."""

    id: str = Field(description="идентификатор гипотезы, например h-1")
    statement: str = Field(description="утверждение о причине, одним-двумя предложениями")
    evidence_ids: list[str] = Field(min_length=1, description="id свидетельств: ev-001, ...")
    confidence: Literal["low", "medium", "high"]


class Patch(Strict):
    """Предложенное исправление. Применяется только после подтверждения человеком (F6)."""

    kind: Literal["Deployment", "StatefulSet", "ConfigMap"]
    name: str
    namespace: str
    patch: dict[str, Any] = Field(description="strategic merge patch объекта")
    rationale: str = Field(description="почему этот патч устраняет причину")
    evidence_ids: list[str] = Field(min_length=1)


class ReviewVerdict(Strict):
    """Решение ревьюера (F5). Три исхода различаются намеренно: ARCHITECTURE §5."""

    verdict: Literal["ok", "needs_more_facts", "incomplete_explanation"]
    gaps: list[str] = Field(description="чего не хватает; пусто, если verdict=ok")


class IncidentRecord(Strict):
    """Запись инцидента в долгосрочную память (F8, MEMORY §3.1)."""

    namespace: str
    kinds: list[str] = Field(description='виды затронутых объектов: ["Deployment", "Pod"]')
    objects: list[str] = Field(description='затронутые объекты: ["payments/api"]')
    symptoms: str
    root_cause: str
    fix: str
    fix_applied: bool
    outcome: Literal["resolved", "mitigated", "unresolved"]
    evidence_ids: list[str]


SCHEMAS: dict[str, type[Strict]] = {
    model.__name__: model for model in (Hypothesis, Patch, ReviewVerdict, IncidentRecord)
}
