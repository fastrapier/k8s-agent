"""Свидетельства (F3).

Схема — docs/ARCHITECTURE.md, раздел 5: `{id, tool, args, ts, summary, raw_ref}`.
Каждый вызов инструмента, удачный или нет, оставляет свидетельство: по id на него
потом ссылается гипотеза аналитика.

Хранилище — в памяти процесса. Один инцидент живёт в одном процессе; переезд в
SQLite-чекпоинтер запланирован на этап М6 и схему свидетельства не меняет.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field


class Evidence(BaseModel):
    """Один зафиксированный факт о кластере."""

    id: str
    tool: str
    args: dict[str, Any] = Field(default_factory=dict)
    ts: str
    summary: str
    raw_ref: str | None = None


class EvidenceStore:
    """Хранилище свидетельств одного инцидента.

    Идентификаторы детерминированные и короткие (`ev-001`), чтобы модель могла
    ссылаться на них в тексте без ошибок копирования.
    """

    def __init__(self) -> None:
        self._items: list[Evidence] = []
        self._raw: dict[str, str] = {}

    def add(
        self,
        tool: str,
        args: dict[str, Any],
        summary: str,
        raw: str | None = None,
    ) -> Evidence:
        number = len(self._items) + 1
        raw_ref = None
        if raw is not None:
            raw_ref = f"raw-{number:03d}"
            self._raw[raw_ref] = raw
        evidence = Evidence(
            id=f"ev-{number:03d}",
            tool=tool,
            args=args,
            ts=datetime.now(UTC).isoformat(timespec="seconds"),
            summary=summary,
            raw_ref=raw_ref,
        )
        self._items.append(evidence)
        return evidence

    def get(self, evidence_id: str) -> Evidence | None:
        return next((item for item in self._items if item.id == evidence_id), None)

    def raw(self, raw_ref: str) -> str | None:
        return self._raw.get(raw_ref)

    def all(self) -> list[Evidence]:
        return list(self._items)

    def clear(self) -> None:
        self._items.clear()
        self._raw.clear()

    def __len__(self) -> int:
        return len(self._items)

    def __bool__(self) -> bool:
        # Пустое хранилище — это всё ещё хранилище: без этого `store or default_store`
        # молча подменял бы переданный store на глобальный.
        return True


# Хранилище по умолчанию: им пользуются инструменты, если store не передан явно.
default_store = EvidenceStore()
