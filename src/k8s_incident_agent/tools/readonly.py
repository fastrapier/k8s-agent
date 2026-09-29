"""Семь read-only инструментов диагностического контура (ТЗ раздел 6).

Правила, общие для всех инструментов:

* сигнатуры совпадают с ТЗ; клиенты, конфиг и хранилище свидетельств передаются
  keyword-only параметрами — это точка подмены в тестах;
* результат всегда `ToolResult`, исключения наружу не летят (ARCHITECTURE §8);
* маскирование применяется ДО записи свидетельства (ISOLATION §6);
* сырой вывод обрезается до `config.tools.max_output_chars` с явной пометкой.

Модуль сознательно не импортирует ничего из контура записи: ни `build_readwrite_clients`,
ни будущий модуль патча. Проверяется тестом `tests/test_isolation_imports.py` (ISOLATION §5).
"""

from __future__ import annotations

import json
from typing import Any

from kubernetes import client as k8s_client
from kubernetes.client.exceptions import ApiException

from k8s_incident_agent.config import AppConfig, get_config
from k8s_incident_agent.evidence import EvidenceStore, default_store
from k8s_incident_agent.k8s_clients import (
    K8sClientError,
    ReadOnlyClients,
    build_readonly_clients,
)
from k8s_incident_agent.masking import mask_object, mask_text
from k8s_incident_agent.text import to_yaml, truncate
from k8s_incident_agent.tools.schemas import ToolResult

# --- Справочник видов объектов ----------------------------------------------------

# kind -> (атрибут набора клиентов, метод чтения, объект в namespace)
_KIND_READERS: dict[str, tuple[str, str, bool]] = {
    "Pod": ("core", "read_namespaced_pod", True),
    "Service": ("core", "read_namespaced_service", True),
    "Endpoints": ("core", "read_namespaced_endpoints", True),
    "ConfigMap": ("core", "read_namespaced_config_map", True),
    "Secret": ("core", "read_namespaced_secret", True),
    "PersistentVolumeClaim": ("core", "read_namespaced_persistent_volume_claim", True),
    "ServiceAccount": ("core", "read_namespaced_service_account", True),
    "Node": ("core", "read_node", False),
    "Deployment": ("apps", "read_namespaced_deployment", True),
    "ReplicaSet": ("apps", "read_namespaced_replica_set", True),
    "StatefulSet": ("apps", "read_namespaced_stateful_set", True),
    "DaemonSet": ("apps", "read_namespaced_daemon_set", True),
    "Job": ("batch", "read_namespaced_job", True),
    "CronJob": ("batch", "read_namespaced_cron_job", True),
    "Ingress": ("networking", "read_namespaced_ingress", True),
}

_KIND_ALIASES = {
    "po": "Pod",
    "deploy": "Deployment",
    "rs": "ReplicaSet",
    "sts": "StatefulSet",
    "ds": "DaemonSet",
    "svc": "Service",
    "cm": "ConfigMap",
    "ep": "Endpoints",
    "pvc": "PersistentVolumeClaim",
    "ing": "Ingress",
    "no": "Node",
    "sa": "ServiceAccount",
    "cj": "CronJob",
}

_KIND_LOOKUP: dict[str, str] = {}
for _kind in _KIND_READERS:
    _KIND_LOOKUP[_kind.lower()] = _kind
    _KIND_LOOKUP[_kind.lower() + "s"] = _kind
_KIND_LOOKUP.update(_KIND_ALIASES)


class UnsupportedKind(ValueError):
    """Вид объекта не входит в список, разрешённый диагностическому контуру."""


def canonical_kind(kind: str) -> str:
    resolved = _KIND_LOOKUP.get((kind or "").strip().lower())
    if resolved is None:
        raise UnsupportedKind(
            f"вид объекта '{kind}' не поддерживается; доступны: "
            + ", ".join(sorted(_KIND_READERS))
        )
    return resolved


# --- Сервисные функции ------------------------------------------------------------

_serializer: k8s_client.ApiClient | None = None


def _to_dict(obj: Any) -> Any:
    """Объект клиента Kubernetes -> обычный словарь (dict проходит насквозь)."""
    if obj is None or isinstance(obj, (dict, list, str, int, float, bool)):
        return obj
    global _serializer
    if _serializer is None:
        _serializer = k8s_client.ApiClient()
    return _serializer.sanitize_for_serialization(obj)


