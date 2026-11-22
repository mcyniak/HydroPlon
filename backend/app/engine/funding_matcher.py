"""Etap 5.3 — dopasowanie programów wsparcia na wybraną datę (wersjonowanie bitemporalne).

Rozdzielone osie (§20.3): kwalifikowalność działania, kwalifikowalność beneficjenta,
status naboru na as_of_date, status naboru "teraz" (reference_today), stan wiedzy
(knowledge_cutoff_date). Nabór nieznany w granicy wiedzy => unknown, nie open.
Nabór przyszły ogłoszony => announced (nigdy open). Wynik nigdy nie oznacza przyznania.

Katalogi:
* tryb live — ``funding_programs.json``: interwencje PS WPR 2023–2027 z oficjalnych dokumentów (źródła i data
  weryfikacji przy każdym programie),
* tryb fixture — ``funding_programs.fixture.json``: programy FIKCYJNE do testów mechanizmu.
"""
from __future__ import annotations

import json
from datetime import date
from functools import lru_cache

from app.config import DATA_DIR, settings
from app.engine.ids import make_id
from app.models.analysis import TimeContext
from app.models.economics import BeneficiaryProfile, CostEstimate, FundingMatch, FundingMatchResult
from app.models.methods import MethodSelectionResult

ENGINE_VERSION = "funding-matcher-0.2.0"
DISCLAIMER_REAL = ("Wskazanie programu nie oznacza spełnienia warunków ani przyznania wsparcia. Stawki i terminy "
                   "według dokumentów na dzień weryfikacji katalogu — przed złożeniem wniosku sprawdź komunikat ARiMR.")
DISCLAIMER_DEMO = ("Wskazanie programu nie oznacza spełnienia warunków ani przyznania wsparcia. "
                   "Program DEMONSTRACYJNY — nie istnieje w rzeczywistości.")
CATALOG_FILES = {"live": "funding_programs.json", "fixture": "funding_programs.fixture.json"}
REQUIREMENT_PL = {"has_producer_number": "numer identyfikacyjny producenta (ARiMR)",
                  "has_legal_title_to_parcel": "tytuł prawny do gruntu (własność lub posiadanie)",
                  "is_active_farmer": "status rolnika aktywnego zawodowo", "is_young_farmer": "status młodego rolnika"}
CALL_STATUS_PL = {"open": "otwarty", "closed": "zakończony", "announced": "zapowiedziany", "unknown": "nieogłoszony"}


@lru_cache(maxsize=4)
def _load(name: str) -> dict:
    return json.loads((DATA_DIR / name).read_text(encoding="utf-8"))


def load_programs(mode: str | None = None) -> dict:
    """Katalog właściwy dla trybu danych (live: dokumenty urzędowe; fixture: programy fikcyjne do testów)."""
    return _load(CATALOG_FILES.get(mode or settings.mode, CATALOG_FILES["fixture"]))


def catalog_for_version(version: str) -> dict:
    """Katalog, z którego powstało dopasowanie (po wersji) — plan nie może mieszać katalogów."""
    for name in dict.fromkeys(CATALOG_FILES.values()):
        cat = _load(name)
        if cat["version"] == version:
            return cat
    raise KeyError(f"Nieznana wersja katalogu programów: {version}")


def _d(v: str | None) -> date | None:
    return date.fromisoformat(v) if v else None


def _ends_before(c: dict, on: date) -> bool:
    vt = _d(c.get("valid_to"))
    return vt is not None and vt < on


def _covers(c: dict, on: date) -> bool:
    vt = _d(c.get("valid_to"))
    return _d(c["valid_from"]) <= on and (vt is None or on <= vt)  # valid_to = None: nabór ciągły


def call_status(calls: list[dict], on: date, knowledge_cutoff: date) -> tuple[str, dict | None]:
    """Status naboru na dzień ``on`` według wiedzy dostępnej do ``knowledge_cutoff``."""
    known = [c for c in calls if _d(c["known_from"]) <= knowledge_cutoff
             and (c.get("known_to") is None or _d(c["known_to"]) >= knowledge_cutoff)]
    if not known:
        return "unknown", None
    for c in sorted(known, key=lambda c: c["valid_from"]):
        if _covers(c, on):
            # Dzień w przyszłości względem granicy wiedzy: tylko "ogłoszony", nigdy "otwarty".
            return ("announced" if on > knowledge_cutoff else "open"), c
    # "Ogłoszony" w danym dniu = opublikowany najpóźniej tego dnia (i w granicy wiedzy), start po tym dniu.
    announced_ref = min(on, knowledge_cutoff)
    future = [c for c in known if _d(c["valid_from"]) > on and _d(c["known_from"]) <= announced_ref]
    if future:
        return "announced", min(future, key=lambda c: c["valid_from"])
    past = [c for c in known if _ends_before(c, on)]
    if past and on <= knowledge_cutoff:
        return "closed", max(past, key=lambda c: c["valid_to"])
    # Dzień przyszły bez ogłoszonego naboru: przyszłe nabory nie są znane => unknown (nie "closed", nie "open").
    return "unknown", None


