"""Контур записи: проверка патча по белому списку и применение (F7, ISOLATION §5–6).

Этот модуль — единственное место, где используется учётка исполнителя (`incident-agent-rw`).
Диагностический контур его не импортирует (tests/test_isolation_imports.py), а граф вызывает
его только после подтверждения человеком (узел approval, `interrupt()`).

Белый список — именно список разрешённого, а не перечень запрещённого: поле, о котором не
подумали (`hostPID`, `volumes[].hostPath`, `initContainers`, …), не пройдёт молча. Нарушение —
отказ кодом, а не предупреждение модели.
"""

from __future__ import annotations

import time
from typing import Any

from kubernetes.client.exceptions import ApiException

from k8s_incident_agent.config import AppConfig, get_config
from k8s_incident_agent.k8s_clients import (
    ReadOnlyClients,
    ReadWriteClients,
    build_readonly_clients,
    build_readwrite_clients,
)

# Где исполнителю разрешено что-то менять. Совпадает с Role incident-agent-rw (RBAC):
# код и RBAC — два независимых рубежа, и расширять их нужно осознанно, вместе.
ALLOWED_NAMESPACES = {"payments"}

# Переменная окружения — только значение или ссылка на ключ Secret/ConfigMap. fieldRef и
# resourceFieldRef не нужны ни одному сценарию; `env: null` или `env: []` стёрли бы весь список.
_ENV = "spec.template.spec.containers[*].env[*]"
_WORKLOAD = (
    "spec.replicas",
    "spec.template.metadata.annotations",            # rollout restart — это аннотация
    "spec.template.spec.containers[*].name",         # ключ слияния: без него не указать контейнер
    f"{_ENV}.name",
    f"{_ENV}.value",
    f"{_ENV}.valueFrom.secretKeyRef.name",
    f"{_ENV}.valueFrom.secretKeyRef.key",
    f"{_ENV}.valueFrom.configMapKeyRef.name",
    f"{_ENV}.valueFrom.configMapKeyRef.key",
    "spec.template.spec.containers[*].resources.limits",     # OOMKilled — поднять лимит памяти
    "spec.template.spec.containers[*].resources.requests",
)
# kind -> разрешённые пути strategic merge patch. `[*]` — любой элемент списка.
ALLOWED_PATHS: dict[str, tuple[str, ...]] = {
    "Deployment": _WORKLOAD,
    "StatefulSet": _WORKLOAD,
    "ConfigMap": ("data",),
}


class PatchRejected(ValueError):
    """Патч не прошёл белый список. Применять его нельзя ни при каком подтверждении."""


def patch_paths(node: Any, prefix: str = "") -> list[str]:
    """Пути всех листьев патча: {"spec": {"replicas": 2}} -> ["spec.replicas"]."""
    if isinstance(node, dict) and node:
        paths = []
        for key, value in node.items():
            paths += patch_paths(value, f"{prefix}.{key}" if prefix else str(key))
        return paths
    if isinstance(node, list) and node:
        return [path for item in node for path in patch_paths(item, prefix + "[*]")]
    return [prefix]


def _allowed(path: str, prefixes: tuple[str, ...]) -> bool:
    return any(path == p or path.startswith(p + ".") or path.startswith(p + "[") for p in prefixes)


def strip_identity(kind: str, name: str, namespace: str, patch: dict) -> dict:
    """Модели часто пишут патч как полный объект: apiVersion, kind, metadata.name/namespace.

    Эти поля ничего не меняют — они лишь называют объект. Если они совпадают с целью, они
    снимаются; если нет — это попытка патчить другой объект, и патч отклоняется. Всё остальное
    проходит через белый список как есть.
    """
    patch = dict(patch)
    patch.pop("apiVersion", None)
    if patch.pop("kind", kind) != kind:
        raise PatchRejected("kind в патче не совпадает с целевым объектом")
    meta = dict(patch.pop("metadata", None) or {})
    if meta.pop("name", name) != name or meta.pop("namespace", namespace) != namespace:
        raise PatchRejected("metadata.name/namespace в патче не совпадают с целевым объектом")
    if meta:
        patch["metadata"] = meta   # остальное в metadata (labels и т.п.) проверит белый список
    return patch


def _check_values(node: Any) -> None:
    """Белый список путей не видит семантики слияния: `null` удаляет поле целиком, а директивы
    `$patch`, `$retainKeys`, `$setElementOrder` меняют сам смысл merge. Ни то, ни другое агенту
    не нужно — запрещаем по значению, а не по пути."""
    if node is None:
        raise PatchRejected("null в патче удаляет поле целиком — запрещено")
    if isinstance(node, dict):
        for key, value in node.items():
            if str(key).startswith("$"):
                raise PatchRejected(f"директива слияния {key} запрещена")
            _check_values(value)
    elif isinstance(node, list):
        for item in node:
            _check_values(item)


