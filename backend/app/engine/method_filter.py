"""Etap 4, moduł 2 — deterministyczny filtr 11 metod w logice trójwartościowej.

Reguły (§36.2):
  * warunek hard: FALSE => not_recommended; UNKNOWN => conditional,
  * warunek conditional: FALSE lub UNKNOWN => conditional,
  * warunek advisory: tylko ostrzeżenie,
  * potrzeba (problem główny metody): TRUE => może być recommended;
    FALSE (brak wykrytego problemu) => conditional (to nie przeciwwskazanie);
    UNKNOWN => conditional.
Filtr nie liczy kosztów, korzyści ani dotacji.
"""
from __future__ import annotations

from typing import Any

from app.engine.facts import FALSE, TRUE, UNKNOWN, build_facts
from app.engine.ids import make_id
from app.engine.method_catalog import load_methods
from app.models.analysis import AnalysisSnapshot
from app.models.crop import CropWaterAssessment
from app.models.methods import FieldContext, MethodFilterResponse, MethodScreening, RuleEvaluation

FILTER_RULES_VERSION = "filter-rules-0.1.0"


def _need(method: dict, facts: dict) -> tuple[str, list[str]]:
    primary = method["target_problems"]["primary"]
    values = {p: facts.get(f"problem:{p}", UNKNOWN) for p in primary}
    matched = [p for p, v in values.items() if v == TRUE]
    if matched:
        return TRUE, matched
    if all(v == FALSE for v in values.values()):
        return FALSE, []
    return UNKNOWN, []