def _beneficiary(reqs: list[str], profile: BeneficiaryProfile) -> tuple[str, list[str]]:
    vals = {r: getattr(profile, r, None) for r in reqs}
    missing = [r for r, v in vals.items() if v is None]
    if any(v is False for v in vals.values()):
        return "FALSE", missing
    return ("TRUE" if not missing else "UNKNOWN"), missing


def _pl(x: float, digits: int = 0) -> str:
    return f"{x:,.{digits}f}".replace(",", " ").replace(".", ",")


def _estimate(prog: dict, methods: list[str], area_ha: float | None) -> list[str]:
    """Orientacyjna kwota dla tej działki — tylko z danych katalogu i powierzchni; bez sumowania praktyk,
    bo łączenie praktyk na tej samej powierzchni ma wyjątki (sprawdzane w warunkach programu)."""
    est = prog.get("estimate") or {}
    kind = est.get("kind")
    out: list[str] = []
    if kind == "points_per_ha" and area_ha:
        for m in methods:
            det = (prog.get("method_details") or {}).get(m) or {}
            pts = det.get("points_per_ha")
            if pts:
                pln = pts * area_ha * est["pln_per_point"]
                out.append(f"{det['practice_pl']}: {pts} pkt/ha × {_pl(area_ha, 2)} ha ≈ {_pl(pln)} zł/rok "
                           f"(przy 1 pkt ≈ {est['pln_per_point']} zł).")
        if out:
            out.append("Kwoty orientacyjne dla tej działki; płatność wymaga minimum punktów liczonego od całego gospodarstwa.")
    elif kind == "eur_per_ha" and area_ha:
        out.append(f"Do ok. {_pl(est['eur_per_ha'] * area_ha, 2)} EUR/rok, jeśli cała działka ({_pl(area_ha, 2)} ha) "
                   "zostanie uznana za zalaną lub podtopioną; stawka szacunkowa.")
    elif kind == "pln_per_ha_unknown_area":
        out.append("Kwota zależy od powierzchni objętej metodą (pas, zadrzewienie) — nie jest znana z analizy działki.")
    return out


