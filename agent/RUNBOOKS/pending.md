# Pending pod

## Признаки
Pod долго остаётся Pending, Deployment не достигает desired replicas.
Сверяй новые Pending-поды со старыми Ready-подами при частичном rollout.

## Частые причины
Неверный nodeSelector или affinity, taint без toleration, нехватка CPU/памяти, quota или PVC.
FailedScheduling сообщает конкретное ограничение; отсутствие события требует другой гипотезы.

## Как проверить
Сделай list pods и describe каждого Pending-пода в том же namespace.
Посмотри свежие события и текст scheduler, затем манифест template workload.
Проверь selector, affinity, tolerations, requests и историю rollout.
Не утверждай наличие метки ноды без отдельного свидетельства о Node.

## Как исправить
Определи минимальное изменение и его влияние на доступные ноды.
Изменение scheduler-полей обычно требует владельца манифеста и не входит в узкий allow-list.
Разрешённый патч делай только для env/resources/replicas/annotations или ConfigMap.data после подтверждения.

## Чего не делать
Не удаляй Pending-поды и не увеличивай replicas для маскировки причины.
Не снимай taint и не меняй node policy без отдельного решения оператора.
Не называй pod неисправным, если он ещё ждёт volume или image pull.
