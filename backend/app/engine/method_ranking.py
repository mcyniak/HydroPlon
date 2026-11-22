"""Etap 4, moduł 3 — deterministyczny i wyjaśnialny ranking metod.

Nie zmienia statusu z filtra. Nie używa kosztów, ROI ani dotacji (I19 — brak
zależności cyklicznej z etapem 5). Bez wag procentowych. Tabela klas (§37.1):
  high       — bezpośrednia (primary) na problem o wysokim nasileniu, dane wystarczające
  medium     — bezpośrednia na umiarkowany lub pomocnicza (secondary) na wysoki
               (także bezpośrednia na wysoki przy niskiej pewności danych)
  low        — bezpośrednia na niski lub pomocnicza na umiarkowany/niski
  unassessed — brak powiązanego wykrytego problemu / braki krytyczne
Zgodność z uprawą (crop_fit): jeśli ocena uprawy wskazuje możliwe niedobory w
oknie krytycznym, a metoda dotyczy dostępności wody dla uprawy, klasa rośnie o
jeden poziom (reguła R-RANK-CROP-01) — tylko przy powiązaniu bezpośrednim (primary).
Gdy problem główny metody jest NIEZNANY (braki krytyczne danych), klasa = unassessed,
a powiązania pomocnicze są podane wyłącznie w uzasadnieniu (R-RANK-NEED-01).
"""
from __future__ import annotations

from app.engine.facts import TRUE, UNKNOWN
from app.engine.ids import make_id
from app.engine.method_catalog import load_methods, methods_by_id
from app.models.methods import MethodFilterResponse, MethodRankingResponse, RankedMethod

RANKING_RULES_VERSION = "ranking-rules-0.1.0"
_ORDER = {"high": 0, "medium": 1, "low": 2, "unassessed": 3}
_UP = {"low": "medium", "medium": "high", "high": "high", "unassessed": "unassessed"}
_LINK_ORDER = {"primary": 0, "secondary": 1, "none": 2}
_CONF_ORDER = {"high": 0, "medium": 1, "low": 2, "unknown": 3}


def _class_for(link: str, severity: str, confidence: str) -> str:
    if link == "primary":
        if severity == "high":
            return "high" if confidence in ("high", "medium") else "medium"
        return "medium" if severity == "moderate" else "low"
    if link == "secondary":
        return "medium" if severity == "high" else "low"
    return "unassessed"


def rank(filter_resp: MethodFilterResponse) -> MethodRankingResponse:
    facts = filter_resp.facts
    details = facts.get("_problem_details", {})
    catalog = methods_by_id()
    ranked: list[tuple[tuple, RankedMethod]] = []
    for s in filter_resp.methods:
        m = catalog[s.method_id]
        linked = []
        best, best_link, best_conf = "unassessed", "none", "unknown"
        for link in ("primary", "secondary"):
            for pid in m["target_problems"][link]:
                if facts.get(f"problem:{pid}") != TRUE:
                    continue
                d = details.get(pid, {"severity": "low", "confidence": "unknown"})
                cls = _class_for(link, d["severity"], d["confidence"])
                linked.append({"problem_id": pid, "link": link, "severity": d["severity"],
                               "confidence": d["confidence"], "class": cls})
                if _ORDER[cls] < _ORDER[best]:
                    best, best_link, best_conf = cls, link, d["confidence"]
        rationale = []
        if linked:
            rationale.append("Powiązania z problemami: " + "; ".join(
                f"{x['problem_id']} ({x['link']}, nasilenie {x['severity']}, pewność {x['confidence']})" for x in linked))
        else:
            rationale.append("Brak wykrytego problemu, na który metoda odpowiada — priorytet nieoceniony.")
        # zgodność z uprawą
        if facts.get("crop.is_arable") == "FALSE" and any(e.condition_id.endswith("_ARABLE") for e in s.rule_evaluations):
            crop_fit = "conflicts"
        elif facts.get("crop.assessed") != TRUE:
            crop_fit = "unknown"
            rationale.append("Ocena wodna uprawy niedostępna (parametry niezweryfikowane lub brak danych) — "
                             "zgodności z potrzebami uprawy nie oceniono.")
        elif m.get("crop_water_relevance") and facts.get("crop.climatic_pressure") == TRUE:
            crop_fit = "supports"
        else:
            crop_fit = "neutral"
        priority = best
        if s.need_status == "UNKNOWN":
            if best != "unassessed":
                rationale.append("Problem główny metody (" + ", ".join(m["target_problems"]["primary"])
                                 + ") nie może zostać oceniony z dostępnych danych — priorytet nieoceniony "
                                 "(R-RANK-NEED-01); powiązania pomocnicze podano informacyjnie.")
            priority = "unassessed"
        elif crop_fit == "supports" and best_link == "primary" and best != "unassessed":
            priority = _UP[best]
            rationale.append("Orientacyjne saldo P−ETc uprawy w oknie krytycznym jest ujemne, a metoda dotyczy "
                             "dostępności wody dla uprawy — priorytet podniesiony o jeden poziom (R-RANK-CROP-01).")
        if s.screening_status != "recommended":
            rationale.append({"conditional": "Status warunkowy z filtra — wymaga weryfikacji przed realizacją.",
                              "not_recommended": "Status 'niezalecana' z filtra — potwierdzona istotna niezgodność."}
                             [s.screening_status])
        key = (_ORDER[priority], _LINK_ORDER[best_link], _CONF_ORDER.get(best_conf, 3), m["catalog_order"], s.method_id)
        ranked.append((key, RankedMethod(
            method_id=s.method_id, name_pl=s.name_pl, screening_status=s.screening_status, priority_class=priority,
            order=0, link_type=best_link, linked_problems=linked, crop_fit=crop_fit, rationale_pl=rationale,
            default_selected=s.screening_status == "recommended")))
    groups: dict[str, list[RankedMethod]] = {"recommended": [], "conditional": [], "not_recommended": []}
    pos = 1
    for status in groups:
        for _, rm in sorted((x for x in ranked if x[1].screening_status == status), key=lambda x: x[0]):
            groups[status].append(rm.model_copy(update={"order": pos}))
            pos += 1
    rid = make_id("rk", {"filter_run_id": filter_resp.filter_run_id, "rules": RANKING_RULES_VERSION})
    return MethodRankingResponse(
        ranking_run_id=rid, analysis_id=filter_resp.analysis_id, crop_assessment_id=filter_resp.crop_assessment_id,
        crop_id=filter_resp.crop_id, field_context=filter_resp.field_context, filter_run_id=filter_resp.filter_run_id,
        ranking_rules_version=RANKING_RULES_VERSION, method_catalog_version=load_methods()["version"],
        recommended=groups["recommended"], conditional=groups["conditional"],
        not_recommended=groups["not_recommended"], screening=filter_resp)


__all__ = ["rank", "RANKING_RULES_VERSION", "UNKNOWN"]
