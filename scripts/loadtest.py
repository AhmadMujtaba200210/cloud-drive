"""Concurrent-user load test for PartsBridge PK.

Run against an instance started with LOADTEST_MODE=1 (outside services simulated, see app/loadtest.py):

    python scripts/loadtest.py http://127.0.0.1:8001 --levels 5,10,50,100,250,500,1000 --pid <uvicorn pid>

Each simulated user: loads the page + assets, then starts a search for its OWN part (no cache sharing: worst case)
and reads the Server-Sent-Events stream to the end. Reports per level: success rate, refusals (429), time to the
first live step, first early result and full result (p50 / p95 / max), plus server CPU % and memory.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import multiprocessing as mp
import os
import statistics as st
import time

import httpx

PARTS = ["AC condenser", "radiator", "brake pads", "headlight", "side mirror", "shock absorbers", "air filter",
         "fuel pump", "alternator", "starter motor", "clutch plate", "water pump", "tail light", "wiper motor"]


async def user(_shared, base: str, uid: str, n: int) -> dict:
    """One visitor = its own connections (like a real browser), not a slot in a shared pool."""
    async with httpx.AsyncClient(timeout=httpx.Timeout(1200, connect=60),
                                 limits=httpx.Limits(max_connections=6, max_keepalive_connections=6)) as client:
        return await _user(client, base, uid, n)


async def _user(client: httpx.AsyncClient, base: str, uid: str, n: int) -> dict:
    out = {"ok": False, "status": None, "first_step": None, "first_partial": None, "result": None, "page": None}
    t0 = time.perf_counter()
    try:
        r = await client.get(base + "/")
        await asyncio.gather(*(client.get(base + p) for p in ("/static/app.js?v=14", "/static/app.css?v=13",
                                                              "/static/catalog.json?v=1")))
        out["page"] = time.perf_counter() - t0
        body = {"make": "Toyota", "model": "Corolla", "year": 2002 + n % 20, "part_name": f"{PARTS[n % len(PARTS)]} {uid}",
                "customer_city": "Lahore"}
        t1 = time.perf_counter()
        async with client.stream("POST", base + "/api/search/stream", json=body,
                                 headers={"X-Loadtest-User": uid}) as resp:
            out["status"] = resp.status_code
            if resp.status_code != 200:
                return out
            ev = None
            async for line in resp.aiter_lines():
                if line.startswith("event:"):
                    ev = line[6:].strip()
                elif line.startswith("data:"):
                    now = time.perf_counter() - t1
                    if ev == "step" and out["first_step"] is None:
                        out["first_step"] = now
                    elif ev == "partial" and out["first_partial"] is None:
                        out["first_partial"] = now
                    elif ev == "result":
                        out["result"], out["ok"] = now, True
                        d = json.loads(line[5:])
                        out["offers"] = len(d.get("local", [])) + len(d.get("contact_only", []))
                    elif ev == "error":
                        out["error"] = line[5:200]
    except Exception as e:  # noqa: BLE001
        out["error"] = f"{type(e).__name__}: {str(e)[:120]}"
    return out


def proc_sample(pid: int | None) -> tuple[float, float]:
    """(cpu seconds, rss MB) of the server process, from /proc."""
    if not pid:
        return 0.0, 0.0
    try:
        f = open(f"/proc/{pid}/stat").read().split()
        cpu = (int(f[13]) + int(f[14])) / os.sysconf("SC_CLK_TCK")
        rss = int(open(f"/proc/{pid}/statm").read().split()[1]) * os.sysconf("SC_PAGE_SIZE") / 2**20
        return cpu, rss
    except OSError:
        return 0.0, 0.0


def pct(v: list[float], p: float) -> float | None:
    v = sorted(x for x in v if x is not None)
    return round(v[min(len(v) - 1, int(p * (len(v) - 1)))], 1) if v else None


def _worker(base: str, ids: list[int], n: int, q) -> None:
    async def run():
        return await asyncio.gather(*(user(None, base, f"u{n}-{i}", i) for i in ids))
    q.put(asyncio.run(run()))


async def level(base: str, n: int, pid: int | None, procs: int = 1) -> dict:
    peak_rss, cpu0, _ = 0.0, *proc_sample(pid)
    t0 = time.perf_counter()
    stop = asyncio.Event()

    async def sampler():
        nonlocal peak_rss
        while not stop.is_set():
            peak_rss = max(peak_rss, proc_sample(pid)[1])
            await asyncio.sleep(1)
    samp = asyncio.create_task(sampler())
    if procs <= 1:
        res = await asyncio.gather(*(user(None, base, f"u{n}-{i}", i) for i in range(n)))
    else:                                   # split users across processes so the CLIENT is never the bottleneck
        q = mp.Queue()
        ps = [mp.Process(target=_worker, args=(base, list(range(k, n, procs)), n, q)) for k in range(procs)]
        for p in ps:
            p.start()
        res = []
        for _ in ps:
            res += await asyncio.to_thread(q.get)
        for p in ps:
            p.join()
    stop.set(); await samp
    wall = time.perf_counter() - t0
    cpu1, rss = proc_sample(pid)
    ok = [r for r in res if r["ok"]]
    return {
        "users": n, "ok": len(ok), "refused_429": sum(r["status"] == 429 for r in res),
        "errors": sum(1 for r in res if not r["ok"] and r["status"] != 429),
        "sample_error": next((r.get("error") for r in res if r.get("error")), None),
        "page_load_p95_s": pct([r["page"] for r in res], .95),
        "first_step_p50_s": pct([r["first_step"] for r in ok], .5), "first_step_p95_s": pct([r["first_step"] for r in ok], .95),
        "early_results_p50_s": pct([r["first_partial"] for r in ok], .5),
        "early_results_p95_s": pct([r["first_partial"] for r in ok], .95),
        "full_result_p50_s": pct([r["result"] for r in ok], .5), "full_result_p95_s": pct([r["result"] for r in ok], .95),
        "full_result_max_s": pct([r["result"] for r in ok], 1.0),
        "wall_s": round(wall, 1), "server_cpu_avg_pct": round(100 * (cpu1 - cpu0) / wall, 1) if pid else None,
        "server_peak_rss_mb": round(peak_rss, 0) if pid else None,
    }


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("base")
    ap.add_argument("--levels", default="5,10,50,100,250,500,1000")
    ap.add_argument("--pid", type=int)
    ap.add_argument("--out", default="loadtest_results.json")
    ap.add_argument("--procs", type=int, default=1, help="client processes (use ~CPU cores for >= 250 users)")
    a = ap.parse_args()
    rows = []
    for n in [int(x) for x in a.levels.split(",")]:
        row = await level(a.base.rstrip("/"), n, a.pid, a.procs)
        rows.append(row)
        print(json.dumps(row), flush=True)
        json.dump(rows, open(a.out, "w"), indent=1)
        await asyncio.sleep(5)


if __name__ == "__main__":
    asyncio.run(main())
