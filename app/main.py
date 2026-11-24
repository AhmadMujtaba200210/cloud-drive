"""FastAPI entrypoint: `uvicorn app.main:app --reload` then open http://127.0.0.1:8000"""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import aggregator, customs
from .config import countries, load
from .models import PartQuery, SearchResponse

STATIC = Path(__file__).resolve().parent.parent / "static"
app = FastAPI(title="PartsBridge PK", version="0.1.0",
              description="Find a car part locally in Pakistan or worldwide, with shipping and Pakistan customs landed cost.")


@app.post("/api/search", response_model=SearchResponse)
async def search(q: PartQuery) -> SearchResponse:
    return await aggregator.search(q)


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
