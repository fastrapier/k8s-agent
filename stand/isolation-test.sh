#!/usr/bin/env bash
# Проверки изоляции из docs/ISOLATION.md раздел 10, доступные на этапе М2.
#
#   №1  учётка диагноста не может писать в кластер (RBAC);
#   №3  значение Secret не попадает в вывод инструментов: рубеж 1 — RBAC, рубеж 2 — маскирование.
#
# Права проверяются через сами kubeconfig'и учёток, а не через `kubectl --as=...` от админа:
# `--as` проверяет правила RBAC, но не то, под чем агент реально ходит в кластер.
#
# Использование: ./isolation-test.sh   (код выхода 1, если хоть одна проверка не прошла)

set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

# Маркер из base/10-secret.yaml: значение Secret payments-db. API отдаёт `data` в base64,
# поэтому ищем обе формы — иначе сломанное маскирование `data` прошло бы незамеченным.
SECRET_MARKER="s3cr3t-do-not-leak"
SECRET_MARKER_B64="$(printf %s "$SECRET_MARKER" | base64)"

require_cluster
require_cmd uv "Запускайте через make isolation-test: Makefile добавляет ~/.local/bin в PATH."
for f in "$RO_KUBECONFIG" "$RW_KUBECONFIG"; do
  [ -f "$f" ] || die "нет $f. Выпустить kubeconfig'и учёток: $STAND_DIR/up.sh"
done

# Админский kubeconfig нужен только для рубежа 2 и живёт до конца скрипта.
ADMIN_KUBECONFIG="$(mktemp)"
trap 'rm -f "$ADMIN_KUBECONFIG"' EXIT
kind get kubeconfig --name "$CLUSTER_NAME" >"$ADMIN_KUBECONFIG"

failures=0
pass() { printf '%s  PASS%s %s\n' "$C_GREEN" "$C_RESET" "$*"; }
fail() { printf '%s  FAIL%s %s\n' "$C_RED$C_BOLD" "$C_RESET" "$*"; failures=$((failures + 1)); }

# Подресурсы — только через --subresource: `can-i create pods/exec` kubectl читает как
# «под с именем exec» и ответит no, даже если exec разрешён.
# can_i <kubeconfig> <ожидание yes|no> <аргументы kubectl auth can-i>
can_i() {
  local kubeconfig="$1" expected="$2" got; shift 2
  got="$(kubectl --kubeconfig "$kubeconfig" --context "$KUBE_CONTEXT" auth can-i "$@" 2>/dev/null || true)"
  if [ "$got" = "$expected" ]; then pass "can-i $* → $got"
  else fail "can-i $* → ${got:-ошибка}, ожидалось $expected"; fi
}

# whoami_is <kubeconfig> <ожидаемый пользователь>
whoami_is() {
  local who
  who="$(kubectl --kubeconfig "$1" --context "$KUBE_CONTEXT" auth whoami \
           -o jsonpath='{.status.userInfo.username}' 2>/dev/null || true)"
  [ -n "$who" ] || die "API-сервер не принял токен из $(basename "$1") (истёк?). Выпустить новый: make stand-up"
  if [ "$who" = "$2" ]; then pass "$(basename "$1") → $who"
  else fail "$(basename "$1") → $who, ожидалось $2"; fi
}

# Вызов инструмента так же, как его вызывает агент; COLUMNS снимает перенос строк rich,
# иначе grep не нашёл бы значение даже при утечке.
agent_tool() { COLUMNS=100000 uv run --quiet --project "$STAND_DIR/.." k8s-agent tool "$@" 2>&1; }

log "Учётки: kubeconfig'и принадлежат ServiceAccount'ам, а не администратору"
whoami_is "$RO_KUBECONFIG" system:serviceaccount:agent-system:incident-agent-ro
whoami_is "$RW_KUBECONFIG" system:serviceaccount:agent-system:incident-agent-rw

log "№1 Диагност (incident-agent-ro): читает, но не пишет, не исполняет и не видит Secret"
can_i "$RO_KUBECONFIG" yes list pods -n "$NAMESPACE"
can_i "$RO_KUBECONFIG" yes get pods --subresource=log -n "$NAMESPACE"
can_i "$RO_KUBECONFIG" yes get nodes
can_i "$RO_KUBECONFIG" no  get secrets -n "$NAMESPACE"
can_i "$RO_KUBECONFIG" no  list secrets -A
can_i "$RO_KUBECONFIG" no  list configmaps -A
can_i "$RO_KUBECONFIG" no  patch deployments -n "$NAMESPACE"
can_i "$RO_KUBECONFIG" no  delete pods -n "$NAMESPACE"
can_i "$RO_KUBECONFIG" no  create pods --subresource=exec -n "$NAMESPACE"
can_i "$RO_KUBECONFIG" no  get pods --subresource=exec -n "$NAMESPACE"
can_i "$RO_KUBECONFIG" no  create pods --subresource=attach -n "$NAMESPACE"
can_i "$RO_KUBECONFIG" no  create pods --subresource=portforward -n "$NAMESPACE"

log "Исполнитель (incident-agent-rw): только patch, только в $NAMESPACE"
can_i "$RW_KUBECONFIG" yes patch deployments -n "$NAMESPACE"
can_i "$RW_KUBECONFIG" yes patch configmaps -n "$NAMESPACE"
can_i "$RW_KUBECONFIG" no  patch deployments -n default
can_i "$RW_KUBECONFIG" no  create deployments -n "$NAMESPACE"
can_i "$RW_KUBECONFIG" no  delete deployments -n "$NAMESPACE"
can_i "$RW_KUBECONFIG" no  get secrets -n "$NAMESPACE"

log "№3 Secret через слой инструментов агента"
out="$(agent_tool k8s_list_pods --ns "$NAMESPACE")" && rc=0 || rc=$?
if [ "$rc" -eq 0 ] && grep -q '^ok ' <<<"$out"; then pass "k8s_list_pods под учёткой диагноста → ok"
else fail "k8s_list_pods под учёткой диагноста → код $rc"; fi

ro_out="$(agent_tool k8s_get_manifest --kind Secret --name payments-db --ns "$NAMESPACE")" && rc=0 || rc=$?
if [ "$rc" -ne 0 ] && grep -q 'нет прав' <<<"$ro_out"; then pass "рубеж 1: Secret под учёткой диагноста → отказ RBAC"
else fail "рубеж 1: Secret под учёткой диагноста → код $rc, отказа RBAC нет"; fi

admin_out="$(agent_tool k8s_get_manifest --kind Secret --name payments-db --ns "$NAMESPACE" \
              --kubeconfig "$ADMIN_KUBECONFIG")" && rc=0 || rc=$?
if [ "$rc" -eq 0 ] && grep -q '\*\*\*masked\*\*\*' <<<"$admin_out"; then
  pass "рубеж 2: тот же Secret под админом → ***masked***"
else fail "рубеж 2: тот же Secret под админом → код $rc, маски нет"; fi

if grep -qF -e "$SECRET_MARKER" -e "$SECRET_MARKER_B64" <<<"$out$ro_out$admin_out"; then
  fail "значение Secret (открытым текстом или base64) есть в выводе инструментов"
else pass "значение Secret ни открытым текстом, ни в base64 в выводе не найдено"; fi

echo
if [ "$failures" -eq 0 ]; then
  ok "Все проверки изоляции прошли"
else
  die "проверок не прошло: $failures"
fi
