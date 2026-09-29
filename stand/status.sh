#!/usr/bin/env bash
# Показать состояние стенда: что поднято и в каком состоянии.
# Ничего не меняет, безопасно запускать в любой момент.
#
# Использование: ./status.sh

set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

require_cmd kubectl "Поставьте kubectl."
require_cmd kind "Поставьте kind."

log "Кластер"
if ! cluster_exists; then
  warn "кластер '$CLUSTER_NAME' не создан. Поднять: $STAND_DIR/up.sh"
  exit 0
fi
info "kind-кластер: $CLUSTER_NAME (контекст $KUBE_CONTEXT)"
if ! kc get --raw /readyz >/dev/null 2>&1; then
  warn "API-сервер не отвечает. Проверьте, что Docker Desktop запущен."
  exit 1
fi
kc get nodes -o wide

echo
log "Namespace $NAMESPACE"
if ! namespace_exists; then
  warn "namespace $NAMESPACE отсутствует. Создать: $STAND_DIR/up.sh"
  exit 0
fi

kc get pods -n "$NAMESPACE" -o wide
echo
kc get deployments,jobs,services,ingress -n "$NAMESPACE" 2>/dev/null || true

echo
log "Сводка по сценариям"

verdict() { # verdict <код> <текст>
  case "$1" in
    ok)      printf '  %s[ сломано как надо ]%s %s\n' "$C_GREEN" "$C_RESET" "$2" ;;
    absent)  printf '  %s[ не применён     ]%s %s\n' "$C_DIM" "$C_RESET" "$2" ;;
    partial) printf '  %s[ ещё не дошёл    ]%s %s\n' "$C_YELLOW" "$C_RESET" "$2" ;;
  esac
}

# S1
if kc get deployment api -n "$NAMESPACE" >/dev/null 2>&1; then
  r="$(max_restarts 'app=api')"
  if [ "$r" -ge 2 ]; then verdict ok "S1 CrashLoopBackOff: под api, restartCount=$r"
  else verdict partial "S1 CrashLoopBackOff: под api, restartCount=$r (ждём >= 2)"; fi
else
  verdict absent "S1 CrashLoopBackOff"
fi

# S2
if kc get service web -n "$NAMESPACE" >/dev/null 2>&1; then
  eps="$(service_endpoint_count web)"; rp="$(ready_pods 'app=web')"
  if [ "$eps" -eq 0 ] && [ "$rp" -ge 1 ]; then
    verdict ok "S2 502 после релиза: подов Ready=$rp, endpoints сервиса web=$eps"
  else
    verdict partial "S2 502 после релиза: подов Ready=$rp, endpoints=$eps (ждём Ready>=1 и endpoints=0)"
  fi
else
  verdict absent "S2 502 после релиза"
fi

# S3
if kc get deployment reports -n "$NAMESPACE" >/dev/null 2>&1; then
  p="$(pending_pods 'app=reports')"
  total="$(kc get pods -n "$NAMESPACE" -l app=reports -o name 2>/dev/null | grep -c . || true)"
  if [ "$p" -ge 2 ]; then verdict ok "S3 не раскатывается: Pending=$p из ${total:-0} подов"
  else verdict partial "S3 не раскатывается: Pending=$p из ${total:-0} (ждём >= 2)"; fi
else
  verdict absent "S3 Deployment не раскатывается"
fi

# S4
if kc get job db-migrate -n "$NAMESPACE" >/dev/null 2>&1; then
  st="$(kc get pods -n "$NAMESPACE" -l job-name=db-migrate \
        -o jsonpath='{.items[0].status.initContainerStatuses[0].state.running.startedAt}' 2>/dev/null || true)"
  comp="$(kc get job db-migrate -n "$NAMESPACE" -o jsonpath='{.status.succeeded}' 2>/dev/null || true)"
  if [ -n "$st" ] && [ -z "$comp" ]; then verdict ok "S4 Job не завершается: init-контейнер работает с $st"
  else verdict partial "S4 Job не завершается: succeeded=${comp:-0}, init started=${st:-нет}"; fi
else
  verdict absent "S4 Job не завершается"
fi

# Здоровый контроль
hr="$(ready_pods 'app=checkout')"
printf '  %s[ контроль        ]%s Здоровый Deployment checkout: Ready=%s (ожидается 2)\n' "$C_BLUE" "$C_RESET" "$hr"

echo
log "Последние события namespace $NAMESPACE"
kc get events -n "$NAMESPACE" --sort-by=.lastTimestamp 2>/dev/null | tail -12 || true
