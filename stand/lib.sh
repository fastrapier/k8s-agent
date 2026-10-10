#!/usr/bin/env bash
# Общие функции скриптов демо-стенда k8s-incident-agent.
# Подключается через `source "$(dirname "$0")/lib.sh"`, самостоятельно не запускается.

set -euo pipefail

STAND_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MANIFESTS_DIR="$STAND_DIR/manifests"

CLUSTER_NAME="${STAND_CLUSTER_NAME:-k8s-incident-agent}"
KUBE_CONTEXT="kind-${CLUSTER_NAME}"
NAMESPACE="${STAND_NAMESPACE:-payments}"

# Таймаут ожидания симптома, секунд. Переопределяется переменной окружения.
WAIT_TIMEOUT="${STAND_WAIT_TIMEOUT:-180}"
# Пауза между опросами состояния, секунд.
POLL_INTERVAL="${STAND_POLL_INTERVAL:-3}"

SCENARIOS=(s1 s2 s3 s4)

if [ -t 1 ]; then
  C_RESET=$'\033[0m'; C_BOLD=$'\033[1m'; C_DIM=$'\033[2m'
  C_RED=$'\033[31m'; C_GREEN=$'\033[32m'; C_YELLOW=$'\033[33m'; C_BLUE=$'\033[34m'
else
  C_RESET=''; C_BOLD=''; C_DIM=''; C_RED=''; C_GREEN=''; C_YELLOW=''; C_BLUE=''
fi

log()  { printf '%s==>%s %s\n' "$C_BLUE$C_BOLD" "$C_RESET" "$*"; }
info() { printf '    %s\n' "$*"; }
dim()  { printf '    %s%s%s\n' "$C_DIM" "$*" "$C_RESET"; }
ok()   { printf '%s  ok%s %s\n' "$C_GREEN" "$C_RESET" "$*"; }
warn() { printf '%sвним%s %s\n' "$C_YELLOW" "$C_RESET" "$*"; }
die()  { printf '%s ОШИБКА%s %s\n' "$C_RED$C_BOLD" "$C_RESET" "$*" >&2; exit 1; }

# kubectl, всегда прибитый к контексту стенда: случайно не попадём в чужой кластер.
kc() { kubectl --context "$KUBE_CONTEXT" "$@"; }

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || die "не найдена команда '$1'. $2"
}

require_tools() {
  require_cmd kubectl "Поставьте kubectl: https://kubernetes.io/docs/tasks/tools/"
  require_cmd kind    "Поставьте kind: https://kind.sigs.k8s.io/docs/user/quick-start/#installation"
  docker info >/dev/null 2>&1 || die "Docker не отвечает. Запустите Docker Desktop и повторите."
}

cluster_exists() {
  kind get clusters 2>/dev/null | grep -qx "$CLUSTER_NAME"
}

require_cluster() {
  cluster_exists || die "кластер '$CLUSTER_NAME' не найден. Сначала выполните: $STAND_DIR/up.sh"
  kc get --raw /readyz >/dev/null 2>&1 || die "кластер '$CLUSTER_NAME' есть, но API-сервер не отвечает."
}

namespace_exists() {
  kc get namespace "$NAMESPACE" >/dev/null 2>&1
}

# --- учётки агента (М2, docs/ISOLATION.md раздел 5) ---

# kubeconfig'и учёток лежат в корне репозитория, в .kube/ (закрыт .gitignore).
# Пути совпадают с k8s.readonly_kubeconfig / k8s.readwrite_kubeconfig в config.yaml.
AGENT_KUBE_DIR="$(cd "$STAND_DIR/.." && pwd)/.kube"
RO_KUBECONFIG="$AGENT_KUBE_DIR/agent-ro.kubeconfig"
RW_KUBECONFIG="$AGENT_KUBE_DIR/agent-rw.kubeconfig"
# Для контейнера агента (docker-compose.yml): тот же токен диагноста, но API-сервер по имени
# ноды kind в docker-сети — 127.0.0.1 изнутри контейнера указывает на сам контейнер.
RO_CONTAINER_KUBECONFIG="$AGENT_KUBE_DIR/agent-ro.container.kubeconfig"
RW_CONTAINER_KUBECONFIG="$AGENT_KUBE_DIR/agent-rw.container.kubeconfig"
CONTAINER_API_SERVER="https://${CLUSTER_NAME}-control-plane:6443"

