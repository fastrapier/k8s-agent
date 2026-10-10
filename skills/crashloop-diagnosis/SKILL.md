---
name: crashloop-diagnosis
description: Применяй при повторных рестартах контейнера и статусе CrashLoopBackOff.
triggers: [crashloop, crashloopbackoff, перезапуск, рестарт]
tools: [k8s_list_pods, k8s_describe, k8s_logs, k8s_get_manifest, k8s_events]
---
1. Зафиксируй namespace и имя пода; не переноси вывод из другого namespace.
2. Вызови `k8s_list_pods(namespace="<ns>", selector="<app=...>")`; проверь READY, STATUS и restart count.
3. Вызови `k8s_describe(kind="Pod", name="<pod>", namespace="<ns>")` для exit code, reason и условий.
4. Вызови `k8s_logs(pod="<pod>", namespace="<ns>", previous=true, tail=200)` — лог ПРЕДЫДУЩЕГО запуска.
5. Затем вызови `k8s_logs(pod="<pod>", namespace="<ns>", previous=false, tail=100)` для текущего старта.
6. Вызови `k8s_events(namespace="<ns>", object="<pod>")`; события подтверждают probes, image pull и убийство контейнера.
7. Вызови `k8s_get_manifest(kind="Deployment", name="<owner>", namespace="<ns>")`; имя владельца — без хеша ReplicaSet (`api`, не `api-86d9b8c4bd`).
8. Если владелец StatefulSet, запроси `k8s_get_manifest(kind="StatefulSet", name="<owner>", namespace="<ns>")`.
9. Secret читать нельзя: RBAC запрещает его получение, значение не запрашивай и не угадывай.
10. Лог «переменная не задана», а в env её нет? Найди в `k8s_list_pods` здоровый (Running) Deployment того же namespace и прочитай его манифест: там видно, из какого Secret и под каким key эта переменная берётся. Без этого шага патч не составить.
11. Если здорового аналога нет, ищи имя Secret и key в соседних env того же манифеста (`valueFrom.secretKeyRef`).
12. Лог `environment variable ... is not set` и отсутствующий `secretKeyRef` подтверждают потерю конфигурации.
13. `OOMKilled` подтверждается reason/exit code и метриками; probe failure — Events и полями liveness/readiness.
14. Ошибка образа подтверждается `ErrImagePull`/`ImagePullBackOff`; образ не меняй, только рекомендуй проверку поставки.
15. Типичный корень — ошибка env, команда запуска, probe или лимит ресурса; поле-улика находится в status, events или template.
16. Для исправления предложи JSON merge patch только для `spec.template.spec.containers[*].env` или `resources`.
17. Допустимы также `spec.replicas`, `spec.template.metadata.annotations` и `ConfigMap.data`; только Deployment/StatefulSet/ConfigMap.
18. Патч не меняет `image`, volumes, securityContext и RBAC; применять его можно лишь после подтверждения человека.
19. Покажи diff и ожидаемый эффект, затем после одобрения проверь rollout и новые логи.
20. Остановись при недостатке свидетельств, запрете RBAC, неоднозначном владельце или исчезновении симптома.
