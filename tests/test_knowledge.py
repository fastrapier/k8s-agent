"""Знания агента в git (ТЗ §7, MEMORY §4): скиллы, SOUL/IDENTITY, runbooks.

Выбор скилла — код, а не модель, поэтому он проверяется на запросах сценариев из ТЗ §2 дословно:
если кто-то поправит триггеры, тест покажет, какой сценарий потерял свой скилл.
"""

from __future__ import annotations

import pytest

from k8s_incident_agent.knowledge import agent_file, load_skills, runbook_sections, select_skill
from k8s_incident_agent.tools import READONLY_TOOLS


@pytest.mark.parametrize(("query", "skill"), [
    ("Под api в namespace payments в CrashLoopBackOff", "crashloop-diagnosis"),
    ("Сервис отдаёт 502 после релиза", "ingress-502"),
    ("Deployment не раскатывается, зависли 2 из 5 подов", "pending-pod"),
    ("Почему job не завершается", "stuck-job"),
    ("Под checkout убит OOMKilled", "oom-analysis"),
    ("Что проверить после деплоя, нужен ли откат", "post-release-checklist"),
], ids=["S1", "S2", "S3", "S4", "oom", "post-release"])
def test_scenario_query_selects_skill(query, skill):
    chosen = select_skill(query)
    assert chosen is not None and chosen[0].name == skill, chosen


def test_out_of_scope_query_selects_nothing():
    assert select_skill("Напиши стихотворение про осень") is None


def test_skills_are_complete():
    skills = load_skills()
    assert {s.name for s in skills} >= {
        "crashloop-diagnosis", "oom-analysis", "pending-pod", "ingress-502", "stuck-job",
        "post-release-checklist"}
    for skill in skills:
        assert skill.description and skill.triggers and skill.body, skill.name
        # Скилл не должен советовать инструмент, которого у агента нет (например, k8s_patch).
        assert set(skill.tools) <= set(READONLY_TOOLS), skill.name


def test_soul_and_identity_present():
    assert "свидетельств" in agent_file("SOUL.md")
    assert agent_file("IDENTITY.md")
    assert agent_file("нет-такого.md") == ""


def test_runbook_toc_has_procedures():
    sections = runbook_sections()
    assert any(title.startswith("CrashLoopBackOff ›") for title in sections)
    assert all(text for text in sections.values())
