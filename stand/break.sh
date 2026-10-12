#!/usr/bin/env bash
# Внести поломку в стенд и ДОЖДАТЬСЯ, пока она реально проявится.
#
# Просто `kubectl apply` тут недостаточно: сразу после apply под ещё Pending/ContainerCreating,
# и демо показывало бы пустоту. Поэтому каждый сценарий ждёт свой симптом — тот же смысл,
# что у поля wait_for в case.yaml (docs/EVALS.md, раздел 4.1).
#
# Использование: ./break.sh [s1|s2|s3|s4|all]
#   без аргумента — all
# Переменные: STAND_WAIT_TIMEOUT (по умолчанию 180с)

set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

usage() {
  cat <<TXT
Использование: $0 [s1|s2|s3|s4|all]

  s1   CrashLoopBackOff: Deployment api без переменной окружения DB_PASSWORD
  s2   502 после релиза: selector сервиса web не совпал с метками подов
  s3   Не раскатывается: 2 из 5 подов reports в Pending из-за nodeSelector disktype=nvme
  s4   Job не завершается: init-контейнер ждёт несуществующий сервис payments-db:5432
  all  все четыре подряд

TXT
}

# Базовые объекты (namespace, Secret, здоровый checkout) нужны любому сценарию.
# apply идемпотентен, поэтому применяем всегда: так стенд чинится и после точечного
# удаления объектов руками, а не только после ./clean.sh all.
ensure_base() {
  log "Проверяю базовые объекты стенда (namespace, Secret, здоровый checkout)"
  kc apply -f "$MANIFESTS_DIR/base/"
  kc rollout status deployment/checkout -n "$NAMESPACE" --timeout="${WAIT_TIMEOUT}s"
}

# --- S1 ---------------------------------------------------------------------
check_s1() { [ "$(max_restarts 'app=api')" -ge 2 ]; }

break_s1() {
  log "S1 — CrashLoopBackOff: Deployment api без DB_PASSWORD"
  # Пересоздаём: если агент уже починил S1 патчем, apply старого манифеста поломку не вернёт —
  # добавленной патчем переменной нет в last-applied, kubectl её не удалит.
  kc delete deployment api -n "$NAMESPACE" --ignore-not-found --wait=true
  # Поды старого Deployment удаляются фоном. Если S1 уже был сломан, их restartCount >= 2
  # засчитался бы новому Deployment, и status показал бы свежий под с restartCount=0.
  kc wait --for=delete pod -l app=api -n "$NAMESPACE" --timeout="${WAIT_TIMEOUT}s"
  kc apply -f "$MANIFESTS_DIR/s1-crashloop/"
  # Симптом: под перезапустился минимум дважды. Первые два рестарта укладываются
  # в backoff 0с + 10с, то есть ~20–40с с учётом скачивания образа.
  wait_for "под api перезапустился >= 2 раз (restartCount >= 2)" check_s1
  kc get pods -n "$NAMESPACE" -l app=api
}

# --- S2 ---------------------------------------------------------------------
check_s2_pods_ready() { [ "$(ready_pods 'app=web')" -ge 2 ]; }
check_s2_no_endpoints() { [ "$(service_endpoint_count web)" -eq 0 ]; }

break_s2() {
  log "S2 — 502 после релиза: selector сервиса web смотрит на version=v1, поды имеют v2"
  kc apply -f "$MANIFESTS_DIR/s2-502/"
  # Сначала убеждаемся, что поды здоровы: в этом вся соль сценария.
  wait_for "оба пода web стали Ready (с подами всё в порядке)" check_s2_pods_ready
  # И только потом — что у сервиса при этом нет ни одного endpoint.
  wait_for "у сервиса web нет ни одного endpoint (источник 502)" check_s2_no_endpoints
  kc get pods -n "$NAMESPACE" -l app=web
  kc get endpoints web -n "$NAMESPACE" 2>/dev/null || true
}

# --- S3 ---------------------------------------------------------------------
check_s3() { [ "$(pending_pods 'app=reports')" -ge 2 ]; }

break_s3() {
  log "S3 — Deployment не раскатывается: 2 из 5 подов reports в Pending"
  info "шаг 1/2: раскатываю исходную рабочую версию reports (3/3)"
  # Пересоздаём, как S1: nodeSelector добавлен патчем, его нет в last-applied — повторный apply
  # его не снимет, и rollout status ждал бы сломанную версию до progress deadline.
  kc delete deployment reports -n "$NAMESPACE" --ignore-not-found --wait=true
  kc apply -f "$MANIFESTS_DIR/s3-pending/10-deployment.yaml"
  kc rollout status deployment/reports -n "$NAMESPACE" --timeout="${WAIT_TIMEOUT}s"
  info "шаг 2/2: накатываю релиз с несуществующим nodeSelector disktype=nvme"
  kc patch deployment reports -n "$NAMESPACE" \
     --patch-file "$MANIFESTS_DIR/s3-pending/20-break-nodeselector.yaml"
  kc annotate deployment reports -n "$NAMESPACE" --overwrite \
     kubernetes.io/change-cause="release v2: добавлен nodeSelector disktype=nvme" >/dev/null
  # Симптом: старый ReplicaSet держит 3 Running, новый поднял 2 пода, которые не планируются.
  wait_for "2 пода reports зависли в Pending (всего подов 5)" check_s3
  kc get pods -n "$NAMESPACE" -l app=reports
}

# --- S4 ---------------------------------------------------------------------
check_s4() {
  local state
  state="$(kc get pods -n "$NAMESPACE" -l job-name=db-migrate \
            -o jsonpath='{.items[*].status.initContainerStatuses[*].state.running.startedAt}' 2>/dev/null || true)"
  [ -n "$state" ]
}

break_s4() {
  log "S4 — Job не завершается: init-контейнер ждёт несуществующий сервис payments-db:5432"
  # Упавший Job не оживёт от apply. Ждём удаления его подов, чтобы не принять старый за симптом.
  kc delete job db-migrate -n "$NAMESPACE" --ignore-not-found --cascade=foreground --wait=true
  kc apply -f "$MANIFESTS_DIR/s4-stuck-job/"
  # Симптом: под Job висит в Init:0/1, init-контейнер в состоянии Running и не готов.
  wait_for "под job db-migrate завис в Init (init-контейнер wait-for-db работает)" check_s4
  kc get pods -n "$NAMESPACE" -l job-name=db-migrate
  kc get job db-migrate -n "$NAMESPACE"
}

# --- main -------------------------------------------------------------------
target="${1:-all}"
case "$target" in
  -h|--help|help) usage; exit 0 ;;
  s1|s2|s3|s4|all) ;;
  *) usage; die "неизвестный сценарий '$target'" ;;
esac

require_cluster
ensure_base

started=$(date +%s)
if [ "$target" = all ]; then
  for s in "${SCENARIOS[@]}"; do "break_$s"; echo; done
else
  "break_$target"
fi

elapsed=$(( $(date +%s) - started ))
echo
ok "Поломки внесены и подтверждены за ${elapsed}с (цель: $target)"
info "Посмотреть картину целиком: ./status.sh"
info "Снять: ./clean.sh $target"
