"""Клиент LLM-шлюза: OpenAI-совместимый `/chat/completions` обычным HTTP (N7).

Один и тот же код вызывает модель и в графе агента, и в сравнении моделей
(evals/model_selection.py): сравнение меряет ровно то, что работает в агенте.
Стоимость берётся из `usage.cost` ответа шлюза (N4).
Прокси — из `HTTPS_PROXY`, его читает urllib (в контейнере это egress-proxy).
"""

from __future__ import annotations

import json
import socket
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, ValidationError

from k8s_incident_agent.config import AppConfig, get_config

TIMEOUT = 120               # таймаут одного вызова, LLM_SELECTION §5
GATEWAY_RETRIES = 3         # 429 и 5xx повторяются, это сбой шлюза, а не ответ модели
FORMAT_RETRIES = 2          # ARCHITECTURE §8: повтор с текстом ошибки валидации, максимум 2


class LLMError(RuntimeError):
    """Шлюз не ответил: таймаут, сеть, ошибка HTTP после повторов."""


@dataclass
class Usage:
    """Сколько стоил разговор с моделью: для отчёта (F10) и метрик (N4)."""

    calls: int = 0
    cost: float = 0.0
    seconds: float = 0.0
    latencies: list[float] = field(default_factory=list)

    def add(self, reply: dict) -> None:
        self.calls += 1
        self.cost += reply["cost"]
        self.seconds += reply["latency"]
        self.latencies.append(reply["latency"])


class LLMClient:
    def __init__(self, config: AppConfig | None = None, model: str | None = None) -> None:
        llm = (config or get_config()).llm
        if not llm.base_url or not llm.api_key:
            raise LLMError("нет LLM_API_URL / LLM_API_TOKEN в local.env")
        self.model = model or llm.model
        self.temperature = llm.temperature
        self.url = llm.base_url.rstrip("/") + "/chat/completions"
        self.headers = {
            "Authorization": "Bearer " + llm.api_key.get_secret_value(),
            "Content-Type": "application/json",
        }

    def raw(self, messages: list, tools: list | None = None,
            response_format: dict | None = None, model: str | None = None) -> dict:
        """Один вызов. Таймаут и ошибка — не исключение, а {'timeout'|'error', latency, cost}."""
        body: dict[str, Any] = {"model": model or self.model, "messages": messages,
                                "temperature": self.temperature, "top_p": 1}
        if tools:
            body["tools"] = tools
        if response_format:
            body["response_format"] = response_format
        data = json.dumps(body).encode()
        for attempt in range(GATEWAY_RETRIES + 1):
            started = time.monotonic()
            try:
                request = urllib.request.Request(self.url, data, self.headers)
                with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
                    payload = json.load(response)
                return {
                    "message": payload["choices"][0]["message"],
                    "latency": time.monotonic() - started,
                    "cost": float((payload.get("usage") or {}).get("cost") or 0.0),
                }
            except TimeoutError:
                return {"timeout": True, "latency": float(TIMEOUT), "cost": 0.0}
            except urllib.error.HTTPError as exc:
                if exc.code not in (429, 500, 502, 503, 504) or attempt == GATEWAY_RETRIES:
                    return {"error": f"HTTP {exc.code}", "latency": 0.0, "cost": 0.0}
            except urllib.error.URLError as exc:
                if isinstance(exc.reason, TimeoutError | socket.timeout):
                    return {"timeout": True, "latency": float(TIMEOUT), "cost": 0.0}
                if attempt == GATEWAY_RETRIES:
                    return {"error": str(exc.reason), "latency": 0.0, "cost": 0.0}
            time.sleep(2 ** attempt)
        raise AssertionError("недостижимо")

    def chat(self, messages: list, usage: Usage, tools: list | None = None) -> dict:
        """Вызов для графа: учитывает стоимость, сбой шлюза — исключение LLMError."""
        reply = self.raw(messages, tools=tools)
        usage.add(reply)
        if "message" not in reply:
            raise LLMError(reply.get("error") or "таймаут шлюза")
        return reply["message"]

    def structured(self, messages: list, schema: type[BaseModel], usage: Usage) -> BaseModel:
        """Ответ строго по pydantic-схеме; невалидный — повтор с текстом ошибки (ARCH §8)."""
        response_format = {"type": "json_schema", "json_schema": {
            "name": schema.__name__, "schema": schema.model_json_schema()}}
        messages = list(messages)
        error = ""
        for _ in range(FORMAT_RETRIES + 1):
            reply = self.raw(messages, response_format=response_format)
            usage.add(reply)
            if "message" not in reply:
                raise LLMError(reply.get("error") or "таймаут шлюза")
            try:
                return schema.model_validate(parse_json(reply["message"]))
            except (json.JSONDecodeError, ValidationError) as exc:
                error = str(exc).splitlines()[0][:300]
                messages += [reply["message"], {"role": "user", "content":
                             f"Ответ не прошёл валидацию: {error}. Верни исправленный JSON."}]
        raise LLMError(f"модель не дала валидный {schema.__name__}: {error}")


def parse_json(message: dict) -> Any:
    """JSON из ответа модели; некоторые модели заворачивают его в ```json ... ```."""
    text = (message.get("content") or "").strip()
    if text.startswith("```"):
        text = text.strip("`").removeprefix("json").strip()
    return json.loads(text)
