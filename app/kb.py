"""Knowledge base on Oracle Autonomous Database (23ai/26ai, native VECTOR type): cuts LLM / scraping calls.

What is stored, and why it is safe to reuse (accuracy first):

  PB_PLAN        planner output per canonical part           14 d   saves the planning LLM call
  PB_PAGE        scraped page markdown per URL               24 h   saves Firecrawl time
  PB_EXTRACTION  raw LLM extraction per (page content hash,  14 d   saves extraction LLM calls; keyed by the
                 part)                                              exact page text, so a changed page (new
                                                                    price) is a cache miss. Code verification
                                                                    (web_deep_search.verify) still re-runs.
  PB_RESULT      verified offers per search + embedding      12 h   saves the whole search on a repeat
  PB_SOURCE      product pages that produced verified        30 d   known-good pages are fetched first next
                 prices + embedding                                 time (prices are re-checked live)

Embeddings (the "generator") come from a small local ONNX model (fastembed, BAAI/bge-small-en-v1.5, 384 dims):
no API calls. Vector search only proposes candidates; `same_part()` (the "validator") decides, because embedding
distance alone confuses e.g. "radiator" with "radiator fan" (0.090) more than "brake pads" with "brake pad set"
(0.096). Reuse requires identical make/model/year/scope AND the same canonical part signature.

Disabled (no-op) unless ORACLE_DB_USER, ORACLE_DB_PASSWORD, ORACLE_DB_DSN are set. Every call has a timeout
and fails soft: if the database is slow or down, research simply runs without it.
"""
from __future__ import annotations

import array
import asyncio
import hashlib
import json
import logging
import os
import re
import time
from typing import Any

log = logging.getLogger("kb")

EMB_MODEL = os.getenv("KB_EMBED_MODEL", "BAAI/bge-small-en-v1.5")
EMB_DIM = 384
TTL = {"plan": 14 * 86400, "page": 86400, "extraction": 14 * 86400, "result": 12 * 3600, "source": 30 * 86400}
OP_TIMEOUT_S = 6.0          # reads: never slow a search down for long
WRITE_TIMEOUT_S = 20.0      # writes run in the background
CLOB_COLS = {"plan", "markdown", "links", "offers", "stats"}


async def upsert(conn, table: str, keys: dict, vals: dict) -> None:
    """UPDATE ... WHERE key; INSERT if nothing matched. Each bind is used once, and large text is bound as CLOB
    (a long str bound as VARCHAR, or a MERGE repeating binds, fails in thin mode with ORA-03146)."""
    import oracledb
    cur = conn.cursor()
    binds = {**keys, **vals}
    sizes = {k: oracledb.DB_TYPE_CLOB for k in binds if k in CLOB_COLS}
    if "emb" in binds:
        sizes["emb"] = oracledb.DB_TYPE_VECTOR
    if sizes:
        cur.setinputsizes(**sizes)
    await cur.execute(f"UPDATE {table} SET " + ", ".join(f"{k} = :{k}" for k in vals) + " WHERE "
                      + " AND ".join(f"{k} = :{k}" for k in keys), binds)
    if cur.rowcount == 0:
        cur = conn.cursor()
        if sizes:
            cur.setinputsizes(**sizes)
        await cur.execute(f"INSERT INTO {table} ({', '.join(binds)}) VALUES ({', '.join(':' + k for k in binds)})", binds)

# ---------------- validator: canonical part signature ----------------
FILLER = {"set", "kit", "pair", "assembly", "assy", "genuine", "original", "oem", "new", "complete", "part", "parts",
          "for", "the", "a", "an", "of", "and", "with", "car", "auto", "spare", "replacement", "brand"}
QUALIFIERS = {"front", "rear", "left", "right", "lh", "rh", "upper", "lower", "inner", "outer", "driver", "passenger",
              "top", "bottom", "back", "fr", "rr"}
