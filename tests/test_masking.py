"""Маскирование секретов — главный тест набора (ISOLATION §6, EVALS уровень 2).

Критерий из docs/EVALS.md: «ноль совпадений по значению в выходе (grep)».
Поэтому проверяется не только структура, но и отсутствие подстроки во всём
сериализованном результате и в сыром выводе свидетельства.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

from conftest import SECRET_VALUE, make_clients, secret_manifest
from k8s_incident_agent.masking import MASK, mask_object, mask_text
from k8s_incident_agent.tools.readonly import k8s_get_manifest


def test_secret_data_masked_keys_preserved():
    masked = mask_object(secret_manifest())
    assert masked["data"] == {"DB_PASSWORD": MASK, "DB_USER": MASK}
    assert SECRET_VALUE not in json.dumps(masked)


def test_secret_last_applied_annotation_is_masked_before_evidence(config, store):
    """Найдено живым прогоном М1: `kubectl apply` кладёт исходный `stringData` в аннотацию."""
    manifest = secret_manifest()
    manifest["metadata"]["annotations"] = {
        "kubectl.kubernetes.io/last-applied-configuration": json.dumps(
            {"kind": "Secret", "stringData": {"password": SECRET_VALUE}}
        ),
        "operator.example/note": f"credential={SECRET_VALUE}",
    }
    core = SimpleNamespace(read_namespaced_secret=lambda name, namespace: manifest)
    result = k8s_get_manifest(
        "Secret", "db-credentials", "payments",
        clients=make_clients(core=core), store=store, config=config,
    )

    assert result.status == "ok"
    assert SECRET_VALUE not in json.dumps(result.model_dump(), ensure_ascii=False)
    assert SECRET_VALUE not in (store.raw(store.get(result.evidence_id).raw_ref) or "")
    assert MASK in result.data["manifest_yaml"]


def test_annotations_of_other_objects_not_masked():
    """У Deployment аннотации нужны диагносту: по ним `k8s_rollout_history` ищет ревизию."""
    annotations = {
        "deployment.kubernetes.io/revision": "3",
        "kubectl.kubernetes.io/last-applied-configuration": '{"kind": "Deployment"}',
    }
    manifest = {"kind": "Deployment", "metadata": {"annotations": dict(annotations)}}
    assert mask_object(manifest)["metadata"]["annotations"] == annotations


def test_string_data_masked_even_without_kind():
    """`stringData` есть только у Secret, поэтому маскируется независимо от kind."""
    masked = mask_object({"stringData": {"token": "plain-text-token"}})
    assert masked["stringData"] == {"token": MASK}


def test_configmap_data_not_masked():
    """ConfigMap — не секрет: его данные нужны диагносту целиком."""
    manifest = {"kind": "ConfigMap", "data": {"LOG_LEVEL": "debug"}}
    assert mask_object(manifest)["data"] == {"LOG_LEVEL": "debug"}


def test_secret_list_items_masked():
    """У элементов SecretList поле kind пустое — маскирование идёт от типа списка."""
    item = dict(secret_manifest(), kind=None)
    item["metadata"]["annotations"] = {"note": SECRET_VALUE}
    masked = mask_object({"kind": "SecretList", "items": [item]})
    assert masked["items"][0]["data"]["DB_PASSWORD"] == MASK
    assert masked["items"][0]["metadata"]["annotations"] == {"note": MASK}
    assert SECRET_VALUE not in json.dumps(masked)


def test_env_secret_key_ref_keeps_key_name_not_value():
    """ISOLATION §6: имя ключа остаётся (оно нужно для диагноза), значение — нет."""
    env = {
        "name": "DB_PASSWORD",
        "value": "leaked-value",
        "valueFrom": {"secretKeyRef": {"name": "db-credentials", "key": "DB_PASSWORD"}},
    }
    masked = mask_object({"kind": "Deployment", "env": [env]})["env"][0]
    assert masked["name"] == "DB_PASSWORD"
    assert masked["valueFrom"]["secretKeyRef"]["key"] == "DB_PASSWORD"
    assert masked["value"] == MASK


def test_plain_env_value_survives():
    """Обычная переменная окружения не маскируется: на ней строится диагноз S1."""
    env = {"name": "LOG_LEVEL", "value": "debug"}
    assert mask_object({"kind": "Deployment", "env": [env]})["env"][0]["value"] == "debug"


def test_mask_text_heuristics():
    text = "\n".join(
        [
            "Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.payloadpayload.signaturesig",
            "aws key AKIAIOSFODNN7EXAMPLE used",
            'DB_PASSWORD="hunter2"',
            "api_key=abcdef123456",
        ]
    )
    masked = mask_text(text)
    assert "eyJhbGciOiJIUzI1NiJ9" not in masked
    assert "AKIAIOSFODNN7EXAMPLE" not in masked
    assert "hunter2" not in masked
    assert "abcdef123456" not in masked
    assert masked.count(MASK) >= 4


def test_mask_text_keeps_image_digest():
    """Ложные срабатывания дорого стоят: sha256-дайджест образа нужен диагносту."""
    line = "image registry.local/api@sha256:" + "a1b2c3d4" * 8
    assert mask_text(line) == line


def test_get_manifest_masks_before_evidence(config, store):
    """Свидетельство пишется уже замаскированным (ISOLATION §6: фильтр до записи)."""
    core = SimpleNamespace(read_namespaced_secret=lambda name, namespace: secret_manifest(name))
    result = k8s_get_manifest(
        "Secret",
        "db-credentials",
        "payments",
        clients=make_clients(core=core),
        store=store,
        config=config,
    )

    assert result.status == "ok"
    assert SECRET_VALUE not in json.dumps(result.model_dump(), ensure_ascii=False)

    evidence = store.get(result.evidence_id)
    assert SECRET_VALUE not in (store.raw(evidence.raw_ref) or "")
    assert MASK in (store.raw(evidence.raw_ref) or "")


def test_get_manifest_drops_managed_fields_and_last_applied(config, store):
    """Служебные копии манифеста вытесняли из промпта env: 6 из 7.7 тыс. символов."""
    deployment = {
        "kind": "Deployment",
        "metadata": {"name": "api", "uid": "u-1", "generation": 2,
                     "managedFields": [{"manager": "kubectl", "fieldsV1": {"f:spec": {}}}],
                     "annotations": {"kubectl.kubernetes.io/last-applied-configuration": "{...}",
                                     "deployment.kubernetes.io/revision": "3"}},
        "spec": {"replicas": 1},
    }
    apps = SimpleNamespace(read_namespaced_deployment=lambda name, namespace: deployment)
    result = k8s_get_manifest("Deployment", "api", "payments", clients=make_clients(apps=apps),
                              store=store, config=config)
    raw = store.raw(store.get(result.evidence_id).raw_ref)
    assert "managedFields" not in raw and "last-applied-configuration" not in raw
    assert "deployment.kubernetes.io/revision" in raw and "u-1" in raw   # нужное осталось
