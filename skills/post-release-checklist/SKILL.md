---
name: post-release-checklist
description: Применяй для общей проверки состояния после релиза или деплоя и безопасного отката.
triggers: [после релиза, после деплоя, rollout history, откат]
tools: [k8s_rollout_history, k8s_get_manifest, k8s_list_pods, k8s_describe, k8s_events]
---
1. Зафиксируй namespace, workload, ожидаемую ревизию и окно релиза.
2. Вызови `k8s_rollout_history(deployment="<name>", namespace="<ns>")`; сравни revision, image и change-cause.
3. Вызови `k8s_get_manifest(kind="Deployment", name="<name>", namespace="<ns>")`; проверь replicas и template.
4. Вызови `k8s_list_pods(namespace="<ns>", selector="<app=...>")`; сверяй Ready, restarts, phase и версии labels.
5. Вызови `k8s_describe(kind="Deployment", name="<name>", namespace="<ns>")`; ищи Progressing/Available и условия.
6. Вызови `k8s_events(namespace="<ns>", object="<name>")`; свежие FailedCreate/FailedScheduling/ProbeFailure важнее старых.
7. Сравни фактический template с предыдущей ревизией; перечисли только изменения, подтверждённые evidence_id.
8. Проверь, что desired replicas совпадает с Ready и что старый ReplicaSet не обслуживает трафик неожиданно.
9. Проверяй связанные Service/Ingress только отдельными манифестами; rollout сам по себе не доказывает доступность.
10. Типичные корни регрессии: env/config, probe, resource, selector, несовместимый image; поле-улика — diff template и events.
11. Если нужен откат, сначала покажи revision и последствия; не выполняй rollback без подтверждения человека.
12. Разрешённый патч может менять только env/resources/replicas/template annotations у Deployment/StatefulSet или `ConfigMap.data`.
13. Image не меняй даже при явной регрессии: предложи откат/замену образа оператору отдельной процедурой.
14. Патч покажи как diff, укажи ожидаемый результат и способ проверки; запись возможна только после подтверждения.
15. После изменения повтори list/describe/events и зафиксируй исход; не стирай историю ревизий.
16. Остановись при расхождении namespace, неполной истории, неоднозначном владельце или отсутствии подтверждения.
