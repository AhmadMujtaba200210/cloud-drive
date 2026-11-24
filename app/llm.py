"""LLM fallback chain: Groq (gpt-oss-120b) -> Gemini -> OpenRouter.

All three expose OpenAI-compatible /chat/completions, so one client handles them.
A provider is skipped if its key is missing; on HTTP error, timeout, or unparseable JSON
the next provider is tried. Keys come from env / .env, never from code.
"""
from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field

import httpx

log = logging.getLogger("llm")


@dataclass
class Provider:
    name: str
    base_url: str
    key_env: str
    model: str
    extra_headers: dict = field(default_factory=dict)

    @property
    def key(self) -> str | None:
        return os.getenv(self.key_env) or None


def default_chain() -> list[Provider]:
    return [
        Provider("groq", "https://api.groq.com/openai/v1", "LAUNCHSHIELD_GROQ_API_KEY",
                 os.getenv("GROQ_MODEL", "openai/gpt-oss-120b")),
        Provider("gemini", "https://generativelanguage.googleapis.com/v1beta/openai", "LAUNCHSHIELD_GEMINI_API_KEY",
                 os.getenv("GEMINI_MODEL", "gemini-3.8-flash")),
        Provider("gemini-lite", "https://generativelanguage.googleapis.com/v1beta/openai", "LAUNCHSHIELD_GEMINI_API_KEY",
                 os.getenv("GEMINI_LITE_MODEL", "gemini-flash-lite-latest")),
        Provider("openrouter", "https://openrouter.ai/api/v1", "LAUNCHSHIELD_OPENROUTER_API_KEY",
                 os.getenv("OPENROUTER_MODEL", "openai/gpt-oss-120b"),
                 {"X-Title": "PartsBridge PK"}),
        Provider("openrouter-free", "https://openrouter.ai/api/v1", "LAUNCHSHIELD_OPENROUTER_API_KEY",
                 os.getenv("OPENROUTER_FREE_MODEL", "openrouter/free"), {"X-Title": "PartsBridge PK"}),
    ]


class LLMUnavailable(RuntimeError):
    pass


def parse_json(text: str):
    """Accept raw JSON or JSON wrapped in ``` fences / surrounding prose."""
    text = text.strip()
    m = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if m:
        text = m.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start = min([i for i in (text.find("{"), text.find("[")) if i >= 0], default=-1)
        if start < 0:
            raise
        end = max(text.rfind("}"), text.rfind("]"))
        return json.loads(text[start:end + 1])


async def complete_json(system: str, user: str, *, chain: list[Provider] | None = None,
                        client: httpx.AsyncClient | None = None, timeout: float = 45,
                        max_tokens: int = 4096) -> tuple[object, str]:
    """Return (parsed JSON, provider name). Raises LLMUnavailable if every provider fails."""
    chain = chain or default_chain()
    errors: list[str] = []
    own = client is None
    client = client or httpx.AsyncClient(timeout=timeout)
    try:
        for p in chain:
            if not p.key:
                errors.append(f"{p.name}: no key")
                continue
            try:
                r = await client.post(
                    f"{p.base_url}/chat/completions",
                    headers={"Authorization": f"Bearer {p.key}", **p.extra_headers},
                    json={"model": p.model, "temperature": 0, "max_tokens": max_tokens,
                          "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]},
                )
                if r.status_code != 200:
                    errors.append(f"{p.name}: HTTP {r.status_code} {r.text[:160]}")
                    continue
                content = r.json()["choices"][0]["message"].get("content") or ""
                return parse_json(content), p.name
            except (httpx.HTTPError, KeyError, IndexError, ValueError) as e:
                errors.append(f"{p.name}: {type(e).__name__}: {str(e)[:160]}")
        log.warning("all LLM providers failed: %s", errors)
        raise LLMUnavailable("; ".join(errors))
    finally:
        if own:
            await client.aclose()
