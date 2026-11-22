"""Etap 5 — FinalPlan (JSON) oraz jego renderowanie do tekstu i PDF.

Kolejność sekcji zgodna z częścią VIII (§57): 1. opłacalność, 2. koszty,
3. dofinansowania, 4. plan wdrożenia, 5. analiza działki i uprawy (aneks).
Tekst i PDF powstają z TEGO SAMEGO obiektu FinalPlan (ten sam plan_id), więc
nie generują niezależnych liczb.
"""
from __future__ import annotations

import unicodedata
from datetime import datetime
from pathlib import Path
from typing import Any

from app.config import APP_VERSION, TZ
from app.engine.ids import make_id
from app.models.analysis import AnalysisSnapshot
from app.models.crop import CropWaterAssessment
from app.models.economics import CostEstimate, FinalPlan, FundingMatchResult, ImplementationPlan, ProfitabilityResult
from app.models.methods import MethodRankingResponse, MethodSelectionResult

PLAN_VERSION = "final-plan-0.1.0"
STATUS_PL = {"recommended": "Rekomendowana", "conditional": "Warunkowa (wymaga weryfikacji)",
             "not_recommended": "Niezalecana w obecnych warunkach"}
COST_STATUS_PL = {"priced": "wycenione", "requires_quote": "do wyceny (brak ceny)",
                  "requires_sizing": "do ustalenia ilości", "requires_design": "wymaga projektu"}
CALL_PL = {"open": "otwarty", "closed": "zamknięty", "announced": "ogłoszony (przyszły)", "unknown": "nieznany"}
MATCH_PL = {"possible": "potencjalnie możliwe", "needs_verification": "wymaga weryfikacji", "not_eligible": "niekwalifikowalne"}



def _d(iso: str) -> str:
    """2027-03-15 -> 15.03.2027"""
    y, m, d = str(iso)[:10].split("-")
    return f"{d}.{m}.{y}"

def _module_summary(snapshot: AnalysisSnapshot) -> dict[str, Any]:
    m = snapshot.modules.model_dump(mode="json")
    w = m["weather"]
    return {
        "soil": {k: m["soil"].get(k) for k in ("status", "retention_class", "granulometry", "organic_matter",
                                                "water_holding_capacity_0_30cm")} | {"metadata": m["soil"]["metadata"]},
        "terrain": {k: m["terrain"].get(k) for k in ("status", "runoff_erosion_risk", "slope", "aspect", "microrelief")}
                   | {"metadata": m["terrain"]["metadata"]},
        "hydrology": {k: m["hydrology"].get(k) for k in ("status", "nearby_water_features", "flood_and_basin_context",
                                                          "farmer_confirmation_flags")} | {"metadata": m["hydrology"]["metadata"]},
        "weather": {k: w.get(k) for k in ("status", "analysis_mode", "climate_deficit_risk", "stations_used",
                                          "decade_summary_up_to_target", "target_date_season_snapshot", "annual_series")}
                   | {"monthly_series_last_24": (w.get("monthly_series") or [])[-24:], "metadata": w["metadata"]},
        "satellite": {k: m["satellite"].get(k) for k in ("status", "latest_valid_observation", "baseline_comparison",
                                                          "time_series")} | {"metadata": m["satellite"]["metadata"]},
    }


