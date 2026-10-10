# Egress-прокси агента (ТЗ N2, docs/ISOLATION.md раздел 3): единственный выход контейнера
# агента наружу. Сам прокси тоже запускается без root и с read-only ФС (docker-compose.yml).
FROM alpine:3.22@sha256:5291449c3df73caf6ed85e649dec1b9e818b39a5d8c871e97afc13e9cd5e8fa8
RUN apk add --no-cache tinyproxy=1.11.2-r0
COPY tinyproxy.conf allowlist /etc/tinyproxy/
USER nobody
ENTRYPOINT ["tinyproxy", "-d", "-c", "/etc/tinyproxy/tinyproxy.conf"]