SYNONYMS = [  # multi-word first; applied to the lower-cased, space-normalised name
    (r"\bhead ?lamps?\b", "headlight"), (r"\bhead ?lights?\b", "headlight"), (r"\btail ?lamps?\b", "taillight"),
    (r"\btail ?lights?\b", "taillight"), (r"\bwing mirrors?\b", "side mirror"), (r"\bdoor mirrors?\b", "side mirror"),
    (r"\bshocks\b", "shock absorber"), (r"\bdampers?\b", "shock absorber"), (r"\bbrake discs?\b", "brake rotor"),
    (r"\bdis[ck] pads?\b", "brake pad"), (r"\bbrake pads?\b", "brake pad"),
    (r"\bac\b|\ba/c\b", "air conditioning"), (r"\bspark ?plugs?\b", "spark plug"), (r"\bwipers?\b", "wiper blade"),
    (r"\bbumpers?\b", "bumper"), (r"\bradiators?\b", "radiator"),
]
_SIDE = {"lh": "left", "rh": "right", "fr": "front", "rr": "rear", "driver": "right", "passenger": "left", "back": "rear"}


def _singular(w: str) -> str:
    if len(w) > 3 and w.endswith("ies"):
        return w[:-3] + "y"
    if len(w) > 3 and w.endswith("s") and not w.endswith(("ss", "us")):
        return w[:-1]
    return w