def build_final_plan(*, snapshot: AnalysisSnapshot, assessment: CropWaterAssessment, ranking: MethodRankingResponse,
                     selection: MethodSelectionResult, cost: CostEstimate, profitability: ProfitabilityResult,
                     funding: FundingMatchResult, implementation: ImplementationPlan) -> FinalPlan:
    ids = {"analysis_id": snapshot.analysis_id, "crop_assessment_id": assessment.crop_assessment_id,
           "ranking_run_id": ranking.ranking_run_id, "filter_run_id": ranking.filter_run_id,
           "selection_id": selection.selection_id, "cost_estimate_id": cost.cost_estimate_id,
           "profitability_id": profitability.profitability_id, "funding_match_id": funding.funding_match_id,
           "implementation_plan_id": implementation.implementation_plan_id}
    plan_id = make_id("plan", {"ids": ids, "version": PLAN_VERSION})
    versions = {**snapshot.source_versions, **snapshot.rules_versions,
                "crop_catalog": assessment.catalog_version, "crop_engine": assessment.engine_version,
                "method_catalog": ranking.method_catalog_version, "filter_rules": ranking.screening.rules_version,
                "ranking_rules": ranking.ranking_rules_version, "selection_rules": selection.selection_rules_version,
                "field_context_version": str(selection.field_context.field_context_version),
                "price_catalog": cost.price_catalog_version, "cost_engine": cost.engine_version,
                "profitability_engine": profitability.engine_version, "funding_catalog": funding.catalog_version,
                "funding_engine": funding.engine_version, "implementation_engine": implementation.engine_version,
                "plan_version": PLAN_VERSION, "app": APP_VERSION}
    warnings = list(dict.fromkeys(snapshot.warnings + selection.warnings + cost.warnings + profitability.warnings
                                  + funding.warnings + implementation.warnings))
    statuses = [cost.status, profitability.status, funding.status]
    overall = "error" if implementation.status == "error" else ("ok" if all(s == "ok" for s in statuses) and
                                                                 snapshot.status == "ok" else "partial")
    tc = snapshot.time_context
    return FinalPlan(
        plan_id=plan_id, plan_version=PLAN_VERSION, status=overall,
        generated_at=datetime.now(TZ).isoformat(timespec="seconds"), ids=ids, versions=versions,
        time_context=tc.model_dump(mode="json"),
        parcel_summary={"parcel_id": snapshot.parcel.parcel_id, "area_ha": snapshot.parcel.metrics.area_ha,
                        "is_multipart": snapshot.parcel.metrics.is_multipart,
                        "location": snapshot.parcel.administrative_location.full_label,
                        "geometry_version": snapshot.parcel.geometry_version, "notices": snapshot.parcel.notices,
                        "source": snapshot.parcel.source.model_dump()},
        crop_summary={"crop_id": assessment.crop_id, "name_pl": assessment.crop.get("name_pl"),
                      "parameter_status": assessment.crop.get("parameter_status"), "assessment_status": assessment.status,
                      "indicative_climatic_pressure": assessment.indicative_climatic_pressure},
        selected_methods=[{"method_id": m.method_id, "name_pl": m.name_pl, "status_pl": STATUS_PL[m.screening_status],
                           "screening_status": m.screening_status, "priority_class": m.priority_class,
                           "origin": m.origin} for m in selection.selected_methods],
        section_1_profitability=profitability.model_dump(mode="json"),
        section_2_costs=cost.model_dump(mode="json"),
        section_3_funding=funding.model_dump(mode="json"),
        section_4_implementation=implementation.model_dump(mode="json"),
        section_5_analysis={"module_statuses": snapshot.module_statuses, "modules": _module_summary(snapshot),
                            "diagnosis": snapshot.diagnosis.model_dump(mode="json"),
                            "crop_assessment": assessment.model_dump(mode="json"),
                            "method_screening": [s.model_dump(mode="json") for s in ranking.screening.methods],
                            "ranking": {g: [r.model_dump(mode="json") for r in getattr(ranking, g)]
                                        for g in ("recommended", "conditional", "not_recommended")},
                            "selection": selection.model_dump(mode="json")},
        warnings=warnings,
        data_provenance={"data_mode": snapshot.data_mode,
                         "is_demonstration": snapshot.data_mode == "fixture" or cost.price_catalog_is_demonstration,
                         "price_catalog_is_demonstration": cost.price_catalog_is_demonstration,
                         "funding_catalog_is_demonstration": funding.catalog_is_demonstration},
        disclaimers_pl=[
            "Raport orientacyjny: nie zastępuje projektu technicznego, doradztwa prawnego ani decyzji administracyjnych.",
            "Nie gwarantujemy zysku, wzrostu plonów ani przyznania dotacji.",
            "Saldo P−ET0/P−ETc jest wskaźnikiem modelu, nie oficjalnym Klimatycznym Bilansem Wodnym IUNG/SMSR.",
        ] + (["TRYB TESTOWY: dane działki, ceny i programy są SYNTETYCZNE."] if snapshot.data_mode == "fixture" else []))


# --------------------------------------------------------------------------- tekst
def _fmt(v: Any, unit: str = "") -> str:
    if v is None:
        return "brak danych"
    if isinstance(v, float):
        s = f"{v:,.2f}".replace(",", " ").replace(".", ",")
        return f"{s} {unit}".strip()
    return f"{v} {unit}".strip()


