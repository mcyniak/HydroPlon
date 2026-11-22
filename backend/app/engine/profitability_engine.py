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
from app.services.errors import InputValidationError

ENGINE_VERSION = "profitability-engine-0.2.0"


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


def _estimate_scenario(gross: float, capex: float, opex: float, costs_complete: bool) -> dict:
    net = round(gross - opex, 2)
    payback = round(capex / net, 1) if net > 0 and capex > 0 and costs_complete else None
    return {
        "annual_gross_benefit_pln": gross,
        "annual_net_benefit_pln": net,
        "simple_payback_years": payback,
    }


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
    if price is None:
        warnings.append("Podaj cenę sprzedaży, aby wyrazić próg opłacalności w dodatkowych tonach na hektar.")
    if inputs.cultivated_area_ha is None:
        assumptions.append("Do obliczeń przyjęto powierzchnię całej działki ewidencyjnej.")
    capex = cost.totals.capex_known_net_pln or 0.0
    opex = cost.totals.opex_annual_known_net_pln or 0.0
    opex_complete = cost.totals.opex_is_complete
    complete = cost.totals.capex_is_complete and opex_complete
    if not complete:
        warnings.append("Próg opłacalności jest dolną granicą. Brakujące wyceny podniosą koszt planu.")
    required = round(capex / horizon + opex, 2) if (capex or opex) else None
    be: dict = {
        "horizon_years": horizon,
        "required_annual_benefit_pln": required,
        "is_lower_bound": not complete,
        "formula_pl": "wymagana roczna korzyść = CAPEX_znany / horyzont + OPEX_roczny_znany (bez dyskontowania)",
        "required_yield_increase_t_ha": None, "required_yield_increase_pct_of_baseline": None,
    }
    if required is not None and price and area:
        dy = required / (price * area)
        be["required_yield_increase_t_ha"] = round(dy, 3)
        if yield_t:
            be["required_yield_increase_pct_of_baseline"] = round(100 * dy / yield_t, 1)
        be["interpretation_pl"] = (f"Przy cenie {_pl(price)} zł/t próg odpowiada dodatkowym {_pl(dy, 2)} t/ha "
                                   "plonu rocznie.")
    sensitivity = [
        {"threshold_pct": pct, **_estimate_scenario(round(required * pct / 100, 2), capex, opex, complete)}
        for pct in (50, 100, 150)
    ] if required is not None and required > 0 else []
    if sensitivity:
        assumptions.append("Warianty zakładają roczne korzyści równe 50%, 100% i 150% progu opłacalności.")

    selected = [m.method_id for m in selection.selected_methods if not m.blocked]
    scen_total, overlap_warnings, used = combine_scenario_benefits(inputs.scenario_annual_benefit_pln, selected)
    yield_change = inputs.scenario_yield_change_t_ha
    savings = inputs.scenario_annual_savings_pln
    if yield_change is not None or savings is not None:
        if inputs.scenario_annual_benefit_pln:
            raise InputValidationError("Podaj założenia dla całego planu albo korzyści poszczególnych metod.")
        if yield_change and price is None:
            raise InputValidationError("Podaj cenę sprzedaży, aby wycenić przyjętą zmianę plonu.")
        scen_total = round((yield_change or 0) * (price or 0) * area + (savings or 0), 2)
        assumptions.append("Korzyść roczna to przyjęta zmiana plonu × cena × powierzchnia plus oszczędności.")
    net = payback = None
    if scen_total is not None:
        kind = "scenario"
        scenario = _estimate_scenario(scen_total, capex, opex, complete)
        net, payback = scenario["annual_net_benefit_pln"], scenario["simple_payback_years"]
        if net <= 0:
            warnings.append("Przy tej korzyści rocznej koszty utrzymania pochłaniają całą korzyść lub więcej.")
        elif not complete:
            warnings.append("Do wyliczenia okresu zwrotu potrzebny jest pełny kosztorys inwestycji i utrzymania.")
        headline = f"Twój scenariusz daje {_pl(scen_total)} zł rocznej korzyści przed kosztami planu."
        assumptions.append("Korzyści w tym wariancie wynikają z założeń użytkownika.")
    elif required is not None:
        kind = "break_even_only"
        headline = f"Taka dodatkowa korzyść roczna pokrywa {'znane ' if not complete else ''}koszty w ciągu {horizon} lat."
    else:
        kind = "insufficient_data"
        headline = "Dla wybranych metod potrzebna jest wycena, żeby obliczyć próg opłacalności."
    baseline = {"crop_id": selection.crop_id, "yield_t_ha": yield_t, "price_pln_t": price, "area_ha": area,
                "baseline_revenue_pln": round(yield_t * price * area, 2) if (yield_t and price and area) else None,
                "source": "dane użytkownika" if inputs.own_yield_t_ha and inputs.own_price_pln_t else "benchmark syntetyczny/brak",
                "is_demonstration": not (inputs.own_yield_t_ha and inputs.own_price_pln_t)}
    pid = make_id("prof", {"cost": cost.cost_estimate_id, "inputs": inputs.model_dump(mode="json"), "area": area,
                           "horizon": horizon, "bm": bm["version"], "engine": ENGINE_VERSION})
    return ProfitabilityResult(
        profitability_id=pid, cost_estimate_id=cost.cost_estimate_id,
        status="ok" if kind == "scenario" and complete else ("partial" if kind != "insufficient_data" else "unavailable"),
        result_kind=kind, baseline=baseline,
        investment_variant={"capex_known_net_pln": cost.totals.capex_known_net_pln,
                            "opex_annual_known_net_pln": cost.totals.opex_annual_known_net_pln,
                            "costs_complete": complete, "opex_is_complete": opex_complete,
                            "subsidy_deducted": False,
                            "subsidy_note_pl": "Dotacji nie odjęto od nakładów — przyznanie nie jest potwierdzone."},
        annual_net_benefit_pln=net, simple_payback_years=payback, break_even=be, sensitivity_cases=sensitivity,
        yield_effect={"status": "not_estimated", "documented_effects": bm["method_effects"]["documented_yield_effect_pct"],
                      "reason_pl": "Wyniki wariantów zależą od przyjętych założeń o korzyściach.",
                      "scenario_inputs": {"own_price_pln_t": inputs.own_price_pln_t,
                                          "scenario_yield_change_t_ha": yield_change,
                                          "scenario_annual_savings_pln": savings},
                      "scenario_benefits_used": used},
        overlap_warnings=overlap_warnings, assumptions=assumptions, warnings=warnings, headline_pl=headline,
        engine_version=ENGINE_VERSION)
