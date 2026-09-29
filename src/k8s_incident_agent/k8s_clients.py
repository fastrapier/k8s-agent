"""Клиенты Kubernetes: два раздельных контура (N1, docs/ISOLATION.md, раздел 5).

Read-only и read-write клиенты создаются разными функциями и читают токены из
разных kubeconfig-файлов: `K8S_RO_KUBECONFIG` (SA `incident-agent-ro`) и
`K8S_RW_KUBECONFIG` (SA `incident-agent-rw`). Общего клиента нет намеренно —
случайно получить права на запись из диагностического кода нельзя.

Модуль read-only инструментов (`tools/readonly.py`) обязан вызывать только
`build_readonly_clients`. Это проверяется тестом `tests/test_isolation_imports.py`.
"""

from __future__ import annotations

from dataclasses import dataclass

from kubernetes import client as k8s_client
from kubernetes import config as k8s_config

from k8s_incident_agent.config import AppConfig, get_config


class K8sClientError(RuntimeError):
    """Не удалось построить клиент: нет kubeconfig, нет контекста, файл битый."""


@dataclass(frozen=True)
class ReadOnlyClients:
    """Набор API диагностического контура. Ни одного мутирующего вызова здесь нет."""

    core: k8s_client.CoreV1Api
    apps: k8s_client.AppsV1Api
    batch: k8s_client.BatchV1Api
    networking: k8s_client.NetworkingV1Api
    custom: k8s_client.CustomObjectsApi


@dataclass(frozen=True)
class ReadWriteClients:
    """Набор API контура исполнения. Используется только узлом executor (этап М4)."""

    core: k8s_client.CoreV1Api
    apps: k8s_client.AppsV1Api


def _api_client(kubeconfig: str | None, context: str) -> k8s_client.ApiClient:
    """Отдельный ApiClient на каждый контур: конфигурация не глобальная, а своя."""
    try:
        return k8s_config.new_client_from_config(config_file=kubeconfig, context=context)
    except Exception as exc:  # noqa: BLE001 — наружу отдаём одну понятную ошибку
        source = kubeconfig or "kubeconfig по умолчанию"
        raise K8sClientError(
            f"не удалось собрать клиент Kubernetes (источник: {source}, контекст: {context}): {exc}"
        ) from exc


def build_readonly_clients(config: AppConfig | None = None) -> ReadOnlyClients:
    """Клиент диагностического контура. Токен — из `k8s.readonly_kubeconfig`."""
    config = config or get_config()
    api = _api_client(config.k8s.readonly_kubeconfig, config.k8s.context)
    return ReadOnlyClients(
        core=k8s_client.CoreV1Api(api),
        apps=k8s_client.AppsV1Api(api),
        batch=k8s_client.BatchV1Api(api),
        networking=k8s_client.NetworkingV1Api(api),
        custom=k8s_client.CustomObjectsApi(api),
    )


def build_readwrite_clients(config: AppConfig | None = None) -> ReadWriteClients:
    """Клиент контура исполнения. Токен — из `k8s.readwrite_kubeconfig`, другой файл."""
    config = config or get_config()
    api = _api_client(config.k8s.readwrite_kubeconfig, config.k8s.context)
    return ReadWriteClients(
        core=k8s_client.CoreV1Api(api),
        apps=k8s_client.AppsV1Api(api),
    )
