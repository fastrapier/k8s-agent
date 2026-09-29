#!/usr/bin/env bash
# Снять поломку. Кластер остаётся поднятым.
#
# Использование: ./clean.sh [s1|s2|s3|s4|all]
#   без аргумента — all
#   all — удаляет namespace payments целиком (самый надёжный способ вернуться
#         к чистому листу; ровно так же делает harness evals между кейсами,
#         docs/EVALS.md раздел 4.3)
#   s1..s4 — удаляет только объекты своего сценария, остальные не трогает

set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

usage() { echo "Использование: $0 [s1|s2|s3|s4|all]"; }

del() { kc delete --ignore-not-found=true "$@"; }

clean_s1() {
  log "S1 — удаляю Deployment api и Service api"
  del -f "$MANIFESTS_DIR/s1-crashloop/" -n "$NAMESPACE"
}

clean_s2() {
  log "S2 — удаляю Ingress/Service/Deployment web"
  del -f "$MANIFESTS_DIR/s2-502/" -n "$NAMESPACE"
}

clean_s3() {
  # 20-break-nodeselector.yaml — это patch-фрагмент, а не объект, удалять по нему нечего.
  log "S3 — удаляю Deployment reports"
  del -f "$MANIFESTS_DIR/s3-pending/10-deployment.yaml" -n "$NAMESPACE"
}

clean_s4() {
  log "S4 — удаляю Job db-migrate"
  del -f "$MANIFESTS_DIR/s4-stuck-job/" -n "$NAMESPACE"
}

clean_all() {
  if ! namespace_exists; then
    ok "Namespace $NAMESPACE уже отсутствует — чистить нечего"
    return 0
  fi
  log "Удаляю namespace $NAMESPACE целиком"
  kc delete namespace "$NAMESPACE" --wait=true --timeout="${WAIT_TIMEOUT}s"
  ok "Namespace $NAMESPACE удалён. Базу поднимет заново ./up.sh или ./break.sh"
}

target="${1:-all}"
case "$target" in
  -h|--help|help) usage; exit 0 ;;
  s1|s2|s3|s4) ;;
  all) ;;
  *) usage; die "неизвестный сценарий '$target'" ;;
esac

require_cluster

if [ "$target" = all ]; then
  clean_all
else
  namespace_exists || die "namespace $NAMESPACE отсутствует — нечего снимать"
  "clean_$target"
  ok "Сценарий $target снят. Остальные объекты стенда не тронуты."
fi
