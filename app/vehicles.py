"""Pakistani vehicle naming: assembler / brand aliases and model generations.

Sellers in Pakistan list parts by generation range ("Corolla 2002-2008", "City 2009-2021 Aspire"), and buyers often
type the local assembler's name ("Corolla Indus" = Toyota Corolla assembled by Indus Motor). Searching the literal
text "Corolla Indus 2002" misses most listings, so queries use the canonical make/model plus the generation range.
"""
from __future__ import annotations

import re

MAKE_ALIASES = {
    "indus": "Toyota", "toyota indus": "Toyota", "indus motor": "Toyota", "indus motors": "Toyota",
    "pak suzuki": "Suzuki", "paksuzuki": "Suzuki", "honda atlas": "Honda", "atlas honda": "Honda",
    "kia lucky": "Kia", "lucky kia": "Kia", "hyundai nishat": "Hyundai", "nishat hyundai": "Hyundai",
    "mercedes": "Mercedes-Benz", "merc": "Mercedes-Benz", "benz": "Mercedes-Benz", "vw": "Volkswagen",
    "range rover": "Land Rover", "chevy": "Chevrolet", "mg motors": "MG", "sazgar": "Haval",
}
# words that name the assembler or a trim, not the model ("Corolla Indus", "Civic Oriel", "City Aspire")
MODEL_NOISE = {"indus", "atlas", "pak", "lucky", "nishat", "pakistan", "local", "japanese", "imported"}
MODEL_ALIASES = {"mehran vx": "Mehran", "mehran vxr": "Mehran", "cultus vxr": "Cultus", "cultus vxl": "Cultus",
                 "wagonr": "Wagon R", "wagon-r": "Wagon R", "corolla gli": "Corolla", "corolla xli": "Corolla",
                 "corolla altis": "Corolla", "corolla grande": "Corolla", "civic oriel": "Civic", "civic reborn": "Civic",
                 "civic rebirth": "Civic", "city aspire": "City", "vitz": "Vitz", "crv": "CR-V", "hrv": "HR-V", "brv": "BR-V"}

# (make, model) -> [(first_year, last_year, local nickname)] for the Pakistani market
GENERATIONS = {
    ("toyota", "corolla"): [(1995, 2001, "Indus Corolla 8th gen"), (2002, 2008, "9th gen"), (2009, 2014, "10th gen"),
                            (2015, 2025, "11th gen")],
    ("honda", "civic"): [(1996, 2000, "6th gen"), (2001, 2005, "7th gen"), (2006, 2012, "Reborn"),
                         (2013, 2016, "Rebirth"), (2016, 2021, "X 10th gen"), (2022, 2026, "11th gen")],
    ("honda", "city"): [(1997, 2002, ""), (2003, 2008, ""), (2009, 2021, "Aspire"), (2021, 2026, "")],
    ("suzuki", "cultus"): [(2000, 2016, "old Cultus"), (2017, 2026, "new Cultus")],
    ("suzuki", "mehran"): [(1989, 2019, "")],
    ("suzuki", "alto"): [(2000, 2012, "old Alto"), (2019, 2026, "660cc VXR")],
    ("suzuki", "wagon r"): [(2014, 2026, "")],
    ("suzuki", "swift"): [(2010, 2021, ""), (2022, 2026, "")],
    ("suzuki", "bolan"): [(1988, 2026, "")],
    ("daihatsu", "cuore"): [(2000, 2012, "")],
    ("toyota", "yaris"): [(2020, 2026, "")],
    ("toyota", "vitz"): [(1999, 2004, ""), (2005, 2010, ""), (2011, 2019, "")],
    ("toyota", "prado"): [(2002, 2009, "120 series"), (2009, 2023, "150 series")],
    ("toyota", "hilux"): [(2005, 2015, "Vigo"), (2016, 2026, "Revo")],
    ("honda", "accord"): [(2003, 2007, ""), (2008, 2012, "")],
    ("kia", "sportage"): [(2019, 2026, "")],
    ("hyundai", "tucson"): [(2020, 2026, "")],
}


def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip())


def normalize(make: str, model: str, year: int | None) -> dict:
    """Returns {make, model, generation: (start, end, nickname)|None, note: str|None (what was reinterpreted)}."""
    mk_in, md_in = _clean(make), _clean(model)
    mk = MAKE_ALIASES.get(mk_in.lower(), mk_in)
    md = md_in
    # "Corolla Indus" / "Indus Corolla": drop assembler words from the model
    words = [w for w in md.split() if w.lower() not in MODEL_NOISE]
    if words and len(words) < len(md.split()):
        md = " ".join(words)
    # make typed into model or model typed into make ("Corolla" as make, "Indus" as model)
    if mk.lower() in ("corolla", "civic", "city", "mehran", "cultus", "alto") and (not md or md.lower() in MODEL_NOISE):
        md, mk = mk, {"corolla": "Toyota"}.get(mk.lower(), "Honda" if mk.lower() in ("civic", "city") else "Suzuki")
    md = MODEL_ALIASES.get(md.lower(), md)
    if md.islower() and (mk.lower(), md) in GENERATIONS:
        md = md.title()
    gen = None
    if year:
        for start, end, nick in GENERATIONS.get((mk.lower(), md.lower()), []):
            if start <= year <= end:
                gen = (start, end, nick)
                break
    changed = (mk.lower(), md.lower()) != (mk_in.lower(), md_in.lower())
    note = None
    if changed:
        note = f"Reading “{mk_in} {md_in}” as {mk} {md}"
        if "indus" in f"{mk_in} {md_in}".lower():
            note += " (assembled in Pakistan by Indus Motor)"
    return {"make": mk, "model": md, "generation": gen, "note": note}
