#!/usr/bin/env bash
# Перевыпустить токены учёток агента (TokenRequest, 24 ч), не трогая кластер и не ходя в сеть.
# Токен истёк — каждый вызов инструмента агента получает Unauthorized.
#
# Использование: ./tokens.sh

set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

require_cluster
log "Выпускаю kubeconfig'и учёток агента в ${AGENT_KUBE_DIR}"
write_agent_kubeconfigs
agent_token_accepted || die "API-сервер не принял новый токен диагноста"
ok "токен диагноста принят"
