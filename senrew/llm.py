"""Google Gemini, with function calling.

One call:

    POST https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent

Three details about this API drive the code below, all confirmed against the
live endpoint rather than assumed:

  1. A functionCall part carries an "id". The matching functionResponse has to
     echo it, or parallel calls get paired up wrongly.
  2. Parts carry a "thoughtSignature" on thinking models. The model's reply
     must go back into the conversation VERBATIM. Rebuilding it from the text
     silently breaks the model's reasoning chain, and the symptom is an agent
     that forgets what it was doing halfway through.
  3. Safety filters block discussion of SQL injection and hardcoded
     credentials - exactly what a code reviewer exists to find. Thresholds are
     set to BLOCK_NONE and a blocked reply is handled rather than crashed on.
"""

import json
import random
import time
from dataclasses import dataclass, field
from typing import Any

import requests

from senrew import config

API_BASE = "https://generativelanguage.googleapis.com/v1beta"
TIMEOUT = 120

# Worth retrying. Anything else is a bug in our request.
RETRYABLE = {429, 500, 502, 503, 504}

SAFETY_SETTINGS = [
    {"category": c, "threshold": "BLOCK_NONE"}
    for c in (
        "HARM_CATEGORY_HARASSMENT",
        "HARM_CATEGORY_HATE_SPEECH",
        "HARM_CATEGORY_SEXUALLY_EXPLICIT",
        "HARM_CATEGORY_DANGEROUS_CONTENT",
    )
]


class ModelBlocked(RuntimeError):
    """The model refused to answer. Retrying will not help."""


class OutOfQuota(RuntimeError):
    """The daily quota is gone. Waiting will not fix it today."""


# Set by the CLI so a long rate-limit wait says why it is waiting instead of
# looking like a hang.
on_wait = None


def _seconds(text: str) -> float:
    """Turn Google's '59s' retryDelay into a number."""
    try:
        return float(str(text).rstrip("s"))
    except ValueError:
        return 0.0


def _quota_failure(response) -> tuple[bool, float, str]:
    """Read a 429 properly. Returns (is_daily, retry_after, description).

    Free-tier keys get two very different 429s and the body is the only way
    to tell them apart:

      PerMinute  5 requests a minute. An agent spends one per step, so this
                 is the normal case, and waiting genuinely fixes it.
      PerDay     nothing more today. Waiting a minute achieves nothing.
    """
    try:
        error = response.json().get("error", {})
    except ValueError:
        return False, 0.0, "rate limited"

    delay, quota_id, limit = 0.0, "", ""
    for detail in error.get("details", []):
        kind = detail.get("@type", "")
        if kind.endswith("RetryInfo"):
            delay = _seconds(detail.get("retryDelay", ""))
        elif kind.endswith("QuotaFailure"):
            for violation in detail.get("violations", []):
                quota_id = violation.get("quotaId", "") or quota_id
                limit = violation.get("quotaValue", "") or limit

    daily = "PerDay" in quota_id
    label = f"{limit}/min free-tier limit" if limit and not daily else "quota"
    return daily, delay, label


@dataclass
class Usage:
    """Running token and cost total for one review."""

    input_tokens: int = 0
    output_tokens: int = 0
    calls: int = 0

    def add(self, payload: dict[str, Any]) -> None:
        meta = payload.get("usageMetadata") or {}
        self.input_tokens += meta.get("promptTokenCount", 0)
        self.output_tokens += meta.get("candidatesTokenCount", 0)
        self.calls += 1

    @property
    def cost_usd(self) -> float:
        return round(
            self.input_tokens / 1e6 * config.PRICE_INPUT_PER_MTOK
            + self.output_tokens / 1e6 * config.PRICE_OUTPUT_PER_MTOK,
            6,
        )


def _redact(text: str) -> str:
    """Strip the API key out of anything we log or raise.

    requests puts the full URL, query string included, into its exception
    messages. Without this a failed call writes the key into the terminal and
    into any error someone pastes into an issue.
    """
    key = config.GEMINI_API_KEY
    return text.replace(key, f"***{key[-4:]}") if key and len(key) > 8 else text


def text_of(content: dict[str, Any]) -> str:
    """The visible text of a reply, ignoring the model's private thinking."""
    parts = content.get("parts") or []
    return "".join(p.get("text", "") for p in parts if not p.get("thought")).strip()


def calls_of(content: dict[str, Any]) -> list[dict[str, Any]]:
    """The function calls in a reply, in order."""
    return [p["functionCall"] for p in (content.get("parts") or []) if "functionCall" in p]


