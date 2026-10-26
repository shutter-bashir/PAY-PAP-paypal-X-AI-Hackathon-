"""The OpenAI-compatible backend (Groq, Ollama) against a fake chat-completions server."""
import json

import httpx
import pytest

from app import ai
from app.config import settings
from tests.test_ai import ITEMS, FakeStore


class FakeServer:
    """Answers POST /chat/completions with the queued replies, recording each request."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.requests = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/chat/completions")
        self.requests.append(json.loads(request.content))
        status, body = self.replies.pop(0)
        return httpx.Response(status, json=body)


def content(text):
    return 200, {"choices": [{"message": {"role": "assistant", "content": text}, "finish_reason": "stop"}]}


def tool_calls(*calls):
    return 200, {"choices": [{"message": {"role": "assistant", "content": None, "tool_calls": [
        {"id": f"call_{i}", "type": "function", "function": {"name": name, "arguments": args}}
        for i, (name, args) in enumerate(calls)]}, "finish_reason": "tool_calls"}]}


def backend(server):
    return ai.OpenAICompatibleBackend("Groq", "https://api.groq.com/openai/v1", "gsk_test", "openai/gpt-oss-120b",
                                      temperature=0.2, transport=httpx.MockTransport(server))


SPEC = json.dumps({"recipient_vetting": ["approved"], "max_amount": 499.99, "explanation": "Approved, under $500."})


def test_structured_answer_uses_json_schema_and_low_temperature():
    server = FakeServer(content(SPEC))
    spec = ai.Assistant(backend(server)).translate_filter("approved contractors under $500")
    assert spec.recipient_vetting == ["approved"] and spec.max_amount == 499.99
    sent = server.requests[0]
    assert sent["model"] == "openai/gpt-oss-120b" and sent["temperature"] == 0.2
    assert sent["response_format"]["type"] == "json_schema"
    assert sent["response_format"]["json_schema"]["name"] == "FilterSpec"


def test_answer_in_code_fences_is_accepted():
    server = FakeServer(content(f"```json\n{SPEC}\n```"))
    assert ai.Assistant(backend(server)).translate_filter("x").max_amount == 499.99


def test_invalid_answer_is_sent_back_once_with_the_errors():
    server = FakeServer(content('{"max_amount": "lots"}'), content(SPEC))
    spec = ai.Assistant(backend(server)).translate_filter("x")
    assert spec.max_amount == 499.99
    retry = server.requests[1]["messages"]
    assert retry[-1]["role"] == "user" and "did not match the schema" in retry[-1]["content"]


def test_two_invalid_answers_make_ai_unavailable():
    server = FakeServer(content("not json"), content("still not json"))
    with pytest.raises(ai.AIUnavailable, match="did not match"):
        ai.Assistant(backend(server)).translate_filter("x")


def test_models_without_json_schema_fall_back_to_json_mode():
    server = FakeServer((400, {"error": {"message": "response_format json_schema is not supported by this model"}}),
                        content(SPEC), content(SPEC))
    b = backend(server)
    ai.Assistant(b).translate_filter("x")
    assert server.requests[1]["response_format"] == {"type": "json_object"}
    ai.Assistant(b).translate_filter("y")  # remembered for later calls
    assert server.requests[2]["response_format"] == {"type": "json_object"}


def test_rate_limit_is_retried_then_reported(monkeypatch):
    monkeypatch.setattr(ai.time, "sleep", lambda s: None)
    limited = (429, {"error": {"message": "Rate limit reached"}})
    with pytest.raises(ai.AIUnavailable, match="429"):
        ai.Assistant(backend(FakeServer(*[limited] * ai.MAX_REQUEST_ATTEMPTS))).translate_filter("x")
    assert ai.Assistant(backend(FakeServer(limited, content(SPEC)))).translate_filter("x").max_amount == 499.99


def test_bad_key_is_reported():
    server = FakeServer((401, {"error": {"message": "Invalid API Key", "code": "invalid_api_key"}}))
    with pytest.raises(ai.AIUnavailable, match="401: invalid_api_key Invalid API Key"):
        ai.Assistant(backend(server)).translate_filter("x")


def test_agent_runs_tools_in_openai_format():
    server = FakeServer(
        tool_calls(("validate_batch", "{}"),
                   ("build_batch", json.dumps({"include_rows": [2, 4, 5], "rationale": "vetted"}))),
        content("Pay rows 2 and 5: $570.00."),
    )
    store = FakeStore()
    reply = ai.Assistant(backend(server)).run_agent(ai.AgentTools(store, settings, batch_id="b1", user_id="u1"), "pay?")
    assert reply.text == "Pay rows 2 and 5: $570.00."
    assert reply.proposal["include_rows"] == [2, 5] and reply.proposal["blocked_rows_dropped"] == [4]
    first = server.requests[0]
    assert [t["function"]["name"] for t in first["tools"]] == [t["name"] for t in ai.TOOL_DEFINITIONS]
    assert first["tool_choice"] == "auto"
    followup = server.requests[1]["messages"]
    assert [m["role"] for m in followup] == ["system", "user", "assistant", "tool", "tool"]
    assert followup[3]["tool_call_id"] == "call_0"
    assert json.loads(followup[4]["content"])["row_count"] == 2


def test_agent_survives_malformed_arguments_and_cannot_submit_unapproved():
    server = FakeServer(tool_calls(("filter_batch", "{not json"), ("submit_batch", "{}")), content("Not approved yet."))
    reply = ai.Assistant(backend(server)).run_agent(
        ai.AgentTools(FakeStore(), settings, batch_id="b1", user_id="u1", paypal_factory=lambda: pytest.fail("no call")),
        "send it")
    tool_messages = [m for m in server.requests[1]["messages"] if m["role"] == "tool"]
    assert json.loads(tool_messages[0]["content"])["is_error"] is True
    assert "no valid approval" in json.loads(tool_messages[1]["content"])["error"]
    assert reply.text == "Not approved yet."
    assert len(ITEMS) == 4  # fixtures untouched


@pytest.mark.parametrize("message, header, expected", [
    ("Rate limit reached ... Please try again in 592.5ms. Need more tokens?", None, 1.0925),
    ("Please try again in 1m2.5s.", None, 30.0),  # capped
    ("Please try again in 4.2s.", None, 4.7),
    ("rate limited", "3", 3.5),
])
def test_retry_wait_follows_groq_hint(message, header, expected):
    response = httpx.Response(429, headers={"retry-after": header} if header else {})
    assert ai._retry_wait(response, message) == pytest.approx(expected)


def test_reasoning_effort_only_for_gpt_oss_models():
    server = FakeServer(content(SPEC), content(SPEC))
    ai.Assistant(backend(server)).translate_filter("x")
    other = ai.OpenAICompatibleBackend("Groq", "https://api.groq.com/openai/v1", "k", "qwen/qwen3.8-27b",
                                       transport=httpx.MockTransport(server))
    ai.Assistant(other).translate_filter("x")
    assert server.requests[0]["reasoning_effort"] == "low" and "reasoning_effort" not in server.requests[1]


def test_cut_off_answer_is_reported():
    server = FakeServer((200, {"choices": [{"message": {"content": '{"max_'}, "finish_reason": "length"}]}))
    with pytest.raises(ai.AIUnavailable, match="cut off"):
        ai.Assistant(backend(server)).translate_filter("x")


class FakeWhisper:
    def __init__(self, *replies):
        self.replies = list(replies)
        self.requests = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/audio/transcriptions")
        self.requests.append(request)
        status, body = self.replies.pop(0)
        return httpx.Response(status, json=body)


def test_transcribe_sends_audio_to_whisper(monkeypatch):
    monkeypatch.setattr(settings, "groq_api_key", type(settings.groq_api_key)("gsk_test"))
    server = FakeWhisper((200, {"text": " What are the biggest risks in this batch? "}))
    text = ai.transcribe(settings, b"x" * 2000, "audio/webm;codecs=opus", transport=httpx.MockTransport(server))
    assert text == "What are the biggest risks in this batch?"
    body = server.requests[0].content
    assert b'filename="question.webm"' in body and b"whisper-large-v3-turbo" in body and b"PayPal payout" in body
    assert server.requests[0].headers["Authorization"] == "Bearer gsk_test"


def test_transcribe_drops_what_whisper_hears_in_silence(monkeypatch):
    monkeypatch.setattr(settings, "groq_api_key", type(settings.groq_api_key)("gsk_test"))
    server = FakeWhisper((200, {"text": "Thank you."}))
    assert ai.transcribe(settings, b"x" * 2000, "audio/mp4", transport=httpx.MockTransport(server)) == ""
    assert b'filename="question.mp4"' in server.requests[0].content


def test_transcribe_needs_the_groq_key(monkeypatch):
    monkeypatch.setattr(settings, "groq_api_key", type(settings.groq_api_key)(""))
    with pytest.raises(ai.AIUnavailable, match="GROQ_API_KEY"):
        ai.transcribe(settings, b"x", "audio/webm")


def test_transcribe_reports_groq_errors(monkeypatch):
    monkeypatch.setattr(settings, "groq_api_key", type(settings.groq_api_key)("gsk_test"))
    server = FakeWhisper((400, {"error": {"message": "file is not valid audio"}}))
    with pytest.raises(ai.AIUnavailable, match="not valid audio"):
        ai.transcribe(settings, b"x" * 2000, "audio/webm", transport=httpx.MockTransport(server))
