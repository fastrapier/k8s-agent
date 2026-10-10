# Зависший Job

## Признаки
Job не достигает completions; pod может быть в Init, Running, Failed или ждать повторной попытки.
Сверяй backoffLimit и число фактических попыток, а не только возраст Job.

## Частые причины
Init-контейнер ждёт DNS/Service/БД, команда миграции зависла, нет volume или образ не скачивается.
Init:0/1 с повторяющимся timeout отличается от main-контейнера с ненулевым exit code.

## Как проверить
Прочитай Job manifest, pod list по job-name и describe pod с initContainerStatuses.
Сними лог нужного init-контейнера; для перезапуска main используй previous=true.
Проверь события pod и манифест Service-зависимости, включая порт и namespace.

## Как исправить
Отдели недоступную зависимость от ошибки команды и зафиксируй evidence id.
Изменения Job spec, initContainers, command и image отдай владельцу как рекомендацию.
В узком патче допустимы только env/resources/replicas/annotations у Deployment/StatefulSet и ConfigMap.data.

## Чего не делать
Не удаляй Job и pod, чтобы сбросить backoff, и не запускай миграцию повторно без оценки идемпотентности.
Не меняй image, DNS или Service самовольно.
Не называй ожидание причиной без лога/события и не раскрывай секреты из env.
