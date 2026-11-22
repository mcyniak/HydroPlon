"""Etap 5.2 — opłacalność bez fikcyjnych prognoz.

Zasady (§20.2, §3 części III):
  * brak zweryfikowanego efektu metody => brak prognozy plonu; liczymy PRÓG opłacalności,
  * ΔM = ΔY × P − ΔK_z; korzyść netto = ΔM + oszczędności − dodatkowy OPEX,
  * prosty okres zwrotu tylko dla dodatniej korzyści i KOMPLETNEGO CAPEX,
  * korzyści metod z tej samej grupy mechanizmu NIE są sumowane (bierzemy najwyższą, z ostrzeżeniem),
  * dotacja nie jest odejmowana od nakładów (brak potwierdzenia przyznania).
"""
from __future__ import annotations

import json
from functools import lru_cache

from app.config import DATA_DIR
from app.engine.ids import make_id
from app.engine.method_catalog import methods_by_id
from app.models.economics import CostEstimate, EconomicInputs, ProfitabilityResult
from app.models.methods import MethodSelectionResult

ENGINE_VERSION = "profitability-engine-0.1.0"


def _pl(x: float, nd: int = 0) -> str:
    """Liczba w zapisie polskim: spacja tysięcy, przecinek dziesiętny."""
    s = f"{x:,.{nd}f}"
    return s.replace(",", " ").replace(".", ",")


@lru_cache(maxsize=1)
def load_benchmarks() -> dict:
    return json.loads((DATA_DIR / "economic_benchmarks.fixture.json").read_text(encoding="utf-8"))


def combine_scenario_benefits(benefits: dict[str, float], selected: list[str]) -> tuple[float | None, list[str], dict]:
    """Łączy korzyści z jawnego scenariusza użytkownika bez sumowania nakładających się mechanizmów."""
    catalog = methods_by_id()
    groups: dict[str, list[tuple[str, float]]] = {}
    for mid, val in benefits.items():
        if mid not in selected:
            continue
        groups.setdefault(catalog[mid]["benefit_overlap_group"], []).append((mid, float(val)))
    warnings, used = [], {}
    total = 0.0
    for group, vals in sorted(groups.items()):
        best = max(vals, key=lambda x: (x[1], x[0]))
        used[group] = {"counted_method": best[0], "value_pln": best[1], "not_summed": [m for m, _ in vals if m != best[0]]}
        total += best[1]
        if len(vals) > 1:
            warnings.append(f"Metody {', '.join(m for m, _ in vals)} działają na ten sam mechanizm ({group}) — "
                            f"ich korzyści nie zostały zsumowane; przyjęto najwyższą deklarowaną wartość.")
    return (round(total, 2) if groups else None), warnings, used