def screen_method(method: dict, facts: dict) -> MethodScreening:
    evaluations: list[RuleEvaluation] = []
    reason_codes: list[str] = []
    explanation: list[str] = []
    verifications: list[str] = []
    missing: list[str] = []
    warnings = list(method.get("warnings_pl", []))
    hard_fail = needs_verification = False
    for c in method["conditions"]:
        value = facts.get(c["fact"], UNKNOWN)
        ok = value == c["expect"]
        if c["severity"] == "advisory":
            outcome = "pass" if ok else ("warning" if value != UNKNOWN else "unknown")
            if outcome == "warning":
                warnings.append(c["explanation_pl"])
                reason_codes.append(c["reason_code"])
            elif outcome == "unknown" and c.get("verification_on_unknown"):
                verifications.append(c["verification_on_unknown"])
        elif ok:
            outcome = "pass"
        elif value == UNKNOWN:
            outcome = "unknown"
            needs_verification = True
            reason_codes.append(c["reason_code"])
            explanation.append(c["explanation_pl"])
            missing.append(c["fact"])
            if c.get("verification_on_unknown"):
                verifications.append(c["verification_on_unknown"])
        else:
            outcome = "fail"
            reason_codes.append(c["reason_code"])
            explanation.append(c["explanation_pl"])
            if c["severity"] == "hard":
                hard_fail = True
            else:
                needs_verification = True
                if c.get("verification_on_unknown"):
                    verifications.append(c["verification_on_unknown"])
        evaluations.append(RuleEvaluation(condition_id=c["condition_id"], fact=c["fact"], value=value,
                                          severity=c["severity"], outcome=outcome, reason_code=c["reason_code"],
                                          explanation_pl=c["explanation_pl"]))
    need, matched = _need(method, facts)
    if need == TRUE:
        explanation.insert(0, "Metoda odpowiada na wykryte problemy działki: " + ", ".join(matched) + ".")
    elif need == FALSE:
        reason_codes.append("NO_DIAGNOSED_NEED")
        explanation.append("Dane nie wskazują problemu, na który metoda odpowiada bezpośrednio — można ją "
                           "rozważyć, ale nie jest rekomendowana automatycznie.")
    else:
        reason_codes.append("NEED_UNKNOWN")
        explanation.append("Brak danych do oceny potrzeby zastosowania metody ("
                           + ", ".join(method["target_problems"]["primary"]) + ").")
        missing.extend(f"problem:{p}" for p in method["target_problems"]["primary"]
                       if facts.get(f"problem:{p}", UNKNOWN) == UNKNOWN)

    if hard_fail:
        status = "not_recommended"
    elif needs_verification or need != TRUE:
        status = "conditional"
    else:
        status = "recommended"

    notes = []
    mid = method["method_id"]
    if mid == "controlled_drainage":
        dp = facts.get("field.drainage_present")
        if dp == TRUE:
            reason_codes.append("DRAINAGE_DECLARED_PRESENT")
            notes.append("Rolnik zadeklarował istniejący drenaż — potrzebna ocena stanu technicznego, nie inwentaryzacja od zera.")
        elif dp == FALSE:
            notes.append("Rolnik zadeklarował brak drenażu podziemnego — nie można rekomendować regulacji istniejącej sieci.")
    if mid == "runoff_slowing" and facts.get("field.existing_weir_declared") == TRUE:
        reason_codes.append("EXISTING_WEIR_DECLARED")
        notes.append("Zadeklarowano zastawkę na rowie — najpierw ocena możliwości wykorzystania istniejącego urządzenia "
                     "(deklaracja nie potwierdza stanu technicznego ani uprawnień wodnoprawnych).")
    if mid in ("retention_pond", "efficient_irrigation") and facts.get("field.existing_pond_declared") == TRUE:
        reason_codes.append("EXISTING_POND_DECLARED")
        notes.append("Zadeklarowano istniejący zbiornik — plan sprawdza możliwość jego wykorzystania zamiast budowy nowego; "
                     "deklaracja nie potwierdza pojemności ani prawa do poboru wody.")
    if mid == "efficient_irrigation" and facts.get("hydrology.ditch_or_stream_adjacent") == TRUE:
        notes.append("Bliskość rowu/cieku nie oznacza prawa do poboru wody.")
    if mid == "retention_pond" and facts.get("terrain.depressions_flag") == TRUE:
        notes.append("NMT wskazuje lokalne obniżenia — to wyłącznie miejsce do oceny terenowej, nie projekt ani objętość zbiornika.")
    return MethodScreening(
        method_id=mid, name_pl=method["name_pl"], screening_status=status, need_status=need,
        matched_problem_ids=matched, reason_codes=list(dict.fromkeys(reason_codes)),
        explanation_pl=list(dict.fromkeys(explanation)), rule_evaluations=evaluations,
        evidence_status="estimated", missing_data=list(dict.fromkeys(missing)),
        required_verifications=list(dict.fromkeys(verifications)), warnings=list(dict.fromkeys(warnings)),
        existing_infrastructure_notes=notes)


def run_filter(snapshot: AnalysisSnapshot, assessment: CropWaterAssessment, fc: FieldContext) -> MethodFilterResponse:
    if assessment.analysis_id != snapshot.analysis_id:
        from app.services.errors import ConsistencyError
        raise ConsistencyError("Ocena uprawy pochodzi z innej analizy działki niż wskazana.",
                               details={"analysis_id": snapshot.analysis_id,
                                        "assessment_analysis_id": assessment.analysis_id})
    facts = build_facts(snapshot, assessment, fc)
    catalog = load_methods()
    screened = [screen_method(m, facts) for m in sorted(catalog["methods"], key=lambda m: m["catalog_order"])]
    public_facts: dict[str, Any] = {k: v for k, v in facts.items()}
    fid = make_id("flt", {"analysis_id": snapshot.analysis_id, "crop_assessment_id": assessment.crop_assessment_id,
                          "field_context": fc.model_dump(mode="json"), "catalog": catalog["version"],
                          "rules": FILTER_RULES_VERSION})
    return MethodFilterResponse(filter_run_id=fid, analysis_id=snapshot.analysis_id,
                                crop_assessment_id=assessment.crop_assessment_id, crop_id=assessment.crop_id,
                                field_context=fc, method_catalog_version=catalog["version"],
                                rules_version=FILTER_RULES_VERSION, facts=public_facts, methods=screened)
