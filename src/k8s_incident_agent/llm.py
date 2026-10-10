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
from k8s_incident_agent.trace import NULL_TRACER, Tracer

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
    def __init__(self, config: AppConfig | None = None, model: str | None = None,
                 tracer: Tracer | None = None) -> None:
        llm = (config or get_config()).llm
        self.tracer = tracer or NULL_TRACER
        if not llm.base_url or not llm.api_key:
            raise LLMError("нет LLM_API_URL / LLM_API_TOKEN в local.env")
        self.model = model or llm.model
        self.temperature = llm.temperature
        self.reasoning_effort = llm.reasoning_effort
        self.url = llm.base_url.rstrip("/") + "/chat/completions"
        self.headers = {
            "Authorization": "Bearer " + llm.api_key.get_secret_value(),
            "Content-Type": "application/json",
        }

    def raw(self, messages: list, tools: list | None = None,
            response_format: dict | None = None, model: str | None = None,
            label: str = "", timeout: float = TIMEOUT) -> dict:
        """Один вызов. Таймаут и ошибка — не исключение, а {'timeout'|'error', latency, cost}.
        Каждый вызов — событие `llm` в трейсе: латентность, цена, токены, промпт и ответ."""
        reply = self._call(messages, tools, response_format, model, timeout)
        self.tracer.emit(
            "llm", model=model or self.model, label=label or ("tools" if tools else "chat"),
            status="ok" if "message" in reply else ("timeout" if reply.get("timeout") else "error"),
            latency=round(reply["latency"], 3), cost=reply["cost"],
            tokens_in=reply.get("tokens_in", 0), tokens_out=reply.get("tokens_out", 0),
            messages=messages, response=reply.get("message"), error=reply.get("error"))
        return reply

    def _call(self, messages: list, tools: list | None, response_format: dict | None,
              model: str | None, timeout: float = TIMEOUT) -> dict:
        body: dict[str, Any] = {"model": model or self.model, "messages": messages,
                                "temperature": self.temperature, "top_p": 1}
        if self.reasoning_effort:
            body["reasoning_effort"] = self.reasoning_effort
        if tools:
            body["tools"] = tools
        if response_format:
            body["response_format"] = response_format
        data = json.dumps(body).encode()
        for attempt in range(GATEWAY_RETRIES + 1):
            started = time.monotonic()
            try:
                request = urllib.request.Request(self.url, data, self.headers)
                with urllib.request.urlopen(request, timeout=timeout) as response:
                    payload = json.load(response)
                usage = payload.get("usage") or {}
                return {
                    "message": payload["choices"][0]["message"],
                    "latency": time.monotonic() - started,
                    "cost": float(usage.get("cost") or 0.0),
                    "tokens_in": usage.get("prompt_tokens") or 0,
                    "tokens_out": usage.get("completion_tokens") or 0,
                }
            except TimeoutError:
                return {"timeout": True, "latency": float(timeout), "cost": 0.0}
            except urllib.error.HTTPError as exc:
                if exc.code not in (429, 500, 502, 503, 504) or attempt == GATEWAY_RETRIES:
                    return {"error": f"HTTP {exc.code}", "latency": 0.0, "cost": 0.0}
            except urllib.error.URLError as exc:
                if isinstance(exc.reason, TimeoutError | socket.timeout):
                    return {"timeout": True, "latency": float(timeout), "cost": 0.0}
                if attempt == GATEWAY_RETRIES:
                    return {"error": str(exc.reason), "latency": 0.0, "cost": 0.0}
            time.sleep(2 ** attempt)
        raise AssertionError("недостижимо")

    def chat(self, messages: list, usage: Usage, tools: list | None = None) -> dict:
        """Вызов для графа: учитывает стоимость, сбой шлюза — исключение LLMError."""
        reply = self.raw(messages, tools=tools, label="tools" if tools else "chat")
        usage.add(reply)
        if "message" not in reply:
            raise LLMError(reply.get("error") or "таймаут шлюза")
        return reply["message"]

    def structured(self, messages: list, schema: type[BaseModel], usage: Usage,
                   free_form: bool = False, timeout: float = TIMEOUT,
                   timeout_retries: int = 0) -> BaseModel:
        """Ответ строго по pydantic-схеме; невалидный — повтор с текстом ошибки (ARCH §8).

        free_form — для схем с открытым объектом (патч `dict[str, Any]`). Декодирование по
        json_schema на таком поле у шлюза зацикливается: модель пишет патч, а затем десятки
        тысяч пробелов до таймаута (2 из 5 прогонов). Тогда схема идёт текстом в промпт,
        шлюз требует только валидный JSON (`json_object`), а форму проверяет pydantic.
        """
        messages = list(messages)
        if free_form:
            response_format = {"type": "json_object"}
            messages.append({"role": "system", "content": "Ответь одним JSON-объектом по схеме:\n"
                             + json.dumps(schema.model_json_schema(), ensure_ascii=False)})
        else:
            response_format = {"type": "json_schema", "json_schema": {
                "name": schema.__name__, "schema": schema.model_json_schema()}}
        error = ""
        attempt = 0
        while attempt <= FORMAT_RETRIES:
            reply = self.raw(messages, response_format=response_format, label=schema.__name__,
                             timeout=timeout)
            usage.add(reply)
            if reply.get("timeout") and timeout_retries > 0:
                timeout_retries -= 1      # зависшее декодирование случайно — повтор обычно проходит
                continue
            attempt += 1
            if "message" not in reply:
                raise LLMError(reply.get("error") or "таймаут шлюза")
            try:
                return schema.model_validate(parse_json(reply["message"]))
            except (json.JSONDecodeError, ValidationError) as exc:
                error = " ".join(str(exc).split())[:400]   # какие поля не так — видно в отчёте
                self.tracer.emit("format_retry", model=self.model, label=schema.__name__,
                                 error=error)
                messages += [reply["message"], {"role": "user", "content":
                             f"Ответ не прошёл валидацию: {error}. Верни исправленный JSON."}]
        raise LLMError(f"модель не дала валидный {schema.__name__}: {error}")


def parse_json(message: dict) -> Any:
    """JSON из ответа модели; некоторые модели заворачивают его в ```json ... ```."""
    text = (message.get("content") or "").strip()
    if text.startswith("```"):
        text = text.strip("`").removeprefix("json").strip()
    return json.loads(text)
