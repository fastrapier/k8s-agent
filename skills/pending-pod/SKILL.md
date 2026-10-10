---
name: pending-pod
description: Применяй, когда под не планируется, rollout застрял или есть FailedScheduling.
triggers: [pending, не раскатывается, rollout status, nodeselector, unschedulable, failedscheduling]
tools: [k8s_list_pods, k8s_describe, k8s_events, k8s_get_manifest, k8s_rollout_history]
---
1. Зафиксируй namespace, Deployment/StatefulSet и число Pending; отдели новые поды от старых Ready.
2. Вызови `k8s_list_pods(namespace="<ns>", selector="<app=...>")`; запиши phase, readiness и owner.
3. Для каждого Pending вызови `k8s_describe(kind="Pod", name="<pod>", namespace="<ns>")`.
4. Вызови `k8s_events(namespace="<ns>", object="<pod>")`; приоритет у свежих `FailedScheduling`.
5. Вызови `k8s_get_manifest(kind="Deployment", name="<name>", namespace="<ns>")`; проверь nodeSelector, affinity, tolerations и resources.
6. Для StatefulSet запроси `k8s_get_manifest(kind="StatefulSet", name="<name>", namespace="<ns>")`.
7. `node(s) didn't match node selector` подтверждает несовпадение `spec.template.spec.nodeSelector` с метками нод.
8. `untolerated taint`, insufficient CPU/memory и quota подтверждают другие корни; не смешивай их с readiness.
9. Вызови `k8s_rollout_history(deployment="<name>", namespace="<ns>")`; сравни ревизию, которая создала Pending-поды.
10. Типичный корень — неверный nodeSelector/affinity, taint без toleration, дефицит ресурсов или quota; поле-улика в Events и template.
11. Отсутствие свежего FailedScheduling и наличие Init/ContainerCreating требуют проверить image pull и volume, а не планировщик.
12. Без чтения объектов Node не утверждай наличие метки; зафиксируй только текст события и манифест workload.
13. Исправление обычно удаляет ошибочный nodeSelector после подтверждения допустимого места запуска.
14. Разрешены лишь env, resources, replicas, template annotations у Deployment/StatefulSet и `ConfigMap.data`.
15. `nodeSelector`, affinity, tolerations, image и PVC не входят в allow-list; image можно только рекомендовать.
16. Покажи diff, влияние на доступные ноды и запроси подтверждение человека до любой записи.
17. После одобрения проверь `k8s_list_pods` и события; не объявляй rollout успешным по одному Ready-поду.
18. Остановись при конфликтующих событиях, квоте без владельца или если изменение scheduler-политики требуется вне области патча.