def evaluate(cost: CostEstimate, selection: MethodSelectionResult, inputs: EconomicInputs,
             area_ha: float, horizon_years: int) -> ProfitabilityResult:
    bm = load_benchmarks()
    crop = bm["crops"].get(selection.crop_id)
    price = inputs.own_price_pln_t or (crop or {}).get("demo_price_pln_t")
    yield_t = inputs.own_yield_t_ha or (crop or {}).get("demo_yield_t_ha")
    area = inputs.cultivated_area_ha or area_ha
    horizon = inputs.payback_target_years or horizon_years
    assumptions: list[str] = []
    warnings: list[str] = []
    if inputs.own_price_pln_t is None and price is not None:
        assumptions.append("Cena produktu z SYNTETYCZNEGO benchmarku testowego (nie GUS/ZSRIR) — wpisz własną cenę.")
    if inputs.own_yield_t_ha is None and yield_t is not None:
        assumptions.append("Plon bazowy z SYNTETYCZNEGO benchmarku testowego — wpisz własny średni plon.")
    if price is None or yield_t is None:
        warnings.append("Brak ceny lub plonu bazowego dla tej uprawy — progu w t/ha nie wyznaczono.")
    if inputs.cultivated_area_ha is None:
        assumptions.append("Powierzchnia = cała działka ewidencyjna (do potwierdzenia).")
    capex = cost.totals.capex_known_net_pln or 0.0
    opex = cost.totals.opex_annual_known_net_pln or 0.0
    complete = cost.totals.capex_is_complete and cost.totals.opex_is_complete
    if not complete:
        warnings.append("Koszty są niekompletne — próg opłacalności jest DOLNĄ granicą (pozycje bez wyceny go podniosą).")
    required = round(capex / horizon + opex, 2) if (capex or opex) else None
    be: dict = {
        "horizon_years": horizon,
        "required_annual_benefit_pln": required,
        "is_lower_bound": not complete,
        "formula_pl": "wymagana roczna korzyść = CAPEX_znany / horyzont + OPEX_roczny_znany (bez dyskontowania)",
        "required_yield_increase_t_ha": None, "required_yield_increase_pct_of_baseline": None,
    }
    if required is not None and price and yield_t and area:
        dy = required / (price * area)
        be["required_yield_increase_t_ha"] = round(dy, 3)
        be["required_yield_increase_pct_of_baseline"] = round(100 * dy / yield_t, 1)
        be["interpretation_pl"] = (f"Aby nakłady zwróciły się w {horizon} lat, potrzebna jest dodatkowa korzyść co najmniej "
                                   f"{_pl(required)} zł/rok, czyli np. przyrost plonu o {_pl(dy, 2)} t/ha "
                                   f"({_pl(100 * dy / yield_t, 1)}% plonu bazowego) przy cenie {_pl(price)} zł/t — "
                                   "bez założenia, że metody taki przyrost dadzą.")
    selected = [m.method_id for m in selection.selected_methods if not m.blocked]
    scen_total, overlap_warnings, used = combine_scenario_benefits(inputs.scenario_annual_benefit_pln, selected)
    net = payback = None
    if scen_total is not None:
        kind = "scenario"
        net = round(scen_total - opex, 2)
        if net > 0 and cost.totals.capex_is_complete and capex > 0:
            payback = round(capex / net, 1)
        elif net <= 0:
            warnings.append("Korzyść netto w scenariuszu nie jest dodatnia — okres zwrotu nie jest wyznaczany.")
        else:
            warnings.append("CAPEX niekompletny — okres zwrotu nie jest wyznaczany.")
        headline = (f"Scenariusz użytkownika: korzyść netto {_pl(net)} zł/rok"
                    + (f", prosty okres zwrotu {_pl(payback, 1)} lat." if payback else " — okres zwrotu nieokreślony."))
        assumptions.append("Korzyści pochodzą z jawnego scenariusza użytkownika, nie z prognozy systemu.")
    elif required is not None:
        kind = "break_even_only"
        headline = ("Do oszacowania: brak zweryfikowanych danych o skuteczności metod. "
                    f"Próg opłacalności: co najmniej {_pl(required)} zł korzyści rocznie"
                    + (" (dolna granica — koszty niepełne)." if not complete else "."))
    else:
        kind = "insufficient_data"
        headline = "Do oszacowania: brak wycenionych kosztów i danych o efektach — opłacalności nie można obliczyć."
    baseline = {"crop_id": selection.crop_id, "yield_t_ha": yield_t, "price_pln_t": price, "area_ha": area,
                "baseline_revenue_pln": round(yield_t * price * area, 2) if (yield_t and price and area) else None,
                "source": "dane użytkownika" if inputs.own_yield_t_ha and inputs.own_price_pln_t else "benchmark syntetyczny/brak",
                "is_demonstration": not (inputs.own_yield_t_ha and inputs.own_price_pln_t)}
    pid = make_id("prof", {"cost": cost.cost_estimate_id, "inputs": inputs.model_dump(mode="json"), "area": area,
                           "horizon": horizon, "bm": bm["version"], "engine": ENGINE_VERSION})
    return ProfitabilityResult(
        profitability_id=pid, cost_estimate_id=cost.cost_estimate_id,
        status="ok" if kind == "scenario" and payback else ("partial" if kind != "insufficient_data" else "unavailable"),
        result_kind=kind, baseline=baseline,
        investment_variant={"capex_known_net_pln": cost.totals.capex_known_net_pln,
                            "opex_annual_known_net_pln": cost.totals.opex_annual_known_net_pln,
                            "costs_complete": complete, "subsidy_deducted": False,
                            "subsidy_note_pl": "Dotacji nie odjęto od nakładów — przyznanie nie jest potwierdzone."},
        annual_net_benefit_pln=net, simple_payback_years=payback, break_even=be,
        yield_effect={"status": "not_estimated", "documented_effects": bm["method_effects"]["documented_yield_effect_pct"],
                      "reason_pl": "Brak zweryfikowanego modelu i źródeł skuteczności metod — nie prognozujemy zmiany plonu.",
                      "scenario_benefits_used": used},
        overlap_warnings=overlap_warnings, assumptions=assumptions, warnings=warnings, headline_pl=headline,
        engine_version=ENGINE_VERSION)
