"""Белый список патчей исполнителя (ISOLATION §6, §10 №6).

Белый список, а не чёрный: всё, чего нет в списке, отклоняется — в том числе поля, о которых
никто не подумал. Тесты проверяют и то, что нужный для S1 патч проходит.
"""

from __future__ import annotations

import pytest

from k8s_incident_agent.executor import (
    PatchRejected,
    patch_paths,
    strip_identity,
    validate_patch,
)

S1_FIX = {"spec": {"template": {"spec": {"containers": [{"name": "api", "env": [
    {"name": "DB_PASSWORD",
     "valueFrom": {"secretKeyRef": {"name": "payments-db", "key": "password"}}}]}]}}}}


def test_patch_paths_flatten_lists():
    assert sorted(patch_paths(S1_FIX)) == [
        "spec.template.spec.containers[*].env[*].name",
        "spec.template.spec.containers[*].env[*].valueFrom.secretKeyRef.key",
        "spec.template.spec.containers[*].env[*].valueFrom.secretKeyRef.name",
        "spec.template.spec.containers[*].name",
    ]


@pytest.mark.parametrize("patch", [
    S1_FIX,
    {"spec": {"replicas": 3}},
    {"spec": {"template": {"metadata": {"annotations": {
        "kubectl.kubernetes.io/restartedAt": "x"}}}}},
    {"spec": {"template": {"spec": {"containers": [{"name": "api", "resources": {
        "limits": {"memory": "256Mi"}}}]}}}},
], ids=["s1-env", "replicas", "restart-annotation", "memory-limit"])
def test_allowed_deployment_patches(patch):
    validate_patch("Deployment", "payments", patch)


@pytest.mark.parametrize("patch", [
    {"spec": {"template": {"spec": {"hostPID": True}}}},
    {"spec": {"template": {"spec": {"hostNetwork": True}}}},
    {"spec": {"template": {"spec": {"volumes": [{"name": "h", "hostPath": {"path": "/"}}]}}}},
    {"spec": {"template": {"spec": {"initContainers": [{"name": "x", "image": "evil"}]}}}},
    {"spec": {"template": {"spec": {"containers": [{"name": "api", "image": "evil:latest"}]}}}},
    {"spec": {"template": {"spec": {"containers": [{"name": "api", "securityContext": {
        "privileged": True}}]}}}},
    {"spec": {"template": {"spec": {"serviceAccountName": "default"}}}},
    {"metadata": {"labels": {"x": "y"}}},
], ids=["hostPID", "hostNetwork", "hostPath", "initContainers", "image", "privileged",
        "serviceAccount", "labels"])
def test_dangerous_paths_rejected(patch):
    with pytest.raises(PatchRejected):
        validate_patch("Deployment", "payments", patch)


@pytest.mark.parametrize(("kind", "namespace", "patch"), [
    ("Secret", "payments", {"data": {"password": "eA=="}}),
    ("Deployment", "kube-system", {"spec": {"replicas": 0}}),
    ("Deployment", "payments", {}),
    ("Service", "payments", {"spec": {"selector": {"version": "v2"}}}),
])
def test_kind_namespace_and_empty_rejected(kind, namespace, patch):
    with pytest.raises(PatchRejected):
        validate_patch(kind, namespace, patch)


def test_configmap_data_allowed():
    validate_patch("ConfigMap", "payments", {"data": {"FEATURE_FLAGS_URL": "http://flags"}})


def test_full_object_patch_reduced_to_changes():
    """Патч полным объектом: идентифицирующие поля снимаются, остальное — белый список."""
    full = {"apiVersion": "apps/v1", "kind": "Deployment",
            "metadata": {"name": "api", "namespace": "payments"}, **S1_FIX}
    assert strip_identity("Deployment", "api", "payments", full) == S1_FIX


@pytest.mark.parametrize("identity", [
    {"kind": "DaemonSet"},
    {"metadata": {"name": "checkout"}},
    {"metadata": {"namespace": "kube-system"}},
])
def test_identity_mismatch_rejected(identity):
    with pytest.raises(PatchRejected):
        strip_identity("Deployment", "api", "payments", {**S1_FIX, **identity})


def test_labels_in_metadata_still_checked():
    patch = strip_identity("Deployment", "api", "payments",
                           {"metadata": {"name": "api", "labels": {"x": "y"}}})
    with pytest.raises(PatchRejected):
        validate_patch("Deployment", "payments", patch)
