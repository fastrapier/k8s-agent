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

METRICS_SERVER_VERSION="v0.9.0"
log "Устанавливаю metrics-server $METRICS_SERVER_VERSION"
kc apply -f "https://github.com/kubernetes-sigs/metrics-server/releases/download/${METRICS_SERVER_VERSION}/components.yaml"

# Kubelet в kind отдаёт самоподписанный сертификат: без этого флага metrics-server
# не может его проверить и не становится Ready. Это стандартная настройка для kind.
# Проверяем аргументы, чтобы повторный запуск не добавлял флаг второй раз.
metrics_server_args="$(kc get deployment metrics-server -n kube-system \
  -o jsonpath='{range .spec.template.spec.containers[0].args[*]}{.}{"\n"}{end}')"
if ! grep -Fxq -- '--kubelet-insecure-tls' <<<"$metrics_server_args"; then
  kc patch deployment metrics-server -n kube-system --type=json \
    -p='[{"op":"add","path":"/spec/template/spec/containers/0/args/-","value":"--kubelet-insecure-tls"}]'
fi

log "Жду готовности metrics-server и первого сбора метрик"
kc rollout status deployment/metrics-server -n kube-system --timeout="${WAIT_TIMEOUT}s"

log "Жду, пока здоровый Deployment checkout станет доступен"
kc rollout status deployment/checkout -n "$NAMESPACE" --timeout="${WAIT_TIMEOUT}s"

# Учётки агента созданы манифестом base/20-agent-rbac.yaml; агент ходит в кластер только под
# ними. В .kube/ лежат только их токены — админского kubeconfig здесь нет намеренно.
log "Выпускаю kubeconfig'и учёток агента в ${AGENT_KUBE_DIR}"
write_agent_kubeconfigs

elapsed=$(( $(date +%s) - started ))
echo
ok "Стенд поднят за ${elapsed}с. Контекст kubectl: $KUBE_CONTEXT, namespace: $NAMESPACE"
info "Дальше: ./break.sh all   — внести все поломки S1–S4"
info "        ./break.sh s1    — только сценарий S1"
info "        ./status.sh      — текущее состояние стенда"
