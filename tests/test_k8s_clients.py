"""Контуры не используют учётку по умолчанию при отсутствии своего kubeconfig."""

from pathlib import Path

import pytest

from k8s_incident_agent.config import find_repo_root, load_config
from k8s_incident_agent.k8s_clients import (
    K8sClientError,
    build_readonly_clients,
    build_readwrite_clients,
    k8s_config,
)


@pytest.mark.parametrize(
    ("build_clients", "field"),
    [
        (build_readonly_clients, "readonly_kubeconfig"),
        (build_readwrite_clients, "readwrite_kubeconfig"),
    ],
)
@pytest.mark.parametrize("missing_file", [False, True])
def test_missing_kubeconfig_fails_before_loading(
    config, tmp_path, monkeypatch, build_clients, field, missing_file
):
    def unexpected_load(**_kwargs):
        pytest.fail("загрузка kubeconfig не должна вызываться")

    monkeypatch.setattr(k8s_config, "new_client_from_config", unexpected_load)
    path = str(tmp_path / "missing.kubeconfig") if missing_file else None
    setattr(config.k8s, field, path)

    with pytest.raises(K8sClientError, match="make stand-up") as error:
        build_clients(config)

    if missing_file:
        assert path in str(error.value)
    else:
        assert "намеренно не используется" in str(error.value)
        assert "администратор" in str(error.value)


def test_repo_config_uses_separate_kubeconfigs():
    root = find_repo_root()
    config = load_config(root / "config.yaml", Path("/nonexistent.env"))

    assert config.k8s.readonly_kubeconfig == str(root / ".kube/agent-ro.kubeconfig")
    assert config.k8s.readwrite_kubeconfig == str(root / ".kube/agent-rw.kubeconfig")
    assert config.k8s.readonly_kubeconfig != config.k8s.readwrite_kubeconfig
