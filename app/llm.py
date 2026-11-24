"""LLM fallback chain: Groq (gpt-oss-120b) -> OpenRouter -> Gemini Flash-Lite (last resort).

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


def _lite(model: str) -> str:
    """Policy: only low-cost Gemini Flash-Lite models. Any override that is not a '-flash-lite' text model is refused."""
    if "flash-lite" not in model or any(x in model for x in ("image", "tts", "audio")):
        raise ValueError(f"Gemini model {model!r} is not an allowed Flash-Lite text model")
    return model


def default_chain() -> list[Provider]:
    return [
        Provider("groq", "https://api.groq.com/openai/v1", "LAUNCHSHIELD_GROQ_API_KEY",
                 os.getenv("GROQ_MODEL", "openai/gpt-oss-120b")),
        Provider("openrouter", "https://openrouter.ai/api/v1", "LAUNCHSHIELD_OPENROUTER_API_KEY",
                 os.getenv("OPENROUTER_MODEL", "openai/gpt-oss-120b"),
                 {"X-Title": "PartsBridge PK"}),
        Provider("openrouter-free", "https://openrouter.ai/api/v1", "LAUNCHSHIELD_OPENROUTER_API_KEY",
                 os.getenv("OPENROUTER_FREE_MODEL", "openrouter/free"), {"X-Title": "PartsBridge PK"}),
        # Gemini: cheap Flash-Lite tier ONLY (pinned versions, so an alias can never drift to a pricier model).
        Provider("gemini-lite", "https://generativelanguage.googleapis.com/v1beta/openai", "LAUNCHSHIELD_GEMINI_API_KEY",
                 _lite(os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite"))),
        Provider("gemini-lite-2", "https://generativelanguage.googleapis.com/v1beta/openai", "LAUNCHSHIELD_GEMINI_API_KEY",
                 _lite(os.getenv("GEMINI_LITE_MODEL", "gemini-3.1-flash-lite"))),
    ]


# Circuit breaker: after a 429/5xx a provider is skipped until its cooldown ends, so one rate-limited
# provider does not add a failed round-trip to every call in a search.
_cooldown_until: dict[str, float] = {}
COOLDOWN_429_S, COOLDOWN_5XX_S = 30.0, 60.0


def _cool(p: "Provider", r: httpx.Response) -> None:
    import time
    if r.status_code == 429:
        try:
            wait = float(r.headers.get("retry-after", COOLDOWN_429_S))
        except ValueError:
            wait = COOLDOWN_429_S
        _cooldown_until[p.name] = time.time() + min(max(wait, 5.0), 300.0)
    elif r.status_code >= 500:
        _cooldown_until[p.name] = time.time() + COOLDOWN_5XX_S


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
            import time
            if _cooldown_until.get(p.name, 0) > time.time():
                errors.append(f"{p.name}: cooling down")
                continue
            try:
                r = await client.post(
                    f"{p.base_url}/chat/completions",
                    headers={"Authorization": f"Bearer {p.key}", **p.extra_headers},
                    json={"model": p.model, "temperature": 0, "max_tokens": max_tokens,
                          "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]},
                )
                if r.status_code != 200:
                    _cool(p, r)
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
