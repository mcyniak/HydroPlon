"""Etapy 3–4 — uprawy, filtr trójwartościowy, ranking, wybór; testy 8–21 specyfikacji."""
from __future__ import annotations

import pytest

from app.engine.crop_catalog import CANONICAL_CROP_IDS, crop_profiles, resolve_crop_id
from app.engine.method_catalog import load_methods
from app.engine.method_selection import _relations
from app.models.methods import METHOD_IDS, FieldContext
from app.services.errors import ConsistencyError, InputValidationError, NotFoundError
from conftest import HIST, PARCEL_A, PARCEL_B, PARCEL_C, chain, screening_of, status_of


# --- Katalog upraw -------------------------------------------------------------------------------------
def test_crop_catalog_eight_ids_three_assessable():
    profiles = crop_profiles()
    assert [p.crop_id for p in profiles] == list(CANONICAL_CROP_IDS)
    assert {p.crop_id for p in profiles if p.available_for_assessment} == {"maize_grain", "winter_wheat", "potato"}


def test_crop_aliases_and_unknown():
    assert resolve_crop_id("maize")[0] == "maize_grain"
    with pytest.raises(NotFoundError):
        resolve_crop_id("banana")


# --- 8. Różnica oceny dwóch upraw na tej samej działce ------------------------------------------------
def test_two_crops_same_parcel_differ_but_geodata_same(orch):
    snap = orch.analyze(PARCEL_A, HIST)
    maize = orch.assess(snap.analysis_id, "maize_grain")
    wheat = orch.assess(snap.analysis_id, "winter_wheat")
    assert maize.analysis_id == wheat.analysis_id == snap.analysis_id
    assert maize.crop_assessment_id != wheat.crop_assessment_id
    mb, wb = maize.crop_water_balance, wheat.crop_water_balance
    assert mb["indicative_critical_window_p_minus_etc_mm"] != wb["indicative_critical_window_p_minus_etc_mm"]
    assert {r["month"] for r in maize.monthly_crop_demand_chart} != {r["month"] for r in wheat.monthly_crop_demand_chart}
    assert maize.crop_water_risk_level == "unknown"
    assert mb["metric_type"] == "indicative_P_minus_ETc_not_IUNG_KBW"


# --- 9. Brak zweryfikowanych parametrów uprawy ---------------------------------------------------------
def test_pending_crop_selectable_but_not_calculated(orch):
    snap = orch.analyze(PARCEL_A, HIST)
    ca = orch.assess(snap.analysis_id, "sugar_beet")
    assert ca.status == "unavailable" and ca.assessable is False
    assert ca.crop_water_balance is None and ca.monthly_crop_demand_chart == []
    rk = orch.recommend(snap.analysis_id, ca.crop_assessment_id)
    assert all(m.crop_fit in ("unknown", "conflicts") for m in rk.all_methods())


# --- 10–12. Drenaż: Tak / Nie / Nie wiem --------------------------------------------------------------
def test_drainage_yes(orch):
    _, _, rk = chain(orch, PARCEL_A, "maize_grain", fc=FieldContext(subsurface_drainage_status="yes"))
    s = screening_of(rk, "controlled_drainage")
    assert s.screening_status == "conditional"
    assert "DRAINAGE_DECLARED_PRESENT" in s.reason_codes
    assert "verify_existing_drainage" not in s.required_verifications
    assert "verify_drainage_technical_state" in s.required_verifications


def test_drainage_no(orch):
    _, _, rk = chain(orch, PARCEL_A, "maize_grain", fc=FieldContext(subsurface_drainage_status="nie"))
    s = screening_of(rk, "controlled_drainage")
    assert s.screening_status == "not_recommended"
    assert "REQUIRES_EXISTING_DRAINAGE" in s.reason_codes


def test_drainage_unknown_is_conditional_not_contraindication(orch):
    _, _, rk = chain(orch, PARCEL_A, "maize_grain")  # domyślnie unknown
    s = screening_of(rk, "controlled_drainage")
    assert s.screening_status == "conditional"
    assert "verify_existing_drainage" in s.required_verifications
    ev = next(e for e in s.rule_evaluations if e.condition_id == "CD_DRAINAGE")
    assert ev.value == "UNKNOWN" and ev.outcome == "unknown"


