"""Общие фикстуры юнит-тестов.

Живой кластер не поднимается (EVALS.md, уровень 2): ответы API-сервера
подделываются объектами-заглушками, которые передаются инструментам через
keyword-only параметр `clients`.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from kubernetes.client.exceptions import ApiException

from k8s_incident_agent.config import (
    AppConfig,
    BudgetConfig,
    K8sConfig,
    LLMConfig,
    ToolsConfig,
)
from k8s_incident_agent.evidence import EvidenceStore

SECRET_VALUE = "c3VwZXItc2VjcmV0LXBhc3N3b3Jk"  # base64 «super-secret-password»


@pytest.fixture
def config() -> AppConfig:
    """Конфиг с маленьким лимитом обрезки: так проще проверять усечение."""
    return AppConfig(
        budget=BudgetConfig(max_tool_calls=15, max_seconds=180, max_review_iterations=2),
        llm=LLMConfig(base_url="https://example.invalid", api_key=None, model="test-model"),
        tools=ToolsConfig(max_output_chars=200, default_log_tail=50),
        k8s=K8sConfig(context="kind-test", default_namespace="payments"),
    )


@pytest.fixture
def store() -> EvidenceStore:
    return EvidenceStore()


def make_clients(**apis) -> SimpleNamespace:
    """Набор заглушек вместо ReadOnlyClients: подставляются только нужные API."""
    defaults = {"core": None, "apps": None, "batch": None, "networking": None, "custom": None}
    defaults.update(apis)
    return SimpleNamespace(**defaults)


def api_error(status: int, message: str = "") -> ApiException:
    exc = ApiException(status=status, reason="test")
    exc.body = json.dumps({"message": message})
    return exc


def log_response(text: str) -> SimpleNamespace:
    """Ответ `read_namespaced_pod_log(_preload_content=False)`: сырые байты в `.data`."""
    return SimpleNamespace(data=text.encode("utf-8"))


def raising(exc: BaseException):
    """Функция-заглушка, которая всегда бросает переданное исключение."""

    def _raise(*_args, **_kwargs):
        raise exc

    return _raise


def secret_manifest(name: str = "db-credentials", namespace: str = "payments") -> dict:
    return {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {"name": name, "namespace": namespace},
        "type": "Opaque",
        "data": {"DB_PASSWORD": SECRET_VALUE, "DB_USER": "cG9zdGdyZXM="},
    }


def pod_manifest(name: str = "api-7d9f", reason: str = "CrashLoopBackOff") -> dict:
    return {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {"name": name, "namespace": "payments"},
        "spec": {"nodeName": "kind-worker", "containers": [{"name": "api"}]},
        "status": {
            "phase": "Running",
            "containerStatuses": [
                {
                    "name": "api",
                    "ready": False,
                    "restartCount": 7,
                    "state": {"waiting": {"reason": reason, "message": "back-off 5m0s"}},
                }
            ],
        },
    }
