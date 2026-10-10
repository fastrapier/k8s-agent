"""Разделение контуров чтения и записи (N1, docs/ISOLATION.md §5, EVALS уровень 2).

Требование дословно: «модуль read-only инструментов не импортирует модуль патча.
Тест на это входит в юнит-набор».

Тест устроен так, чтобы продолжить работать, когда модуль патча появится на этапе М4:
он не проверяет отсутствие файла, он обходит граф импортов от `tools/readonly.py`
вглубь по пакету и убеждается, что контур записи в него не втянут ни напрямую,
ни транзитивно.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

import k8s_incident_agent
from k8s_incident_agent.tools import READONLY_TOOLS

PACKAGE = "k8s_incident_agent"
PACKAGE_ROOT = Path(k8s_incident_agent.__file__).parent

# Модули контура записи. Исполнитель живёт вне tools/ (каталог tools/ — только read-only,
# см. тест ниже); прежние имена оставлены, чтобы файл с таким именем тоже был пойман.
WRITE_MODULES = {f"{PACKAGE}.executor", f"{PACKAGE}.tools.patch", f"{PACKAGE}.tools.executor"}

# Имена контура записи, которых не должно быть в модулях каталога tools/.
WRITE_NAMES = {"build_readwrite_clients", "ReadWriteClients", "k8s_patch", "k8s_rollout_restart"}

ENTRY_MODULE = f"{PACKAGE}.tools.readonly"


def _module_file(module: str) -> Path | None:
    relative = Path(*module.split(".")[1:])
    candidates = (
        PACKAGE_ROOT / relative.with_suffix(".py"),
        PACKAGE_ROOT / relative / "__init__.py",
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def _imports(tree: ast.AST) -> set[str]:
    """Все имена модулей, которые импортирует файл (включая `from x import y` как `x.y`)."""
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
            found.update(f"{node.module}.{alias.name}" for alias in node.names)
    return found


def _reachable(entry: str) -> dict[str, set[str]]:
    """Обход графа импортов внутри пакета: модуль -> что он импортирует."""
    graph: dict[str, set[str]] = {}
    queue = [entry]
    while queue:
        module = queue.pop()
        if module in graph:
            continue
        path = _module_file(module)
        if path is None:
            continue
        imports = _imports(ast.parse(path.read_text(encoding="utf-8")))
        graph[module] = imports
        for name in imports:
            if name.startswith(f"{PACKAGE}.") and name not in graph:
                queue.append(name)
    return graph


def test_entry_module_is_analyzable():
    """Страховка от «зелёного из-за опечатки»: точка входа действительно разобрана."""
    graph = _reachable(ENTRY_MODULE)
    assert ENTRY_MODULE in graph
    assert f"{PACKAGE}.k8s_clients" in graph  # модуль клиентов реально достижим


def test_readonly_tools_do_not_import_write_contour():
    graph = _reachable(ENTRY_MODULE)
    for module, imports in graph.items():
        leaked = {name for name in imports if name in WRITE_MODULES}
        assert not leaked, f"{module} импортирует контур записи: {sorted(leaked)}"


def test_tools_package_does_not_reference_write_names():
    """`k8s_clients` вправе определять оба клиента, но каталог tools/ — только read-only."""
    for path in (PACKAGE_ROOT / "tools").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        used = {
            node.id if isinstance(node, ast.Name) else node.attr
            for node in ast.walk(tree)
            if isinstance(node, (ast.Name, ast.Attribute))
        }
        used |= {name.split(".")[-1] for name in _imports(tree)}
        forbidden = used & WRITE_NAMES
        assert not forbidden, f"{path.name} использует имена контура записи: {sorted(forbidden)}"


def test_registry_contains_only_readonly_tools():
    """Инструмент, которого нет в реестре, модель вызвать не может (L3)."""
    assert set(READONLY_TOOLS) == {
        "k8s_list_pods",
        "k8s_describe",
        "k8s_events",
        "k8s_logs",
        "k8s_rollout_history",
        "k8s_get_manifest",
        "k8s_top",
    }


@pytest.mark.parametrize("name", sorted(READONLY_TOOLS))
def test_every_tool_has_argument_schema(name):
    """ТЗ раздел 6: реестр инструментов со схемой через pydantic."""
    schema = READONLY_TOOLS[name].json_schema()
    assert schema["type"] == "object"
    assert schema.get("additionalProperties") is False