# --- 13. Istniejący zbiornik = Tak; 14. Zastawka = Tak --------------------------------------------------
def test_existing_pond_declared(orch):
    fc = FieldContext(existing_retention_status="yes", existing_retention_types=["pond"])
    _, _, rk = chain(orch, PARCEL_A, "maize_grain", fc=fc)
    pond = screening_of(rk, "retention_pond")
    assert pond.screening_status == "conditional" and "EXISTING_POND_DECLARED" in pond.reason_codes
    assert any("nie potwierdza" in n for n in pond.existing_infrastructure_notes)
    irr = screening_of(rk, "efficient_irrigation")
    assert irr.screening_status == "conditional"  # deklaracja zbiornika ≠ prawo do poboru
    assert "verify_water_abstraction_rights" in irr.required_verifications


def test_existing_weir_declared(orch):
    fc = FieldContext(existing_retention_status="yes", existing_retention_types=["weir"])
    _, _, rk = chain(orch, PARCEL_A, "maize_grain", fc=fc)
    s = screening_of(rk, "runoff_slowing")
    assert s.screening_status == "conditional" and "EXISTING_WEIR_DECLARED" in s.reason_codes


def test_field_context_subtypes_cleared_when_not_yes():
    fc = FieldContext(existing_retention_status="unknown", existing_retention_types=["pond"])
    assert fc.existing_retention_types == []


# --- 15. Brak potwierdzonego prawa do poboru wody -------------------------------------------------------
def test_no_confirmed_water_right_blocks_unconditional_irrigation(orch):
    _, _, rk = chain(orch, PARCEL_A, "maize_grain")  # rów przy działce, prawo nieznane
    s = screening_of(rk, "efficient_irrigation")
    assert s.screening_status == "conditional"
    assert "WATER_ABSTRACTION_RIGHT_UNCONFIRMED" in s.reason_codes
    assert any("nie oznacza prawa do poboru" in n for n in s.existing_infrastructure_notes)


# --- 16. 11 metod dokładnie raz; 17. recommended domyślnie wybrane ---------------------------------------
def test_all_eleven_methods_exactly_once(orch):
    for pid in (PARCEL_A, PARCEL_B, PARCEL_C):
        _, _, rk = chain(orch, pid, "maize_grain")
        ids = [m.method_id for m in rk.all_methods()]
        assert sorted(ids) == sorted(METHOD_IDS) and len(ids) == 11
        assert [m.order for m in rk.all_methods()] == list(range(1, 12))


def test_recommended_selected_by_default(orch):
    snap, _, rk = chain(orch, PARCEL_A, "maize_grain")
    sel = orch.select(snap.analysis_id, rk.ranking_run_id)
    assert set(sel.selected_method_ids) == {m.method_id for m in rk.recommended}
    assert all(m.origin == "default_recommended" for m in sel.selected_methods)
    assert all(m.default_selected == (m.screening_status == "recommended") for m in rk.all_methods())


def test_recommendations_differ_between_parcels(orch):
    _, _, ra = chain(orch, PARCEL_A, "maize_grain")
    _, _, rb = chain(orch, PARCEL_B, "maize_grain")
    assert {m.method_id for m in ra.recommended} != {m.method_id for m in rb.recommended}
    assert status_of(ra, "contour_farming") == "recommended" and status_of(rb, "contour_farming") == "not_recommended"


def test_incomplete_data_parcel_gives_conditional_not_contraindications(orch):
    _, _, rk = chain(orch, PARCEL_C, "maize_grain")
    assert rk.recommended == []
    reasons = {c for s in rk.screening.methods for c in s.reason_codes}
    assert "NEED_UNKNOWN" in reasons
    assert all(m.priority_class == "unassessed" for m in rk.all_methods())


# --- 18. Zmiana uprawy zmienia ranking --------------------------------------------------------------------
def test_crop_change_changes_ranking(orch):
    snap = orch.analyze(PARCEL_A, HIST)
    maize = orch.recommend(snap.analysis_id, orch.assess(snap.analysis_id, "maize_grain").crop_assessment_id)
    grass = orch.recommend(snap.analysis_id, orch.assess(snap.analysis_id, "grassland").crop_assessment_id)
    assert maize.ranking_run_id != grass.ranking_run_id
    assert status_of(maize, "cover_crops") == "recommended" and status_of(grass, "cover_crops") == "not_recommended"
    pm = {m.method_id: m.priority_class for m in maize.all_methods()}
    pg = {m.method_id: m.priority_class for m in grass.all_methods()}
    assert pm != pg


