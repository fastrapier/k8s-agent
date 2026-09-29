"""Обрезка сырого вывода инструментов.

ТЗ раздел 6: «сырой вывод обрезается до лимита». Лимит живёт в `config.tools.max_output_chars`,
факт обрезки помечается явно, чтобы модель не приняла обрубок за полный ответ.
"""

from __future__ import annotations

from typing import Any

import yaml

TRUNCATION_NOTE = "\n... [обрезано: показано {shown} из {total} символов]"


def truncate(text: str, limit: int) -> tuple[str, bool]:
    """Возвращает (текст, был_ли_обрезан). Пометка дописывается в сам текст."""
    if limit <= 0 or len(text) <= limit:
        return text, False
    shown = text[:limit]
    return shown + TRUNCATION_NOTE.format(shown=limit, total=len(text)), True


def to_yaml(obj: Any) -> str:
    """Стабильный YAML для сырого вывода: без сортировки ключей, без ASCII-эскейпов."""
    return yaml.safe_dump(obj, allow_unicode=True, sort_keys=False, default_flow_style=False)