def chat(
    contents: list[dict[str, Any]],
    system: str = "",
    tools: list[dict[str, Any]] | None = None,
    usage: Usage | None = None,
) -> dict[str, Any]:
    """One turn. Returns the model's `content` object UNCHANGED.

    Returning it unchanged is deliberate - see note 2 in the module docstring.
    Callers append it straight back onto `contents`.
    """
    if config.USE_FAKE_MODEL:
        from senrew import fake_llm

        return fake_llm.chat(contents, system, tools, usage)

    if not config.GEMINI_API_KEY:
        raise RuntimeError(
            "No GEMINI_API_KEY. Put one in .env, or set USE_FAKE_MODEL=true to "
            "run offline with canned output."
        )

    body: dict[str, Any] = {
        "contents": contents,
        "safetySettings": SAFETY_SETTINGS,
        "generationConfig": {
            "temperature": config.TEMPERATURE,
            "maxOutputTokens": config.MAX_OUTPUT_TOKENS,
        },
    }
    if system:
        body["systemInstruction"] = {"parts": [{"text": system}]}
    if tools:
        body["tools"] = [{"functionDeclarations": tools}]
        body["toolConfig"] = {"functionCallingConfig": {"mode": "AUTO"}}

    url = f"{API_BASE}/models/{config.GEMINI_MODEL}:generateContent"
    last_error = ""
    attempt = 0
    waits = 0  # rate-limit waits get their own budget, see below

    while True:
        try:
            response = requests.post(
                url, params={"key": config.GEMINI_API_KEY}, json=body, timeout=TIMEOUT
            )

            if response.status_code == 200:
                payload = response.json()
                if usage is not None:
                    usage.add(payload)
                return _content_of(payload)

            if response.status_code == 429:
                daily, delay, label = _quota_failure(response)
                if daily:
                    raise OutOfQuota(
                        "Gemini daily quota is gone. An agent makes one call "
                        "per step, so it uses quota far faster than a "
                        "single-prompt tool. Try again tomorrow, use another "
                        "key, or run offline with USE_FAKE_MODEL=true."
                    )

                # Per-minute limit. Waiting genuinely fixes this, and Google
                # tells us exactly how long, so honour that rather than
                # guessing with exponential backoff.
                if waits >= config.RATE_LIMIT_RETRIES:
                    raise OutOfQuota(
                        f"Still rate limited after {waits} waits ({label}). "
                        f"The free tier allows very few requests per minute "
                        f"and an agent needs one per step. Consider a paid "
                        f"key, a smaller MAX_STEPS, or USE_FAKE_MODEL=true."
                    )
                waits += 1
                pause = min(max(delay, 5.0), config.RATE_LIMIT_MAX_WAIT)
                if on_wait:
                    on_wait(f"rate limited ({label}), waiting {pause:.0f}s")
                time.sleep(pause)
                continue  # does not count as a failure, only as a wait

            if response.status_code not in RETRYABLE:
                detail = _redact(response.text[:300])
                if response.status_code == 404:
                    raise RuntimeError(
                        f"Model '{config.GEMINI_MODEL}' not found. It may have "
                        f"been retired - check https://aistudio.google.com"
                    )
                raise RuntimeError(f"Gemini {response.status_code}: {detail}")

            last_error = f"{response.status_code}: {_redact(response.text[:200])}"

        except requests.RequestException as exc:
            last_error = _redact(f"{type(exc).__name__}: {exc}")

        if attempt >= config.MAX_RETRIES:
            raise RuntimeError(f"Gemini failed after retries. Last error: {last_error}")

        # Full jitter. Without it, parallel retries collide and fail together.
        time.sleep(random.uniform(0, min(30.0, 2**attempt)))
        attempt += 1


def _content_of(payload: dict[str, Any]) -> dict[str, Any]:
    """Pull the reply out of a response, or explain why there is none."""
    blocked = (payload.get("promptFeedback") or {}).get("blockReason")
    if blocked:
        raise ModelBlocked(f"prompt blocked: {blocked}")

    candidates = payload.get("candidates") or []
    if not candidates:
        raise ModelBlocked("no candidates returned")

    content = candidates[0].get("content") or {}
    if not content.get("parts"):
        reason = candidates[0].get("finishReason", "unknown")
        raise ModelBlocked(f"empty reply, finishReason={reason}")

    return content


def tool_result(call: dict[str, Any], result: Any) -> dict[str, Any]:
    """Build the functionResponse part that answers one call.

    The call's "id" is echoed back. With parallel calls, dropping it makes
    Gemini pair answers with the wrong questions.
    """
    payload = {"name": call["name"], "response": {"result": result}}
    if call.get("id"):
        payload["id"] = call["id"]
    return {"functionResponse": payload}
