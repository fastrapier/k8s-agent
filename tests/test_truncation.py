"""Обрезка сырого вывода до лимита из конфига (ТЗ раздел 6)."""

from __future__ import annotations

from types import SimpleNamespace

from conftest import log_response, make_clients
from k8s_incident_agent.text import truncate
from k8s_incident_agent.tools.readonly import k8s_logs


def test_truncate_short_text_untouched():
    text, truncated = truncate("короткая строка", 100)
    assert text == "короткая строка"
    assert truncated is False


def test_truncate_marks_cut():
    text, truncated = truncate("x" * 500, 100)
    assert truncated is True
    assert text.startswith("x" * 100)
    assert "обрезано" in text
    assert "500" in text  # в пометке видно, сколько было на самом деле


def test_logs_truncated_to_config_limit(config, store):
    long_log = "\n".join(f"line {i} of application log" for i in range(500))
    core = SimpleNamespace(read_namespaced_pod_log=lambda **_: log_response(long_log))

    result = k8s_logs(
        "api-7d9f",
        namespace="payments",
        clients=make_clients(core=core),
        store=store,
        config=config,
    )

    assert result.truncated is True
    assert "обрезано" in result.data["log"]
    assert len(result.data["log"]) < len(long_log)


def test_logs_pass_tail_and_previous_to_api(config, store):
    captured = {}

    def read_log(**kwargs):
        captured.update(kwargs)
        return log_response("boom")

    k8s_logs(
        "api-7d9f",
        container="api",
        previous=True,
        namespace="payments",
        clients=make_clients(core=SimpleNamespace(read_namespaced_pod_log=read_log)),
        store=store,
        config=config,
    )

    assert captured["previous"] is True
    assert captured["container"] == "api"
    # tail не задан -> берётся значение из конфига, а не жёстко зашитое в коде
    assert captured["tail_lines"] == config.tools.default_log_tail


def test_logs_decoded_as_lines_not_bytes_repr(config, store):
    """Живой прогон 2026-10-09: клиент отдавал «b'...\\n...'» одной строкой, и FATAL-строка S1
    тонула в repr байтов. Лог читается без предзагрузки и декодируется явно."""
    captured = {}
    log = "[api] starting\n[api] FATAL: environment variable DB_PASSWORD is not set\n"

    def read_log(**kwargs):
        captured.update(kwargs)
        return log_response(log)

    result = k8s_logs(
        "api-7d9f",
        previous=True,
        namespace="payments",
        clients=make_clients(core=SimpleNamespace(read_namespaced_pod_log=read_log)),
        store=store,
        config=config,
    )

    assert captured["_preload_content"] is False
    assert result.data["log"] == log
    assert "2 строк" in result.summary
    assert "FATAL: environment variable DB_PASSWORD is not set" in result.summary
