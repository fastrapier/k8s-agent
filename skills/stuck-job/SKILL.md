---
name: stuck-job
description: Применяй, когда Job не завершается, завис в init или повторяет неудачные попытки.
triggers: [job, не завершается, "init:", миграц]
tools: [k8s_list_pods, k8s_describe, k8s_logs, k8s_events, k8s_get_manifest]
---
1. Зафиксируй namespace, Job и его completion/backoff; не путай Running init с Failed Job.
2. Вызови `k8s_get_manifest(kind="Job", name="<job>", namespace="<ns>")`; прочитай completions, backoffLimit и template.
3. Вызови `k8s_list_pods(namespace="<ns>", selector="job-name=<job>")`; найди под и состояние контейнеров.
4. Вызови `k8s_describe(kind="Pod", name="<pod>", namespace="<ns>")`; проверь initContainerStatuses, conditions и exit code.
5. Для init-контейнера вызови `k8s_logs(pod="<pod>", container="<init>", tail=200, previous=false, namespace="<ns>")`.
6. Для завершившегося основного контейнера при рестарте используй `k8s_logs(pod="<pod>", container="<main>", tail=200, previous=true, namespace="<ns>")`.
7. Вызови `k8s_events(namespace="<ns>", object="<pod>")`; отличай scheduling, image pull, volume и probe.
8. `Init:0/1` плюс повторяющийся timeout подтверждают блокировку init; отсутствие init и exit code 1 указывает на main.
9. Для зависимости проверь её манифест через `k8s_get_manifest(kind="Service", name="<svc>", namespace="<ns>")`.
10. Типичные корни: недоступный Service/DNS, миграция ждёт БД, неверный volume или команда; поле-улика — init command, logs, events.
11. Ошибка образа подтверждается `ErrImagePull`; не исправляй её заменой image.
12. Сопоставь selector/порт зависимости и имя DNS, но не утверждай доступность без события или лога.
13. Патч обычно добавляет корректный env/config или увеличивает resources; остановку Job не маскируй удалением доказательств.
14. Разрешены env/resources/replicas/template annotations у Deployment/StatefulSet и `ConfigMap.data`; Job spec вне allow-list.
15. Image, initContainers, command, Service и DNS не патчь; предложи владельцу манифеста отдельное исправление.
16. Любой разрешённый патч покажи diff-ом и жди подтверждения человека; сам Job не перезапускай.
17. После одобрения проверь новый pod, логи init и `k8s_describe(kind="Job", name="<job>", namespace="<ns>")` до completion.
18. Остановись при неизвестной зависимости, истёкшем backoff или недостатке свидетельств о безопасном изменении.
