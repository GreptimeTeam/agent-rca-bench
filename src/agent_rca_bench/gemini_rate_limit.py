"""Process-wide input pacing for the benchmark's single Gemini project."""

from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass
from threading import Condition

import httpx
from openai import OpenAIError

INPUT_TOKENS_PER_MINUTE = 1_800_000


@dataclass
class Reservation:
    timestamp: float
    tokens: int
    input_bytes: int


class GeminiRateLimiter:
    def __init__(self, budget: int = INPUT_TOKENS_PER_MINUTE, *, window: float = 60.0):
        if budget <= 0 or window <= 0:
            raise ValueError("Gemini token budget and window must be positive")
        self.budget = budget
        self.window = window
        self.condition = Condition()
        self.reservations: list[Reservation] = []
        # This is an estimate, not Gemini tokenization. Actual usage can raise the ratio.
        self.tokens_per_byte = 0.5

    def before_request(self, request: httpx.Request) -> None:
        body = json.loads(request.content)
        payload = {key: body[key] for key in ("messages", "tools") if key in body}
        size = len(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode())
        with self.condition:
            while True:
                estimate = math.ceil(size * self.tokens_per_byte) + 1024
                if estimate > self.budget:
                    raise OpenAIError("Gemini request estimate exceeds the entire TPM budget")
                now = time.monotonic()
                self.reservations = [
                    item for item in self.reservations if now - item.timestamp < self.window
                ]
                if sum(item.tokens for item in self.reservations) + estimate <= self.budget:
                    reservation = Reservation(now, estimate, size)
                    self.reservations.append(reservation)
                    request.extensions["gemini_token_reservation"] = reservation
                    return
                delay = min(item.timestamp + self.window - now for item in self.reservations)
                self.condition.wait(timeout=delay)

    def after_response(self, response: httpx.Response) -> None:
        if not response.is_success:
            # Failed attempts retain their reservation; SDK retries pass the request hook again.
            return
        reservation = response.request.extensions.get("gemini_token_reservation")
        if reservation is None:
            return
        # This client uses non-streaming completions; the SDK can reread the buffered body.
        response.read()
        try:
            body = response.json()
        except ValueError:
            # Leave malformed responses to the SDK; a hook exception could retry a paid request.
            return
        usage = body.get("usage") if isinstance(body, dict) else None
        if not isinstance(usage, dict):
            return
        actual = usage.get("prompt_tokens")
        if not isinstance(actual, int) or actual < 0:
            return
        with self.condition:
            self.tokens_per_byte = max(self.tokens_per_byte, actual / reservation.input_bytes)
            reservation.tokens = actual
            # Keep late responses charged for a full window instead of losing their correction.
            reservation.timestamp = time.monotonic()
            if not any(item is reservation for item in self.reservations):
                self.reservations.append(reservation)
            self.condition.notify_all()


GEMINI_RATE_LIMITER = GeminiRateLimiter()
