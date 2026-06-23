"""The LLM judge adapts its request shape to the model (GPT-5 / o-series),
which renamed ``max_tokens`` and restrict ``temperature`` / ``response_format``.
Uses a fake client so no network or ``openai`` install is needed."""

from __future__ import annotations

from agent_evals.judges.base_openai import OpenAIJudge


class _Msg:
    def __init__(self, content: str) -> None:
        self.content = content


class _Choice:
    def __init__(self, content: str) -> None:
        self.message = _Msg(content)


class _Resp:
    def __init__(self, content: str) -> None:
        self.choices = [_Choice(content)]


class _Completions:
    """Fake chat.completions that 400s whenever a 'forbidden' kwarg is present."""

    def __init__(self, fail_on: set[str]) -> None:
        self.fail_on = set(fail_on)
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        for p in self.fail_on:
            if p in kwargs:
                raise RuntimeError(
                    f"Error code: 400 - Unsupported parameter: '{p}' is not supported with this model"
                )
        return _Resp('{"score": 1.0, "pass": true, "rationale": "ok"}')


class _Client:
    def __init__(self, fail_on: set[str]) -> None:
        self.chat = type("Chat", (), {"completions": _Completions(fail_on)})()


def _judge(fail_on: set[str]) -> OpenAIJudge:
    return OpenAIJudge(model="gpt-5.2", client=_Client(fail_on))


def _last_call(j: OpenAIJudge) -> dict:
    return j._client.chat.completions.calls[-1]


def test_renames_max_tokens_to_max_completion_tokens():
    j = _judge({"max_tokens"})
    v = j.evaluate(criteria="c", response="r")
    assert v.score == 1.0
    assert j._token_param == "max_completion_tokens"
    assert _last_call(j).get("max_completion_tokens") == 400
    assert "max_tokens" not in _last_call(j)


def test_drops_temperature_and_renames_token_param():
    j = _judge({"max_tokens", "temperature"})
    v = j.evaluate(criteria="c", response="r")
    assert v.score == 1.0
    assert j._token_param == "max_completion_tokens" and j._send_temperature is False
    assert "temperature" not in _last_call(j)


def test_drops_response_format():
    j = _judge({"response_format"})
    v = j.evaluate(criteria="c", response="r")
    assert v.score == 1.0
    assert j._send_response_format is False
    assert "response_format" not in _last_call(j)


def test_non_parameter_error_surfaces_not_loops():
    class _AuthErr:
        def create(self, **kwargs):
            raise RuntimeError("Error code: 401 - invalid api key")

    client = type("Cl", (), {"chat": type("C", (), {"completions": _AuthErr()})()})()
    j = OpenAIJudge(model="x", client=client)
    v = j.evaluate(criteria="c", response="r")
    assert v.raw and v.raw.get("error")  # surfaced as an error verdict, not retried forever
