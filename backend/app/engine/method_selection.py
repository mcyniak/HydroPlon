"""Etap 4, moduł 4 — wybór wielu metod, relacje par, wspólne zakresy i wymagane ilości.

Zasady (§38): domyślnie zaznaczone są metody ``recommended`` danego rankingu; ręczne
korekty są zachowane; ``hard_conflict`` tylko przy potwierdzonych faktach; wspólne
zakresy są SYGNALIZOWANE (deduplikacja kosztów należy do cost_engine); nieznane
ilości pozostają null (bez formularza dla rolnika).
"""
from __future__ import annotations

from typing import Any

from app.engine.facts import TRUE
from app.engine.ids import make_id
from app.engine.method_catalog import load_pair_rules, methods_by_id
from app.models.methods import (
    METHOD_IDS,
    MethodRankingResponse,
    MethodRelation,
    MethodSelectionResult,
    QuantityRequirement,
    SelectedMethod,
)
from app.models.parcel import ParcelProfile
from app.services.errors import InputValidationError

SELECTION_RULES_VERSION = "selection-rules-0.1.0"


def resolve_quantities(method: dict, parcel: ParcelProfile, user_quantities: dict[str, dict[str, float]],
                       cultivated_area_ha: float | None = None) -> list[QuantityRequirement]:
    out = []
    user = user_quantities.get(method["method_id"], {})
    for q in method["quantity_parameters"]:
        kind = q["quantity_kind"]
        if kind in user:
            value = float(user[kind])
            if value <= 0:
                raise InputValidationError(f"Ilość {kind} dla {method['method_id']} musi być dodatnia.")
            out.append(QuantityRequirement(method_id=method["method_id"], quantity_kind=kind, unit=q["unit"],
                                           quantity_value=value, sizing_status="known", quantity_source="user_input",
                                           reliability="deklaracja użytkownika", missing_inputs=[]))
        elif q["derivation"] == "parcel_area":
            if cultivated_area_ha:
                out.append(QuantityRequirement(method_id=method["method_id"], quantity_kind=kind, unit="ha",
                                               quantity_value=cultivated_area_ha, sizing_status="known",
                                               quantity_source="user_input:cultivated_area_ha",
                                               reliability="powierzchnia uprawna podana przez użytkownika",
                                               missing_inputs=[]))
            else:
                out.append(QuantityRequirement(
                    method_id=method["method_id"], quantity_kind=kind, unit="ha",
                    quantity_value=parcel.metrics.area_ha, sizing_status="derived",
                    quantity_source="parcel.metrics.area_ha (geometria ULDK)",
                    reliability="założenie: zabieg obejmie całą działkę ewidencyjną — do potwierdzenia (I15/K18)",
                    missing_inputs=["confirmed_work_area_ha"]))
        else:
            status = "requires_design" if q["derivation"] == "requires_design" else "unknown"
            out.append(QuantityRequirement(method_id=method["method_id"], quantity_kind=kind, unit=q["unit"],
                                           quantity_value=None, sizing_status=status, quantity_source="none",
                                           reliability="brak danych — nie szacujemy",
                                           missing_inputs=[q.get("missing_input", kind)]))
    return out


def _relations(selected: list[str], facts: dict[str, Any]) -> list[MethodRelation]:
    sel = set(selected)
    out = []
    for rule in load_pair_rules()["rules"]:
        a, b = rule["methods"]
        if a not in sel or b not in sel:
            continue
        rtype = rule["relation_type"]
        escalated = None
        esc = rule.get("escalate_to_hard_if") or []
        if rtype == "conditional_conflict" and esc and all(facts.get(f) == TRUE for f in esc):
            rtype, escalated = "hard_conflict", "conditional_conflict"
        out.append(MethodRelation(method_a=a, method_b=b, relation_type=rtype, reason_code=rule["reason_code"],
                                  explanation_pl=rule["explanation_pl"],
                                  required_verifications=rule["required_verifications"],
                                  shared_scope_keys=rule["shared_scope_keys"], escalated_from=escalated))
    return out


