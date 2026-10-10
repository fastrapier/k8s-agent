"""Клиент шлюза: структурированный ответ и его два режима (ARCHITECTURE §8)."""

from __future__ import annotations

import json

import pytest

from k8s_incident_agent.llm import LLMClient, LLMError, Usage
from k8s_incident_agent.schemas import PatchProposal, Route


class Scripted(LLMClient):
    """Шлюз подменён: отдаёт ответы по очереди и запоминает запросы."""

    def __init__(self, replies):
        self.replies, self.requests = list(replies), []

    def raw(self, messages, tools=None, response_format=None, model=None):
        self.requests.append({"messages": list(messages), "response_format": response_format})
        return {"message": {"content": self.replies.pop(0)}, "latency": 0.1, "cost": 0.0}


def test_closed_schema_goes_to_gateway_as_json_schema():
    llm = Scripted(['{"route": "diagnose", "reason": "x"}'])
    assert llm.structured([], Route, Usage()).route == "diagnose"
    assert llm.requests[0]["response_format"]["type"] == "json_schema"


def test_free_form_sends_schema_as_text_and_validates_locally():
    """Патч — открытый объект: json_schema на нём зацикливал шлюз, поэтому json_object."""
    llm = Scripted(['{"patch": null, "recommendation": "ok"}'])
    assert llm.structured([], PatchProposal, Usage(), free_form=True).patch is None
    request = llm.requests[0]
    assert request["response_format"] == {"type": "json_object"}
    assert json.dumps(PatchProposal.model_json_schema(), ensure_ascii=False) \
        in request["messages"][-1]["content"]


def test_invalid_answer_is_retried_with_error_then_fails():
    llm = Scripted(['{"route": "nope"}'] * 3)
    with pytest.raises(LLMError):
        llm.structured([], Route, Usage())
    assert len(llm.requests) == 3
    assert "не прошёл валидацию" in llm.requests[1]["messages"][-1]["content"]
