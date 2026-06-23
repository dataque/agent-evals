"""Offline tests for the langchain_azure judge.

A fake chat client (one with an ``.invoke(messages)`` method) is injected, so
these run without langchain installed and without any network — exercising the
same code path the real ``AzureChatOpenAI`` would, minus the SDK.
"""

from __future__ import annotations

from agent_evals.judges.langchain_azure import LangchainAzureJudge
from agent_evals.judges.select import build_judge


class _FakeMessage:
    def __init__(self, content: str) -> None:
        self.content = content


class _FakeChat:
    """Stands in for AzureChatOpenAI: records messages, returns canned content."""

    def __init__(self, content: str) -> None:
        self._content = content
        self.seen: list = []

    def invoke(self, messages):
        self.seen.append(messages)
        return _FakeMessage(self._content)


def test_parses_judge_json_and_passes_through_role_dicts():
    client = _FakeChat('{"score": 0.875, "pass": true, "rationale": "grounded"}')
    judge = LangchainAzureJudge(client=client)

    v = judge.evaluate(criteria="is it grounded?", response="yes", question="q", context="ctx")

    assert v.score == 0.875 and v.passed is True and v.rationale == "grounded"
    # langchain coercion contract: a list of {role, content} dicts, system then user
    (msgs,) = client.seen
    assert [m["role"] for m in msgs] == ["system", "user"]
    assert "RESPONSE" in msgs[1]["content"]


def test_judge_backend_error_becomes_error_verdict_not_a_real_zero():
    class _Boom:
        def invoke(self, messages):
            raise RuntimeError("403 Public access is disabled")

    v = LangchainAzureJudge(client=_Boom()).evaluate(criteria="c", response="r")

    assert v.score == 0.0 and v.passed is None
    assert v.raw.get("error") and "403" in v.raw["error"]


def test_build_judge_resolves_langchain_azure_and_aliases():
    for name in ("langchain_azure", "langchain", "azure_langchain"):
        assert isinstance(build_judge(name), LangchainAzureJudge)
