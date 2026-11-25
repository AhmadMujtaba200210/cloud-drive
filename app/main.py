"""FastAPI entrypoint: `uvicorn app.main:app --reload` then open http://127.0.0.1:8000"""
from __future__ import annotations

from pathlib import Path

import os
import secrets
import time
from collections import defaultdict, deque

from fastapi import FastAPI, HTTPException, Request

from .dotenv import load_dotenv

load_dotenv()
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import aggregator, customs
from .config import countries, load
from .models import PartQuery, SearchResponse

STATIC = Path(__file__).resolve().parent.parent / "static"
app = FastAPI(title="PartsBridge PK", version="0.1.0",
              description="Find a car part locally in Pakistan or worldwide, with shipping and Pakistan customs landed cost.")


# ---- public-server protection: every search spends LLM quota, so cap it per client IP ----
_hits: dict[str, deque] = defaultdict(deque)


def _client_ip(request: Request) -> str:
    # uvicorn runs with --proxy-headers behind nginx, so request.client is the real visitor IP
    return request.client.host if request.client else "unknown"


def _guard(request: Request) -> None:
    token = os.getenv("ACCESS_TOKEN")
    if token:
        sent = request.headers.get("x-access-token") or request.query_params.get("token") or ""
        if not secrets.compare_digest(sent, token):
            raise HTTPException(401, "Access token required")
    limit = int(os.getenv("RATE_LIMIT_PER_HOUR", "30"))
    if limit <= 0:
        return
    now, q = time.time(), _hits[_client_ip(request)]
    while q and now - q[0] > 3600:
        q.popleft()
    if len(q) >= limit:
        raise HTTPException(429, f"Search limit reached ({limit}/hour). Try again later.",
                            headers={"Retry-After": str(int(3600 - (now - q[0])) + 1)})
    q.append(now)


@app.post("/api/search", response_model=SearchResponse)
async def search(q: PartQuery, request: Request) -> SearchResponse:
    _guard(request)
    return await aggregator.search(q)


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


@app.get("/api/countries")
def list_countries() -> dict[str, str]:
    return countries()


@app.get("/api/customs/estimate")
def customs_estimate(part_name: str, value_pkr: float, freight_pkr: float = 0, channel: str = "courier", filer: bool = True):
    return customs.estimate(part_name, value_pkr, freight_pkr, channel, filer)


@app.get("/api/customs/rules")
def customs_rules():
    cfg = load("pk_customs")
    return {"rules": cfg["rules"], "last_verified": cfg["last_verified"], "status": cfg["_status"]}


app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")
