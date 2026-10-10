# Образ агента (ТЗ N2, docs/ISOLATION.md раздел 4).
#
# Две стадии: в builder uv собирает виртуальное окружение по uv.lock, в итоговый образ
# переезжает только окружение и config.yaml. uv, исходники и кеши в рантайме не нужны.
# Базовые образы прибиты дайджестом (N6): тег можно перезаписать, дайджест — нет.
# Ограничения рантайма (read-only rootfs, cap-drop, no-new-privileges) — в docker-compose.yml.

FROM ghcr.io/astral-sh/uv:0.12.20@sha256:100047e74f30778ab704942321a09750d6158739573ff58bf3924085cc6cd2d8 AS uv

FROM python:3.12-slim@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1 AS builder
COPY --from=uv /uv /usr/local/bin/uv
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
# Сначала только зависимости: слой кешируется, пока не меняется uv.lock.
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project
COPY src ./src
# --no-editable: пакет ставится в окружение целиком, исходники в итоговом образе не нужны.
RUN uv sync --locked --no-dev --no-editable

FROM python:3.12-slim@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1
# Пользователь без пароля, без домашнего каталога и без shell для входа.
RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin agent
WORKDIR /app
# Чекпоинты графа (SQLite) — единственное, что агент пишет. Каталог принадлежит агенту, именованный
# том в compose наследует эти права; корневая ФС остаётся только для чтения.
RUN mkdir /app/data && chown 10001:10001 /app/data
COPY --from=builder /app/.venv /app/.venv
COPY config.yaml ./
# Знания агента (ТЗ §7): SOUL, IDENTITY, скиллы, runbooks — часть образа, в рантайме только чтение.
COPY skills ./skills
COPY agent ./agent
ENV PATH=/app/.venv/bin:$PATH
USER 10001
ENTRYPOINT ["k8s-agent"]
