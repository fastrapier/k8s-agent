"""Семантическая и процедурная память агента: SOUL, IDENTITY, скиллы, runbooks (ТЗ §7–8, MEMORY §4).

Markdown-файлы в git, без векторов. SOUL и IDENTITY подставляются в системные промпты всегда.
Скилл выбирается кодом по `triggers` из фронтматтера — детерминированно и объяснимо: в отчёте видно,
какой скилл и по какому слову подключён. Runbook читается по оглавлению (подход PageIndex): модель
выбирает раздел по заголовкам, в промпт попадает только он.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import cache
from pathlib import Path

import yaml

from k8s_incident_agent.config import find_repo_root

_FRONTMATTER = re.compile(r"\A---\n(.*?)\n---\n(.*)\Z", re.S)


@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    triggers: tuple[str, ...]
    tools: tuple[str, ...]
    body: str

    def matched(self, query: str) -> list[str]:
        text = query.lower()
        return [t for t in self.triggers if t in text]


def _root() -> Path:
    return find_repo_root()


@cache
def load_skills(root: Path | None = None) -> tuple[Skill, ...]:
    skills = []
    for path in sorted((root or _root()).glob("skills/*/SKILL.md")):
        match = _FRONTMATTER.match(path.read_text(encoding="utf-8"))
        if not match:
            raise ValueError(f"{path}: нет фронтматтера --- ... ---")
        meta = yaml.safe_load(match.group(1)) or {}
        skills.append(Skill(
            name=meta["name"], description=meta["description"],
            triggers=tuple(str(t).lower() for t in meta.get("triggers") or ()),
            tools=tuple(meta.get("tools") or ()), body=match.group(2).strip()))
    return tuple(skills)


def select_skill(query: str, skills: tuple[Skill, ...] | None = None,
                 ) -> tuple[Skill, list[str]] | None:
    """Скилл с наибольшим числом совпавших триггеров; при равенстве — с более длинными совпадениями
    (они конкретнее: «после релиза» против «релиз»). Ничего не совпало — скилла нет."""
    best = None
    for skill in skills if skills is not None else load_skills():
        hits = skill.matched(query)
        if hits:
            key = (len(hits), sum(map(len, hits)))
            if best is None or key > best[0]:
                best = (key, skill, hits)
    return (best[1], best[2]) if best else None


@cache
def agent_file(name: str, root: Path | None = None) -> str:
    """agent/SOUL.md, agent/IDENTITY.md и т.п.; нет файла — пустая строка (тесты, CI без знаний)."""
    path = (root or _root()) / "agent" / name
    return path.read_text(encoding="utf-8").strip() if path.is_file() else ""


@cache
def runbook_sections(root: Path | None = None) -> dict[str, str]:
    """Оглавление runbooks: «файл › заголовок раздела» -> текст раздела."""
    sections: dict[str, str] = {}
    for path in sorted((root or _root()).glob("agent/RUNBOOKS/*.md")):
        title, current, lines = path.stem, None, []
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.startswith("# "):
                title = line[2:].strip()
            elif line.startswith("## "):
                if current:
                    sections[current] = "\n".join(lines).strip()
                current, lines = f"{title} › {line[3:].strip()}", []
            elif current:
                lines.append(line)
        if current:
            sections[current] = "\n".join(lines).strip()
    return sections
