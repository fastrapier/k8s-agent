# CrashLoopBackOff

## Признаки
Pod многократно перезапускает контейнер и остаётся в CrashLoopBackOff.
Считай симптомом высокий restart count, а причиной — только подтверждённый статус, лог или событие.

## Частые причины
Неверный env или ConfigMap, ошибка команды, liveness probe, OOMKilled, неподходящий образ.
Secret не читается диагностом; проверяй наличие secretKeyRef по манифесту и сравнивай здоровые нагрузки.

## Как проверить
Собери pod list, describe, события и previous-лог; previous=true нужен для завершившегося запуска.
Проверь Last State, exit code, reason, probe и template workload.
Сравни текущий template с устойчивой ревизией, не раскрывая секреты.

## Как исправить
Сначала сформулируй корень и покажи evidence id.
Предложи только разрешённые env/resources/replicas/annotations или ConfigMap.data.
Покажи diff и жди подтверждения человека; после него проверь rollout и новый лог.

## Чего не делать
Не перезапускай workload для проверки гипотезы и не удаляй под, чтобы скрыть симптом.
Не меняй image, Secret, RBAC или поля вне allow-list.
Не называй причину по одному слову из лога и не игнорируй namespace.
