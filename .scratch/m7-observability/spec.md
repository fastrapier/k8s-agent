# М7: наблюдаемость — трейс каждого прогона, метрики, алерты, дашборд (N4, N5)

Статус: спека. Срок защиты 2026-10-13, сначала ширина.

## Зачем

N5: каждый запуск оставляет трейс со всеми вызовами LLM и инструментов, метрики уходят в Prometheus,
алерты — в мессенджер. N4: стоимость инцидента фиксируется в трейсе, порог алерта задаётся конфигом.
Приёмка roadmap: прогон виден в UI трейсера; `curl /metrics` отдаёт счётчики; `promtool check rules`
зелёный; один алерт стреляет искусственно.

Польза уже доказана в этой работе. Без замера по узлам не нашли бы две главные проблемы М4–М5:
- скрытое рассуждение модели до 74 с на узел;
- зависание `propose` на json_schema.

Трейс превращает такой замер из разового скрипта в свойство каждого прогона.

## Решения

1. **Трейс — JSONL-файл на прогон**, `data/traces/<run_id>.jsonl` (run_id = thread_id).
   - События пишутся по одной строке JSON: `run_start`, `node`, `llm`, `format_retry`, `tool`,
     `memory_search`, `approval`, `run_end`.
   - Событие `llm` содержит промпт и ответ: это данные уже после маскирования, ровно то, что ушло в шлюз.
   - Вместо Langfuse — упрощение, его обоснование записывается в DEFENSE.
2. **Метрики считаются из трейсов**, а не счётчиками в процессе.
   - CLI живёт около 30 с, а Prometheus скрейпит раз в 15 с. Счётчики в процессе потребовали бы
     pushgateway — ещё один сервис.
   - `k8s-agent metrics serve` при каждом скрейпе агрегирует файлы трейсов. Трейс — источник истины,
     метрика — его агрегат; процесс можно перезапускать без потери счётчиков.
3. **Длительность прогона** = сумма длительностей узлов. Время, пока человек думает над патчем, в неё не
   входит: иначе p95 мерил бы скорость человека, а не системы (OBSERVABILITY §2).
4. **UI трейсера** — `k8s-agent trace <run_id>`: временная шкала узлов, вызовов модели и инструментов с
   латентностью и ценой. Grafana показывает агрегаты.
5. **Алерты.**
   - `observability/alerts.yml` — правила из OBSERVABILITY §5.
   - `alerts.test.yml` — юнит-тесты promtool: на синтетических рядах алерт стреляет, это и есть
     «искусственно».
   - `alertmanager.yml` — маршрут в Telegram; проверяется `amtool check-config`.
6. **Дашборд и стек.** Grafana JSON с провижинингом. `observability/docker-compose.yml` поднимает
   Prometheus, Alertmanager и Grafana отдельным файлом, чтобы compose агента (изоляция М2) не трогался.

## Контракт метрик (имена фиксированы — на них опираются алерты и дашборд)

| Метрика | Тип | Лейблы |
|---|---|---|
| `agent_runs_total` | counter | `route` (diagnose/runbook_qa/out_of_scope/failed), `outcome` (resolved/unresolved/refused/answered/budget_exceeded/error) |
| `agent_run_duration_seconds` | histogram 5,10,20,30,60,90,120,180,300,600 | `route` |
| `agent_run_cost_usd` | histogram 0.005,0.01,0.02,0.05,0.1,0.2,0.5,1 | `route` |
| `agent_llm_calls_total` | counter | `model`, `node`, `status` (ok/timeout/error) |
| `agent_llm_latency_seconds` | histogram 1,2,5,10,20,30,60,120 | `model`, `node` |
| `agent_llm_cost_usd_total` | counter | `model`, `node` |
| `agent_llm_tokens_total` | counter | `model`, `node`, `direction` (in/out) |
| `agent_structured_output_retries_total` | counter | `model`, `node` |
| `agent_tool_calls_total` | counter | `tool`, `status` (ok/degraded/error/denied) |
| `agent_tool_duration_seconds` | histogram 0.1,0.25,0.5,1,2,5,10 | `tool` |
| `agent_evidence_truncated_total` | counter | `tool` |
| `agent_review_iterations` | histogram 1,2,3 | — |
| `agent_approvals_total` | counter | `decision` (approved/rejected) |
| `agent_memory_search_hits` | histogram 0,1,2,3 | — |

`status="denied"` — вызов, отклонённый кодом: незарегистрированный инструмент (`k8s_patch`), нет
namespace, аргументы не по схеме, бюджет исчерпан.

## Почему не проще

| Вариант | Почему нет |
|---|---|
| Langfuse self-hosted | Postgres, ClickHouse, Redis, S3 и несколько контейнеров на ноутбуке (риск ТЗ §13). Всё, что нужно для приёмки, даёт JSONL плюс `k8s-agent trace` |
| prometheus_client-счётчики в процессе | CLI завершается раньше скрейпа; pushgateway — лишний сервис |
| Только логи, без метрик | N5 требует Prometheus и алерты |
| Алерты без тестов | «Стреляет искусственно» должно быть воспроизводимо: `promtool test rules` |

## Проверка

- Юнит-тесты:
  - трейс прогона на FakeLLM содержит узлы, вызовы LLM и инструментов, `run_end` с исходом;
  - отказ (`denied`) попадает в трейс;
  - метрики из набора трейсов дают ожидаемые счётчики и гистограммы;
  - каждая метрика из алертов и дашборда существует в выводе коллектора.
- `make alerts-check`: `promtool check rules`, `promtool test rules`, `amtool check-config`.
- Вживую:
  - S1 → `k8s-agent trace <id>`;
  - `curl localhost:9108/metrics`;
  - `make obs-up` → дашборд в Grafana с данными прогона.
