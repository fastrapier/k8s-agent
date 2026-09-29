"""Маскирование секретов — уровень L3 из docs/ISOLATION.md, раздел 6.

Второй рубеж после RBAC. Применяется ДО записи свидетельства, поэтому
незамаскированное значение не попадает ни в хранилище свидетельств, ни в трейс.

Что закрывается:

1. `data` / `stringData` у Secret -> `***masked***`;
2. `env` с `valueFrom.secretKeyRef` -> имя ключа остаётся, значение не остаётся;
3. эвристика по содержимому логов: длинный base64, `AKIA...`, `Bearer ...`, `token=...`.
"""

from __future__ import annotations

import re
from typing import Any

MASK = "***masked***"

# --- Эвристики для логов (пункт 3) ------------------------------------------------

_BEARER = re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._\-+/=]{8,}")
_AWS_KEY = re.compile(r"\b(?:AKIA|ASIA|AGPA|AIDA)[0-9A-Z]{12,20}\b")
_JWT = re.compile(r"\beyJ[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{6,}\b")
_KEYVALUE = re.compile(
    r"(?i)\b([a-z0-9_.\-]*(?:password|passwd|token|secret|api[_-]?key|access[_-]?key)"
    r"[a-z0-9_.\-]*)\s*([:=])\s*(\"[^\"]*\"|'[^']*'|\S+)"
)
_LONG_BLOB = re.compile(r"[A-Za-z0-9+/=_-]{40,}")


def _blob_looks_like_secret(value: str) -> bool:
    """Отсекаем ложные срабатывания: длинная строка в одном регистре — это не ключ.

    Так из-под эвристики выходят sha256-дайджесты образов и пути, которые нужны диагносту.
    """
    return (
        any(c.islower() for c in value)
        and any(c.isupper() for c in value)
        and any(c.isdigit() for c in value)
    )


def mask_text(text: str) -> str:
    """Эвристическое маскирование секретов в свободном тексте (логи, сообщения событий)."""
    if not text:
        return text
    masked = _BEARER.sub(lambda m: f"{m.group(1)} {MASK}", text)
    masked = _AWS_KEY.sub(MASK, masked)
    masked = _JWT.sub(MASK, masked)
    masked = _KEYVALUE.sub(lambda m: f"{m.group(1)}{m.group(2)}{MASK}", masked)
    masked = _LONG_BLOB.sub(
        lambda m: MASK if _blob_looks_like_secret(m.group(0)) else m.group(0), masked
    )
    return masked


# --- Маскирование объектов кластера (пункты 1 и 2) --------------------------------


def _mask_secret_payload(value: Any) -> Any:
    """`data`/`stringData`: ключи видны, значения — нет. Имена ключей нужны диагносту."""
    if isinstance(value, dict):
        return dict.fromkeys(value, MASK)
    return MASK


def _is_secret(node: dict[str, Any], forced: bool) -> bool:
    if forced:
        return True
    kind = node.get("kind")
    return isinstance(kind, str) and kind == "Secret"


def mask_object(node: Any, _forced_secret: bool = False) -> Any:
    """Рекурсивно маскирует манифест или список манифестов.

    `_forced_secret` протаскивается внутрь элементов SecretList: у элементов списка,
    который вернул API-сервер, поле `kind` обычно пустое.
    """
    if isinstance(node, list):
        return [mask_object(item, _forced_secret) for item in node]
    if not isinstance(node, dict):
        return node

    kind = node.get("kind")
    items_forced = _forced_secret or kind == "SecretList"
    secret = _is_secret(node, _forced_secret)

    result: dict[str, Any] = {}
    for key, value in node.items():
        if key in ("data", "stringData") and (secret or key == "stringData"):
            # `stringData` есть только у Secret, поэтому маскируется всегда,
            # даже если kind по какой-то причине потерян.
            result[key] = _mask_secret_payload(value)
            continue
        if key == "items":
            result[key] = mask_object(value, items_forced)
            continue
        result[key] = mask_object(value, _forced_secret)

    # env-переменная, значение которой приходит из Secret: имя ключа оставляем,
    # значение — нет (ISOLATION §6). В манифесте `value` рядом с `valueFrom` быть
    # не должно, но если оно там есть, наружу оно не уйдёт.
    value_from = result.get("valueFrom")
    if isinstance(value_from, dict) and "secretKeyRef" in value_from and "value" in result:
        result["value"] = MASK
    return result