def render_blocks(plan: FinalPlan) -> list[tuple[str, str]]:
    b: list[tuple[str, str]] = []
    H, P, L = "h", "p", "li"
    tc, ps, cs = plan.time_context, plan.parcel_summary, plan.crop_summary
    b.append(("title", "HydroPlon — Twój plan gospodarowania wodą"))
    b.append((P, f"Działka: {ps['parcel_id']} ({_fmt(ps['area_ha'], 'ha')}; {ps.get('location') or 'lokalizacja nieznana'})"))
    b.append((P, f"Uprawa: {cs.get('name_pl')} ({cs['crop_id']}); data analizy: {tc['as_of_date']} "
                 f"(tryb: {tc['mode']}); wiedza do: {tc['knowledge_cutoff_date']}; wygenerowano: {plan.generated_at}"))
    modules = plan.section_5_analysis["modules"]
    imgw_date = (modules["weather"].get("metadata") or {}).get("last_observation_date")
    sentinel_date = (modules["satellite"].get("latest_valid_observation") or {}).get("acquisition_date")
    b.append((P, f"Ostatnia obserwacja IMGW w analizie: {_d(imgw_date) if imgw_date else 'brak danych'}."))
    b.append((P, f"Ostatnia użyteczna scena Sentinel-2: {_d(sentinel_date) if sentinel_date else 'brak danych'}."))
    b.append((P, f"Plan: {plan.plan_id}; status: {plan.status}"))
    for d in plan.disclaimers_pl:
        b.append((L, d))
    b.append((P, "Wybrane metody: " + "; ".join(f"{m['name_pl']} [{m['status_pl']}, priorytet {m['priority_class']}]"
                                                   for m in plan.selected_methods)))
    pr = plan.section_1_profitability
    b.append((H, "1. Opłacalność i potencjalne korzyści"))
    b.append((P, pr["headline_pl"]))
    be = pr["break_even"]
    if be.get("interpretation_pl"):
        b.append((L, be["interpretation_pl"]))
    b.append((L, f"Roczna korzyść netto: {_fmt(pr['annual_net_benefit_pln'], 'zł')}; prosty okres zwrotu: "
                 f"{_fmt(pr['simple_payback_years'], 'lat')}"))
    b.append((L, pr["yield_effect"]["reason_pl"]))
    for x in pr["overlap_warnings"] + pr["assumptions"]:
        b.append((L, x))
    c = plan.section_2_costs
    t = c["totals"]
    b.append((H, "2. Koszty wdrożenia"))
    b.append((P, f"CAPEX znany (netto): {_fmt(t['capex_known_net_pln'], 'zł')}; OPEX roczny znany (netto): "
                 f"{_fmt(t['opex_annual_known_net_pln'], 'zł')}; status sumy: {t['total_status']}"))
    b.append((P, t["explanation_pl"]))
    for i in c["items"]:
        amount = _fmt(i["total_net_pln"], "zł") if i["status"] == "priced" else COST_STATUS_PL[i["status"]]
        q = _fmt(i["quantity"], i["unit"]) if i["quantity"] is not None else f"? {i['unit']}"
        shared = " [wspólna]" if i["shared"] else ""
        b.append((L, f"{i['name_pl']}{shared} — {i['cost_type']}, {q} × {_fmt(i['unit_price_net_pln'], 'zł')} = {amount} "
                     f"(metody: {', '.join(i['method_ids'])})"))
    for a in c["assumptions"]:
        b.append((L, a))
    f = plan.section_3_funding
    b.append((H, "3. Możliwe dofinansowania"))
    if not f["matches"]:
        b.append((P, "Nie znaleziono dopasowanych programów w katalogu."))
    for m in f["matches"]:
        code = f"{m['code']} " if m.get("code") else ""
        b.append((L, f"{code}{m['program_name_pl']} — {MATCH_PL[m['match_status']]}; nabór w dniu analizy: "
                     f"{CALL_PL[m['call_status_on_as_of']]}, obecnie: {CALL_PL[m['call_status_now']]}; "
                     f"forma: {m['support_type']}. {' '.join(m['reasons_pl'])}"))
        for line in m.get("rates_pl", []) + m.get("estimate_pl", []):
            b.append((L, f"    {line}"))
        if m.get("how_to_apply_pl"):
            b.append((L, f"    Jak złożyć wniosek: {m['how_to_apply_pl']}"))
        if m.get("call_basis_pl"):
            b.append((L, f"    Termin: {m['call_basis_pl']}"))
        if m.get("source_urls"):
            b.append((L, f"    Źródła (stan na {m.get('verified_at')}): {', '.join(m['source_urls'])}"))
    for w in f["double_funding_warnings"] + f["warnings"]:
        b.append((L, w))
    im = plan.section_4_implementation
    b.append((H, "4. Plan wdrożenia"))
    if im["cycle_detected"]:
        b.append((P, "BŁĄD: wykryto cykl zależności — harmonogram nie został wygenerowany."))
    for task in im["tasks"]:
        dep = f" (po: {', '.join(d.split('-', 2)[1] for d in task['depends_on'])})" if task["depends_on"] else ""
        when = ""
        if task.get("window_from"):
            when = f" [najbliższy termin: {_d(task['window_from'])}–{_d(task['window_to'])}; {task['window_note_pl']}]"
        prog = f"; program: {', '.join(task['program_ids'])}" if task.get("program_ids") else ""
        b.append((L, f"[{task['order']:02d}] Faza {task['phase']} {task['phase_label_pl']}: {task['title_pl']} — "
                     f"{task['timing_pl']}{when}{dep}; metody: {', '.join(task['method_ids'])}{prog}; status: {task['status']}"))
    for s in im["stop_go_conditions"]:
        b.append((L, s))
    a = plan.section_5_analysis
    b.append((H, "5. Szczegóły analizy działki i uprawy"))
    b.append((P, "Statusy modułów: " + ", ".join(f"{k}: {v}" for k, v in a["module_statuses"].items())))
    d = a["diagnosis"]
    for p in d["problems"]:
        b.append((L, f"Problem {p['problem_id']}: nasilenie {p['severity']}, pewność {p['confidence']} — "
                     f"{p['reasons'][0]} Wskaźniki: {p['supporting_indicators']}"))
    for con in d["constraints"]:
        b.append((L, f"Ograniczenie {con['constraint_id']} ({con['status']}): {con['reason_pl']}"))
    for g in d.get("data_gaps", []):
        b.append((L, f"Luka danych — {g['module']}: nie ustalono {', '.join(g['cannot_determine'])}."))
    ca = a["crop_assessment"]
    cwb = ca.get("crop_water_balance") or {}
    b.append((P, f"Ocena uprawy: status {ca['status']}; saldo P−ETc w oknie krytycznym: "
                 f"{_fmt(cwb.get('indicative_critical_window_p_minus_etc_mm'), 'mm')}; sezon: "
                 f"{_fmt(cwb.get('indicative_season_p_minus_etc_mm'), 'mm')} (wskaźnik orientacyjny, nie KBW IUNG)."))
    crop = ca.get("crop") or {}
    if crop.get("parameter_basis_pl"):
        b.append((P, crop["parameter_basis_pl"]))
    for source in crop.get("parameter_sources") or []:
        b.append((L, f"{source['label']} · {source['url']}"))
    for s in a["method_screening"]:
        b.append((L, f"{s['name_pl']}: {STATUS_PL[s['screening_status']]} — {' '.join(s['explanation_pl'][:2])}"))
    for mod, info in a["modules"].items():
        for lim in (info.get("metadata") or {}).get("limitations", [])[:3]:
            b.append((L, f"[{mod}] {lim}"))
    b.append((H, "Wersje danych i reguł"))
    b.append((P, "; ".join(f"{k}={v}" for k, v in plan.versions.items())))
    if plan.warnings:
        b.append((H, "Ostrzeżenia"))
        for w in plan.warnings:
            b.append((L, w))
    return b


