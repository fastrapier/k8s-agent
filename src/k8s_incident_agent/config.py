"""Загрузка конфигурации.

`config.yaml` лежит в корне репозитория и содержит плейсхолдеры вида `${VAR}`.
Значения плейсхолдеров берутся из окружения процесса и из `local.env`
(файл не попадает в git). Секреты хранятся в `SecretStr`, поэтому случайная
печать объекта конфига не раскрывает токен.
"""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

CONFIG_FILENAME = "config.yaml"
ENV_FILENAME = "local.env"

_PLACEHOLDER = re.compile(r"^\$\{([A-Z0-9_]+)\}$")


def find_repo_root() -> Path:
    """Ищет каталог, в котором лежит `config.yaml`: от cwd и от кода вверх по дереву."""
    starts = [Path.cwd(), Path(__file__).resolve().parent]
    for start in starts:
        for directory in (start, *start.parents):
            if (directory / CONFIG_FILENAME).is_file():
                return directory
    return Path.cwd()


class EnvSettings(BaseSettings):
    """Настройки LLM. Переменная окружения важнее строки в `local.env`."""

    model_config = SettingsConfigDict(
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    llm_api_token: SecretStr | None = None
    llm_api_url: str | None = None

    def as_mapping(self) -> dict[str, str]:
        """Плоская карта ИМЯ_ПЕРЕМЕННОЙ -> значение для подстановки в yaml."""
        raw = {
            "LLM_API_TOKEN": self.llm_api_token,
            "LLM_API_URL": self.llm_api_url,
        }
        result: dict[str, str] = {}
        for key, value in raw.items():
            if value is None:
                continue
            result[key] = value.get_secret_value() if isinstance(value, SecretStr) else value
        return result


class BudgetConfig(BaseModel):
    """Потолки одного инцидента. Обоснование чисел — docs/TZ.md, N3 и F5."""

    max_tool_calls: int
    max_seconds: int
    max_review_iterations: int


class LLMConfig(BaseModel):
    base_url: str | None = None
    api_key: SecretStr | None = None
    model: str
    temperature: float = 0.0
    # Глубина скрытого рассуждения модели. Не задано — по умолчанию шлюза (так мерилось М3).
    reasoning_effort: Literal["low", "medium", "high"] | None = None


class ToolsConfig(BaseModel):
    max_output_chars: int
    default_log_tail: int


class K8sConfig(BaseModel):
    context: str
    default_namespace: str
    readonly_kubeconfig: str | None = None
    readwrite_kubeconfig: str | None = None


class AppConfig(BaseModel):
    budget: BudgetConfig
    llm: LLMConfig
    tools: ToolsConfig
    k8s: K8sConfig


def _expand(node: Any, values: dict[str, str]) -> Any:
    """Рекурсивно подставляет `${VAR}`. Неизвестная переменная превращается в None."""
    if isinstance(node, dict):
        return {key: _expand(value, values) for key, value in node.items()}
    if isinstance(node, list):
        return [_expand(item, values) for item in node]
    if isinstance(node, str):
        match = _PLACEHOLDER.match(node.strip())
        if match:
            return values.get(match.group(1))
    return node


def load_config(config_path: Path | None = None, env_path: Path | None = None) -> AppConfig:
    """Читает config.yaml и подмешивает значения из окружения и local.env."""
    root = find_repo_root()
    config_path = config_path or root / CONFIG_FILENAME
    env_path = env_path or root / ENV_FILENAME

    # pydantic-settings сама отдаёт приоритет переменным окружения над local.env:
    # в контейнере файла может не быть, и это рабочий сценарий.
    settings = EnvSettings(_env_file=str(env_path) if env_path.is_file() else None)
    values = settings.as_mapping()

    raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    config = AppConfig.model_validate(_expand(raw, values))
    for field in ("readonly_kubeconfig", "readwrite_kubeconfig"):
        path = getattr(config.k8s, field)
        if path is not None:
            path = Path(path).expanduser()
            if not path.is_absolute():
                path = (config_path.parent / path).resolve()
            setattr(config.k8s, field, str(path))
    return config


@lru_cache(maxsize=1)
def get_config() -> AppConfig:
    """Конфиг процесса. Кэшируется, чтобы файл читался один раз."""
    return load_config()


def redacted_dict(config: AppConfig) -> dict[str, Any]:
    """Конфиг в виде словаря, пригодного для печати: токен заменён меткой."""
    from k8s_incident_agent.masking import MASK

    data = config.model_dump(mode="python")
    data["llm"]["api_key"] = MASK if config.llm.api_key is not None else None
    return data