def _namespace(namespace: str | None, config: AppConfig) -> str:
    return namespace or config.k8s.default_namespace


def _api_error(exc: ApiException) -> str:
    """Человекочитаемое описание отказа API-сервера."""
    detail = ""
    if exc.body:
        try:
            detail = json.loads(exc.body).get("message", "")
        except (ValueError, AttributeError):
            detail = str(exc.body)[:200]
    known = {
        401: "не авторизован: токен ServiceAccount не принят",
        403: "нет прав: read-only ServiceAccount не может читать этот ресурс",
        404: "объект не найден",
        409: "конфликт версий объекта",
    }
    head = known.get(exc.status, f"API-сервер вернул статус {exc.status}")
    return f"{head}. {detail}".strip()


def _clients(clients: ReadOnlyClients | None, config: AppConfig) -> ReadOnlyClients:
    return clients if clients is not None else build_readonly_clients(config)


def _fail(
    tool: str, args: dict[str, Any], message: str, store: EvidenceStore
) -> ToolResult:
    """Ошибка тоже становится свидетельством: план корректируется по ней (ARCHITECTURE §8)."""
    evidence = store.add(tool=tool, args=args, summary=f"ошибка: {message}")
    return ToolResult(
        tool=tool, status="error", evidence_id=evidence.id, summary=evidence.summary, error=message
    )


def _guard(tool: str, args: dict[str, Any], store: EvidenceStore, work) -> ToolResult:
    """Единая обёртка: любая ошибка кластера превращается в результат со статусом error."""
    try:
        return work()
    except ApiException as exc:
        return _fail(tool, args, _api_error(exc), store)
    except (K8sClientError, UnsupportedKind) as exc:
        return _fail(tool, args, str(exc), store)
    except Exception as exc:  # noqa: BLE001 — кластер недоступен, сеть, TLS и прочее
        return _fail(tool, args, f"кластер недоступен: {type(exc).__name__}: {exc}", store)


def _ok(
    tool: str,
    args: dict[str, Any],
    summary: str,
    data: Any,
    store: EvidenceStore,
    raw: str | None = None,
    truncated: bool = False,
    status: str = "ok",
) -> ToolResult:
    evidence = store.add(tool=tool, args=args, summary=summary, raw=raw)
    return ToolResult(
        tool=tool,
        status=status,
        evidence_id=evidence.id,
        summary=summary,
        data=data,
        truncated=truncated,
    )


def _read_object(clients: ReadOnlyClients, kind: str, name: str, namespace: str) -> dict[str, Any]:
    """Читает объект и возвращает словарь с гарантированно проставленным `kind`.

    `kind` проставляется явно: API-сервер в ответах на `read_*` его часто не заполняет,
    а маскирование Secret опирается именно на него.
    """
    resolved = canonical_kind(kind)
    attribute, method, namespaced = _KIND_READERS[resolved]
    api = getattr(clients, attribute)
    raw = getattr(api, method)(name, namespace) if namespaced else getattr(api, method)(name)
    obj = _to_dict(raw) or {}
    obj["kind"] = resolved
    return obj


