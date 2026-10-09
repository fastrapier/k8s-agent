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

# Маркер из base/10-secret.yaml: значение Secret payments-db.
SECRET_MARKER="s3cr3t-do-not-leak"

require_cluster
require_cmd uv "Запускайте через make isolation-test: Makefile добавляет ~/.local/bin в PATH."
for f in "$RO_KUBECONFIG" "$RW_KUBECONFIG" "$ADMIN_KUBECONFIG"; do
  [ -f "$f" ] || die "нет $f. Выпустить kubeconfig'и учёток: $STAND_DIR/up.sh"
done

failures=0
pass() { printf '%s  PASS%s %s\n' "$C_GREEN" "$C_RESET" "$*"; }
fail() { printf '%s  FAIL%s %s\n' "$C_RED$C_BOLD" "$C_RESET" "$*"; failures=$((failures + 1)); }

# can_i <kubeconfig> <ожидание yes|no> <глагол> <ресурс> [-n ns | -A]
can_i() {
  local kubeconfig="$1" expected="$2" got; shift 2
  got="$(kubectl --kubeconfig "$kubeconfig" --context "$KUBE_CONTEXT" auth can-i "$@" 2>/dev/null || true)"
  if [ "$got" = "$expected" ]; then pass "can-i $* → $got"
  else fail "can-i $* → ${got:-ошибка}, ожидалось $expected"; fi
}

# Вызов инструмента так же, как его вызывает агент; COLUMNS снимает перенос строк rich,
# иначе grep не нашёл бы значение даже при утечке.
agent_tool() { COLUMNS=100000 uv run --quiet k8s-agent tool "$@" 2>&1; }

log "Учётки: kubeconfig'и принадлежат ServiceAccount'ам, а не администратору"
for pair in "$RO_KUBECONFIG:system:serviceaccount:agent-system:incident-agent-ro" \
            "$RW_KUBECONFIG:system:serviceaccount:agent-system:incident-agent-rw"; do
  file="${pair%%:*}"; want="${pair#*:}"
  who="$(kubectl --kubeconfig "$file" --context "$KUBE_CONTEXT" auth whoami -o jsonpath='{.status.userInfo.username}' 2>/dev/null || true)"
  if [ "$who" = "$want" ]; then pass "$(basename "$file") → $who"
  else fail "$(basename "$file") → ${who:-ошибка}, ожидалось $want"; fi
done

log "№1 Диагност (incident-agent-ro): читает, но не пишет и не видит Secret"
can_i "$RO_KUBECONFIG" yes list pods -n "$NAMESPACE"
can_i "$RO_KUBECONFIG" yes get pods/log -n "$NAMESPACE"
can_i "$RO_KUBECONFIG" yes list nodes
can_i "$RO_KUBECONFIG" no  get secrets -n "$NAMESPACE"
can_i "$RO_KUBECONFIG" no  list secrets -A
can_i "$RO_KUBECONFIG" no  patch deployments -n "$NAMESPACE"
can_i "$RO_KUBECONFIG" no  delete pods -n "$NAMESPACE"
can_i "$RO_KUBECONFIG" no  create pods/exec -n "$NAMESPACE"

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

if grep -qF "$SECRET_MARKER" <<<"$out$ro_out$admin_out"; then fail "значение Secret есть в выводе инструментов"
else pass "значение Secret в выводе инструментов не найдено (оба рубежа)"; fi

echo
if [ "$failures" -eq 0 ]; then
  ok "Все проверки изоляции прошли"
else
  die "проверок не прошло: $failures"
fi
