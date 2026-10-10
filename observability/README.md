# Наблюдаемость агента

Стек запускается отдельным Compose-проектом `k8s-incident-agent-obs`.

- `prometheus.yml` — сбор метрик агента на `host.docker.internal:9108`.
- `alerts.yml` — правила Prometheus из `docs/OBSERVABILITY.md` §5.
- `alertmanager.yml` — маршрутизация critical и warning в Telegram.
- `grafana/` — datasource и дашборд `k8s-incident-agent`.
- `secrets/telegram_token` — локальный токен Telegram, в git не попадает.

Поднять стек: `make obs-up`.

Grafana доступна на <http://127.0.0.1:3000>, Prometheus — на `:9090`.
Логин в Grafana не нужен, роль анонимного доступа — Viewer.

Проверить конфигурации и синтетические срабатывания: `make alerts-check`.

Метрики агрегируются при сборе из JSONL-трейсов, чтобы короткие CLI-прогоны не терялись.
Решение и контракт: [спека М7](../.scratch/m7-observability/spec.md).
