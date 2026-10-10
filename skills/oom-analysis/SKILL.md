---
name: oom-analysis
description: Применяй при OOMKilled, нехватке памяти или подозрении на превышение memory limit.
triggers: [oom, oomkilled, память, memory]
tools: [k8s_list_pods, k8s_top, k8s_describe, k8s_get_manifest, k8s_events, k8s_logs]
---
1. Зафиксируй namespace, pod и контейнер; разделяй OOM контейнера и давление памяти ноды.
2. Вызови `k8s_list_pods(namespace="<ns>", selector="<app=...>")`; отметь restart count и STATUS.
3. Вызови `k8s_describe(kind="Pod", name="<pod>", namespace="<ns>")`; ищи `reason: OOMKilled`, exit code 137 и Last State.
4. Вызови `k8s_top(namespace="<ns>")`; статус `degraded` означает отсутствие metrics-server, а не нормальное потребление.
5. Вызови `k8s_events(namespace="<ns>", object="<pod>")`; MemoryPressure и eviction указывают на ноду.
6. Вызови `k8s_get_manifest(kind="Deployment", name="<owner>", namespace="<ns>")`; прочитай requests/limits каждого контейнера.
7. Для StatefulSet запроси `k8s_get_manifest(kind="StatefulSet", name="<owner>", namespace="<ns>")`.
8. Сопоставь пики `k8s_top` с лимитом, но не выдавай метрику без evidence_id за факт.
9. `OOMKilled` с exit 137 и лимитом — подтверждение превышения cgroup; отсутствие reason опровергает этот корень.
10. Высокий usage при низком request объясняет вытеснение при конкуренции, но сам по себе не доказывает OOM.
11. Типичные корни: утечка, неверный лимит, большой batch, кэш или node pressure; поле-улика — status, resources и events.
12. Логи `k8s_logs(pod="<pod>", container="<container>", previous=true, tail=200, namespace="<ns>")` ищут момент перед убийством.
13. Сравни тот же workload с устойчивой репликой и предыдущей ревизией через `k8s_rollout_history(deployment="<name>", namespace="<ns>")`.
14. Патч обычно повышает `resources.limits.memory` и при необходимости `requests.memory` контейнера.
15. Разрешены только `spec.template.spec.containers[*].resources`, env, replicas и annotations; ConfigMap — только `data`.
16. Не меняй `image`, nodeSelector, affinity или границы namespace; смену образа лишь рекомендуй.
17. Патч показывается diff-ом и ждёт явного подтверждения человека; без него кластер не изменяй.
18. После одобрения проверь rollout, `k8s_top` и отсутствие новых OOMKilled; старые рестарты не стирай из отчёта.
19. Остановись, если metrics недоступны, причина может быть node pressure или данных недостаточно для безопасного размера.