# kubeconfig для ServiceAccount агента: тот же API-сервер и CA, что у админского контекста,
# но вместо сертификата администратора — токен учётки. Токен выпускается через TokenRequest
# на сутки: украденный файл завтра бесполезен, а ./up.sh выпускает новый.
# Использование: write_agent_kubeconfig <serviceaccount> <файл> [адрес API-сервера]
write_agent_kubeconfig() {
  local sa="$1" out="$2" server="${3:-}" ca token
  [ -n "$server" ] || server="$(kc config view --raw --minify -o jsonpath='{.clusters[0].cluster.server}')"
  ca="$(kc config view --raw --minify -o jsonpath='{.clusters[0].cluster.certificate-authority-data}')"
  token="$(kc create token "$sa" -n agent-system --duration=24h)"
  mkdir -p "$(dirname "$out")" && chmod 700 "$(dirname "$out")"
  rm -f "$out"   # umask действует только при создании файла
  (
    umask 077
    cat >"$out" <<EOF
apiVersion: v1
kind: Config
clusters:
  - name: $KUBE_CONTEXT
    cluster:
      server: $server
      certificate-authority-data: $ca
users:
  - name: $sa
    user:
      token: $token
contexts:
  - name: $KUBE_CONTEXT
    context:
      cluster: $KUBE_CONTEXT
      user: $sa
current-context: $KUBE_CONTEXT
EOF
  )
}

# Ожидание симптома с таймаутом.
# Использование: wait_for "человекочитаемое описание" функция_проверки
# Функция проверки возвращает 0, когда условие выполнено.
wait_for() {
  local desc="$1"; shift
  local checker=("$@")
  local started elapsed
  started=$(date +%s)

  info "жду: $desc (таймаут ${WAIT_TIMEOUT}с)"
  while true; do
    if "${checker[@]}"; then
      elapsed=$(( $(date +%s) - started ))
      printf '\r%*s\r' 72 ''
      ok "$desc — достигнуто за ${elapsed}с"
      return 0
    fi
    elapsed=$(( $(date +%s) - started ))
    if [ "$elapsed" -ge "$WAIT_TIMEOUT" ]; then
      printf '\r%*s\r' 72 ''
      printf '%s ОШИБКА%s таймаут %sс: не дождались состояния «%s».\n' \
        "$C_RED$C_BOLD" "$C_RESET" "$WAIT_TIMEOUT" "$desc" >&2
      dump_diagnostics >&2
      printf '\nЧто делать: посмотрите вывод выше. Частые причины — не скачался образ busybox:1.36\n' >&2
      printf 'или ноды кластера ещё не готовы. Увеличить таймаут: STAND_WAIT_TIMEOUT=300 %s\n' "$0" >&2
      exit 1
    fi
    printf '    %s... %sс%s\r' "$C_DIM" "$elapsed" "$C_RESET"
    sleep "$POLL_INTERVAL"
  done
}

# Диагностика при таймауте: то, на что в первую очередь посмотрит человек.
dump_diagnostics() {
  echo
  echo "--- поды в namespace $NAMESPACE ---"
  kc get pods -n "$NAMESPACE" -o wide 2>&1 || true
  echo "--- последние события ---"
  kc get events -n "$NAMESPACE" --sort-by=.lastTimestamp 2>&1 | tail -20 || true
  echo "--- ноды ---"
  kc get nodes 2>&1 || true
}

# --- элементарные проверки состояния, используются и в break.sh, и в status.sh ---

# Максимальный restartCount среди подов по селектору меток.
max_restarts() {
  local selector="$1" raw
  raw="$(kc get pods -n "$NAMESPACE" -l "$selector" \
          -o jsonpath='{.items[*].status.containerStatuses[*].restartCount}' 2>/dev/null || true)"
  if [ -z "$raw" ]; then echo 0; return; fi
  tr ' ' '\n' <<<"$raw" | sort -rn | head -1
}

# Число подов по селектору в фазе Pending.
pending_pods() {
  local selector="$1" raw n
  raw="$(kc get pods -n "$NAMESPACE" -l "$selector" --field-selector=status.phase=Pending \
          -o name 2>/dev/null || true)"
  n="$(grep -c . <<<"$raw" || true)"
  echo "${n:-0}"
}

# Число готовых адресов в EndpointSlice сервиса.
service_endpoint_count() {
  local svc="$1" raw n
  raw="$(kc get endpointslices -n "$NAMESPACE" -l "kubernetes.io/service-name=$svc" \
          -o jsonpath='{.items[*].endpoints[*].addresses[*]}' 2>/dev/null || true)"
  n="$(tr ' ' '\n' <<<"$raw" | grep -c . || true)"
  echo "${n:-0}"
}

# Число подов по селектору в состоянии Ready.
ready_pods() {
  local selector="$1" raw n
  raw="$(kc get pods -n "$NAMESPACE" -l "$selector" \
          -o jsonpath='{range .items[*]}{range .status.conditions[?(@.type=="Ready")]}{.status}{"\n"}{end}{end}' 2>/dev/null || true)"
  n="$(grep -c '^True$' <<<"$raw" || true)"
  echo "${n:-0}"
}
