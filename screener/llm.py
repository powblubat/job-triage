"""The model call, through OpenRouter.

One function, one shape: system prompt in, text out. OpenRouter speaks the
OpenAI chat-completions format for every model it fronts, so plain requests
covers it and no vendor SDK is needed. The key is OPENROUTER_API_KEY in .env.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

import requests

URL = "https://openrouter.ai/api/v1/chat/completions"
TIMEOUT_SECONDS = 60

# The cheap categorizer and, later, the screener. Model ids as OpenRouter
# names them; both checked against its model list on 2026-09-15.
CLASSIFY_MODEL = "anthropic/claude-haiku-4.5"
SCREEN_MODEL = "anthropic/claude-sonnet-5"


@dataclass(frozen=True)
class Client:
    api_key: str


@dataclass(frozen=True)
class Reply:
    """The text, plus what it cost. Token counts ride along because screening
    a backlog is the first thing here that spends real money, and a run that
    silently costs ten times what you expected is worth noticing."""

    text: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    # OpenRouter reports what the call actually cost, which beats guessing
    # from token counts and per-model prices that change.
    cost: float = 0.0


def client_from_env() -> Client | None:
    """None when there's no key, so a pull without one still runs; it just
    can't ask the model anything."""
    key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    return Client(key) if key else None


def ask(client: Client, model: str, system: str, user: str, max_tokens: int) -> Reply | None:
    """The reply, or None if the call failed. Callers treat None as
    "unanswered" and take the safe direction; a network blip must not kill jobs."""
    try:
        response = requests.post(
            URL,
            headers={
                "Authorization": f"Bearer {client.api_key}",
                # OpenRouter asks for these on every request; they name the app in
                # its dashboard and nothing else.
                "HTTP-Referer": "https://github.com/powblubat/job-triage",
                "X-Title": "job-triage",
            },
            json={
                "model": model,
                "max_tokens": max_tokens,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
            },
            timeout=TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        body = response.json()
        usage = body.get("usage") or {}
        text = body["choices"][0]["message"]["content"]
        # A refusal or an empty completion comes back as null content. That is
        # a failed answer, not an answer of "", and callers already handle None.
        if not text:
            return None
        return Reply(
            text=text,
            prompt_tokens=usage.get("prompt_tokens", 0),
            completion_tokens=usage.get("completion_tokens", 0),
            cost=usage.get("cost", 0.0) or 0.0,
        )
    except (requests.RequestException, KeyError, IndexError, TypeError, ValueError):
        return None
