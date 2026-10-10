"""Эпизодическая память (F8, S5, MEMORY §3): запись, фильтры до ранжирования, замещение дублей."""

from __future__ import annotations

from datetime import UTC, datetime

from k8s_incident_agent.memory import MemoryStore, fts_query
from k8s_incident_agent.schemas import IncidentRecord

NOW = datetime(2026, 10, 10, tzinfo=UTC)


def record(**overrides) -> IncidentRecord:
    data = dict(
        namespace="payments", kinds=["Deployment"], objects=["payments/api"],
        symptoms="Под api в CrashLoopBackOff", root_cause="нет переменной DB_PASSWORD",
        fix="env DB_PASSWORD из secretKeyRef payments-db/password", fix_applied=True,
        outcome="resolved", evidence_ids=["ev-003"])
    return IncidentRecord(**{**data, **overrides})


def test_write_and_find_by_symptom():
    store = MemoryStore()
    incident_id = store.write(record(), created_at="2026-09-14T10:00:00+00:00")
    assert incident_id == "inc-2026-09-14-001"
    found = store.search("api в payments падает в CrashLoopBackOff", now=NOW)
    assert [p.id for p in found] == [incident_id]
    assert found[0].objects == ["payments/api"]


def test_ids_are_sequential_per_day():
    store = MemoryStore()
    day = "2026-09-14T10:00:00+00:00"
    assert store.write(record(), created_at=day) == "inc-2026-09-14-001"
    assert store.write(record(objects=["payments/web"], fix="другое"), created_at=day) \
        == "inc-2026-09-14-002"


def test_namespace_is_a_filter_not_a_preference():
    store = MemoryStore()
    store.write(record(namespace="billing", objects=["billing/api"]))
    assert store.search("api CrashLoopBackOff", namespace="payments", now=NOW) == []
    assert len(store.search("api CrashLoopBackOff", namespace="billing", now=NOW)) == 1


def test_unresolved_is_stored_but_not_found():
    store = MemoryStore()
    incident_id = store.write(record(outcome="unresolved", fix_applied=False))
    assert store.get(incident_id) is not None
    assert store.search("api CrashLoopBackOff", now=NOW) == []


def test_duplicate_supersedes_old_record():
    store = MemoryStore()
    old = store.write(record(), created_at="2026-09-14T10:00:00+00:00")
    new = store.write(record(), created_at="2026-10-10T10:00:00+00:00")
    assert [p.id for p in store.search("api CrashLoopBackOff", now=NOW)] == [new]
    assert store.db.execute("SELECT superseded_by FROM incidents WHERE id = ?",
                            (old,)).fetchone()[0] == new


def test_other_fix_on_same_object_is_not_a_duplicate():
    store = MemoryStore()
    store.write(record())
    store.write(record(root_cause="OOMKilled", fix="limits.memory 256Mi"))
    assert len(store.search("api CrashLoopBackOff OOMKilled", now=NOW)) == 2


def test_stale_record_ranks_lower():
    store = MemoryStore()
    store.write(record(objects=["payments/old"], fix="a"), created_at="2025-01-01T00:00:00+00:00")
    store.write(record(objects=["payments/new"], fix="b"), created_at="2026-10-01T00:00:00+00:00")
    found = store.search("api CrashLoopBackOff DB_PASSWORD", now=NOW)
    assert [p.objects for p in found] == [["payments/new"], ["payments/old"]]


def test_secrets_are_masked_before_storage():
    store = MemoryStore()
    incident_id = store.write(record(fix="задать DB_PASSWORD=hunter2-very-secret-42"))
    stored = store.get(incident_id)
    assert "hunter2" not in stored.fix and "***masked***" in stored.fix


def test_long_fields_are_truncated():
    store = MemoryStore()
    stored = store.get(store.write(record(symptoms="лог " * 1000)))
    assert len(stored.symptoms) <= 600


def test_seed_is_idempotent(tmp_path):
    fixture = tmp_path / "past.yaml"
    fixture.write_text(
        "- id: inc-2026-09-14-001\n  created_at: '2026-09-14T10:00:00+00:00'\n"
        "  namespace: payments\n  kinds: [Deployment]\n  objects: [payments/api]\n"
        "  symptoms: api в CrashLoopBackOff\n  root_cause: нет DB_PASSWORD\n"
        "  fix: env из secretKeyRef\n  fix_applied: true\n  outcome: resolved\n"
        "  evidence_ids: [ev-003]\n", encoding="utf-8")
    store = MemoryStore()
    assert store.seed(fixture) == ["inc-2026-09-14-001"]
    assert store.seed(fixture) == ["inc-2026-09-14-001"]
    assert len(store.all()) == 1
    assert len(store.search("CrashLoopBackOff", now=NOW)) == 1


def test_fts_query_is_safe_and_stems():
    assert fts_query('api: NEAR "x" OR падает') == '"api"* OR "near"* OR "пада"*'
    assert fts_query("?? !!") == ""
