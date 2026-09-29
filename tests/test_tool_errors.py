"""Ошибки кластера и мягкая деградация (ТЗ раздел 6, ARCHITECTURE §8)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from conftest import api_error, make_clients, raising
from k8s_incident_agent.k8s_clients import K8sClientError
from k8s_incident_agent.tools.readonly import k8s_describe, k8s_get_manifest, k8s_logs, k8s_top


def test_object_not_found_is_structured_result(config, store):
    core = SimpleNamespace(read_namespaced_pod=raising(api_error(404, "pods 'api' not found")))

    result = k8s_describe(
        "Pod", "api", "payments", clients=make_clients(core=core), store=store, config=config
    )

    assert result.status == "error"
    assert "не найден" in result.error
    # ошибка тоже попадает в свидетельства: по ней корректируется план
    assert store.get(result.evidence_id).tool == "k8s_describe"


def test_forbidden_is_structured_result(config, store):
    core = SimpleNamespace(read_namespaced_secret=raising(api_error(403, "forbidden")))

    result = k8s_get_manifest(
        "Secret", "db", "payments", clients=make_clients(core=core), store=store, config=config
    )

    assert result.status == "error"
    assert "нет прав" in result.error


def test_cluster_unreachable_is_structured_result(config, store):
    core = SimpleNamespace(read_namespaced_pod_log=raising(ConnectionError("connection refused")))

    result = k8s_logs(
        "api", namespace="payments", clients=make_clients(core=core), store=store, config=config
    )

    assert result.status == "error"
    assert "кластер недоступен" in result.error


def test_missing_kubeconfig_is_structured_result(config, store, monkeypatch):
    """Клиент не собрался — это тоже результат, а не падение процесса."""
    import k8s_incident_agent.tools.readonly as readonly

    monkeypatch.setattr(
        readonly, "build_readonly_clients", raising(K8sClientError("нет файла kubeconfig"))
    )
    result = k8s_describe("Pod", "api", "payments", store=store, config=config)

    assert result.status == "error"
    assert "kubeconfig" in result.error


def test_unsupported_kind_is_structured_result(config, store):
    result = k8s_get_manifest(
        "CustomResourceDefinition",
        "widgets",
        "payments",
        clients=make_clients(),
        store=store,
        config=config,
    )

    assert result.status == "error"
    assert "не поддерживается" in result.error


@pytest.mark.parametrize("status", [404, 503])
def test_top_degrades_without_metrics_server(config, store, status):
    custom = SimpleNamespace(list_namespaced_custom_object=raising(api_error(status, "no metrics")))

    result = k8s_top("payments", clients=make_clients(custom=custom), store=store, config=config)

    assert result.status == "degraded"
    assert "metrics-server" in result.summary
    assert result.data is None
    assert store.get(result.evidence_id) is not None


def test_top_other_api_error_is_error(config, store):
    custom = SimpleNamespace(list_namespaced_custom_object=raising(api_error(403, "forbidden")))

    result = k8s_top("payments", clients=make_clients(custom=custom), store=store, config=config)

    assert result.status == "error"


def test_top_success(config, store):
    metrics = {
        "items": [
            {
                "metadata": {"name": "api-7d9f"},
                "containers": [{"name": "api", "usage": {"cpu": "12m", "memory": "180Mi"}}],
            }
        ]
    }
    custom = SimpleNamespace(list_namespaced_custom_object=lambda **_: metrics)

    result = k8s_top("payments", clients=make_clients(custom=custom), store=store, config=config)

    assert result.status == "ok"
    assert result.data["pods"][0]["containers"][0]["memory"] == "180Mi"
