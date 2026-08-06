"""Tests for the Gemini layer: rate limits, refusals, and key redaction.

The 429 handling matters more than it looks. Free keys allow only a handful of
requests per MINUTE and an agent spends one per step, so a normal review hits
the limit repeatedly. Treating that as fatal would make the tool unusable on
the tier most people will run it on.
"""

from types import SimpleNamespace

import pytest
import requests

from senrew import config, llm


def response(status: int, payload=None, text: str = ""):
    return SimpleNamespace(
        status_code=status,
        json=lambda: (payload if payload is not None else {}),
        text=text or "",
    )


def rate_limited(quota_id: str, delay: str = "59s", limit: str = "5"):
    """A 429 shaped exactly like the real one."""
    return response(429, {"error": {
        "code": 429,
        "message": "You exceeded your current quota",
        "details": [
            {"@type": "type.googleapis.com/google.rpc.QuotaFailure",
             "violations": [{"quotaId": quota_id, "quotaValue": limit}]},
            {"@type": "type.googleapis.com/google.rpc.RetryInfo",
             "retryDelay": delay},
        ],
    }}, text="quota")


def ok(text: str = "hello"):
    return response(200, {
        "candidates": [{"content": {"role": "model", "parts": [{"text": text}]},
                        "finishReason": "STOP"}],
        "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 3},
    })


@pytest.fixture(autouse=True)
def no_waiting(monkeypatch):
    monkeypatch.setattr(config, "GEMINI_API_KEY", "test-key-1234567890")
    monkeypatch.setattr(config, "USE_FAKE_MODEL", False)
    monkeypatch.setattr(llm.time, "sleep", lambda _s: None)
    monkeypatch.setattr(llm, "on_wait", None)


def send(monkeypatch, responses):
    queue = list(responses)
    calls = []

    def post(*a, **k):
        calls.append(1)
        return queue.pop(0) if queue else ok()

    monkeypatch.setattr(llm.requests, "post", post)
    return calls


# --- rate limits -----------------------------------------------------------


def test_a_per_minute_limit_waits_and_succeeds(monkeypatch):
    """The common free-tier case. Waiting really does fix it."""
    calls = send(monkeypatch, [
        rate_limited("GenerateRequestsPerMinutePerProjectPerModel-FreeTier"),
        ok("recovered"),
    ])

    content = llm.chat([{"role": "user", "parts": [{"text": "hi"}]}])

    assert llm.text_of(content) == "recovered"
    assert len(calls) == 2


def test_the_wait_honours_the_delay_google_sent(monkeypatch):
    """Guessing with exponential backoff either wastes time or retries too soon."""
    slept = []
    monkeypatch.setattr(llm.time, "sleep", lambda s: slept.append(s))
    send(monkeypatch, [
        rate_limited("GenerateRequestsPerMinutePerProjectPerModel-FreeTier", delay="42s"),
        ok(),
    ])

    llm.chat([{"role": "user", "parts": [{"text": "hi"}]}])

    assert slept == [42.0]


def test_the_wait_is_capped(monkeypatch):
    slept = []
    monkeypatch.setattr(llm.time, "sleep", lambda s: slept.append(s))
    monkeypatch.setattr(config, "RATE_LIMIT_MAX_WAIT", 30)
    send(monkeypatch, [
        rate_limited("GenerateRequestsPerMinutePerProjectPerModel-FreeTier", delay="600s"),
        ok(),
    ])

    llm.chat([{"role": "user", "parts": [{"text": "hi"}]}])

    assert slept == [30]


def test_a_daily_limit_gives_up_immediately(monkeypatch):
    """Waiting a minute for a daily quota is pure delay."""
    calls = send(monkeypatch, [rate_limited("GenerateRequestsPerDayPerProject-FreeTier")])

    with pytest.raises(llm.OutOfQuota, match="daily quota"):
        llm.chat([{"role": "user", "parts": [{"text": "hi"}]}])

    assert len(calls) == 1, "a daily limit must not be retried"


def test_endless_rate_limiting_eventually_stops(monkeypatch):
    monkeypatch.setattr(config, "RATE_LIMIT_RETRIES", 3)
    calls = send(monkeypatch, [
        rate_limited("GenerateRequestsPerMinutePerProjectPerModel-FreeTier")
        for _ in range(20)
    ])

    with pytest.raises(llm.OutOfQuota, match="Still rate limited"):
        llm.chat([{"role": "user", "parts": [{"text": "hi"}]}])

    assert len(calls) == 4  # the first try plus three waits


