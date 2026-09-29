#!/usr/bin/env bash
# Поднять демо-стенд: кластер kind + namespace payments + базовые объекты.
# Идемпотентно: если кластер уже есть, он не пересоздаётся.
#
# Использование: ./up.sh
# Переменные: STAND_CLUSTER_NAME, STAND_NAMESPACE, STAND_WAIT_TIMEOUT

set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

started=$(date +%s)

log "Проверяю инструменты"
require_tools
dim "kind:    $(kind version 2>/dev/null | head -1)"
dim "kubectl: $(kubectl version --client -o json 2>/dev/null | sed -n 's/.*"gitVersion": "\([^"]*\)".*/\1/p' | head -1)"

if cluster_exists; then
  log "Кластер '$CLUSTER_NAME' уже существует — пересоздавать не буду"
else
  log "Создаю кластер '$CLUSTER_NAME' (первый раз это дольше: тянется образ ноды)"
  kind create cluster --config "$STAND_DIR/kind.yaml" --wait 120s
fi

log "Жду готовности нод"
kc wait --for=condition=Ready nodes --all --timeout="${WAIT_TIMEOUT}s" >/dev/null
kc get nodes -o wide

log "Применяю базовые объекты (namespace, Secret, здоровый Deployment checkout)"
kc apply -f "$MANIFESTS_DIR/base/"

log "Жду, пока здоровый Deployment checkout станет доступен"
kc rollout status deployment/checkout -n "$NAMESPACE" --timeout="${WAIT_TIMEOUT}s"

elapsed=$(( $(date +%s) - started ))
echo
ok "Стенд поднят за ${elapsed}с. Контекст kubectl: $KUBE_CONTEXT, namespace: $NAMESPACE"
info "Дальше: ./break.sh all   — внести все поломки S1–S4"
info "        ./break.sh s1    — только сценарий S1"
info "        ./status.sh      — текущее состояние стенда"
