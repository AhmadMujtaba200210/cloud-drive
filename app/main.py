"""FastAPI entrypoint: `uvicorn app.main:app --reload` then open http://127.0.0.1:8000"""
from __future__ import annotations

from .dotenv import load_dotenv

load_dotenv()

import asyncio  # noqa: E402
import contextlib  # noqa: E402
import json  # noqa: E402
import logging  # noqa: E402
import os  # noqa: E402
import secrets  # noqa: E402
import time  # noqa: E402
from collections import defaultdict, deque  # noqa: E402
from pathlib import Path  # noqa: E402

from fastapi import FastAPI, HTTPException, Query, Request  # noqa: E402
from fastapi.responses import FileResponse, StreamingResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402

from . import aggregator, customs, loadtest, progress  # noqa: E402
from .config import countries, load  # noqa: E402
from .models import PartQuery, SearchResponse  # noqa: E402

log = logging.getLogger("partsbridge")
loadtest.install()                       # no-op unless LOADTEST_MODE=1 (staging only)
STATIC = Path(__file__).resolve().parent.parent / "static"
_docs = os.getenv("DISABLE_DOCS") != "1"
app = FastAPI(title="PartsBridge PK", version="0.2.0",
              description="Find a car part locally in Pakistan or worldwide, with shipping and Pakistan customs landed cost.",
              docs_url="/docs" if _docs else None, redoc_url=None, openapi_url="/openapi.json" if _docs else None)

# ---------------- security headers ----------------
CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; "
       "font-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'")


@app.middleware("http")
async def security_headers(request: Request, call_next):
    resp = await call_next(request)
    h = resp.headers
    if not request.url.path.startswith(("/docs", "/openapi")):      # Swagger UI loads its own CDN assets
        h.setdefault("Content-Security-Policy", CSP)
    h.setdefault("X-Content-Type-Options", "nosniff")
    h.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    h.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=(), payment=(), usb=()")
    h.setdefault("Cross-Origin-Opener-Policy", "same-origin")
    h.setdefault("X-Frame-Options", "DENY")
    if request.url.path.startswith("/api/"):
        h.setdefault("Cache-Control", "no-store")
    return resp


# ---------------- public-server protection: every uncached search spends LLM quota ----------------
_hits: dict[str, deque] = defaultdict(deque)
_running: dict[str, str] = {}              # client IP -> key of its uncached search in progress
_sem: asyncio.Semaphore | None = None
_bg: set[asyncio.Task] = set()
_last_prune = 0.0


def _client_ip(request: Request) -> str:
    # uvicorn runs with --proxy-headers behind nginx, so request.client is the real visitor IP
    if loadtest.ENABLED and request.headers.get("x-loadtest-user"):
        return "lt:" + request.headers["x-loadtest-user"][:40]     # each simulated user is its own visitor
    return request.client.host if request.client else "unknown"


def _slots() -> asyncio.Semaphore:
    global _sem
    if _sem is None:
        _sem = asyncio.Semaphore(max(1, int(os.getenv("MAX_CONCURRENT_SEARCHES", "3"))))
    return _sem


def _prune(now: float) -> None:
    global _last_prune
    if now - _last_prune < 300:
        return
    _last_prune = now
    for ip in [ip for ip, q in _hits.items() if not q or now - q[-1] > 3600]:
        del _hits[ip]


def _check_token(request: Request) -> None:
    token = os.getenv("ACCESS_TOKEN")
    if token:
        sent = request.headers.get("x-access-token") or request.query_params.get("token") or ""
        if not secrets.compare_digest(sent.encode(), token.encode()):
            raise HTTPException(401, "Access token required")


def _search_key(q: PartQuery) -> str:
    from .sources.web_deep_search import WebDeepSearch
    return WebDeepSearch.cache_key(q)


def _guard(request: Request, q: PartQuery) -> str | None:
    """Token + per-IP hourly limit + one running search per IP. Cached searches are free.
    Returns the IP to release when the search ends (None when nothing was reserved).
    A visitor whose connection dropped may REJOIN their own running search (same part): that is free and shares
    the result of the search already running instead of being refused."""
    _check_token(request)
    if aggregator.is_cached(q):
        return None
    ip, now = _client_ip(request), time.time()
    _prune(now)
    if ip in _running:
        if _running[ip] == _search_key(q):
            request.state.rejoin = True
            return None
        raise HTTPException(429, "You already have a search running. Please wait for it to finish.",
                            headers={"Retry-After": "30"})
    limit = int(os.getenv("RATE_LIMIT_PER_HOUR", "30"))
    if limit > 0:
        q_ = _hits[ip]
        while q_ and now - q_[0] > 3600:
            q_.popleft()
        if len(q_) >= limit:
            raise HTTPException(429, f"Search limit reached ({limit}/hour). Try again later.",
                                headers={"Retry-After": str(int(3600 - (now - q_[0])) + 1)})
        q_.append(now)
    _running[ip] = _search_key(q)
    return ip