def test_the_wait_is_announced(monkeypatch):
    """A silent 60-second pause looks like a hang."""
    said = []
    monkeypatch.setattr(llm, "on_wait", said.append)
    send(monkeypatch, [
        rate_limited("GenerateRequestsPerMinutePerProjectPerModel-FreeTier"), ok(),
    ])

    llm.chat([{"role": "user", "parts": [{"text": "hi"}]}])

    assert "rate limited" in said[0]
    assert "5/min" in said[0]


# --- other failures --------------------------------------------------------


def test_a_400_is_not_retried(monkeypatch):
    calls = send(monkeypatch, [response(400, {}, "bad request")])

    with pytest.raises(RuntimeError, match="Gemini 400"):
        llm.chat([{"role": "user", "parts": [{"text": "hi"}]}])

    assert len(calls) == 1


def test_a_404_points_at_the_model_name(monkeypatch):
    send(monkeypatch, [response(404, {}, "not found")])

    with pytest.raises(RuntimeError, match="may have been retired"):
        llm.chat([{"role": "user", "parts": [{"text": "hi"}]}])


def test_a_500_is_retried_then_reported(monkeypatch):
    monkeypatch.setattr(config, "MAX_RETRIES", 2)
    calls = send(monkeypatch, [response(500, {}, "boom") for _ in range(10)])

    with pytest.raises(RuntimeError, match="failed after retries"):
        llm.chat([{"role": "user", "parts": [{"text": "hi"}]}])

    assert len(calls) == 3


def test_a_network_error_is_retried(monkeypatch):
    monkeypatch.setattr(config, "MAX_RETRIES", 1)

    def post(*a, **k):
        raise requests.ConnectionError("reset")

    monkeypatch.setattr(llm.requests, "post", post)

    with pytest.raises(RuntimeError, match="failed after retries"):
        llm.chat([{"role": "user", "parts": [{"text": "hi"}]}])


def test_the_api_key_is_never_echoed_in_an_error(monkeypatch):
    """requests puts the full URL, key included, in its exception messages."""
    key = "AIzaSy-super-secret-value"
    monkeypatch.setattr(config, "GEMINI_API_KEY", key)
    send(monkeypatch, [response(400, {}, f"bad request key={key}")])

    with pytest.raises(RuntimeError) as exc:
        llm.chat([{"role": "user", "parts": [{"text": "hi"}]}])

    assert key not in str(exc.value)
    assert "***" in str(exc.value)


# --- refusals --------------------------------------------------------------


def test_a_blocked_prompt_raises_model_blocked(monkeypatch):
    send(monkeypatch, [response(200, {"promptFeedback": {"blockReason": "SAFETY"}})])

    with pytest.raises(llm.ModelBlocked, match="SAFETY"):
        llm.chat([{"role": "user", "parts": [{"text": "hi"}]}])


def test_no_candidates_raises_model_blocked(monkeypatch):
    send(monkeypatch, [response(200, {"candidates": []})])

    with pytest.raises(llm.ModelBlocked, match="no candidates"):
        llm.chat([{"role": "user", "parts": [{"text": "hi"}]}])


def test_an_empty_reply_names_the_finish_reason(monkeypatch):
    send(monkeypatch, [response(200, {"candidates": [
        {"content": {"parts": []}, "finishReason": "MAX_TOKENS"}]})])

    with pytest.raises(llm.ModelBlocked, match="MAX_TOKENS"):
        llm.chat([{"role": "user", "parts": [{"text": "hi"}]}])


# --- reading replies -------------------------------------------------------


def test_private_thinking_is_not_treated_as_the_answer():
    content = {"parts": [{"text": "reasoning...", "thought": True},
                         {"text": "the answer"}]}
    assert llm.text_of(content) == "the answer"


def test_usage_accumulates_and_costs(monkeypatch):
    usage = llm.Usage()
    send(monkeypatch, [ok(), ok()])

    for _ in range(2):
        llm.chat([{"role": "user", "parts": [{"text": "hi"}]}], usage=usage)

    assert usage.calls == 2
    assert usage.input_tokens == 20
    assert usage.cost_usd > 0