def select_methods(ranking: MethodRankingResponse, parcel: ParcelProfile,
                   selected_method_ids: list[str] | None = None,
                   user_quantities: dict[str, dict[str, float]] | None = None,
                   cultivated_area_ha: float | None = None) -> MethodSelectionResult:
    user_quantities = user_quantities or {}
    all_ranked = {m.method_id: m for m in ranking.all_methods()}
    default = [m.method_id for m in ranking.recommended]
    if selected_method_ids is None:
        chosen = list(default)
    else:
        unknown = [m for m in selected_method_ids if m not in METHOD_IDS]
        if unknown:
            raise InputValidationError(f"Nieznane identyfikatory metod: {unknown}")
        if len(set(selected_method_ids)) != len(selected_method_ids):
            raise InputValidationError("Lista metod zawiera powtórzone identyfikatory.")
        chosen = list(selected_method_ids)
    for mid in user_quantities:
        if mid not in METHOD_IDS:
            raise InputValidationError(f"Ilości podano dla nieznanej metody: {mid}")
    catalog = methods_by_id()
    chosen.sort(key=lambda m: all_ranked[m].order)
    facts = ranking.screening.facts
    screening = {s.method_id: s for s in ranking.screening.methods}
    selected = [SelectedMethod(method_id=m, name_pl=all_ranked[m].name_pl,
                               screening_status=all_ranked[m].screening_status,
                               priority_class=all_ranked[m].priority_class,
                               origin="default_recommended" if m in default else "manual_added",
                               blocked=all_ranked[m].screening_status == "not_recommended") for m in chosen]
    relations = _relations(chosen, facts)
    shared: dict[str, list[str]] = {}
    for m in chosen:
        for key in catalog[m]["shared_scope_keys"]:
            shared.setdefault(key, []).append(m)
    shared = {k: v for k, v in sorted(shared.items()) if len(v) > 1}
    quantities = [q for m in chosen for q in resolve_quantities(catalog[m], parcel, user_quantities, cultivated_area_ha)]
    verifications = []
    for m in chosen:
        for v in screening[m].required_verifications:
            verifications.append({"verification_id": v, "method_id": m, "source": "method_filter"})
    for rel in relations:
        for v in rel.required_verifications:
            verifications.append({"verification_id": v, "method_id": f"{rel.method_a}+{rel.method_b}",
                                  "source": f"relation:{rel.relation_type}"})
    warnings = []
    if not chosen:
        status = "empty"
        warnings.append("Nie wybrano żadnej metody — plan nie zostanie utworzony.")
    elif any(s.blocked for s in selected) or any(r.relation_type == "hard_conflict" for r in relations):
        status = "blocked"
        warnings.extend(f"Metoda {s.method_id} ma potwierdzone przeciwwskazanie (niezalecana) — nie może trafić do "
                        "planu realizacyjnego." for s in selected if s.blocked)
    elif (any(s.screening_status == "conditional" for s in selected)
          or any(r.relation_type in ("conditional_conflict", "dependency") for r in relations)
          or any(q.sizing_status in ("unknown", "requires_design") for q in quantities)):
        status = "conditional"
    else:
        status = "ready"
    if status == "ready":
        warnings.append("Status 'ready' oznacza wstępną spójność zestawu, nie gwarancję wykonalności technicznej.")
    if any(q.sizing_status == "derived" for q in quantities):
        warnings.append("Powierzchnię zabiegów przyjęto równą powierzchni działki ewidencyjnej — do potwierdzenia.")
    deselected = [m for m in default if m not in chosen]
    if deselected:
        warnings.append("Odznaczono metody rekomendowane: " + ", ".join(deselected) + " (wybór użytkownika zachowany).")
    sid = make_id("sel", {"analysis_id": ranking.analysis_id, "ranking_run_id": ranking.ranking_run_id,
                          "selected": chosen, "user_quantities": user_quantities,
                          "cultivated_area_ha": cultivated_area_ha, "rules": SELECTION_RULES_VERSION})
    return MethodSelectionResult(
        selection_id=sid, analysis_id=ranking.analysis_id, ranking_run_id=ranking.ranking_run_id,
        crop_id=ranking.crop_id, crop_assessment_id=ranking.crop_assessment_id, field_context=ranking.field_context,
        status=status, selected_methods=selected, deselected_recommended=deselected, method_relations=relations,
        shared_scope_keys=shared, quantity_requirements=quantities, required_verifications=verifications,
        warnings=warnings, selection_rules_version=SELECTION_RULES_VERSION, user_quantities=user_quantities)
