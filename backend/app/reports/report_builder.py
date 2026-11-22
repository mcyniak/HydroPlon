"""Plan końcowy i renderowanie wspólnej diagnozy wodnej, tekstu oraz PDF."""
from __future__ import annotations

import unicodedata
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from fpdf import FPDF

from app.config import APP_VERSION, TZ
from app.engine.ids import make_id
from app.models.analysis import AnalysisSnapshot
from app.models.crop import CropWaterAssessment
from app.models.economics import CostEstimate, FinalPlan, FundingMatchResult, ImplementationPlan, ProfitabilityResult
from app.models.methods import MethodRankingResponse, MethodSelectionResult
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
    for section in (state, causes):
        b.append(("phase", section["title"]))
        b.extend((L, entry["text"]) for entry in section["entries"])
    b.append((H, recommendations["title"]))
    for entry in recommendations["entries"]:
        kind, label = ("action", f"**{entry['label']}**") if concise else (L, entry["label"])
        b.append((kind, f"{label}: {entry['text']}"))
    if not concise:
        priorities = {"high": "wysoki", "medium": "średni", "low": "niski", "unassessed": "nieustalony"}
        b.append((P, "Wybrane metody: " + "; ".join(
            f"{m['name_pl']} ({m['status_pl'].lower()}, priorytet {priorities[m['priority_class']]})"
            for m in plan.selected_methods
        )))
    pr = plan.section_1_profitability
    b.append((H, "1. Opłacalność i potencjalne korzyści"))
    b.append((P, pr["headline_pl"]))
    be = pr["break_even"]
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
    b.append((H, "2. Koszty wdrożenia"))
    if concise:
        b.append((P, f"Wycenione netto: jednorazowo {_fmt(t['capex_known_net_pln'], 'zł')} · "
                     f"rocznie {_fmt(t['opex_annual_known_net_pln'], 'zł')}."))
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
    b.append((H, "3. Możliwe dofinansowania"))
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
    b.append((H, "4. Plan wdrożenia"))
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
    b.append(("phase", sources["title"]))
    for observation in water_report["dates"][1:]:
        b.append((L, f"{observation['label']}: {observation['text']}."))
    b.extend((L, f"{entry['label']}: {entry['text']}") for entry in sources["entries"])
    b.append(("phase", limitations["title"]))
    b.extend((L, f"{entry['label']}: {entry['text']}" if entry['label'] else entry["text"])
             for entry in limitations["entries"])
    status = {"ok": "kompletny", "partial": "częściowy", "unavailable": "dane niedostępne",
              "unknown": "nieustalony", "error": "błąd"}.get(plan.status, plan.status)
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


# --------------------------------------------------------------------------- PDF
_FONT_CANDIDATES = [
    ("C:/Windows/Fonts/arial.ttf", "C:/Windows/Fonts/arialbd.ttf"),
    ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
    ("/Library/Fonts/Arial Unicode.ttf", "/Library/Fonts/Arial Unicode.ttf"),
]


def _ascii(text: str) -> str:
    text = text.replace("ł", "l").replace("Ł", "L").replace("−", "-").replace("—", "-").replace("→", "->")
    return unicodedata.normalize("NFKD", text).encode("latin-1", "ignore").decode("latin-1")


_INK = (24, 38, 56)
_MUTED = (65, 83, 103)
_GREEN = (20, 82, 59)
_GOLD = (186, 131, 13)
_RULE = (186, 203, 193)
_PANEL = (243, 247, 244)
_LOGO = Path(__file__).with_name("logo-hydroplon.png")


