"""CLI `k8s-agent`.

* `k8s-agent run "<описание проблемы>"` — расследование инцидента графом агента (М4);
* `k8s-agent config show` — печать конфига с замаскированным токеном;
* `k8s-agent tool <имя> [аргументы]` — прямой вызов одного read-only инструмента, без графа и LLM.
"""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path

import typer
from pydantic import ValidationError
from rich.console import Console
from rich.markdown import Markdown
from rich.markup import escape
from rich.syntax import Syntax
from rich.table import Table

from k8s_incident_agent.config import find_repo_root, get_config, redacted_dict
from k8s_incident_agent.evidence import default_store
from k8s_incident_agent.tools import READONLY_TOOLS

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Мультиагентная диагностика инцидентов в Kubernetes.",
)
config_app = typer.Typer(no_args_is_help=True, help="Конфигурация.")
app.add_typer(config_app, name="config")

console = Console()


@config_app.command("show")
def config_show() -> None:
    """Показать текущий конфиг. Токен LLM заменён меткой и в вывод не попадает."""
    config = get_config()
    data = redacted_dict(config)
    table = Table("Параметр", "Значение", title="config.yaml + local.env")
    for section, values in data.items():
        for key, value in values.items():
            table.add_row(f"{section}.{key}", "не задано" if value is None else str(value))
    console.print(table)


@app.command("tool")
def run_tool(
    tool_name: str = typer.Argument(..., help="имя инструмента, например k8s_list_pods"),
    namespace: str | None = typer.Option(None, "--ns", "--namespace", help="namespace"),
    selector: str | None = typer.Option(None, "--selector", help="label selector"),
    kind: str | None = typer.Option(None, "--kind", help="вид объекта"),
    name: str | None = typer.Option(None, "--name", help="имя объекта"),
    object_: str | None = typer.Option(None, "--object", help="имя объекта-источника событий"),
    pod: str | None = typer.Option(None, "--pod", help="имя пода"),
    container: str | None = typer.Option(None, "--container", help="имя контейнера"),
    tail: int | None = typer.Option(None, "--tail", help="сколько последних строк лога"),
    previous: bool | None = typer.Option(None, "--previous", help="лог предыдущего запуска"),
    deployment: str | None = typer.Option(None, "--deployment", help="имя Deployment"),
    raw: bool = typer.Option(False, "--raw", help="показать сырой вывод свидетельства"),
    kubeconfig: str | None = typer.Option(
        None,
        "--kubeconfig",
        help="подменить учётку диагноста: показать второй рубеж защиты (маскирование), "
        "как если бы RBAC настроили шире; относительный путь — от текущего каталога",
    ),
) -> None:
    """Вызвать один read-only инструмент и напечатать структурированный результат."""
    spec = READONLY_TOOLS.get(tool_name)
    if spec is None:
        console.print(f"[red]неизвестный инструмент:[/red] {tool_name}")
        console.print("доступны: " + ", ".join(sorted(READONLY_TOOLS)))
        raise typer.Exit(2)

    supplied = {
        "namespace": namespace,
        "selector": selector,
        "kind": kind,
        "name": name,
        "object": object_,
        "pod": pod,
        "container": container,
        "tail": tail,
        "previous": previous,
        "deployment": deployment,
    }
    known = set(spec.args_model.model_fields)
    args = {key: value for key, value in supplied.items() if value is not None and key in known}

    try:
        validated = spec.args_model(**args)
    except ValidationError as exc:
        console.print(f"[red]неверные аргументы для {tool_name}:[/red]")
        for error in exc.errors():
            console.print(f"  {'.'.join(str(p) for p in error['loc'])}: {error['msg']}")
        raise typer.Exit(2) from exc

    call_args = validated.model_dump()
    if kubeconfig:
        # Только для ручного вызова: граф агента этой опции не видит.
        path = str(Path(kubeconfig).expanduser())
        config = get_config()
        k8s = config.k8s.model_copy(update={"readonly_kubeconfig": path})
        call_args["config"] = config.model_copy(update={"k8s": k8s})
        console.print(f"ВНИМАНИЕ: вызов не под учёткой диагноста, а под {path}",
                      style="bold yellow", markup=False)
    result = spec.func(**call_args)

    colour = {"ok": "green", "degraded": "yellow", "error": "red"}[result.status]
    # Текст из кластера (логи, события) — данные, а не разметка rich: «[/api]» в логе
    # иначе уронил бы CLI с MarkupError, а «[api]» молча исчез бы из вывода. Автоподсветку
    # тоже не включаем: она вставляет коды цвета внутрь строк лога.
    console.print(f"[{colour}]{result.status}[/{colour}] {result.evidence_id}: "
                  f"{escape(result.summary)}", highlight=False)
    if result.error and result.error not in result.summary:
        console.print(f"[red]{escape(result.error)}[/red]", highlight=False)
    if result.data is not None:
        payload = json.dumps(result.data, ensure_ascii=False, indent=2)
        console.print(Syntax(payload, "json", theme="ansi_dark", word_wrap=True))
    if result.truncated:
        console.print("[yellow]вывод обрезан до лимита config.tools.max_output_chars[/yellow]")
    if raw:
        evidence = default_store.get(result.evidence_id)
        if evidence and evidence.raw_ref:
            console.print(default_store.raw(evidence.raw_ref) or "", markup=False, highlight=False)

    if result.status == "error":
        raise typer.Exit(1)


