"""Model client: the only code that talks to the language-model providers.

Groq and NVIDIA NIM both speak the OpenAI chat format, so one implementation
serves every role. Each role has a primary and a fallback model; a model that
is rate limited, slow, cut off or unreadable hands over to the next one, and
when none answers usably within the role's time the check counts as "could not
run". ``RecordedProvider`` replays recorded answers so the hosted demo and the
test suite run with no keys and no network.

Model answers are untrusted text: callers validate them against a strict
schema and never act on them directly.
"""

import hashlib
import json
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

import httpx
from pydantic import SecretStr

from agentcosign.core.config import ModelRoleConfig
from agentcosign.core.exceptions import ModelUnavailableError

GROQ_BASE = "https://api.groq.com/openai/v1"
# a rate-limited model that names no retry time rests for this long
DEFAULT_REST_SECONDS = 60.0
CUT_OFF = "length"

Message = Mapping[str, str]


@dataclass(frozen=True, slots=True)
class ModelAnswer:
    """One model's text answer and where it came from."""

    text: str
    model: str
    recorded: bool = False


class LLMProvider(Protocol):
    """Asks one role's models for an answer."""

    async def complete(self, messages: Sequence[Message]) -> ModelAnswer:
        """The first usable answer, or ``ModelUnavailableError``."""
        ...


def prompt_key(role: str, messages: Sequence[Message]) -> str:
    """A stable name for a role's prompt, used to find a recorded answer."""
    canonical = json.dumps(
        {"role": role, "messages": [dict(m) for m in messages]},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _usable_text(response: httpx.Response) -> str | None:
    """The answer's text, or None when it is missing, empty or cut off."""
    try:
        choice = response.json()["choices"][0]
    except (ValueError, KeyError, IndexError, TypeError):
        return None
    if not isinstance(choice, Mapping) or choice.get("finish_reason") == CUT_OFF:
        return None
    text = (choice.get("message") or {}).get("content")
    return text if isinstance(text, str) and text.strip() else None


# guardrail: allow float-annotation -- seconds to rest, not money
def _rest_seconds(response: httpx.Response) -> float:
    try:
        # guardrail: allow float-conversion -- seconds from a header, not money
        return max(float(response.headers.get("retry-after", "")), 0.0)
    except ValueError:
        return DEFAULT_REST_SECONDS


class OpenAICompatibleProvider:
    """One role's primary and fallback models at an OpenAI-compatible provider."""

    def __init__(
        self,
        role: str,
        config: ModelRoleConfig,
        api_key: SecretStr,
        *,
        http: httpx.AsyncClient | None = None,
        # guardrail: allow float-annotation -- monotonic seconds, not money
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not api_key.get_secret_value():
            raise ModelUnavailableError(f"no key is configured for {role}")
        self._role = role
        self._config = config
        self._key = api_key
        self._http = http or httpx.AsyncClient(
            base_url=config.base_url or GROQ_BASE, timeout=config.timeout_seconds
        )
        self._clock = clock
        self._primary_rests_until = 0.0

    async def aclose(self) -> None:
        """Release the connection pool."""
        await self._http.aclose()

    async def complete(self, messages: Sequence[Message]) -> ModelAnswer:
        """Ask the primary, then the fallback, within the role's time."""
        deadline = self._clock() + self._config.timeout_seconds
        for model in (self._config.primary, self._config.fallback):
            primary = model == self._config.primary
            if primary and self._clock() < self._primary_rests_until:
                continue  # rate limited a moment ago: do not wait for it
            left = deadline - self._clock()
            if left <= 0:
                break
            answer = await self._ask(model, messages, left, primary)
            if answer is not None:
                return answer
        raise ModelUnavailableError(
            f"no {self._role} model gave a usable answer", {"role": self._role}
        )

    async def _ask(
        self,
        model: str,
        messages: Sequence[Message],
        # guardrail: allow float-annotation -- seconds left, not money
        left: float,
        primary: bool,
    ) -> ModelAnswer | None:
        body: dict[str, Any] = {
            "model": model,
            "messages": [dict(m) for m in messages],
            "temperature": self._config.temperature,
            "max_tokens": self._config.max_tokens,
        }
        try:
            response = await self._http.post(
                "/chat/completions",
                json=body,
                headers={"Authorization": f"Bearer {self._key.get_secret_value()}"},
                timeout=left,
            )
        except httpx.HTTPError:
            return None
        if response.status_code == httpx.codes.TOO_MANY_REQUESTS:
            if primary:
                self._primary_rests_until = self._clock() + _rest_seconds(response)
            return None
        if response.status_code != httpx.codes.OK:
            return None
        text = _usable_text(response)
        return None if text is None else ModelAnswer(text, model)


class RecordedProvider:
    """Answers from recordings of real model runs; anything new could not run."""

    def __init__(self, role: str, answers: Mapping[str, str]) -> None:
        self._role = role
        self._answers = dict(answers)

    async def complete(self, messages: Sequence[Message]) -> ModelAnswer:
        """The recorded answer for exactly this role and prompt."""
        text = self._answers.get(prompt_key(self._role, messages))
        if text is None:
            raise ModelUnavailableError(
                f"no recorded {self._role} answer for this message",
                {"role": self._role},
            )
        return ModelAnswer(text, "recorded", recorded=True)
