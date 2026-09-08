from concurrent.futures import ThreadPoolExecutor
from threading import Event
from types import SimpleNamespace

import httpx
import openai
import pytest

from agent_rca_bench import gemini_rate_limit as pacing
from agent_rca_bench.gemini_rate_limit import GeminiRateLimiter


def request(text="hello"):
    return httpx.Request(
        "POST",
        "https://example.test/chat/completions",
        json={
            "messages": [{"role": "user", "content": text}],
            "tools": [{"type": "function", "function": {"name": "query"}}],
        },
    )


def test_two_workers_share_budget_and_wait_outside_transport(monkeypatch):
    now = [0.0]
    monkeypatch.setattr(pacing, "time", SimpleNamespace(monotonic=lambda: now[0]))
    limiter = GeminiRateLimiter(1800)
    first, second = request(), request()
    limiter.before_request(first)
    waiting, sent = Event(), Event()
    original_wait = limiter.condition.wait

    def wait(timeout):
        waiting.set()
        return original_wait(timeout)

    monkeypatch.setattr(limiter.condition, "wait", wait)

    def send():
        limiter.before_request(second)
        sent.set()

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(send)
        try:
            assert waiting.wait(2)
            assert not sent.is_set()
        finally:
            with limiter.condition:
                now[0] = 60.0
                limiter.condition.notify_all()
        future.result(timeout=2)
    assert sent.is_set()
    assert len(limiter.reservations) == 1
    assert limiter.reservations[0] is second.extensions["gemini_token_reservation"]


def test_actual_usage_corrects_reservation_and_estimation(monkeypatch):
    now = [0.0]
    monkeypatch.setattr(pacing, "time", SimpleNamespace(monotonic=lambda: now[0]))
    limiter = GeminiRateLimiter(10_000)
    req = request()
    limiter.before_request(req)
    now[0] = 80.0
    limiter.before_request(request())
    response = httpx.Response(200, request=req, json={"usage": {"prompt_tokens": 3000}})
    limiter.after_response(response)
    assert sum(item.tokens for item in limiter.reservations) > 3000
    assert req.extensions["gemini_token_reservation"].timestamp == 80.0
    later = request()
    limiter.before_request(later)
    assert later.extensions["gemini_token_reservation"].tokens >= 3000


def test_sdk_retry_is_paced_and_preserves_request_and_response():
    limiter = GeminiRateLimiter(10_000)
    bodies = []

    def respond(req):
        bodies.append(req.content)
        if len(bodies) == 1:
            return httpx.Response(429, headers={"retry-after-ms": "1"}, json={"error": "busy"})
        return httpx.Response(
            200,
            json={
                "id": "test",
                "created": 1,
                "model": "gemini-3.8-flash",
                "object": "chat.completion",
                "choices": [],
                "usage": {"prompt_tokens": 20, "completion_tokens": 1, "total_tokens": 21},
            },
        )

    with openai.OpenAI(
        api_key="test",
        base_url="https://example.test",
        max_retries=1,
        http_client=httpx.Client(
            transport=httpx.MockTransport(respond),
            event_hooks={
                "request": [limiter.before_request],
                "response": [limiter.after_response],
            },
        ),
    ) as client:
        response = client.chat.completions.create(
            model="gemini-3.8-flash", messages=[{"role": "user", "content": "hello"}]
        )
    assert len(bodies) == 2 and bodies[0] == bodies[1]
    assert response.usage.prompt_tokens == 20
    assert len(limiter.reservations) == 2
    assert limiter.reservations[0].tokens > 20
    assert limiter.reservations[1].tokens == 20


def test_sdk_does_not_retry_local_budget_rejection():
    limiter = GeminiRateLimiter(1800)
    attempts = []
    sends = []

    def before(req):
        attempts.append(req)
        limiter.before_request(req)

    with (
        openai.OpenAI(
            api_key="test",
            base_url="https://example.test",
            max_retries=10,
            http_client=httpx.Client(
                transport=httpx.MockTransport(lambda req: sends.append(req)),
                event_hooks={"request": [before]},
            ),
        ) as client,
        pytest.raises(openai.OpenAIError, match="entire TPM budget"),
    ):
        client.chat.completions.create(
            model="gemini-3.8-flash", messages=[{"role": "user", "content": "x" * 5000}]
        )
    assert limiter.reservations == []
    assert len(attempts) == 1
    assert sends == []


@pytest.mark.parametrize("body", [b"invalid JSON", b'{"usage":null}', b"[]"])
def test_malformed_usage_does_not_trigger_a_retry_from_the_hook(body):
    limiter = GeminiRateLimiter()
    req = request()
    limiter.before_request(req)
    estimate = limiter.reservations[0].tokens
    response = httpx.Response(200, request=req, content=body)
    limiter.after_response(response)
    assert response.content == body
    assert limiter.reservations[0].tokens == estimate


def test_response_without_reservation_does_not_raise_or_change_pacing():
    limiter = GeminiRateLimiter()
    response = httpx.Response(200, request=request(), json={"usage": {"prompt_tokens": 4096}})
    limiter.after_response(response)
    assert limiter.reservations == []
    assert limiter.tokens_per_byte == 0.5