def match_funding(selection: MethodSelectionResult, cost: CostEstimate, tc: TimeContext,
                  profile: BeneficiaryProfile, mode: str | None = None) -> FundingMatchResult:
    catalog = load_programs(mode)
    demo = bool(catalog.get("is_demonstration"))
    selected = [m.method_id for m in selection.selected_methods if not m.blocked]
    crop_id = getattr(selection, "crop_id", None)
    area_ha = cost.totals.reference_area_ha
    matches: list[FundingMatch] = []
    matched_methods: set[str] = set()
    for prog in catalog["programs"]:
        methods = [m for m in selected if m in prog["eligible_methods"]]
        if not methods:
            continue
        if prog.get("crop_ids") and crop_id not in prog["crop_ids"]:
            continue  # np. retencjonowanie wody dotyczy tylko trwałych użytków zielonych
        items = [i for i in cost.items if set(i.method_ids) & set(methods)
                 and i.cost_category in prog["eligible_cost_categories"]]
        action = "TRUE" if items or methods else "FALSE"
        benef, missing = _beneficiary(prog["beneficiary_requirements"], profile)
        st_asof, call = call_status(prog["calls"], tc.as_of_date, tc.knowledge_cutoff_date)
        st_now, call_now = call_status(prog["calls"], tc.reference_today, tc.knowledge_cutoff_date)
        details = prog.get("method_details") or {}
        names = [details[m]["practice_pl"] if m in details else m for m in methods]
        reasons = [f"Program obejmuje: {'; '.join(names)}."]
        if benef == "FALSE":
            status = "not_eligible"
            reasons.append("Profil beneficjenta nie spełnia co najmniej jednego warunku programu.")
        elif benef == "TRUE" and st_asof == "open":
            status = "possible"
            reasons.append("Działanie i podane dane beneficjenta odpowiadają zakresowi programu; nabór otwarty w dniu analizy.")
        else:
            status = "needs_verification"
            if missing:
                reasons.append("Do potwierdzenia dane gospodarstwa: "
                               + ", ".join(REQUIREMENT_PL.get(x, x) for x in missing) + ".")
            if st_asof != "open":
                reasons.append(f"Nabór w dniu analizy: {CALL_STATUS_PL.get(st_asof, st_asof)}.")
        historical = tc.mode == "historical"
        if historical and st_asof == "open" and st_now != "open":
            reasons.append(f"Nabór był otwarty w dniu analizy ({tc.as_of_date.isoformat()}), ale obecnie ma status "
                           f"„{CALL_STATUS_PL.get(st_now, st_now)}” — nie można dziś złożyć wniosku w tamtym naborze.")
        if not prog["calls"]:
            reasons.append("Brak ogłoszonego naboru tego programu.")
        shown = call or call_now
        matched_methods.update(methods)
        matches.append(FundingMatch(
            program_id=prog["program_id"], program_name_pl=prog["name_pl"], institution=prog["institution"],
            support_type=prog["support_type"], method_ids=methods, cost_item_ids=[i.cost_item_id for i in items],
            action_eligibility=action, beneficiary_eligibility=benef, match_status=status,
            call_id=call["call_id"] if call else None, call_status_on_as_of=st_asof, call_status_now=st_now,
            call_window={"valid_from": shown["valid_from"], "valid_to": shown.get("valid_to"),
                         "known_from": shown["known_from"]} if shown else None,
            is_historical_view=historical, conditions_pl=prog["conditions_pl"], missing_data=missing,
            reasons_pl=reasons, official_source_url=prog["official_source_url"],
            verified_at=catalog["catalog_verified_at"], is_demonstration=demo,
            disclaimer_pl=DISCLAIMER_DEMO if demo else DISCLAIMER_REAL,
            code=prog.get("code"), rates_pl=prog.get("rates_pl", []), estimate_pl=_estimate(prog, methods, area_ha),
            method_practices_pl={m: details[m]["practice_pl"] for m in methods if m in details},
            how_to_apply_pl=prog.get("how_to_apply_pl"), call_basis_pl=(shown or {}).get("basis_pl"),
            source_urls=prog.get("source_urls", [])))
    # Ryzyko podwójnego finansowania tych samych kosztów
    double = []
    by_prog = {m.program_id: m for m in matches}
    for prog in catalog["programs"]:
        for other in prog.get("funds_same_costs_as", []):
            if prog["program_id"] in by_prog and other in by_prog and prog["program_id"] < other:
                common = sorted(set(by_prog[prog["program_id"]].cost_item_ids) & set(by_prog[other].cost_item_ids))
                names = {i.cost_item_id: i.name_pl for i in cost.items}
                common = [names.get(c, c) for c in common]
                if common:
                    double.append(f"Programy {prog.get('code') or prog['program_id']} i "
                                  f"{by_prog[other].code or other} mogą finansować te same koszty "
                                  f"({', '.join(common)}) — kwot nie sumowano; łączenie wymaga sprawdzenia zasad.")
    if demo:
        warnings = ["Katalog programów jest DEMONSTRACYJNY — nie zawiera rzeczywistych naborów."]
    else:
        warnings = [f"Katalog programów: interwencje PS WPR 2023–2027 według dokumentów ARiMR, CDR i MRiRW "
                    f"(stan na {catalog['catalog_verified_at']}). Stawki i terminy mogą się zmienić — przed złożeniem "
                    "wniosku sprawdź aktualny komunikat ARiMR."]
    if tc.mode == "future":
        warnings.append(f"Data przyszła: uwzględniono wyłącznie nabory znane do {tc.knowledge_cutoff_date.isoformat()}; "
                        "nieogłoszone nabory mają status 'unknown'.")
    fid = make_id("fund", {"selection": selection.selection_id, "cost": cost.cost_estimate_id,
                           "time": tc.model_dump(mode="json", exclude={"note_pl"}), "profile": profile.model_dump(),
                           "catalog": catalog["version"], "engine": ENGINE_VERSION})
    return FundingMatchResult(
        funding_match_id=fid, selection_id=selection.selection_id, as_of_date=tc.as_of_date.isoformat(),
        knowledge_cutoff_date=tc.knowledge_cutoff_date.isoformat(), reference_today=tc.reference_today.isoformat(),
        status="ok" if matches else "partial", catalog_version=catalog["version"],
        catalog_is_demonstration=demo, matches=matches,
        unmatched_method_ids=[m for m in selected if m not in matched_methods],
        double_funding_warnings=double, warnings=warnings, engine_version=ENGINE_VERSION)
