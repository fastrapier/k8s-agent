"""Загрузка конфига и маскирование токена в выводе (М0, N3/N6)."""

from __future__ import annotations

from pathlib import Path

import pytest

from k8s_incident_agent.config import find_repo_root, load_config, redacted_dict
from k8s_incident_agent.masking import MASK

CONFIG_TEMPLATE = """
budget: {max_tool_calls: 15, max_seconds: 180, max_review_iterations: 2}
llm:
  base_url: ${LLM_API_URL}
  api_key: ${LLM_API_TOKEN}
  model: deepseek-v3.2
tools: {max_output_chars: 8000, default_log_tail: 200}
k8s:
  context: kind-k8s-incident-agent
  default_namespace: default
  readonly_kubeconfig: .kube/agent-ro.kubeconfig
  readwrite_kubeconfig: .kube/agent-rw.kubeconfig
"""


def _write(tmp_path: Path, env_body: str) -> tuple[Path, Path]:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(CONFIG_TEMPLATE, encoding="utf-8")
    env_path = tmp_path / "local.env"
    env_path.write_text(env_body, encoding="utf-8")
    return config_path, env_path


def test_env_placeholders_resolved_from_env_file(tmp_path):
    config_path, env_path = _write(
        tmp_path, "LLM_API_TOKEN=super-token\nLLM_API_URL=https://gateway.example/v1\n"
    )
    config = load_config(config_path, env_path)

    assert config.llm.base_url == "https://gateway.example/v1"
    assert config.llm.api_key.get_secret_value() == "super-token"


def test_kubeconfig_paths_resolved_from_config_directory(tmp_path, monkeypatch):
    directory = tmp_path / "settings"
    directory.mkdir()
    config_path, env_path = _write(directory, "")
    monkeypatch.chdir(tmp_path)

    config = load_config(config_path.relative_to(tmp_path), env_path)

    assert config.k8s.readonly_kubeconfig == str(directory / ".kube/agent-ro.kubeconfig")
    assert config.k8s.readwrite_kubeconfig == str(directory / ".kube/agent-rw.kubeconfig")


@pytest.mark.parametrize("value", ["/absolute/agent.kubeconfig", None])
def test_absolute_and_missing_kubeconfig_paths_unchanged(tmp_path, value):
    config_path, env_path = _write(tmp_path, "")
    config_path.write_text(
        CONFIG_TEMPLATE.replace(".kube/agent-ro.kubeconfig", value or "null").replace(
            ".kube/agent-rw.kubeconfig", value or "null"
        ),
        encoding="utf-8",
    )

    config = load_config(config_path, env_path)

    assert config.k8s.readonly_kubeconfig == value
    assert config.k8s.readwrite_kubeconfig == value


def test_kubeconfig_tilde_expanded(tmp_path):
    config_path, env_path = _write(tmp_path, "")
    config_path.write_text(
        CONFIG_TEMPLATE.replace(".kube/agent-ro.kubeconfig", "~/agent-ro.kubeconfig"),
        encoding="utf-8",
    )

    config = load_config(config_path, env_path)

    assert config.k8s.readonly_kubeconfig == str(Path.home() / "agent-ro.kubeconfig")


def test_missing_env_placeholder_becomes_none(tmp_path, monkeypatch):
    monkeypatch.delenv("LLM_API_URL", raising=False)
    config_path, env_path = _write(tmp_path, "")

    assert load_config(config_path, env_path).llm.base_url is None


def test_token_never_appears_in_string_form(tmp_path):
    """SecretStr защищает от случайной печати конфига в лог."""
    config_path, env_path = _write(tmp_path, "LLM_API_TOKEN=super-token\n")
    config = load_config(config_path, env_path)

    assert "super-token" not in str(config)
    assert "super-token" not in repr(config)


def test_redacted_dict_masks_token(tmp_path):
    config_path, env_path = _write(tmp_path, "LLM_API_TOKEN=super-token\n")
    config = load_config(config_path, env_path)

    data = redacted_dict(config)
    assert data["llm"]["api_key"] == MASK
    assert "super-token" not in str(data)


def test_env_variable_wins_over_env_file(tmp_path, monkeypatch):
    config_path, env_path = _write(tmp_path, "LLM_API_URL=https://from-file/v1\n")
    monkeypatch.setenv("LLM_API_URL", "https://from-env/v1")

    assert load_config(config_path, env_path).llm.base_url == "https://from-env/v1"


def test_repo_config_holds_budget_numbers_from_tz():
    """Числа бюджета живут в конфиге, а не в коде (ТЗ N3, F5)."""
    config = load_config(find_repo_root() / "config.yaml", Path("/nonexistent.env"))

    assert config.budget.max_tool_calls == 15
    assert config.budget.max_seconds == 180
    assert config.budget.max_review_iterations == 2
    assert config.k8s.context == "kind-k8s-incident-agent"