class _ReportPdf(FPDF):
    def __init__(self, plan: FinalPlan) -> None:
        super().__init__(format="A4")
        self.plan_id = plan.plan_id
        self.set_margins(18, 12, 18)
        self.set_auto_page_break(auto=True, margin=23)
        self.set_title("Plan gospodarowania wodą")
        self.set_author("HydroPlon")
        self.set_lang("pl-PL")
        self.alias_nb_pages()
        self.body_font = "Helvetica"
        for regular, bold in _FONT_CANDIDATES:
            if Path(regular).exists():
                self.add_font("Body", "", regular)
                self.add_font("Body", "B", bold if Path(bold).exists() else regular)
                self.body_font = "Body"
                break

    def doc_text(self, text: str) -> str:
        text = text.replace("−", "-").replace("–", "-").replace("—", "-")
        return _ascii(text) if self.body_font == "Helvetica" else text

    def header(self) -> None:
        self.image(_LOGO, x=self.l_margin - 3, y=8, w=20)
        self.set_xy(self.l_margin + 16, 13)
        self.set_font(self.body_font, "B", 11)
        self.set_text_color(*_GREEN)
        self.cell(36, 8, "HydroPlon")
        self.set_font(self.body_font, "", 9)
        self.set_text_color(*_MUTED)
        self.cell(self.epw - 52, 8, self.doc_text("Raport dla działki rolnej"), align="R")
        self.set_draw_color(*_RULE)
        self.line(self.l_margin, 27, self.w - self.r_margin, 27)
        self.set_y(33)

    def footer(self) -> None:
        self.set_y(-18)
        self.set_draw_color(*_RULE)
        self.line(self.l_margin, self.y, self.w - self.r_margin, self.y)
        self.ln(2)
        self.set_font(self.body_font, "", 8)
        self.set_text_color(*_MUTED)
        self.cell(self.epw - 34, 5, f"HydroPlon {APP_VERSION}  |  {self.plan_id}")
        self.cell(34, 5, f"Strona {self.page_no()} / {{nb}}", align="R")

    def write_section(self, title: str, *, overview: bool = False, space_needed: float = 24) -> None:
        if self.will_page_break(space_needed):
            self.add_page()
        else:
            self.ln(3)
        self.start_section(title)
        self.set_font(self.body_font, "B", 14 if overview else 13)
        self.set_text_color(*((255, 255, 255) if overview else _GREEN))
        self.set_fill_color(*(_GREEN if overview else _PANEL))
        y = self.y
        self.multi_cell(self.epw, 6.2, title, align="L", fill=True, padding=(2, 4), new_x="LMARGIN", new_y="NEXT")
        self.set_fill_color(*_GOLD)
        self.rect(self.l_margin, y, 1, self.y - y, "F")
        self.ln(2.5)

    def write_paragraph(self, kind: str, text: str) -> None:
        styles = {
            "p": ("", 10, 4.7, _INK, 1.8, 0),
            "li": ("", 10, 4.7, _INK, .9, 0),
            "phase": ("B", 11, 5.3, _GREEN, 1.6, 13),
            "action": ("", 10, 4.7, _INK, 2, 0),
            "task_compact": ("", 10, 4.7, _INK, 1.7, 0),
        }
        style, font_size, line_height, color, after, keep_with_next = styles[kind]
        indent = (10 if text.startswith("    ") else 5) if kind == "li" else 0
        text = text.strip()
        self.set_font(self.body_font, style, font_size)
        self.set_text_color(*color)
        self.set_x(self.l_margin + indent)
        width = self.epw - indent
        markdown = kind in ("action", "task_compact")
        height = self.multi_cell(width, line_height, text, align="L", markdown=markdown, dry_run=True, output="HEIGHT")
        if height <= self.page_break_trigger - 33 and self.will_page_break(height + after + keep_with_next):
            self.add_page()
            self.set_x(self.l_margin + indent)
        if indent:
            self.set_fill_color(*_GREEN)
            self.ellipse(self.x - 2.4, self.y + 2, .85, .85, style="F")
        self.multi_cell(width, line_height, text, align="L", markdown=markdown, new_x="LMARGIN", new_y="NEXT")
        self.ln(after)


def render_pdf(plan: FinalPlan) -> bytes:
    pdf = _ReportPdf(plan)
    pdf.add_page()
    previous_kind = None
    for kind, text in render_blocks(plan, concise=True):
        if previous_kind == "meta" and kind != "meta":
            pdf.ln(4)
        previous_kind = kind
        if kind != "source":
            text = pdf.doc_text(text)
        if kind == "title":
            pdf.set_font(pdf.body_font, "B", 25)
            pdf.set_text_color(*_INK)
            pdf.multi_cell(pdf.epw, 11, text, align="L", new_x="LMARGIN", new_y="NEXT")
            pdf.ln(4)
        elif kind == "meta":
            pdf.set_font(pdf.body_font, "", 10)
            pdf.set_text_color(*_INK)
            pdf.set_fill_color(*_PANEL)
            pdf.multi_cell(pdf.epw, 5, text, align="L", fill=True, padding=(.85, 4), new_x="LMARGIN", new_y="NEXT")
        elif kind in ("h", "overview"):
            pdf.write_section(text, overview=kind == "overview")
        elif kind == "appendix":
            pdf.write_section(text, space_needed=56)
        elif kind == "source":
            pdf.set_font(pdf.body_font, "", 9)
            pdf.set_text_color(0, 85, 165)
            label = pdf.doc_text(f"Źródło · {urlsplit(text).netloc}")
            pdf.multi_cell(pdf.epw, 4.5, label, link=text, align="L", new_x="LMARGIN", new_y="NEXT")
        else:
            pdf.write_paragraph(kind, text)
    return bytes(pdf.output())
