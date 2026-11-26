"""Live research narration for the UI.

The pipeline calls `say(...)` at each real step (planning, searching, reading a page, verifying a price...).
The streaming endpoint binds a queue to the current request with `bind()`; every task spawned from that
request (asyncio.gather copies the context) then reports into the same queue. Without a bound queue,
`say` is a no-op, so the plain JSON endpoint, tests and CLI scripts are unaffected.

Events are plain dicts: {"kind": <icon key>, "text": <one human sentence>, ...optional data}.
Kinds the UI knows: think, plan, search, pick, read, follow, found, verify, drop, oem, cost, wait, done, warn.
"""
from __future__ import annotations

import asyncio
import contextvars
import time
from urllib.parse import urlparse

_queue: contextvars.ContextVar[asyncio.Queue | None] = contextvars.ContextVar("progress_queue", default=None)
_t0: contextvars.ContextVar[float] = contextvars.ContextVar("progress_t0", default=0.0)
# Builds a full (partial) search response from the offers found so far; set by aggregator.search for streaming.
_partial: contextvars.ContextVar = contextvars.ContextVar("progress_partial", default=None)


def bind(queue: asyncio.Queue) -> None:
    _queue.set(queue)
    _t0.set(time.time())


def active() -> bool:
    return _queue.get() is not None


def say(kind: str, text: str, **data) -> None:
    q = _queue.get()
    if q is None:
        return
    evt = {"kind": kind, "text": text[:400], "t": round(time.time() - _t0.get(), 1)}
    evt.update({k: v for k, v in data.items() if v is not None})
    try:
        q.put_nowait(("step", evt))
    except asyncio.QueueFull:          # a stalled client must never block the search
        pass


def set_partial_builder(fn) -> None:
    _partial.set(fn)


def partial(offers: list, note: str) -> None:
    """Stream early results: the user sees verified offers while the price hunt keeps going."""
    q, fn = _queue.get(), _partial.get()
    if q is None or fn is None or not offers:
        return
    try:
        data = fn(offers)
        data["partial_note"] = note
        q.put_nowait(("partial", data))
    except Exception:          # noqa: BLE001 - early results are a nicety; never break the search
        pass


def host(url: str) -> str:
    """Short, human label for a URL: 'amayama.com/en/part/...'."""
    p = urlparse(url)
    h = (p.hostname or "").removeprefix("www.")
    path = p.path if len(p.path) <= 28 else p.path[:26] + "…"
    return h + (path if path not in ("", "/") else "")