def _release(ip: str | None) -> None:
    if ip is not None:
        _running.pop(ip, None)


SEARCH_CEILING_S = int(os.getenv("SEARCH_CEILING_S", "240"))
KEEPALIVE_S = 8          # the browser treats 25 s of silence as a dead connection and reconnects
_waiting = 0
_recent_secs: deque = deque(maxlen=20)        # durations of recent searches, for the wait estimate


@contextlib.asynccontextmanager
async def _slot_inner():
    """Concurrency cap with an honest queue message: place in line + estimated wait from recent search times."""
    global _waiting
    sem = _slots()
    if sem.locked():
        _waiting += 1
        place = _waiting
        cap = max(1, int(os.getenv("MAX_CONCURRENT_SEARCHES", "3")))
        avg = (sum(_recent_secs) / len(_recent_secs)) if _recent_secs else 90
        eta = int(avg * (1 + (place - 1) // cap))
        progress.say("wait", f"The server is busy: you're number {place} in line. Estimated wait about "
                             f"{max(1, round(eta / 60))} min; your search starts automatically, keep this tab open.")
        try:
            await sem.acquire()
        finally:
            _waiting -= 1
        progress.say("think", "Your turn: starting the research now.")
    else:
        await sem.acquire()
    t0 = time.time()
    try:
        yield
    finally:
        _recent_secs.append(time.time() - t0)
        sem.release()


def _slot():
    return _slot_inner()


@app.post("/api/search", response_model=SearchResponse)
async def search(q: PartQuery, request: Request) -> SearchResponse:
    ip = _guard(request, q)
    try:
        async with _slot():
            return await aggregator.search(q)
    finally:
        _release(ip)


def _sse(event: str, data) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False, separators=(',', ':'))}\n\n"


@app.post("/api/search/stream")
async def search_stream(q: PartQuery, request: Request):
    """Server-Sent Events: `step` events narrate the research live; a final `result` (or `error`) event ends it.
    The search keeps running if the browser disconnects, so its results are cached for the retry."""
    ip = _guard(request, q)
    queue: asyncio.Queue = asyncio.Queue(maxsize=2000)

    def put(item) -> None:
        with contextlib.suppress(asyncio.QueueFull):
            queue.put_nowait(item)

    rejoin = getattr(request.state, "rejoin", False)

    async def produce() -> None:
        progress.bind(queue)
        try:
            if rejoin:
                progress.say("think", "Reconnected. Your search is still running on the server; the results will "
                                      "appear here as soon as it finishes.")
            async with (contextlib.nullcontext() if rejoin else _slot()):
                # Hard ceiling: whatever happens downstream, the visitor gets an answer instead of an endless wait.
                job = asyncio.ensure_future(aggregator.search(q))
                done, _ = await asyncio.wait({job}, timeout=SEARCH_CEILING_S)
                if not done:
                    job.add_done_callback(lambda t: t.cancelled() or t.exception())
                    job.cancel()
                    log.error("search exceeded %ss and was abandoned", SEARCH_CEILING_S)
                    put(("error", {"message": "This search took too long and was stopped. Please try again; parts "
                                              "found so far are kept for the next attempt."}))
                    return
                res = job.result()
            put(("result", res.model_dump(mode="json")))
        except Exception:
            log.exception("search failed")
            put(("error", {"message": "The search failed unexpectedly. Please try again in a minute."}))
        finally:
            _release(ip)
            put(None)

    task = asyncio.create_task(produce())
    _bg.add(task)
    task.add_done_callback(_bg.discard)

    async def events():
        yield ": " + " " * 2048 + "\n\n"            # flush proxies' first buffer so the feed starts at once
        yield _sse("step", {"kind": "think", "text": "Starting the research…", "t": 0})
        while True:
            try:
                item = await asyncio.wait_for(queue.get(), timeout=KEEPALIVE_S)
            except asyncio.TimeoutError:
                yield ": keep-alive\n\n"
                continue
            if item is None:
                break
            yield _sse(*item)

    return StreamingResponse(events(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"})


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


@app.get("/api/countries")
def list_countries() -> dict[str, str]:
    return countries()


@app.get("/api/customs/estimate")
def customs_estimate(part_name: str = Query(..., max_length=100), value_pkr: float = Query(..., ge=0, le=1e10),
                     freight_pkr: float = Query(0, ge=0, le=1e9), channel: str = Query("courier", pattern="^(courier|cargo)$"),
                     filer: bool = True):
    return customs.estimate(part_name, value_pkr, freight_pkr, channel, filer)


@app.get("/api/customs/rules")
def customs_rules():
    cfg = load("pk_customs")
    return {"rules": cfg["rules"], "last_verified": cfg["last_verified"], "status": cfg["_status"]}


app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})
