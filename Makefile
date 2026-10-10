# k8s-incident-agent — курсовой проект.
# Каждая цель соответствует пункту приёмки из .scratch/roadmap.md.

SHELL := /bin/bash
export PATH := $(HOME)/.local/bin:$(PATH)

.DEFAULT_GOAL := help
.PHONY: help install lock test lint lint-fix config-show tool \
        stand-up stand-break stand-status stand-clean stand-down stand demo-secrets \
        demo-s1 demo-s1-container demo-s5 demo-s6 memory-list \
        isolation-test agent-build agent-tool eval-models alerts-check obs-up obs-down metrics check

help:  ## список целей
	@grep -hE '^[a-z0-9-]+:.*?##' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

# --- М0: каркас -------------------------------------------------------------

install:  ## поставить зависимости (нужен uv: ~/.local/bin/uv)
	uv sync

lock:  ## пересобрать uv.lock после правки pyproject.toml (N6)
	uv lock

test:  ## юнит-тесты, живой кластер не нужен (EVALS, уровень 2)
	uv run pytest -q

lint:  ## статические проверки
	uv run ruff check .

lint-fix:
	uv run ruff check --fix .

config-show:  ## приёмка М0: конфиг, токен должен быть ***masked***
	uv run k8s-agent config show

# --- М1: стенд и инструменты ------------------------------------------------

stand-up:  ## поднять kind-кластер и здоровую базу
	./stand/up.sh

stand-break:  ## внести поломку: SCENARIO=s1|s2|s3|s4|all (по умолчанию all)
	./stand/break.sh $(or $(SCENARIO),all)

stand-status:  ## что поднято и в каком состоянии
	./stand/status.sh

stand-clean:  ## снять поломку: SCENARIO=s1..s4 точечно, all — снести namespace
	./stand/clean.sh $(or $(SCENARIO),all)

stand-down:  ## снести кластер целиком
	./stand/down.sh

stand: stand-up stand-break stand-status  ## одна команда: поднять и сломать всё

tool:  ## вызов инструмента: make tool TOOL=k8s_list_pods ARGS="--ns payments"
	uv run k8s-agent tool $(TOOL) $(ARGS)

demo-secrets:  ## приёмка М1/М2: api в CrashLoopBackOff, Secret защищён двумя рубежами
	@echo "--- поды в payments под учёткой диагноста: ожидается api в CrashLoopBackOff ---"
	uv run k8s-agent tool k8s_list_pods --ns payments
	@echo
	@echo "--- рубеж 1, RBAC: учётке диагноста читать Secret нельзя, ожидается отказ ---"
	-uv run k8s-agent tool k8s_get_manifest --kind Secret --name payments-db --ns payments
	@echo
	@echo "--- рубеж 2, маскирование: тот же вызов под админом, как если бы RBAC настроили шире ---"
	uv run k8s-agent tool k8s_get_manifest --kind Secret --name payments-db --ns payments \
		--kubeconfig $(HOME)/.kube/config

# --- М4: агент ---------------------------------------------------------------

S1_QUERY = Под api в namespace payments в CrashLoopBackOff, разберись

demo-s1:  ## приёмка М4: диагноз S1 → патч → подтверждение человеком → применение → проверка
	uv run k8s-agent run "$(S1_QUERY)"

demo-s1-container:  ## то же, но агент в изолированном контейнере (без root, сеть только через прокси)
	@test -f .kube/agent-rw.container.kubeconfig || { echo "нет kubeconfig контейнера: make stand-up"; exit 1; }
	docker compose run --rm agent run "$(S1_QUERY)"

demo-s6:  ## приёмка М4: запрос вне области — вежливый отказ без единого вызова инструментов
	uv run k8s-agent run "Напиши стихотворение про осень"

# --- М6: память --------------------------------------------------------------

demo-s5:  ## приёмка М6: прошлый инцидент из памяти найден и упомянут в отчёте (S1 должен быть сломан)
	uv run k8s-agent memory seed evals/fixtures/memory/s5-past-incidents.yaml
	uv run k8s-agent run "$(S1_QUERY)"

memory-list:  ## записи эпизодической памяти data/memory.sqlite
	uv run k8s-agent memory list

# --- М2: изоляция -----------------------------------------------------------

isolation-test:  ## приёмка М2: проверки изоляции из docs/ISOLATION.md §10
	./stand/isolation-test.sh

# --- М3: сравнение моделей -------------------------------------------------

eval-models:  ## М3: сравнение моделей; MODELS=a,b RUNS=3 (по умолчанию 4 кандидата × 3 прогона)
	uv run python evals/model_selection.py $(if $(MODELS),--models $(MODELS)) $(if $(RUNS),--runs $(RUNS))


agent-build:  ## собрать образ агента (Dockerfile)
	docker compose build agent

agent-tool:  ## инструмент из контейнера агента: make agent-tool TOOL=k8s_list_pods ARGS="--ns payments"
	@test -f .kube/agent-ro.container.kubeconfig || { echo "нет kubeconfig контейнера: make stand-up"; exit 1; }
	docker compose run --rm agent tool $(TOOL) $(ARGS)

# --- М7: наблюдаемость ------------------------------------------------------

alerts-check:  ## проверить правила, юнит-тесты promtool и конфигурацию Alertmanager
	docker run --rm -v "$(CURDIR)/observability:/etc/prometheus:ro" -w /etc/prometheus \
		--entrypoint promtool prom/prometheus:v3.7.3 check rules alerts.yml
	docker run --rm -v "$(CURDIR)/observability:/etc/prometheus:ro" -w /etc/prometheus \
		--entrypoint promtool prom/prometheus:v3.7.3 test rules alerts.test.yml
	docker run --rm -v "$(CURDIR)/observability:/etc/alertmanager:ro" \
		--entrypoint amtool prom/alertmanager:v0.29.0 check-config /etc/alertmanager/alertmanager.yml

obs-up:  ## поднять Prometheus, Alertmanager и Grafana (http://127.0.0.1:3000)
	docker compose -f observability/docker-compose.yml up -d

obs-down:  ## остановить стек наблюдаемости
	docker compose -f observability/docker-compose.yml down

metrics:  ## /metrics агента на :9108 — метрики из трейсов data/traces
	uv run k8s-agent metrics serve --port 9108

check: lint test  ## всё, что гоняется без кластера
