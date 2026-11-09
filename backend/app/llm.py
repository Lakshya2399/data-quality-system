"""Thin Gemini wrapper. The ONLY module that talks to the Gemini SDK or reads the API key.

generate_json() asks for JSON, parses it into a Pydantic model, optionally runs a semantic
`validator`, and on any problem re-prompts with the specific errors (up to max_attempts).
Transient API failures (429/5xx, timeouts) are retried separately with exponential backoff.

Design choice: JSON mode + the output format spelled out in the prompt + Pydantic validation,
rather than the SDK's native response_schema. Gemini's schema subset is picky (no free-form
dicts, quirks around defaults); this approach works across SDK versions and models, and the
retry loop covers the occasional malformed answer.
"""
from __future__ import annotations

import copy
import json
import re
import time
from typing import Callable, Protocol, TypeVar

from pydantic import BaseModel, ValidationError

from .config import settings

T = TypeVar("T", bound=BaseModel)

SYSTEM_JSON = (
    "You are a meticulous data quality engineer. "
    "Respond with a single valid JSON object and nothing else: no prose, no markdown fences."
)
TRANSIENT_CODES = {429, 500, 502, 503, 504}
AuditHook = Callable[[str, str, str], None]          # (agent, prompt, raw_response)


class LLMError(Exception):
    """The LLM could not be reached or kept failing."""


class LLMOutputError(LLMError):
    """The LLM answered, but never produced output that passed validation."""

    def __init__(self, agent: str, problems: list[str], last_output: str):
        self.agent, self.problems, self.last_output = agent, problems, last_output
        super().__init__(f"{agent}: no valid output after retries. Problems: " + " | ".join(problems))


class LLM(Protocol):
    def generate_json(
        self,
        prompt: str,
        schema: type[T],
        *,
        system: str | None = None,
        validator: Callable[[T], list[str]] | None = None,
        agent: str = "llm",
    ) -> T: ...


# --------------------------------------------------------------------------- helpers
def extract_json(text: str):
    """Parse JSON from a model response, tolerating code fences and surrounding prose."""
    t = (text or "").strip()
    if not t:
        raise ValueError("empty response")
    fenced = re.match(r"^```(?:json)?\s*(.*?)\s*```$", t, re.S | re.I)
    if fenced:
        t = fenced.group(1)
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        start, end = t.find("{"), t.rfind("}")
        if start != -1 and end > start:
            try:
                return json.loads(t[start : end + 1])
            except json.JSONDecodeError:
                pass
        raise ValueError(f"response is not valid JSON (starts with: {t[:80]!r})") from None


def format_validation_error(exc: Exception, limit: int = 8) -> str:
    if isinstance(exc, ValidationError):
        errs = exc.errors()
        parts = [f"{'.'.join(str(p) for p in e['loc']) or '<root>'}: {e['msg']}" for e in errs[:limit]]
        more = f" (+{len(errs) - limit} more)" if len(errs) > limit else ""
        return "; ".join(parts) + more
    return str(exc)


def _retry_feedback(problems: list[str], previous: str) -> str:
    bullets = "\n".join(f"- {p}" for p in problems)
    return (
        "YOUR PREVIOUS RESPONSE WAS REJECTED. Fix every problem below and return the COMPLETE corrected "
        f"JSON object (not a diff).\nProblems:\n{bullets}\n\nPrevious response (truncated):\n{previous[:1500]}"
    )


def _is_transient(exc: Exception) -> bool:
    return getattr(exc, "code", None) in TRANSIENT_CODES or isinstance(exc, (TimeoutError, ConnectionError))


# --------------------------------------------------------------------------- client
class GeminiLLM:
    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
        temperature: float | None = None,
        max_attempts: int = 3,
        transport_retries: int = 3,
        audit: AuditHook | None = None,
        client=None,                      # injectable for tests: needs .models.generate_content(...)
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.model = model or settings.gemini_model
        self.temperature = settings.llm_temperature if temperature is None else temperature
        self.max_attempts = max_attempts
        self.transport_retries = transport_retries
        self.audit = audit
        self._sleep = sleep
        if client is None:
            key = settings.gemini_api_key if api_key is None else api_key
            if not key:
                raise LLMError("GEMINI_API_KEY is not set (copy .env.example to .env and add your key)")
            from google import genai

            client = genai.Client(api_key=key)
        self._client = client

    def with_audit(self, hook: AuditHook) -> "GeminiLLM":
        """A copy of this client that also reports every call to `hook` (used to tag calls with a run id).
        The original is untouched, so one shared client can serve many concurrent runs."""
        clone = copy.copy(self)
        previous = self.audit

        def both(agent: str, prompt: str, response: str) -> None:
            if previous:
                previous(agent, prompt, response)
            hook(agent, prompt, response)

        clone.audit = both
        return clone

    def _call(self, prompt: str, system: str | None) -> str:
        from google.genai import types

        config = types.GenerateContentConfig(
            system_instruction=system or SYSTEM_JSON,
            temperature=self.temperature,
            response_mime_type="application/json",
        )
        delay = 1.0
        for attempt in range(self.transport_retries + 1):
            try:
                resp = self._client.models.generate_content(model=self.model, contents=prompt, config=config)
                return resp.text or ""
            except Exception as exc:  # noqa: BLE001
                if attempt >= self.transport_retries or not _is_transient(exc):
                    raise LLMError(f"Gemini call failed: {exc}") from exc
                self._sleep(delay)
                delay *= 2
        raise AssertionError("unreachable")

    def generate_json(
        self,
        prompt: str,
        schema: type[T],
        *,
        system: str | None = None,
        validator: Callable[[T], list[str]] | None = None,
        agent: str = "llm",
    ) -> T:
        feedback: str | None = None
        problems: list[str] = []
        text = ""
        for _ in range(self.max_attempts):
            full_prompt = prompt if feedback is None else f"{prompt}\n\n{feedback}"
            text = self._call(full_prompt, system)
            if self.audit:
                self.audit(agent, full_prompt, text)
            try:
                obj = schema.model_validate(extract_json(text))
            except ValueError as exc:                     # JSONDecodeError and ValidationError both qualify
                problems = [format_validation_error(exc)]
            else:
                problems = list(validator(obj)) if validator else []
                if not problems:
                    return obj
            feedback = _retry_feedback(problems, text)
        raise LLMOutputError(agent, problems, text)
