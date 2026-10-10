#!/usr/bin/env bash
# Проверки изоляции из docs/ISOLATION.md раздел 10, доступные на этапе М2.
#
#   №1  учётка диагноста не может писать в кластер (RBAC);
#   №3  значение Secret не попадает в вывод инструментов: рубеж 1 — RBAC, рубеж 2 — маскирование;
#   №4  контейнер агента: не root, read-only rootfs, без capabilities, seccomp, только нужные тома.
#   №5  сеть агента: наружу только LLM-шлюз и API-сервер, и только через egress-proxy.
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
for f in "$RO_KUBECONFIG" "$RW_KUBECONFIG" "$RO_CONTAINER_KUBECONFIG"; do
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
else fail "k8s_list_pods под учёткой диагноста → код $rc: $(tail -1 <<<"$out")"; fi

ro_out="$(agent_tool k8s_get_manifest --kind Secret --name payments-db --ns "$NAMESPACE")" && rc=0 || rc=$?
if [ "$rc" -ne 0 ] && grep -q 'нет прав' <<<"$ro_out"; then pass "рубеж 1: Secret под учёткой диагноста → отказ RBAC"
else fail "рубеж 1: Secret под учёткой диагноста → код $rc, отказа RBAC нет: $(tail -1 <<<"$ro_out")"; fi

admin_out="$(agent_tool k8s_get_manifest --kind Secret --name payments-db --ns "$NAMESPACE" \
              --kubeconfig "$ADMIN_KUBECONFIG")" && rc=0 || rc=$?
if [ "$rc" -eq 0 ] && grep -q '\*\*\*masked\*\*\*' <<<"$admin_out"; then
  pass "рубеж 2: тот же Secret под админом → ***masked***"
else fail "рубеж 2: тот же Secret под админом → код $rc, маски нет: $(tail -1 <<<"$admin_out")"; fi

if grep -qF -e "$SECRET_MARKER" -e "$SECRET_MARKER_B64" <<<"$out$ro_out$admin_out"; then
  fail "значение Secret (открытым текстом или base64) есть в выводе инструментов"
else pass "значение Secret ни открытым текстом, ни в base64 в выводе не найдено"; fi

log "№4 Контейнер агента (docker-compose.yml)"
require_cmd docker "Нужен Docker: в нём живёт и kind, и контейнер агента."
compose() { docker compose -f "$STAND_DIR/../docker-compose.yml" "$@"; }
compose build --quiet   # все сервисы: иначе проверялся бы прокси со старым allow-list

# Одна сессия контейнера, несколько замеров; скрипт идёт через stdin, без слоёв кавычек.
# Запись — в /var/tmp, а не в /app: /app принадлежит root, и не-root процесс получил бы отказ
# даже на записываемой ФС. Capabilities — по CapBnd: CapEff у не-root нулевой всегда.
# Монтирования — списком: так ловится docker.sock по любому пути, лишний том и снятый :ro.
probe="$(compose run --rm -T --entrypoint sh agent -s 2>&1 <<'SH' || true
echo "uid=$(id -u)"
touch /var/tmp/probe 2>&1 | grep -q "Read-only file system" && echo rootfs=ro || echo rootfs=rw
grep -E "^(CapBnd|NoNewPrivs|Seccomp):" /proc/self/status | tr -d ' \t'
echo "mounts=$(awk '$5 !~ "^/(proc|sys|dev)(/|$)" && $5 !~ "^/etc/(hosts|hostname|resolv[.]conf)$" && $5 != "/" { split($6, o, ","); print $5 ":" o[1] }' /proc/self/mountinfo | sort | paste -sd, -)"
SH
)"
expect_in() { # expect_in <вывод замеров> <ожидаемая строка> <что она значит>
  if grep -qx "$2" <<<"$1"; then pass "$3: $2"
  else fail "$3: ожидалось $2, получено: $(grep -E "^${2%%[=:]*}[=:]" <<<"$1" || tail -1 <<<"$1")"; fi
}
expect_in "$probe" "uid=10001" "не root"
expect_in "$probe" "rootfs=ro" "корневая ФС только для чтения"
expect_in "$probe" "CapBnd:0000000000000000" "capabilities сброшены (cap_drop ALL)"
expect_in "$probe" "NoNewPrivs:1" "повышение привилегий запрещено (no-new-privileges)"
expect_in "$probe" "Seccomp:2" "профиль seccomp включён"
expect_in "$probe" "mounts=/app/.kube/agent-ro.kubeconfig:ro,/tmp:rw" \
  "смонтированы только kubeconfig диагноста (ro) и /tmp — ни docker.sock, ни лишних томов"

# Агент в этих ограничениях работает (к API-серверу — через egress-proxy, см. №5),
# и в контейнере он тоже под учёткой диагноста.
c_out="$(compose run --rm -e COLUMNS=100000 agent tool k8s_list_pods --ns "$NAMESPACE" 2>&1)" && rc=0 || rc=$?
if [ "$rc" -eq 0 ] && grep -q '^ok ' <<<"$c_out"; then pass "k8s_list_pods из контейнера → ok"
else fail "k8s_list_pods из контейнера → код $rc: $(tail -1 <<<"$c_out")"; fi
c_out="$(compose run --rm -e COLUMNS=100000 agent tool k8s_get_manifest --kind Secret --name payments-db --ns "$NAMESPACE" 2>&1)" && rc=0 || rc=$?
if [ "$rc" -ne 0 ] && grep -q 'нет прав' <<<"$c_out"; then pass "Secret из контейнера → отказ RBAC (учётка диагноста)"
else fail "Secret из контейнера → код $rc, отказа RBAC нет: $(tail -1 <<<"$c_out")"; fi

