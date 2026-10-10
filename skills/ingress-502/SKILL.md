---
name: ingress-502
description: Применяй при 502/503 и ошибке маршрута Ingress–Service–Endpoints после изменения.
triggers: ["502", "503", endpoints, ingress, отдаёт]
tools: [k8s_get_manifest, k8s_list_pods, k8s_describe, k8s_events]
---
1. Зафиксируй namespace, host/path Ingress и имя Service; код 502/503 не считай причиной сам по себе.
2. Вызови `k8s_get_manifest(kind="Ingress", name="<ingress>", namespace="<ns>")`; выпиши backend service и port.
3. Вызови `k8s_get_manifest(kind="Service", name="<service>", namespace="<ns>")`; сравни selector и targetPort.
4. Вызови `k8s_get_manifest(kind="Endpoints", name="<service>", namespace="<ns>")`; проверь addresses и ports.
5. Вызови `k8s_list_pods(namespace="<ns>", selector="<selector из Service>")`; проверь Ready и метки.
6. Если Endpoints пусты при Ready-подах, это подтверждает несовпадение selector с labels подов или readiness gates.
7. Если endpoints есть, проверь совпадение service port/targetPort и порт контейнера в манифесте Deployment.
8. `k8s_describe(kind="Service", name="<service>", namespace="<ns>")` и `k8s_events(namespace="<ns>", object="<service>")` ищут ошибки публикации.
9. Сравни текущие labels/selector с предыдущей ревизией через `k8s_get_manifest(kind="Deployment", name="<owner>", namespace="<ns>")`.
10. Типичные корни: selector остался от старой версии, неверный порт, неготовые поды или backend policy; поля-улики — selector, labels, ports.
11. Код 502 при живых endpoints чаще указывает на backend connection/targetPort; пустые endpoints обычно дают 503 у ingress-controller.
12. Не объявляй проблему ingress-controller без его объекта, события или отдельного свидетельства.
13. Исправление обычно меняет `Service.spec.selector`, но этот путь вне allow-list агента; предложи его владельцу манифеста.
14. В агентском патче разрешены только env, resources, replicas, template annotations у Deployment/StatefulSet и `ConfigMap.data`.
15. Image, Service/Ingress spec, labels и selector не патчь; изменение образа только рекомендация.
16. Если исправление вне allow-list, покажи точный манифестный diff как рекомендацию и запроси ручное изменение.
17. После подтверждённого разрешённого патча проверь поды и endpoints заново; без подтверждения ничего не меняй.
18. Остановись, если цепочка Ingress → Service → Endpoints не полностью наблюдаема или controller не установлен.