def part_signature(name: str) -> str:
    """'Genuine Front Brake Pads Set' -> 'brake front pad'. Order-free, plural-free, filler-free, synonyms merged."""
    s = re.sub(r"[^\w/ -]+", " ", (name or "").lower())
    s = re.sub(r"[-_/]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    for pat, rep in SYNONYMS:
        s = re.sub(pat, rep, s)
    toks = [_SIDE.get(t, t) for t in (_singular(t) for t in s.split())]
    return " ".join(sorted({t for t in toks if t not in FILLER}))


def same_part(a: str, b: str) -> bool:
    """Validator: reuse only if the canonical signatures match exactly (qualifiers like front/rear included)."""
    sa, sb = part_signature(a), part_signature(b)
    return bool(sa) and sa == sb


def norm(s: str | None) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def query_key(make: str, model: str, year: int | None, part: str, scope: str, oem: str | None = None) -> str:
    raw = "|".join([norm(make), norm(model), str(year or ""), part_signature(part), scope, norm(oem)])
    return hashlib.sha1(raw.encode()).hexdigest()


def _j(v):
    """IS JSON columns come back already parsed on Oracle 23ai/26ai; older drivers return text."""
    if v is None:
        return None
    if isinstance(v, (bytes, bytearray)):
        v = v.decode()
    return json.loads(v) if isinstance(v, str) else v


def sha1(s: str) -> str:
    return hashlib.sha1((s or "").encode("utf-8", "replace")).hexdigest()


# ---------------- generator: local embeddings ----------------
_model = None


def _load_model():
    global _model
    if _model is None:
        from fastembed import TextEmbedding                      # optional dependency, imported lazily
        _model = TextEmbedding(EMB_MODEL, cache_dir=os.getenv("KB_MODEL_DIR") or None)
    return _model


async def embed(text: str) -> array.array | None:
    """384-dim float32, L2-normalised. None if the model is unavailable (KB then works by exact keys only)."""
    try:
        def run():
            v = next(iter(_load_model().embed([text])))
            n = float((v * v).sum()) ** 0.5 or 1.0
            return array.array("f", (float(x) / n for x in v))
        return await asyncio.to_thread(run)
    except Exception as e:                                        # noqa: BLE001 - fail soft
        log.warning("embedding unavailable: %s", e)
        return None


# ---------------- connection ----------------
DDL = [
    """CREATE TABLE pb_plan (k VARCHAR2(40) PRIMARY KEY, plan CLOB CHECK (plan IS JSON), created_at NUMBER NOT NULL)""",
    """CREATE TABLE pb_page (url_hash VARCHAR2(40) PRIMARY KEY, url VARCHAR2(2000), content_hash VARCHAR2(40),
         title VARCHAR2(1000), markdown CLOB, links CLOB CHECK (links IS JSON), fetched_at NUMBER NOT NULL)""",
    """CREATE TABLE pb_extraction (content_hash VARCHAR2(40), part_key VARCHAR2(40), offers CLOB CHECK (offers IS JSON),
         provider VARCHAR2(60), created_at NUMBER NOT NULL, CONSTRAINT pb_extraction_pk PRIMARY KEY (content_hash, part_key))""",
    f"""CREATE TABLE pb_result (k VARCHAR2(40) PRIMARY KEY, mk VARCHAR2(60), mdl VARCHAR2(80), yr NUMBER(4),
         scope VARCHAR2(40), part_text VARCHAR2(200), part_sig VARCHAR2(200), offers CLOB CHECK (offers IS JSON),
         stats CLOB CHECK (stats IS JSON), created_at NUMBER NOT NULL, emb VECTOR({EMB_DIM}, FLOAT32))""",
    "CREATE INDEX pb_result_mmy ON pb_result (mk, mdl, yr, scope)",
    f"""CREATE TABLE pb_source (url_hash VARCHAR2(40) PRIMARY KEY, url VARCHAR2(2000), vendor VARCHAR2(200),
         country VARCHAR2(2), mk VARCHAR2(60), mdl VARCHAR2(80), part_sig VARCHAR2(200), title VARCHAR2(400),
         price NUMBER, currency VARCHAR2(3), last_verified NUMBER NOT NULL, hits NUMBER DEFAULT 1,
         emb VECTOR({EMB_DIM}, FLOAT32))""",
    "CREATE INDEX pb_source_mm ON pb_source (mk, mdl, country)",
]


class KB:
    def __init__(self) -> None:
        self.pool = None
        self._init_lock = asyncio.Lock()
        self._failed_at = 0.0
        self._down_until = 0.0          # circuit breaker: skip the KB entirely after a hung call
        self.stats: dict[str, int] = {}

    @staticmethod
    def configured() -> bool:
        return all(os.getenv(k) for k in ("ORACLE_DB_USER", "ORACLE_DB_PASSWORD", "ORACLE_DB_DSN")) \
            and os.getenv("KB_DISABLED") != "1"

    async def _pool(self):
        if self.pool is not None:
            return self.pool
        if not self.configured() or time.time() - self._failed_at < 120:      # back off after a failure
            return None
        async with self._init_lock:
            if self.pool is None:
                try:
                    import oracledb
                    oracledb.defaults.fetch_lobs = False
                    wallet = os.getenv("ORACLE_WALLET_DIR")
                    self.pool = oracledb.create_pool_async(
                        user=os.environ["ORACLE_DB_USER"], password=os.environ["ORACLE_DB_PASSWORD"],
                        dsn=os.environ["ORACLE_DB_DSN"], config_dir=wallet, wallet_location=wallet,
                        wallet_password=os.getenv("ORACLE_WALLET_PASSWORD") or None,
                        min=1, max=int(os.getenv("KB_POOL_MAX", "4")), increment=1, timeout=300, ping_interval=30,
                        tcp_connect_timeout=8)
                except Exception as e:                                       # noqa: BLE001
                    self._failed_at = time.time()
                    log.warning("knowledge base unavailable: %s", e)
                    return None
        return self.pool

    async def _run(self, fn, default=None, timeout: float = OP_TIMEOUT_S):
        """Run fn(conn) with a HARD timeout; any error -> default (research continues without the KB).

        asyncio.wait_for cancels and then WAITS for the cancellation; a driver stuck on a dead socket can ignore it
        and hang the whole search. So the call runs as its own task, we stop waiting at the deadline, abandon it,
        and open the circuit breaker so the next calls skip the KB for a while instead of each waiting again."""
        if time.time() < self._down_until:
            return default
        pool = await self._pool()
        if pool is None:
            return default

        async def go():
            async with pool.acquire() as conn:
                conn.call_timeout = int(timeout * 1000)        # server round-trips also give up on their own
                return await fn(conn)
        task = asyncio.ensure_future(go())
        done, _ = await asyncio.wait({task}, timeout=timeout)
        if not done:
            task.cancel()
            task.add_done_callback(lambda t: t.cancelled() or t.exception())   # never "exception never retrieved"
            self._down_until = time.time() + 120
            self.stats["timeouts"] = self.stats.get("timeouts", 0) + 1
            log.warning("kb op timed out after %.0fs; skipping the knowledge base for 2 min", timeout)
            return default
        exc = task.exception()
        if exc is not None:
            log.warning("kb op failed: %s", str(exc)[:200])
            self.stats["errors"] = self.stats.get("errors", 0) + 1
            return default
        return task.result()

    def _hit(self, name: str) -> None:
        self.stats[name] = self.stats.get(name, 0) + 1

    # ---------- schema ----------
    async def init_schema(self) -> list[str]:
        async def fn(conn):
            out = []
            for stmt in DDL:
                try:
                    await conn.cursor().execute(stmt)
                    out.append("created: " + stmt.split("(")[0].strip())
                except Exception as e:                                         # ORA-00955 / ORA-01408: exists
                    if "ORA-00955" in str(e) or "ORA-01408" in str(e):
                        out.append("exists:  " + stmt.split("(")[0].strip())
                    else:
                        raise
            return out
        pool = await self._pool()
        if pool is None:
            raise RuntimeError("knowledge base not configured or unreachable")
        async with pool.acquire() as conn:
            return await fn(conn)

    async def purge_expired(self) -> dict:
        now = time.time()

        async def fn(conn):
            n = {}
            for table, col, ttl in (("pb_plan", "created_at", TTL["plan"]), ("pb_page", "fetched_at", TTL["page"]),
                                    ("pb_extraction", "created_at", TTL["extraction"]),
                                    ("pb_result", "created_at", TTL["result"] * 4),
                                    ("pb_source", "last_verified", TTL["source"])):
                cur = conn.cursor()
                await cur.execute(f"DELETE FROM {table} WHERE {col} < :t", t=now - ttl)
                n[table] = cur.rowcount
            await conn.commit()
            return n
        return await self._run(fn, {})

    # ---------- plans ----------
    async def get_plan(self, key: str) -> dict | None:
        async def fn(conn):
            cur = conn.cursor()
            await cur.execute("SELECT plan FROM pb_plan WHERE k = :k AND created_at > :t", k=key,
                              t=time.time() - TTL["plan"])
            row = await cur.fetchone()
            return _j(row[0]) if row else None
        out = await self._run(fn)
        if out:
            self._hit("plan_hits")
        return out

    async def put_plan(self, key: str, plan: dict) -> None:
        async def fn(conn):
            await upsert(conn, "pb_plan", {"k": key},
                         {"plan": json.dumps(plan, ensure_ascii=False), "created_at": time.time()})
            await conn.commit()
        await self._run(fn, timeout=WRITE_TIMEOUT_S)

    async def get_page(self, url: str) -> dict | None:
        async def fn(conn):
            cur = conn.cursor()
            await cur.execute("SELECT url, title, markdown, links FROM pb_page WHERE url_hash = :h AND fetched_at > :t",
                              h=sha1(url), t=time.time() - TTL["page"])
            row = await cur.fetchone()
            if not row or not row[2]:
                return None
            return {"url": url, "title": row[1] or "", "markdown": row[2], "links": _j(row[3]) or []}
        out = await self._run(fn)
        if out:
            self._hit("page_hits")
        return out

    async def put_page(self, page: dict) -> None:
        if not page.get("markdown") or page.get("snippet_only"):
            return
        md = page["markdown"][:400_000]

        async def fn(conn):
            await upsert(conn, "pb_page", {"url_hash": sha1(page["url"])},
                         {"url": page["url"][:2000], "content_hash": sha1(md), "title": (page.get("title") or "")[:1000],
                          "markdown": md, "links": json.dumps((page.get("links") or [])[:400]), "fetched_at": time.time()})
            await conn.commit()
        await self._run(fn, timeout=WRITE_TIMEOUT_S)

    async def get_extractions(self, content_hashes: list[str], part_key: str) -> dict[str, list[dict]]:
        if not content_hashes:
            return {}

        async def fn(conn):
            cur = conn.cursor()
            binds = {f"c{i}": h for i, h in enumerate(content_hashes)}
            await cur.execute(f"""SELECT content_hash, offers FROM pb_extraction WHERE part_key = :pk AND created_at > :t
                AND content_hash IN ({",".join(":" + k for k in binds)})""", pk=part_key,
                              t=time.time() - TTL["extraction"], **binds)
            return {h: _j(o) for h, o in await cur.fetchall()}
        out = await self._run(fn, {})
        if out:
            self.stats["extraction_hits"] = self.stats.get("extraction_hits", 0) + len(out)
        return out

    async def put_extractions(self, rows: list[tuple[str, list[dict], str]], part_key: str) -> None:
        if not rows:
            return

        async def fn(conn):
            for c, offers, provider in rows:
                await upsert(conn, "pb_extraction", {"content_hash": c, "part_key": part_key},
                             {"offers": json.dumps(offers, ensure_ascii=False), "provider": provider[:60],
                              "created_at": time.time()})
            await conn.commit()
        await self._run(fn, timeout=WRITE_TIMEOUT_S)

    async def find_result(self, make: str, model: str, year: int | None, part: str, scope: str,
                          oem: str | None) -> tuple[list[dict], dict, str, float] | None:
        """Exact key first; else nearest stored searches for the same make/model/year/scope by embedding, accepted
        only if `same_part` agrees. Returns (offers, stats, matched_part_text, age_s)."""
        k = query_key(make, model, year, part, scope, oem)
        now = time.time()

        async def exact(conn):
            cur = conn.cursor()
            await cur.execute("SELECT offers, stats, part_text, created_at FROM pb_result WHERE k = :k AND created_at > :t",
                              k=k, t=now - TTL["result"])
            return await cur.fetchone()
        row = await self._run(exact)
        if not row and not oem:
            vec = await embed(f"{make} {model} {part}")
            if vec is not None:
                async def near(conn):
                    cur = conn.cursor()
                    await cur.execute("""SELECT offers, stats, part_text, created_at,
                            VECTOR_DISTANCE(emb, :v, COSINE) d FROM pb_result
                        WHERE mk = :mk AND mdl = :md AND NVL(yr, 0) = :yr AND scope = :sc AND created_at > :t
                        ORDER BY d FETCH FIRST 5 ROWS ONLY""",
                                      v=vec, mk=norm(make), md=norm(model), yr=year or 0, sc=scope, t=now - TTL["result"])
                    return await cur.fetchall()
                for cand in await self._run(near, []) or []:
                    if cand[4] is not None and cand[4] < 0.2 and same_part(part, cand[2]):
                        row = cand[:4]
                        break
        if not row:
            return None
        self._hit("result_hits")
        return _j(row[0]), _j(row[1]), row[2], now - float(row[3])

    async def put_result(self, make: str, model: str, year: int | None, part: str, scope: str, oem: str | None,
                         offers: list[dict], stats: dict) -> None:
        vec = await embed(f"{make} {model} {part}")

        async def fn(conn):
            await upsert(conn, "pb_result", {"k": query_key(make, model, year, part, scope, oem)},
                         {"mk": norm(make), "mdl": norm(model), "yr": year, "scope": scope, "part_text": part[:200],
                          "part_sig": part_signature(part)[:200], "offers": json.dumps(offers, ensure_ascii=False),
                          "stats": json.dumps(stats, ensure_ascii=False, default=str), "created_at": time.time(),
                          "emb": vec})
            await conn.commit()
        await self._run(fn, timeout=WRITE_TIMEOUT_S)

    async def known_sources(self, make: str, model: str, part: str, country: str, limit: int = 4) -> list[dict]:
        vec = await embed(f"{make} {model} {part}")
        if vec is None:
            return []
        sig = part_signature(part)

        async def fn(conn):
            cur = conn.cursor()
            await cur.execute("""SELECT url, vendor, title, part_sig, VECTOR_DISTANCE(emb, :v, COSINE) d FROM pb_source
                WHERE mk = :mk AND mdl = :md AND country = :cc AND last_verified > :t
                ORDER BY d FETCH FIRST 12 ROWS ONLY""", v=vec, mk=norm(make), md=norm(model), cc=country,
                              t=time.time() - TTL["source"])
            return await cur.fetchall()
        rows = await self._run(fn, []) or []
        out = [{"url": r[0], "title": r[2] or "", "snippet": f"Previously verified seller: {r[1]}", "known": True}
               for r in rows if r[3] == sig][:limit]
        if out:
            self.stats["source_hits"] = self.stats.get("source_hits", 0) + len(out)
        return out

    async def put_sources(self, make: str, model: str, part: str, offers: list[Any]) -> None:
        good = [o for o in offers if o.price is not None and o.url and o.vendor.country not in ("XX",)
                and getattr(o, "match_kind", "exact") == "exact"]
        if not good:
            return
        vec = await embed(f"{make} {model} {part}")
        sig = part_signature(part)

        async def fn(conn):
            seen = set()
            for o in good:
                h = sha1(o.url)
                if h in seen:
                    continue
                seen.add(h)
                cur = conn.cursor()
                await cur.execute("""UPDATE pb_source SET price = :p, currency = :c, last_verified = :t, hits = hits + 1
                    WHERE url_hash = :h""", {"p": o.price, "c": o.currency[:3], "t": time.time(), "h": h})
                if cur.rowcount == 0:
                    await upsert(conn, "pb_source", {"url_hash": h},
                                 {"url": o.url[:2000], "vendor": o.vendor.name[:200], "country": o.vendor.country,
                                  "mk": norm(make), "mdl": norm(model), "part_sig": sig[:200],
                                  "title": (o.title or "")[:400], "price": o.price, "currency": o.currency[:3],
                                  "last_verified": time.time(), "hits": 1, "emb": vec})
            await conn.commit()
        await self._run(fn, timeout=WRITE_TIMEOUT_S)

    async def ping(self) -> str:
        async def fn(conn):
            cur = conn.cursor()
            await cur.execute("SELECT banner_full FROM v$version FETCH FIRST 1 ROWS ONLY")
            r = await cur.fetchone()
            return r[0] if r else "connected"
        return await self._run(fn, "unreachable")


kb = KB()


async def _cli(cmd: str) -> None:
    from .dotenv import load_dotenv
    load_dotenv()
    if not KB.configured():
        raise SystemExit("ORACLE_DB_USER / ORACLE_DB_PASSWORD / ORACLE_DB_DSN are not set")
    if cmd == "init":
        for line in await kb.init_schema():
            print(line)
    elif cmd == "ping":
        print(await kb.ping())
    elif cmd == "purge":
        print(await kb.purge_expired())
    elif cmd == "warm-model":
        print("embedding dims:", len(await embed("warm up") or []))
    else:
        raise SystemExit("usage: python -m app.kb init|ping|purge|warm-model")


if __name__ == "__main__":
    import sys
    asyncio.run(_cli(sys.argv[1] if len(sys.argv) > 1 else "ping"))