def _progress(node: str, change: dict) -> str:
    """Одна строка о шаге графа: что узел сделал."""
    change = change or {}
    if node == "classify":
        return f"маршрут: {change.get('route')}"
    if node == "plan":
        return "план: " + "; ".join(step["goal"] for step in change.get("plan", []))
    if node == "diagnose":
        new = ", ".join(e["id"] for e in change.get("evidence", [])) or "новых нет"
        return f"свидетельства: {new} (вызовов инструментов всего: {change.get('tool_calls')})"
    if node == "analyze":
        return f"гипотез: {len(change.get('hypotheses', []))}"
    if node == "review":
        return f"ревью: {change.get('review_verdict')} (итерация {change.get('review_iterations')})"
    if node == "propose":
        patch = change.get("proposed_patch")
        if not patch:
            return "патча нет, только рекомендация"
        return f"патч: {patch['kind']}/{patch['name']}"
    if node == "execute":
        result = change.get("execution_result")
        return f"исполнение: {result['status']}" if result else "исполнение: не выполнялось"
    return ""


@app.command("run")
def run_incident(
    query: str = typer.Argument(..., help="описание проблемы, как его написал бы инженер"),
    thread: str | None = typer.Option(None, "--thread", help="id инцидента для продолжения"),
    model: str | None = typer.Option(None, "--model", help="модель вместо llm.model из конфига"),
) -> None:
    """Расследовать инцидент. Патч применяется только после вашего подтверждения."""
    from langgraph.checkpoint.sqlite import SqliteSaver
    from langgraph.types import Command

    from k8s_incident_agent.graph import Agent, build_graph
    from k8s_incident_agent.llm import LLMClient

    config = get_config()
    thread_id = thread or uuid.uuid4().hex[:8]
    checkpoints = find_repo_root() / "data" / "checkpoints.sqlite"
    checkpoints.parent.mkdir(exist_ok=True)
    llm = LLMClient(config, model=model)
    agent = Agent(llm, config)
    console.print(f"[dim]инцидент {thread_id}, модель {llm.model}[/dim]")

    with SqliteSaver.from_conn_string(str(checkpoints)) as saver:
        graph = build_graph(agent).compile(checkpointer=saver)
        run_config = {"configurable": {"thread_id": thread_id}}
        payload: object = {"query": query, "started": time.time(), "evidence": []}
        while True:
            pending = None
            for update in graph.stream(payload, run_config, stream_mode="updates"):
                for node, change in update.items():
                    if node == "__interrupt__":
                        pending = change[0].value
                    elif line := _progress(node, change):
                        console.print(f"[cyan]{node:>9}[/cyan] {escape(line)}")
            if pending is None:
                break
            # F6: человек видит объект, патч, обоснование — и решает сам.
            patch = pending["patch"]
            console.print(f"\n[bold yellow]Предлагается патч {patch['kind']}/{patch['name']} "
                          f"в {patch['namespace']}[/bold yellow]")
            console.print(Syntax(json.dumps(patch["patch"], ensure_ascii=False, indent=2), "json"))
            console.print(escape(pending["recommendation"]), markup=False)
            approved = typer.confirm("Применить патч?", default=False)
            payload = Command(resume="approved" if approved else "rejected")
        report = graph.get_state(run_config).values.get("report", "")
    console.print()
    console.print(Markdown(report))


if __name__ == "__main__":  # pragma: no cover
    app()