def validate_patch(kind: str, namespace: str, patch: dict) -> list[str]:
    """Проверка по белому списку. Возвращает пути патча или бросает PatchRejected."""
    if kind not in ALLOWED_PATHS:
        raise PatchRejected(f"вид {kind} не разрешён исполнителю; "
                            f"разрешены: {sorted(ALLOWED_PATHS)}")
    if namespace not in ALLOWED_NAMESPACES:
        raise PatchRejected(f"namespace {namespace} не разрешён; "
                            f"разрешены: {sorted(ALLOWED_NAMESPACES)}")
    if not isinstance(patch, dict) or not patch:
        raise PatchRejected("патч пустой")
    _check_values(patch)
    paths = patch_paths(patch)
    denied = [p for p in paths if not _allowed(p, ALLOWED_PATHS[kind])]
    if denied:
        raise PatchRejected(f"пути вне белого списка для {kind}: {', '.join(sorted(set(denied)))}")
    replicas = (patch.get("spec") or {}).get("replicas")
    if replicas is not None and (not isinstance(replicas, int) or replicas < 1):
        # replicas: 0 «чинит» CrashLoop, выключая сервис, и пост-проверка сочла бы это успехом.
        raise PatchRejected("spec.replicas должен быть целым ≥ 1")
    return paths


def patch_containers(patch: dict) -> set[str]:
    """Имена контейнеров, которые затрагивает патч рабочей нагрузки."""
    containers = (((patch.get("spec") or {}).get("template") or {}).get("spec") or {}) \
        .get("containers") or []
    return {c.get("name") for c in containers if isinstance(c, dict)}


def _read(ro: ReadOnlyClients, kind: str, name: str, namespace: str) -> Any:
    if kind == "Deployment":
        return ro.apps.read_namespaced_deployment(name, namespace)
    if kind == "StatefulSet":
        return ro.apps.read_namespaced_stateful_set(name, namespace)
    return ro.core.read_namespaced_config_map(name, namespace)


def apply_patch(
    kind: str, name: str, namespace: str, patch: dict, *, expected: dict | None = None,
    rw: ReadWriteClients | None = None, ro: ReadOnlyClients | None = None,
    config: AppConfig | None = None, wait_seconds: int = 90,
) -> dict:
    """Применить проверенный патч и проверить результат (пост-проверка, F7).

    `expected` — uid и generation объекта, каким его видел агент, когда предлагал патч. Человек
    подтверждал патч к тому объекту; если его с тех пор пересоздали или изменили spec, патч не
    применяется — нужен новый прогон (между показом и подтверждением может пройти час).

    Отката нет намеренно (ARCHITECTURE §8): если пост-проверка не прошла, результат помечается
    `post_check_failed`, и человеку выдаётся команда отката — решение о ней за ним.
    """
    patch = strip_identity(kind, name, namespace, patch)
    validate_patch(kind, namespace, patch)
    config = config or get_config()
    ro = ro or build_readonly_clients(config)
    try:
        live = _read(ro, kind, name, namespace)
    except ApiException as exc:
        return {"status": "apply_failed", "detail": f"объект не прочитан: {exc.status}"}
    if expected:
        seen = (expected.get("uid"), expected.get("generation"))
        now = (live.metadata.uid, live.metadata.generation)
        if seen != now:
            return {"status": "stale", "detail": f"объект изменился после показа патча "
                    f"(uid/generation {seen} → {now}); патч не применён, нужен новый прогон"}
    if kind != "ConfigMap":
        unknown = patch_containers(patch) - {c.name for c in live.spec.template.spec.containers}
        if unknown:
            # Strategic merge добавил бы новый контейнер вместо правки существующего.
            return {"status": "apply_failed",
                    "detail": f"в {kind}/{name} нет контейнеров {sorted(unknown)}"}
    rw = rw or build_readwrite_clients(config)
    try:
        if kind == "Deployment":
            rw.apps.patch_namespaced_deployment(name, namespace, patch)
        elif kind == "StatefulSet":
            rw.apps.patch_namespaced_stateful_set(name, namespace, patch)
        else:
            rw.core.patch_namespaced_config_map(name, namespace, patch)
    except ApiException as exc:
        return {"status": "apply_failed", "detail": f"API-сервер вернул {exc.status}: {exc.reason}"}

    rollback = f"kubectl rollout undo {kind.lower()}/{name} -n {namespace}"
    if kind == "ConfigMap":
        return {"status": "applied",
                "detail": "ConfigMap обновлён; поды перечитают его при рестарте"}
    deadline = time.monotonic() + wait_seconds
    status: dict = {}
    while time.monotonic() < deadline:
        obj = _read(ro, kind, name, namespace)
        want = obj.spec.replicas or 0
        status = {"desired": want, "updated": obj.status.updated_replicas or 0,
                  "ready": obj.status.ready_replicas or 0}
        if (obj.status.observed_generation or 0) >= (obj.metadata.generation or 0) \
                and status["updated"] == want and status["ready"] == want:
            return {"status": "applied", "detail": f"раскатка завершена: {status}"}
        time.sleep(3)
    return {"status": "post_check_failed",
            "detail": f"за {wait_seconds} с раскатка не завершилась: {status}. Откат: {rollback}"}