def render_text(plan: FinalPlan) -> str:
    out = []
    for kind, text in render_blocks(plan):
        out.append({"title": f"# {text}\n", "h": f"\n## {text}\n", "p": f"{text}\n", "li": f"- {text}"}[kind])
    return "\n".join(out) + "\n"


# --------------------------------------------------------------------------- PDF
_FONT_CANDIDATES = [
    ("C:/Windows/Fonts/arial.ttf", "C:/Windows/Fonts/arialbd.ttf"),
    ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
    ("/Library/Fonts/Arial Unicode.ttf", "/Library/Fonts/Arial Unicode.ttf"),
]


def _ascii(text: str) -> str:
    text = text.replace("ł", "l").replace("Ł", "L").replace("−", "-").replace("—", "-").replace("→", "->")
    return unicodedata.normalize("NFKD", text).encode("latin-1", "ignore").decode("latin-1")


def render_pdf(plan: FinalPlan) -> bytes:
    from fpdf import FPDF
    pdf = FPDF(format="A4")
    pdf.set_auto_page_break(auto=True, margin=15)
    regular = bold = None
    for r_path, b_path in _FONT_CANDIDATES:
        if Path(r_path).exists():
            regular, bold = r_path, (b_path if Path(b_path).exists() else r_path)
            break
    if regular:
        pdf.add_font("Body", "", regular)
        pdf.add_font("Body", "B", bold)
        family, conv = "Body", (lambda s: s.replace("−", "-"))
    else:
        family, conv = "Helvetica", _ascii
    pdf.add_page()
    width = pdf.w - pdf.l_margin - pdf.r_margin
    for kind, text in render_blocks(plan):
        if kind == "title":
            style, size, line_height = "B", 15, 8
        elif kind == "h":
            if pdf.will_page_break(14):
                pdf.add_page()
            else:
                pdf.ln(2)
            style, size, line_height = "B", 12, 7
        else:
            style, size, line_height = "", 10, 5
        pdf.set_font(family, style, size)
        prefix = "• " if kind == "li" else ""
        pdf.multi_cell(width, line_height, conv(prefix + text), align="L", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font(family, "", 8)
    footer = f"plan_id={plan.plan_id} | wygenerowano {plan.generated_at} | HydroPlon {APP_VERSION}"
    pdf.multi_cell(width, 4, conv(footer), new_x="LMARGIN", new_y="NEXT")
    return bytes(pdf.output())
