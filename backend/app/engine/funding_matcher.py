"""Etap 5.3 — dopasowanie programów wsparcia na wybraną datę (wersjonowanie bitemporalne).

Rozdzielone osie (§20.3): kwalifikowalność działania, kwalifikowalność beneficjenta,
status naboru na as_of_date, status naboru "teraz" (reference_today), stan wiedzy
(knowledge_cutoff_date). Nabór nieznany w granicy wiedzy => unknown, nie open.
Nabór przyszły ogłoszony => announced (nigdy open). Wynik nigdy nie oznacza przyznania.
"""
from __future__ import annotations

import json
from datetime import date
from functools import lru_cache

from app.config import DATA_DIR
from app.engine.ids import make_id
from app.models.analysis import TimeContext
from app.models.economics import BeneficiaryProfile, CostEstimate, FundingMatch, FundingMatchResult
from app.models.methods import MethodSelectionResult

ENGINE_VERSION = "funding-matcher-0.1.0"
DISCLAIMER = ("Wskazanie programu nie oznacza spełnienia warunków ani przyznania wsparcia. "
              "Program DEMONSTRACYJNY — nie istnieje w rzeczywistości.")


@lru_cache(maxsize=1)
def load_programs() -> dict:
    return json.loads((DATA_DIR / "funding_programs.fixture.json").read_text(encoding="utf-8"))


def _d(v: str | None) -> date | None:
    return date.fromisoformat(v) if v else None


def call_status(calls: list[dict], on: date, knowledge_cutoff: date) -> tuple[str, dict | None]:
    """Status naboru na dzień ``on`` według wiedzy dostępnej do ``knowledge_cutoff``."""
    known = [c for c in calls if _d(c["known_from"]) <= knowledge_cutoff
             and (c.get("known_to") is None or _d(c["known_to"]) >= knowledge_cutoff)]
    if not known:
        return "unknown", None
    for c in sorted(known, key=lambda c: c["valid_from"]):
        vf, vt = _d(c["valid_from"]), _d(c["valid_to"])
        if vf <= on <= vt:
            # Dzień w przyszłości względem granicy wiedzy: tylko "ogłoszony", nigdy "otwarty".
            return ("announced" if on > knowledge_cutoff else "open"), c
    # "Ogłoszony" w danym dniu = opublikowany najpóźniej tego dnia (i w granicy wiedzy), start po tym dniu.
    announced_ref = min(on, knowledge_cutoff)
    future = [c for c in known if _d(c["valid_from"]) > on and _d(c["known_from"]) <= announced_ref]
    if future:
        return "announced", min(future, key=lambda c: c["valid_from"])
    past = [c for c in known if _d(c["valid_to"]) < on]
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


def match_funding(selection: MethodSelectionResult, cost: CostEstimate, tc: TimeContext,
                  profile: BeneficiaryProfile) -> FundingMatchResult:
    catalog = load_programs()
    selected = [m.method_id for m in selection.selected_methods if not m.blocked]
    matches: list[FundingMatch] = []
    matched_methods: set[str] = set()
    for prog in catalog["programs"]:
        methods = [m for m in selected if m in prog["eligible_methods"]]
        if not methods:
            continue
        items = [i for i in cost.items if set(i.method_ids) & set(methods)
                 and i.cost_category in prog["eligible_cost_categories"]]
        action = "TRUE" if items or methods else "FALSE"
        benef, missing = _beneficiary(prog["beneficiary_requirements"], profile)
        st_asof, call = call_status(prog["calls"], tc.as_of_date, tc.knowledge_cutoff_date)
        st_now, call_now = call_status(prog["calls"], tc.reference_today, tc.knowledge_cutoff_date)
        reasons = [f"Program obejmuje metody: {', '.join(methods)}."]
        if benef == "FALSE":
            status = "not_eligible"
            reasons.append("Profil beneficjenta nie spełnia co najmniej jednego warunku programu.")
        elif benef == "TRUE" and st_asof == "open":
            status = "possible"
            reasons.append("Działanie i podane dane beneficjenta odpowiadają zakresowi programu; nabór otwarty w dniu analizy.")
        else:
            status = "needs_verification"
            if missing:
                reasons.append("Brak danych o beneficjencie: " + ", ".join(missing) + ".")
            if st_asof != "open":
                reasons.append(f"Status naboru w dniu analizy: {st_asof}.")
        historical = tc.mode == "historical"
        if historical and st_asof == "open" and st_now != "open":
            reasons.append(f"Nabór był otwarty w dniu analizy ({tc.as_of_date.isoformat()}), ale obecnie ma status "
                           f"'{st_now}' — nie można dziś złożyć wniosku w tamtym naborze.")
        if not prog["calls"]:
            reasons.append("Brak znanych naborów tego programu.")
        matched_methods.update(methods)
        matches.append(FundingMatch(
            program_id=prog["program_id"], program_name_pl=prog["name_pl"], institution=prog["institution"],
            support_type=prog["support_type"], method_ids=methods, cost_item_ids=[i.cost_item_id for i in items],
            action_eligibility=action, beneficiary_eligibility=benef, match_status=status,
            call_id=call["call_id"] if call else None, call_status_on_as_of=st_asof, call_status_now=st_now,
            call_window={"valid_from": call["valid_from"], "valid_to": call["valid_to"],
                         "known_from": call["known_from"]} if call else None,
            is_historical_view=historical, conditions_pl=prog["conditions_pl"], missing_data=missing,
            reasons_pl=reasons, official_source_url=prog["official_source_url"],
            verified_at=catalog["catalog_verified_at"], is_demonstration=catalog["is_demonstration"],
            disclaimer_pl=DISCLAIMER))
    # Ryzyko podwójnego finansowania tych samych kosztów
    double = []
    by_prog = {m.program_id: m for m in matches}
    for prog in catalog["programs"]:
        for other in prog.get("funds_same_costs_as", []):
            if prog["program_id"] in by_prog and other in by_prog and prog["program_id"] < other:
                common = sorted(set(by_prog[prog["program_id"]].cost_item_ids) & set(by_prog[other].cost_item_ids))
                if common:
                    double.append(f"Programy {prog['program_id']} i {other} mogą finansować te same koszty "
                                  f"({', '.join(common)}) — kwot nie sumowano; łączenie wymaga sprawdzenia zasad.")
    warnings = ["Katalog programów jest DEMONSTRACYJNY — nie zawiera rzeczywistych naborów."]
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
        catalog_is_demonstration=catalog["is_demonstration"], matches=matches,
        unmatched_method_ids=[m for m in selected if m not in matched_methods],
        double_funding_warnings=double, warnings=warnings, engine_version=ENGINE_VERSION)