log "№5 Сеть агента: наружу только через egress-proxy и только к двум адресам"
# LLM-шлюз — запись allow-list прокси, кроме API-сервера (формат «имя:порт»).
llm_host="$(grep -v control-plane "$STAND_DIR/egress-proxy/allowlist" | head -1 | cut -d: -f1)"
net="$(compose run --rm -T -e LLM_HOST="$llm_host" --entrypoint python agent - 2>&1 <<'PY' || true
import errno, os, socket, urllib.request, urllib.error

def via_proxy(url):
    """reachable — ответил адресат; proxy-denied — отказал сам прокси; иначе — ошибка."""
    try:
        urllib.request.urlopen(url, timeout=8)
        return "reachable"
    except urllib.error.HTTPError as exc:
        # обычный HTTP: 403 самого tinyproxy отличаем по заголовку Server
        if exc.code == 403 and (exc.headers.get("Server") or "").startswith("tinyproxy"):
            return "proxy-denied"
        return "reachable"
    except urllib.error.URLError as exc:
        # CONNECT: отказ прокси приходит как «Tunnel connection failed: 403 …»
        if "Tunnel connection failed: 403" in str(exc.reason):
            return "proxy-denied"
        return "error"

def direct_ip(ip, port):
    """no-route только при ENETUNREACH/EHOSTUNREACH: таймаут файрвола среды ничего не доказывает."""
    try:
        socket.create_connection((ip, port), timeout=4).close()
        return "reachable"
    except OSError as exc:
        return "no-route" if exc.errno in (errno.ENETUNREACH, errno.EHOSTUNREACH) else "error"

def dns(name):
    try:
        socket.gethostbyname(name)
        return "resolved"
    except socket.gaierror:
        return "none"

node = "k8s-incident-agent-control-plane"
print("llm_via_proxy=" + via_proxy(f"https://{os.environ['LLM_HOST']}/v1/models"))
print("foreign_via_proxy=" + via_proxy("https://example.com/"))
print("llm_other_port_via_proxy=" + via_proxy(f"https://{os.environ['LLM_HOST']}:6443/"))
print("kubelet_via_proxy=" + via_proxy(f"https://{node}:10250/"))
print("node_plain_http_via_proxy=" + via_proxy(f"http://{node}:10256/healthz"))
print("foreign_direct_ip=" + direct_ip("1.1.1.1", 443))
print("foreign_dns=" + dns("example.com"))
PY
)"
expect_in "$net" "llm_via_proxy=reachable" "LLM-шлюз $llm_host:443 через прокси доступен"
expect_in "$net" "foreign_via_proxy=proxy-denied" "посторонний адрес через прокси → 403 прокси"
expect_in "$net" "llm_other_port_via_proxy=proxy-denied" "имя шлюза на чужом порту (6443) → 403: разрешены пары имя:порт"
expect_in "$net" "kubelet_via_proxy=proxy-denied" "kubelet (10250) на ноде с API-сервером → 403"
expect_in "$net" "node_plain_http_via_proxy=proxy-denied" "обычный HTTP на ноду (kube-proxy :10256) → 403"
expect_in "$net" "foreign_direct_ip=no-route" "напрямую по IP мимо прокси → маршрута нет"
expect_in "$net" "foreign_dns=none" "внешние имена напрямую не резолвятся"

# Хост Docker через адрес моста. internal-сеть убирает маршрут наружу, но не адрес моста —
# поэтому проверка с контролем: временный слушатель на хосте должен быть виден из обычной сети
# kind (иначе проверка ничего не доказывает) и не виден из сети агента.
listener="$(docker run -d --rm --network host --entrypoint python k8s-incident-agent:dev -m http.server 18765)"
trap 'rm -f "$ADMIN_KUBECONFIG"; docker rm -f "$listener" >/dev/null 2>&1 || true' EXIT
sleep 2
host_probe='import sys, urllib.request as u
try:
    u.build_opener(u.ProxyHandler({})).open(sys.argv[1], timeout=4); print("reachable")
except Exception:
    print("unreachable")'
kind_gw="$(docker network inspect kind -f '{{range .IPAM.Config}}{{.Gateway}} {{end}}' | tr ' ' '\n' | grep -m1 '\.')"
agent_subnet="$(docker network inspect k8s-incident-agent_agent-egress -f '{{range .IPAM.Config}}{{.Subnet}}{{end}}')"
agent_gw="$(python3 -c "import ipaddress, sys; print(ipaddress.ip_network(sys.argv[1])[1])" "$agent_subnet")"
control="$(docker run --rm --network kind --entrypoint python k8s-incident-agent:dev -c "$host_probe" "http://$kind_gw:18765/" 2>&1)"
from_agent="$(compose run --rm -T --entrypoint python agent -c "$host_probe" "http://$agent_gw:18765/" 2>&1 | tail -1)"
if [ "$control" != reachable ]; then fail "контроль: слушатель на хосте недоступен даже из сети kind — проверка недействительна"
elif [ "$from_agent" = unreachable ]; then pass "хост Docker через адрес моста ($agent_gw) из сети агента недоступен (из сети kind — доступен)"
else fail "хост Docker через адрес моста ($agent_gw) из сети агента доступен: $from_agent"; fi
docker rm -f "$listener" >/dev/null 2>&1 || true

echo
if [ "$failures" -eq 0 ]; then
  ok "Все проверки изоляции прошли"
else
  die "проверок не прошло: $failures"
fi