def _container_states(container_statuses: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    for status in container_statuses:
        state = status.get("state") or {}
        phase = next(iter(state), None)
        detail = state.get(phase) or {} if phase else {}
        rows.append(
            {
                "name": status.get("name"),
                "ready": status.get("ready"),
                "restarts": status.get("restartCount", 0),
                "state": phase,
                "reason": detail.get("reason"),
                "exit_code": detail.get("exitCode"),
                "message": mask_text(detail.get("message") or "") or None,
            }
        )
    return rows


def _pod_row(pod: dict[str, Any]) -> dict[str, Any]:
    meta = pod.get("metadata") or {}
    spec = pod.get("spec") or {}
    status = pod.get("status") or {}
    statuses = status.get("containerStatuses") or []
    containers = _container_states(statuses)
    ready = sum(1 for c in containers if c["ready"])
    reason = status.get("reason") or next((c["reason"] for c in containers if c["reason"]), None)
    return {
        "name": meta.get("name"),
        "phase": status.get("phase"),
        "ready": f"{ready}/{len(spec.get('containers') or statuses)}",
        "restarts": sum(c["restarts"] or 0 for c in containers),
        "reason": reason,
        "node": spec.get("nodeName"),
        "start_time": status.get("startTime"),
        "containers": containers,
    }


# --- Инструменты ------------------------------------------------------------------


def k8s_list_pods(
    namespace: str | None = None,
    selector: str | None = None,
    *,
    clients: ReadOnlyClients | None = None,
    store: EvidenceStore | None = None,
    config: AppConfig | None = None,
) -> ToolResult:
    """Список подов namespace с состоянием контейнеров и причинами отказа."""
    config = get_config() if config is None else config
    store = default_store if store is None else store
    namespace = _namespace(namespace, config)
    args = {"namespace": namespace, "selector": selector}

    def work() -> ToolResult:
        api = _clients(clients, config).core
        response = _to_dict(api.list_namespaced_pod(namespace, label_selector=selector))
        pods = [_pod_row(item) for item in (response.get("items") or [])]
        broken = [p for p in pods if p["reason"] or p["phase"] not in ("Running", "Succeeded")]
        summary = f"namespace {namespace}: подов {len(pods)}"
        if broken:
            details = ", ".join(f"{p['name']}={p['reason'] or p['phase']}" for p in broken[:5])
            summary += f", проблемных {len(broken)} ({details})"
        else:
            summary += ", все в рабочем состоянии"
        raw, truncated = truncate(to_yaml(pods), config.tools.max_output_chars)
        return _ok(
            "k8s_list_pods",
            args,
            summary,
            {"namespace": namespace, "count": len(pods), "pods": pods},
            store,
            raw,
            truncated,
        )

    return _guard("k8s_list_pods", args, store, work)


def k8s_describe(
    kind: str,
    name: str,
    namespace: str | None = None,
    *,
    clients: ReadOnlyClients | None = None,
    store: EvidenceStore | None = None,
    config: AppConfig | None = None,
) -> ToolResult:
    """Сводка по объекту: метаданные, статус, условия и связанные события."""
    config = get_config() if config is None else config
    store = default_store if store is None else store
    namespace = _namespace(namespace, config)
    args = {"kind": kind, "name": name, "namespace": namespace}

    def work() -> ToolResult:
        bundle = _clients(clients, config)
        obj = mask_object(_read_object(bundle, kind, name, namespace))
        meta = obj.get("metadata") or {}
        status = obj.get("status") or {}
        events = _fetch_events(bundle, namespace, name)
        described = {
            "kind": obj.get("kind"),
            "name": meta.get("name"),
            "namespace": meta.get("namespace"),
            "labels": meta.get("labels"),
            "created": meta.get("creationTimestamp"),
            "owner": [o.get("name") for o in (meta.get("ownerReferences") or [])],
            "status": {
                key: value
                for key, value in status.items()
                if key in ("phase", "replicas", "readyReplicas", "availableReplicas", "reason")
            },
            "conditions": [
                {
                    "type": c.get("type"),
                    "status": c.get("status"),
                    "reason": c.get("reason"),
                    "message": mask_text(c.get("message") or "") or None,
                }
                for c in (status.get("conditions") or [])
            ],
            "containers": _container_states(status.get("containerStatuses") or []),
            "events": events[-10:],
        }
        problems = [c["reason"] for c in described["containers"] if c["reason"]]
        summary = f"{described['kind']} {name} в namespace {namespace}"
        if problems:
            summary += f": контейнеры в состоянии {', '.join(sorted(set(problems)))}"
        elif described["status"]:
            summary += f": {described['status']}"
        raw, truncated = truncate(to_yaml(obj), config.tools.max_output_chars)
        return _ok("k8s_describe", args, summary, described, store, raw, truncated)

    return _guard("k8s_describe", args, store, work)


def _fetch_events(
    clients: ReadOnlyClients, namespace: str, object_name: str | None
) -> list[dict[str, Any]]:
    """Общий сбор событий: используется и `k8s_events`, и `k8s_describe`."""
    selector = f"involvedObject.name={object_name}" if object_name else None
    response = _to_dict(clients.core.list_namespaced_event(namespace, field_selector=selector))
    rows = []
    for item in response.get("items") or []:
        involved = item.get("involvedObject") or {}
        rows.append(
            {
                "type": item.get("type"),
                "reason": item.get("reason"),
                "object": f"{involved.get('kind')}/{involved.get('name')}",
                "count": item.get("count"),
                "first": item.get("firstTimestamp") or item.get("eventTime"),
                "last": item.get("lastTimestamp") or item.get("eventTime"),
                "message": mask_text(item.get("message") or ""),
            }
        )
    rows.sort(key=lambda row: row["last"] or "")
    return rows


def k8s_events(
    namespace: str | None = None,
    object: str | None = None,  # noqa: A002 — имя параметра зафиксировано ТЗ раздел 6
    *,
    clients: ReadOnlyClients | None = None,
    store: EvidenceStore | None = None,
    config: AppConfig | None = None,
) -> ToolResult:
    """События namespace, при необходимости отфильтрованные по имени объекта."""
    config = get_config() if config is None else config
    store = default_store if store is None else store
    namespace = _namespace(namespace, config)
    args = {"namespace": namespace, "object": object}

    def work() -> ToolResult:
        events = _fetch_events(_clients(clients, config), namespace, object)
        warnings = [e for e in events if e["type"] == "Warning"]
        scope = f"объекта {object}" if object else f"namespace {namespace}"
        summary = f"событий по {scope}: {len(events)}, из них Warning: {len(warnings)}"
        if warnings:
            summary += f"; последнее: {warnings[-1]['reason']} — {warnings[-1]['message'][:120]}"
        raw, truncated = truncate(to_yaml(events), config.tools.max_output_chars)
        return _ok(
            "k8s_events",
            args,
            summary,
            {"namespace": namespace, "object": object, "count": len(events), "events": events},
            store,
            raw,
            truncated,
        )

    return _guard("k8s_events", args, store, work)


def k8s_logs(
    pod: str,
    container: str | None = None,
    tail: int | None = None,
    previous: bool = False,
    *,
    namespace: str | None = None,
    clients: ReadOnlyClients | None = None,
    store: EvidenceStore | None = None,
    config: AppConfig | None = None,
) -> ToolResult:
    """Логи пода. `previous=True` — лог предыдущего запуска, основной случай CrashLoopBackOff."""
    config = get_config() if config is None else config
    store = default_store if store is None else store
    namespace = _namespace(namespace, config)
    tail = tail or config.tools.default_log_tail
    args = {
        "pod": pod,
        "container": container,
        "tail": tail,
        "previous": previous,
        "namespace": namespace,
    }

    def work() -> ToolResult:
        api = _clients(clients, config).core
        text = api.read_namespaced_pod_log(
            name=pod,
            namespace=namespace,
            container=container,
            tail_lines=tail,
            previous=previous,
        )
        # Маскирование до обрезки и до записи свидетельства (ISOLATION §6).
        masked = mask_text(text or "")
        body, truncated = truncate(masked, config.tools.max_output_chars)
        lines = body.splitlines()
        summary = (
            f"лог пода {pod}"
            + (f" (контейнер {container})" if container else "")
            + (" предыдущего запуска" if previous else "")
            + f": {len(lines)} строк"
        )
        if lines:
            summary += f"; последняя: {lines[-1][:160]}"
        return _ok(
            "k8s_logs",
            args,
            summary,
            {"pod": pod, "container": container, "previous": previous, "log": body},
            store,
            masked,
            truncated,
        )

    return _guard("k8s_logs", args, store, work)


def k8s_rollout_history(
    deployment: str,
    namespace: str | None = None,
    *,
    clients: ReadOnlyClients | None = None,
    store: EvidenceStore | None = None,
    config: AppConfig | None = None,
) -> ToolResult:
    """История релизов Deployment: ревизии из ReplicaSet, образы и change-cause."""
    config = get_config() if config is None else config
    store = default_store if store is None else store
    namespace = _namespace(namespace, config)
    args = {"deployment": deployment, "namespace": namespace}

    def work() -> ToolResult:
        api = _clients(clients, config).apps
        deploy = _to_dict(api.read_namespaced_deployment(deployment, namespace)) or {}
        match_labels = ((deploy.get("spec") or {}).get("selector") or {}).get("matchLabels") or {}
        selector = ",".join(f"{k}={v}" for k, v in sorted(match_labels.items())) or None
        response = _to_dict(api.list_namespaced_replica_set(namespace, label_selector=selector))
        revisions = []
        for item in response.get("items") or []:
            meta = item.get("metadata") or {}
            annotations = meta.get("annotations") or {}
            owners = {o.get("name") for o in (meta.get("ownerReferences") or [])}
            if owners and deployment not in owners:
                continue
            spec = item.get("spec") or {}
            containers = ((spec.get("template") or {}).get("spec") or {}).get("containers") or []
            revisions.append(
                {
                    "revision": annotations.get("deployment.kubernetes.io/revision"),
                    "replicaset": meta.get("name"),
                    "created": meta.get("creationTimestamp"),
                    "replicas": spec.get("replicas"),
                    "images": [c.get("image") for c in containers],
                    "change_cause": annotations.get("kubernetes.io/change-cause"),
                }
            )
        revisions.sort(
            key=lambda row: int(row["revision"]) if str(row["revision"]).isdigit() else 0
        )
        current = (deploy.get("metadata") or {}).get("annotations", {}).get(
            "deployment.kubernetes.io/revision"
        )
        summary = (
            f"Deployment {deployment} в namespace {namespace}: ревизий {len(revisions)}"
            f", текущая {current}"
        )
        raw, truncated = truncate(to_yaml(revisions), config.tools.max_output_chars)
        return _ok(
            "k8s_rollout_history",
            args,
            summary,
            {
                "deployment": deployment,
                "namespace": namespace,
                "current_revision": current,
                "revisions": revisions,
            },
            store,
            raw,
            truncated,
        )

    return _guard("k8s_rollout_history", args, store, work)


def k8s_get_manifest(
    kind: str,
    name: str,
    namespace: str | None = None,
    *,
    clients: ReadOnlyClients | None = None,
    store: EvidenceStore | None = None,
    config: AppConfig | None = None,
) -> ToolResult:
    """Манифест объекта с маскированием секретов (ТЗ раздел 6, ISOLATION §6)."""
    config = get_config() if config is None else config
    store = default_store if store is None else store
    namespace = _namespace(namespace, config)
    args = {"kind": kind, "name": name, "namespace": namespace}

    def work() -> ToolResult:
        obj = _read_object(_clients(clients, config), kind, name, namespace)
        masked = mask_object(obj)
        full_yaml = to_yaml(masked)
        body, truncated = truncate(full_yaml, config.tools.max_output_chars)
        summary = f"манифест {masked.get('kind')} {name} из namespace {namespace}"
        if truncated:
            summary += " (обрезан до лимита)"
        return _ok(
            "k8s_get_manifest",
            args,
            summary,
            {
                "kind": masked.get("kind"),
                "name": name,
                "namespace": namespace,
                "manifest_yaml": body,
            },
            store,
            full_yaml,
            truncated,
        )

    return _guard("k8s_get_manifest", args, store, work)


def k8s_top(
    namespace: str | None = None,
    *,
    clients: ReadOnlyClients | None = None,
    store: EvidenceStore | None = None,
    config: AppConfig | None = None,
) -> ToolResult:
    """Потребление CPU и памяти подами. Требует metrics-server; без него деградирует.

    Отсутствие metrics-server (404 от `metrics.k8s.io` или 503) — не ошибка диагностики,
    а ожидаемое состояние части кластеров: возвращается статус `degraded`, план
    продолжается без этих данных.
    """
    config = get_config() if config is None else config
    store = default_store if store is None else store
    namespace = _namespace(namespace, config)
    args = {"namespace": namespace}

    def work() -> ToolResult:
        api = _clients(clients, config).custom
        try:
            response = _to_dict(
                api.list_namespaced_custom_object(
                    group="metrics.k8s.io",
                    version="v1beta1",
                    namespace=namespace,
                    plural="pods",
                )
            )
        except ApiException as exc:
            if exc.status in (404, 503):
                summary = (
                    f"metrics-server в кластере недоступен (статус {exc.status}), "
                    f"метрики по namespace {namespace} не собраны"
                )
                evidence = store.add(tool="k8s_top", args=args, summary=summary)
                return ToolResult(
                    tool="k8s_top",
                    status="degraded",
                    evidence_id=evidence.id,
                    summary=summary,
                    error=_api_error(exc),
                )
            raise
        rows = []
        for item in response.get("items") or []:
            containers = [
                {
                    "name": c.get("name"),
                    "cpu": (c.get("usage") or {}).get("cpu"),
                    "memory": (c.get("usage") or {}).get("memory"),
                }
                for c in (item.get("containers") or [])
            ]
            rows.append({"pod": (item.get("metadata") or {}).get("name"), "containers": containers})
        summary = f"потребление ресурсов в namespace {namespace}: подов с метриками {len(rows)}"
        raw, truncated = truncate(to_yaml(rows), config.tools.max_output_chars)
        return _ok(
            "k8s_top",
            args,
            summary,
            {"namespace": namespace, "pods": rows},
            store,
            raw,
            truncated,
        )

    return _guard("k8s_top", args, store, work)
