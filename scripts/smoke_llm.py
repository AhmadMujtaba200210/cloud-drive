"""Live check: each LLM provider answers, and extraction on a synthetic page is grounded.
Run: python3 scripts/smoke_llm.py   (uses keys from .env; costs a few cents at most)"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.dotenv import load_dotenv  # noqa: E402

load_dotenv()
from app import llm  # noqa: E402
from app.models import PartQuery  # noqa: E402
from app.sources.web_deep_search import EXTRACT_SYSTEM, _extract_prompt, verify  # noqa: E402

PAGES = [
    {"url": "https://a.example/listing/1", "title": "Al Noor Auto Spare Parts - BMW",
     "markdown": "Al Noor Auto Spare Parts, Shop 12, Al Aweer, Dubai. Tel +971 4 320 1188.\n"
                 "BMW i7 G70 steering wheel M sport, used, genuine - AED 3,400. Ships worldwide.\n"
                 "BMW X5 G05 headlight - AED 2,100."},
    {"url": "https://b.example/dealer", "title": "Tokyo Parts Hub",
     "markdown": "Tokyo Parts Hub (Ota-ku, Tokyo). Exporters of used European parts. Contact us for prices on BMW 7 series parts."},
]


async def main():
    for p in llm.default_chain():
        try:
            _, used = await llm.complete_json("JSON only.", 'Return {"ok": true}', chain=[p], max_tokens=200)
            print(f"[ok]   {p.name:16s} {p.model}")
        except llm.LLMUnavailable as e:
            print(f"[fail] {p.name:16s} {p.model}: {str(e)[:120]}")
    q = PartQuery(make="BMW", model="i7", year=2024, part_name="steering wheel")
    out, used = await llm.complete_json(EXTRACT_SYSTEM, _extract_prompt(q, "AE", PAGES), max_tokens=4000)
    print("extraction provider:", used, "| raw offers:", len(out.get("offers", [])))
    pages = {p["url"]: p for p in PAGES}
    for r in out.get("offers", []):
        o = verify(r, pages, "AE", used, {"est_weight_kg": 3.5, "est_dims_cm": [45, 45, 20]})
        print("  kept" if o else "  DROPPED", r.get("vendor_name"), "|", r.get("price"), r.get("currency"),
              "->", (o.price, o.currency, o.vendor.phone) if o else "")


asyncio.run(main())
