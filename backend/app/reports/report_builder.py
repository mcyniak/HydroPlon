"""Plan końcowy i renderowanie wspólnej diagnozy wodnej, tekstu oraz PDF."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from app.config import APP_VERSION, TZ
from app.engine.ids import make_id
from app.models.analysis import AnalysisSnapshot
from app.models.crop import CropWaterAssessment
from app.models.economics import CostEstimate, FinalPlan, FundingMatchResult, ImplementationPlan, ProfitabilityResult
from app.models.methods import MethodRankingResponse, MethodSelectionResult
from app.reports.pdf_layout import render_layout
from app.reports.water_report import build_water_report

PLAN_VERSION = "final-plan-0.2.0"
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
    plan = FinalPlan(
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
    plan.section_5_analysis["water_report"] = build_water_report(plan)
    return plan


# --------------------------------------------------------------------------- tekst
def _fmt(v: Any, unit: str = "") -> str:
    if v is None:
        return "brak danych"
    if isinstance(v, float):
        s = f"{v:,.2f}".replace(",", " ").replace(".", ",")
        return f"{s} {unit}".strip()
    return f"{v} {unit}".strip()


def render_blocks(plan: FinalPlan, *, concise: bool = False) -> list[tuple[str, str]]:
    b: list[tuple[str, str]] = []
    H, P, L = "h", "p", "li"
    tc, ps, cs = plan.time_context, plan.parcel_summary, plan.crop_summary
    b.append(("title", "Plan gospodarowania wodą"))
    mode = {"historical": "historyczna", "current": "bieżąca", "future": "scenariuszowa"}.get(tc["mode"], tc["mode"])
    generated_at = datetime.fromisoformat(plan.generated_at).strftime("%d.%m.%Y, %H:%M")
    b.append(("meta", f"Działka: {ps['parcel_id']} · {_fmt(ps['area_ha'], 'ha')} · {ps.get('location') or 'lokalizacja nieznana'}"))
    b.append(("meta", f"Uprawa: {cs.get('name_pl') or cs['crop_id']}"))
    b.append(("meta", f"Data analizy: {_d(tc['as_of_date'])} · analiza {mode}"))
    analysis = plan.section_5_analysis
    water_report = analysis.get("water_report") or build_water_report(plan)
    state, causes, recommendations, sources, limitations = water_report["sections"]
    if plan.data_provenance["data_mode"] == "fixture":
        b.append(("meta", "Dane testowe · działka i obserwacje syntetyczne"))
    b.append(("overview", "Diagnoza wodna działki i uprawy"))
    if not concise:
        for section in (state, causes):
            b.append(("phase", section["title"]))
            b.extend((L, entry["text"]) for entry in section["entries"])
    b.append(("recommendations" if concise else H, recommendations["title"]))
    if not concise:
        b.extend((L, f"{entry['label']}: {entry['text']}") for entry in recommendations["entries"])
        priorities = {"high": "wysoki", "medium": "średni", "low": "niski", "unassessed": "nieustalony"}
        b.append((P, "Wybrane metody: " + "; ".join(
            f"{m['name_pl']} ({m['status_pl'].lower()}, priorytet {priorities[m['priority_class']]})"
            for m in plan.selected_methods
        )))
    pr = plan.section_1_profitability
    b.append(("profitability" if concise else H, "1. Opłacalność i potencjalne korzyści"))
    be = pr["break_even"]
    if concise:
        annual = pr["annual_net_benefit_pln"]
        threshold = be.get("required_annual_benefit_pln")
        if annual is not None:
            b.append(("metric", _fmt(annual, "zł/rok")))
        elif threshold is not None:
            lower = "od " if be["is_lower_bound"] else ""
            b.append(("metric", f"Próg {lower}{_fmt(threshold, 'zł/rok')}"))
        else:
            b.append(("metric", "Potrzebna wycena"))
    b.append((P, pr["headline_pl"]))
    if be.get("interpretation_pl") and not concise:
        b.append((L, be["interpretation_pl"]))
    if not concise or pr["annual_net_benefit_pln"] is not None:
        b.append((L, f"Roczna korzyść netto: {_fmt(pr['annual_net_benefit_pln'], 'zł')}; prosty okres zwrotu: "
                     f"{_fmt(pr['simple_payback_years'], 'lat')}"))
    if not concise or pr["annual_net_benefit_pln"] is None:
        b.append((L, pr["yield_effect"]["reason_pl"]))
    for x in pr["overlap_warnings"] + ([] if concise else pr["assumptions"]):
        b.append((L, x))
    c = plan.section_2_costs
    t = c["totals"]
    b.append(("costs" if concise else H, "2. Koszty wdrożenia"))
    if concise:
        capex = t["capex_known_net_pln"]
        b.append(("metric", _fmt(capex, "zł") + ("" if t["capex_is_complete"] else " +") if capex is not None
                  else "Do wyceny"))
        opex = t["opex_annual_known_net_pln"]
        b.append((P, f"Nakłady inwestycyjne netto. Utrzymanie rocznie: {_fmt(opex, 'zł')}."))
    else:
        b.append((P, f"CAPEX znany (netto): {_fmt(t['capex_known_net_pln'], 'zł')}; OPEX roczny znany (netto): "
                     f"{_fmt(t['opex_annual_known_net_pln'], 'zł')}; status sumy: {t['total_status']}"))
    b.append((P, t["explanation_pl"]))
    for i in c["items"]:
        amount = _fmt(i["total_net_pln"], "zł") if i["status"] == "priced" else COST_STATUS_PL[i["status"]]
        q = _fmt(i["quantity"], i["unit"]) if i["quantity"] is not None else f"? {i['unit']}"
        shared = " [wspólna]" if i["shared"] else ""
        if concise:
            cost_type = "jednorazowo" if i["cost_type"] == "CAPEX" else "rocznie"
            quantity = q if i["quantity"] is not None else "ilość do ustalenia"
            b.append((L, f"{i['name_pl']}{shared} · {cost_type} · {quantity} · {amount}"))
        else:
            b.append((L, f"{i['name_pl']}{shared} — {i['cost_type']}, {q} × {_fmt(i['unit_price_net_pln'], 'zł')} = {amount} "
                         f"(metody: {', '.join(i['method_ids'])})"))
    if not concise:
        b.extend((L, assumption) for assumption in c["assumptions"])
    f = plan.section_3_funding
    b.append(("funding" if concise else H, "3. Możliwe dofinansowania"))
    if not f["matches"]:
        b.append((P, "Nie znaleziono dopasowanych programów w katalogu."))
    for m in f["matches"]:
        code = f"{m['code']} " if m.get("code") else ""
        name = f"{code}{m['program_name_pl']} — {MATCH_PL[m['match_status']]}"
        call = CALL_PL[m["call_status_now"]]
        if concise:
            b.append(("phase", name))
            b.append((P, f"Nabór: {call}"))
        else:
            b.append((L, f"{name}; nabór w dniu analizy: {CALL_PL[m['call_status_on_as_of']]}, obecnie: {call}; "
                         f"forma: {m['support_type']}. {' '.join(m['reasons_pl'])}"))
        for line in m.get("rates_pl", []) + m.get("estimate_pl", []):
            b.append((L, f"    {line}"))
        if not concise and m.get("how_to_apply_pl"):
            b.append((L, f"    Jak złożyć wniosek: {m['how_to_apply_pl']}"))
        if m.get("call_basis_pl"):
            b.append((L, f"    Termin: {m['call_basis_pl']}"))
        if concise:
            b.extend(("source", url) for url in m.get("source_urls") or [])
        elif m.get("source_urls"):
            b.append((L, f"    Źródła (stan na {m.get('verified_at')}): {', '.join(m['source_urls'])}"))
    for w in f["double_funding_warnings"] + f["warnings"]:
        b.append((L, w))
    im = plan.section_4_implementation
    b.append(("implementation" if concise else H, "4. Plan wdrożenia"))
    if concise:
        upcoming = sorted((task for task in im["tasks"] if task.get("window_from")),
                          key=lambda task: task["window_from"])[:3]
        for task in upcoming:
            start = _d(task["window_from"])
            when = f"{start} – {_d(task['window_to'])}" if task.get("window_to") else f"od {start}"
            b.append(("deadline", f"{when} · {task['title_pl']}"))
    if im["cycle_detected"]:
        b.append((P, "BŁĄD: wykryto cykl zależności — harmonogram nie został wygenerowany."))
    phase = None
    task_statuses = {"ready_to_consider": "do rozważenia", "requires_verification": "wymaga weryfikacji"}
    for task in im["tasks"]:
        if task["phase"] != phase:
            phase = task["phase"]
            b.append(("phase", f"Faza {phase} · {task['phase_label_pl']}"))
        dep = f" (po: {', '.join(d.split('-', 2)[1] for d in task['depends_on'])})" if task["depends_on"] else ""
        when = ""
        if task.get("window_from"):
            when = f" [najbliższy termin: {_d(task['window_from'])}–{_d(task['window_to'])}; {task['window_note_pl']}]"
        prog = f"; program: {', '.join(task['program_ids'])}" if task.get("program_ids") else ""
        title = f"{task['order']:02d}. {task['title_pl']}"
        if concise:
            verification = " · wymaga weryfikacji" if task["status"] == "requires_verification" else ""
            b.append(("task_compact", f"**{title}** · {task['timing_pl']}{when}{dep}{verification}"))
        else:
            b.append(("task", title))
            b.append(("task_detail", f"{task['timing_pl']}{when}{dep}; metody: {', '.join(task['method_ids'])}"
                                     f"{prog}; status: {task_statuses.get(task['status'], task['status'])}"))
    for s in im["stop_go_conditions"]:
        b.append((L, s))
    b.append(("appendix" if concise else H, "5. Szczegóły analizy działki i uprawy"))
    b.append(("sources" if concise else "phase", sources["title"]))
    for observation in water_report["dates"][1:]:
        b.append((L, f"{observation['label']}: {observation['text']}."))
    b.extend((L, f"{entry['label']}: {entry['text']}") for entry in sources["entries"])
    b.append(("limitations" if concise else "phase", limitations["title"]))
    b.extend((L, f"{entry['label']}: {entry['text']}" if entry['label'] else entry["text"])
             for entry in limitations["entries"])
    status = {"ok": "kompletny", "partial": "częściowy", "unavailable": "dane niedostępne",
              "unknown": "nieustalony", "error": "błąd"}.get(plan.status, plan.status)
    if concise:
        b.append(("notes", ""))
    b.append((P, f"Wygenerowano: {generated_at} · status: {status} · plan: {plan.plan_id}"))
    b.extend((L, disclaimer) for disclaimer in plan.disclaimers_pl)
    if concise:
        shown = {text.strip() for _, text in b}
        warnings = [warning for warning in dict.fromkeys(plan.warnings) if warning not in shown]
        if warnings:
            b.append(("phase", "Istotne uwagi"))
            b.extend((L, warning) for warning in warnings)
        return b
    a = analysis
    b.append((P, "Statusy modułów: " + ", ".join(f"{k}: {v}" for k, v in a["module_statuses"].items())))
    d = a["diagnosis"]
    for p in d["problems"]:
        b.append((L, f"Problem {p['problem_id']}: nasilenie {p['severity']}, pewność {p['confidence']} — "
                     f"{p['reasons'][0]} Wskaźniki: {p['supporting_indicators']}"))
    for con in d["constraints"]:
        b.append((L, f"Ograniczenie {con['constraint_id']} ({con['status']}): {con['reason_pl']}"))
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
        out.append({"title": f"# {text}\n", "h": f"\n## {text}\n", "overview": f"\n## {text}\n", "phase": f"\n### {text}\n",
                    "task": f"- {text}", "task_detail": f"  {text}", "p": f"{text}\n",
                    "meta": f"{text}\n", "li": f"- {text}"}[kind])
    return "\n".join(out) + "\n"


def render_pdf(plan: FinalPlan) -> bytes:
    water_report = plan.section_5_analysis.get("water_report") or build_water_report(plan)
    return render_layout(plan, water_report, render_blocks(plan, concise=True))