def test_ranking_does_not_change_filter_status_and_has_no_costs(orch):
    _, _, rk = chain(orch, PARCEL_A, "maize_grain")
    by = {s.method_id: s.screening_status for s in rk.screening.methods}
    assert all(m.screening_status == by[m.method_id] for m in rk.all_methods())
    assert "cost" not in rk.model_dump_json().lower().replace("cost_", "")  # brak kryteriów kosztowych


# --- 19. Ręczna zmiana zachowana; 20. konflikt; 21. wspólny zakres ---------------------------------------
def test_manual_selection_preserved_and_validated(orch):
    snap, _, rk = chain(orch, PARCEL_A, "maize_grain")
    chosen = ["crop_residues", "reduced_tillage", "efficient_irrigation"]
    sel = orch.select(snap.analysis_id, rk.ranking_run_id, chosen)
    assert set(sel.selected_method_ids) == set(chosen)
    assert {m.method_id: m.origin for m in sel.selected_methods}["reduced_tillage"] == "manual_added"
    assert "cover_crops" in sel.deselected_recommended
    assert sel.status == "conditional"
    with pytest.raises(InputValidationError):
        orch.select(snap.analysis_id, rk.ranking_run_id, ["crop_residues", "crop_residues"])
    with pytest.raises(InputValidationError):
        orch.select(snap.analysis_id, rk.ranking_run_id, ["not_a_method"])


def test_conflict_between_methods_is_conditional_not_hard(orch):
    snap, _, rk = chain(orch, PARCEL_A, "maize_grain")
    sel = orch.select(snap.analysis_id, rk.ranking_run_id, ["vegetative_strips", "windbreaks"])
    rel = next(r for r in sel.method_relations if {r.method_a, r.method_b} == {"vegetative_strips", "windbreaks"})
    assert rel.relation_type == "conditional_conflict"
    assert sel.status == "conditional"


def test_hard_conflict_only_with_confirmed_facts():
    rels = _relations(["vegetative_strips", "windbreaks"], {"layout.same_strip_confirmed": "UNKNOWN"})
    assert rels[0].relation_type == "conditional_conflict"
    rels = _relations(["vegetative_strips", "windbreaks"], {"layout.same_strip_confirmed": "TRUE"})
    assert rels[0].relation_type == "hard_conflict" and rels[0].escalated_from == "conditional_conflict"


def test_shared_scope_signalled(orch):
    snap, _, rk = chain(orch, PARCEL_A, "maize_grain")
    sel = orch.select(snap.analysis_id, rk.ranking_run_id, ["organic_matter", "cover_crops"])
    assert set(sel.shared_scope_keys["soil_testing"]) == {"organic_matter", "cover_crops"}


def test_not_recommended_selection_is_blocked(orch):
    snap, _, rk = chain(orch, PARCEL_B, "winter_wheat", fc=FieldContext(subsurface_drainage_status="no"))
    sel = orch.select(snap.analysis_id, rk.ranking_run_id, ["crop_residues", "controlled_drainage"])
    assert sel.status == "blocked"


def test_unknown_quantities_stay_null(orch):
    snap, _, rk = chain(orch, PARCEL_A, "maize_grain")
    sel = orch.select(snap.analysis_id, rk.ranking_run_id, ["retention_pond", "crop_residues"])
    q = {(x.method_id, x.quantity_kind): x for x in sel.quantity_requirements}
    pond = q[("retention_pond", "pond_volume_m3")]
    assert pond.quantity_value is None and pond.sizing_status == "requires_design"
    area = q[("crop_residues", "work_area_ha")]
    assert area.sizing_status == "derived" and area.quantity_value == 4.8


# --- Spójność identyfikatorów ------------------------------------------------------------------------------
def test_mixing_analyses_is_rejected(orch):
    snap_a, ca_a, rk_a = chain(orch, PARCEL_A, "maize_grain")
    snap_b = orch.analyze(PARCEL_B, HIST)
    with pytest.raises(ConsistencyError):
        orch.recommend(snap_b.analysis_id, ca_a.crop_assessment_id)
    with pytest.raises(ConsistencyError):
        orch.select(snap_b.analysis_id, rk_a.ranking_run_id)


def test_method_catalog_versioned_and_valid():
    data = load_methods()
    assert data["version"] and data["status"] == "draft"
    assert len(data["methods"]) == 11
