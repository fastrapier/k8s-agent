"""Ошибки кластера и мягкая деградация (ТЗ раздел 6, ARCHITECTURE §8)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from conftest import api_error, make_clients, raising
from k8s_incident_agent import cli
from k8s_incident_agent.tools import ToolSpec
from k8s_incident_agent.tools.readonly import k8s_describe, k8s_get_manifest, k8s_logs, k8s_top
from k8s_incident_agent.tools.schemas import ToolArgs, ToolResult


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


def test_missing_kubeconfig_is_structured_result(config, store):
    """Клиент не собрался — это тоже результат, а не падение процесса."""
    result = k8s_describe("Pod", "api", "payments", store=store, config=config)

    assert result.status == "error"
    assert "kubeconfig" in result.error
    assert "make stand-up" in result.error


@pytest.mark.parametrize("override", [False, True])
def test_cli_kubeconfig_override(config, monkeypatch, override):
    received = []

    def tool(**kwargs):
        received.append(kwargs)
        return ToolResult(tool="test", status="ok", evidence_id="ev-1", summary="готово")

    monkeypatch.setattr(cli, "get_config", lambda: config)
    monkeypatch.setattr(
        cli, "READONLY_TOOLS", {"test": ToolSpec("test", tool, ToolArgs, "тестовый инструмент")}
    )
    args = ["tool", "test"]
    if override:
        args += ["--kubeconfig", "~/admin.kubeconfig"]

    result = CliRunner().invoke(cli.app, args)

    assert result.exit_code == 0, result.output
    if override:
        passed = received[0]["config"]
        assert passed.k8s.readonly_kubeconfig == str(Path.home() / "admin.kubeconfig")
        assert passed.k8s.readwrite_kubeconfig == config.k8s.readwrite_kubeconfig
        assert passed is not config
        assert config.k8s.readonly_kubeconfig is None
        assert "ВНИМАНИЕ" in result.output
        assert "admin.kubeconfig" in result.output
    else:
        assert received == [{}]
        assert "ВНИМАНИЕ" not in result.output


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
