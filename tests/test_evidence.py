"""Разбор ответов API в свидетельства (F3, ARCHITECTURE §5)."""

from __future__ import annotations

from types import SimpleNamespace

from conftest import make_clients, pod_manifest
from k8s_incident_agent.evidence import EvidenceStore
from k8s_incident_agent.tools.readonly import k8s_events, k8s_list_pods, k8s_rollout_history


def test_evidence_ids_are_short_and_sequential():
    store = EvidenceStore()
    first = store.add("k8s_list_pods", {"namespace": "payments"}, "первое", raw="raw text")
    second = store.add("k8s_events", {"namespace": "payments"}, "второе")

    assert first.id == "ev-001"
    assert second.id == "ev-002"
    assert first.raw_ref == "raw-001"
    assert store.raw(first.raw_ref) == "raw text"
    assert second.raw_ref is None
    assert store.get("ev-002").summary == "второе"
    assert len(store) == 2


def test_evidence_schema_fields():
    """Схема свидетельства зафиксирована в ARCHITECTURE §5."""
    store = EvidenceStore()
    evidence = store.add("k8s_logs", {"pod": "api"}, "лог собран")
    assert set(evidence.model_dump()) == {"id", "tool", "args", "ts", "summary", "raw_ref"}
    assert evidence.tool == "k8s_logs"
    assert evidence.args == {"pod": "api"}
    assert evidence.ts.endswith("+00:00")


def test_list_pods_parsed_into_rows_and_evidence(config, store):
    listing = {"items": [pod_manifest(), pod_manifest("api-abc1", reason=None)]}
    core = SimpleNamespace(list_namespaced_pod=lambda namespace, label_selector=None: listing)

    result = k8s_list_pods("payments", clients=make_clients(core=core), store=store, config=config)

    assert result.status == "ok"
    assert result.data["count"] == 2
    row = result.data["pods"][0]
    assert row["name"] == "api-7d9f"
    assert row["restarts"] == 7
    assert row["ready"] == "0/1"
    assert row["reason"] == "CrashLoopBackOff"
    assert "CrashLoopBackOff" in result.summary

    evidence = store.get(result.evidence_id)
    assert evidence.tool == "k8s_list_pods"
    assert evidence.args == {"namespace": "payments", "selector": None}
    assert evidence.raw_ref is not None


def test_list_pods_uses_default_namespace_from_config(config, store):
    captured = {}

    def list_pods(namespace, label_selector=None):
        captured["namespace"] = namespace
        return {"items": []}

    k8s_list_pods(
        clients=make_clients(core=SimpleNamespace(list_namespaced_pod=list_pods)),
        store=store,
        config=config,
    )
    # имя namespace приходит из конфига, в коде инструмента его нет
    assert captured["namespace"] == config.k8s.default_namespace


def test_events_sorted_and_counted(config, store):
    listing = {
        "items": [
            {
                "type": "Warning",
                "reason": "BackOff",
                "message": "Back-off restarting failed container",
                "count": 12,
                "lastTimestamp": "2026-09-28T10:05:00Z",
                "involvedObject": {"kind": "Pod", "name": "api-7d9f"},
            },
            {
                "type": "Normal",
                "reason": "Pulled",
                "message": "Container image already present",
                "count": 1,
                "lastTimestamp": "2026-09-28T10:00:00Z",
                "involvedObject": {"kind": "Pod", "name": "api-7d9f"},
            },
        ]
    }
    core = SimpleNamespace(list_namespaced_event=lambda namespace, field_selector=None: listing)

    result = k8s_events(
        "payments", "api-7d9f", clients=make_clients(core=core), store=store, config=config
    )

    assert [e["reason"] for e in result.data["events"]] == ["Pulled", "BackOff"]
    assert "Warning: 1" in result.summary


def test_rollout_history_revisions_sorted(config, store):
    deployment = {
        "metadata": {"name": "api", "annotations": {"deployment.kubernetes.io/revision": "2"}},
        "spec": {"selector": {"matchLabels": {"app": "api"}}},
    }
    replica_sets = {
        "items": [
            {
                "metadata": {
                    "name": "api-2",
                    "annotations": {"deployment.kubernetes.io/revision": "2"},
                    "ownerReferences": [{"name": "api"}],
                },
                "spec": {"replicas": 2, "template": {"spec": {"containers": [{"image": "api:2"}]}}},
            },
            {
                "metadata": {
                    "name": "api-1",
                    "annotations": {
                        "deployment.kubernetes.io/revision": "1",
                        "kubernetes.io/change-cause": "initial",
                    },
                    "ownerReferences": [{"name": "api"}],
                },
                "spec": {"replicas": 0, "template": {"spec": {"containers": [{"image": "api:1"}]}}},
            },
        ]
    }
    apps = SimpleNamespace(
        read_namespaced_deployment=lambda name, namespace: deployment,
        list_namespaced_replica_set=lambda namespace, label_selector=None: replica_sets,
    )

    result = k8s_rollout_history(
        "api", "payments", clients=make_clients(apps=apps), store=store, config=config
    )

    assert [r["revision"] for r in result.data["revisions"]] == ["1", "2"]
    assert result.data["current_revision"] == "2"
    assert result.data["revisions"][1]["images"] == ["api:2"]
