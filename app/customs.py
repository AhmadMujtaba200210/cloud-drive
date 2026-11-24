"""Pakistan import duty & tax estimator.

Cascade (standard Pakistan Customs computation):
    CIF            = item value + freight + insurance (1% if not declared)
    Assessable (AV)= CIF x (1 + landing charge 1%)
    CD, ACD, RD    = rate x AV
    Sales Tax      = 18% x (AV + CD + ACD + RD)
    Income Tax     = WHT% x (AV + CD + ACD + RD + ST)        (higher for non-filers)

Every rate is read from config/pk_customs.json and must be verified against the live tariff.
"""
from __future__ import annotations

from .config import load
from .models import CustomsBreakdown, DutyLine


def classify(part_name: str) -> tuple[str, dict]:
    """Keyword-based HS heading lookup. Longest keyword match wins so 'steering wheel' beats 'wheel'."""
    cfg = load("pk_customs")
    name = part_name.lower()
    best_code, best_len = cfg["default_hs"], 0
    for code, prof in cfg["hs_profiles"].items():
        for kw in prof["keywords"]:
            if kw in name and len(kw) > best_len:
                best_code, best_len = code, len(kw)
    return best_code, cfg["hs_profiles"][best_code]


def estimate(part_name: str, item_value_pkr: float, freight_pkr: float,
             channel: str, is_filer: bool, condition: str = "new") -> CustomsBreakdown:
    cfg = load("pk_customs")
    hs, prof = classify(part_name)
    warnings: list[str] = []
    if cfg.get("last_verified") is None:
        warnings.append("Duty rates are UNVERIFIED placeholders. Confirm with a customs agent or WeBOC before paying.")
    if prof.get("dangerous_goods"):
        warnings.append("This part may be classed as Dangerous Goods for air transport (e.g. airbag inflator / Li-ion). "
                        "Many sellers ship it only by sea or remove the module.")
    if "steering wheel" in part_name.lower():
        warnings.append("Steering wheels are often sold with the airbag removed. If the airbag is included it is "
                        "Dangerous Goods for air shipping; confirm with the seller.")
    if condition == "used":
        warnings.append("Used auto parts are restricted under the Import Policy Order; check before importing.")

    if item_value_pkr <= cfg["courier_de_minimis_pkr"] and channel == "courier":
        cmin, cmax = cfg["clearance_days"]["courier"]
        return CustomsBreakdown(
            hs_code=hs, hs_description=prof["desc"], channel=channel,
            declared_value_pkr=item_value_pkr, assessable_value_pkr=item_value_pkr,
            lines=[], total_duties_pkr=0.0, clearance_days_min=cmin, clearance_days_max=cmax,
            de_minimis_applied=True, rules=cfg["rules"], warnings=warnings,
            config_last_verified=cfg.get("last_verified"),
        )
    if channel == "courier" and item_value_pkr > cfg["courier_value_cap_pkr"]:
        warnings.append(f"Value exceeds PKR {cfg['courier_value_cap_pkr']:,}; courier may route this as formal cargo "
                        "(Goods Declaration + customs agent).")

    insurance = item_value_pkr * cfg["insurance_pct_if_undeclared"] / 100
    cif = item_value_pkr + freight_pkr + insurance
    av = cif * (1 + cfg["landing_charge_pct"] / 100)

    lines: list[DutyLine] = []

    def add(name: str, pct: float, base: float) -> float:
        amt = base * pct / 100
        lines.append(DutyLine(name=name, rate_pct=pct, base_pkr=round(base), amount_pkr=round(amt)))
        return amt

    cd = add("Customs Duty (CD)", prof["cd"], av)
    acd = add("Additional Customs Duty (ACD)", prof["acd"], av)
    rd = add("Regulatory Duty (RD)", prof["rd"], av)
    st = add("Sales Tax (ST)", cfg["sales_tax_pct"], av + cd + acd + rd)
    wht_pct = cfg["withholding_income_tax_pct"]["filer" if is_filer else "non_filer"]
    it = add("Withholding Income Tax" + ("" if is_filer else " (non-filer rate)"), wht_pct, av + cd + acd + rd + st)

    cmin, cmax = cfg["clearance_days"]["cargo" if channel == "cargo" else "courier"]
    return CustomsBreakdown(
        hs_code=hs, hs_description=prof["desc"], channel=channel,
        declared_value_pkr=round(item_value_pkr), assessable_value_pkr=round(av),
        lines=lines, total_duties_pkr=round(cd + acd + rd + st + it),
        clearance_days_min=cmin, clearance_days_max=cmax,
        rules=cfg["rules"], warnings=warnings, config_last_verified=cfg.get("last_verified"),
    )
