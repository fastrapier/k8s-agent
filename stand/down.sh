#!/usr/bin/env bash
# Снести кластер стенда целиком. Идемпотентно.
#
# Использование: ./down.sh

set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

require_cmd kind "Поставьте kind: https://kind.sigs.k8s.io/docs/user/quick-start/#installation"

# Контейнеры агента (docker-compose.yml): egress-прокси висит в сети kind и без кластера не нужен.
if command -v docker >/dev/null 2>&1; then
  docker compose -f "$STAND_DIR/../docker-compose.yml" down --remove-orphans >/dev/null 2>&1 || true
fi

if ! cluster_exists; then
  ok "Кластер '$CLUSTER_NAME' не существует — сносить нечего"
  exit 0
fi

log "Сношу кластер '$CLUSTER_NAME'"
kind delete cluster --name "$CLUSTER_NAME"
ok "Кластер '$CLUSTER_NAME' удалён. Образы Docker остались в кеше — следующий подъём будет быстрее."
