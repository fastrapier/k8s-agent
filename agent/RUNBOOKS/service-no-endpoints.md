# Service без endpoints

## Признаки
Service существует, но Endpoints пусты; клиенты получают 503 или gateway error.
Ready-поды могут существовать рядом, поэтому проверяй связность объектов.

## Частые причины
Selector Service не совпал с labels подов после релиза, поды не Ready или указан неверный порт.
Неверный targetPort и политика backend дают другой класс ошибки, чем пустой список адресов.

## Как проверить
Прочитай манифест Ingress, затем Service, Endpoints и workload в одном namespace.
Сравни selector Service с labels pod и service port с targetPort/containerPort.
Проверь readiness conditions и свежие события подов; не полагайся на curl без controller.

## Как исправить
Сформулируй, какое звено цепочки Ingress → Service → Endpoints нарушено.
Selector Service и spec Ingress могут быть вне allow-list агента: дай оператору точный diff-рекомендацию.
Разрешённые env/resources/replicas/annotations или ConfigMap.data меняй только после подтверждения.

## Чего не делать
Не объявляй Ingress сломанным, если endpoints пусты из-за labels подов.
Не меняй image и не перезапускай поды как способ создать endpoints.
Не утверждай 502/503 без свидетельства о backend и не смешивай namespace.
