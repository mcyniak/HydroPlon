"""Etap 5 — koszty, opłacalność, dotacje, harmonogram; testy 21–29 specyfikacji."""
from __future__ import annotations

import copy
from datetime import date

import pytest

from app.engine.funding_matcher import call_status, load_programs
from app.engine.implementation_planner import build_plan, find_cycle, load_templates
from app.engine.profitability_engine import combine_scenario_benefits
from app.models.economics import BeneficiaryProfile, CostItem, EconomicInputs
from app.models.methods import FieldContext
from conftest import HIST, PARCEL_A, TODAY, chain


def _sel(orch, methods, fc=None, as_of=HIST, crop="maize_grain"):
    snap, _, rk = chain(orch, PARCEL_A, crop, as_of=as_of, fc=fc)
    return snap, rk, orch.select(snap.analysis_id, rk.ranking_run_id, methods)


# --- 21. Wspólny koszt dwóch metod — naliczony raz ----------------------------------------------------
def test_shared_soil_test_counted_once(orch):
    _, _, sel = _sel(orch, ["organic_matter", "cover_crops", "reduced_tillage"])
    ce = orch.costs(sel.selection_id)
    soil = [i for i in ce.items if i.component_id == "soil_testing_basic"]
    assert len(soil) == 1 and soil[0].shared
    assert set(soil[0].method_ids) == {"organic_matter", "cover_crops", "reduced_tillage"}
    assert ce.deduplicated_shared_items


def test_shared_hydro_assessment_counted_once(orch):
    _, _, sel = _sel(orch, ["runoff_slowing", "retention_pond"])
    ce = orch.costs(sel.selection_id)
    assert len([i for i in ce.items if i.component_id == "technical_assessment_hydro"]) == 1


# --- 22. Brak ceny; 23. brak ilości robót — nigdy 0 zł ---------------------------------------------------
def test_missing_price_is_requires_quote_not_zero(orch):
    _, _, sel = _sel(orch, ["reduced_tillage"])
    ce = orch.costs(sel.selection_id)
    eq = next(i for i in ce.items if i.component_id == "reduced_tillage_equipment")
    assert eq.status == "requires_quote" and eq.total_net_pln is None
    assert ce.totals.capex_is_complete is False and ce.totals.total_status == "partial"
    assert eq.cost_item_id in ce.totals.unpriced_item_ids


def test_missing_quantity_is_requires_sizing(orch):
    _, _, sel = _sel(orch, ["retention_pond", "vegetative_strips"])
    ce = orch.costs(sel.selection_id)
    pond = next(i for i in ce.items if i.component_id == "pond_earthworks")
    assert pond.quantity is None and pond.status == "requires_design" and pond.total_net_pln is None
    strip = next(i for i in ce.items if i.component_id == "strip_establishment")
    assert strip.unit_price_net_pln == 1.2 and strip.status == "requires_design"  # cena jest, ilości brak


def test_cost_item_model_rejects_fake_zero():
    with pytest.raises(ValueError):
        CostItem(cost_item_id="x", component_id="x", name_pl="x", method_ids=["a"], shared=False, cost_type="CAPEX",
                 cost_category="x", frequency="one_time", quantity=None, unit="szt.", quantity_status="unknown",
                 unit_price_net_pln=None, vat_rate=None, total_net_pln=0.0, total_gross_pln=0.0,
                 status="requires_sizing", price_source=None, price_as_of_date=None, is_demonstration=True)


def test_quantity_times_unit_price_and_user_override(orch):
    snap, rk, sel = _sel(orch, ["crop_residues"])
    ce = orch.costs(sel.selection_id)
    it = next(i for i in ce.items if i.component_id == "residue_chopping_pass")
    assert it.total_net_pln == pytest.approx(4.8 * 180.0) and it.quantity_status == "derived"
    sel2 = orch.select(snap.analysis_id, rk.ranking_run_id, ["crop_residues"],
                       user_quantities={"crop_residues": {"work_area_ha": 3.5}})
    it2 = next(i for i in orch.costs(sel2.selection_id).items if i.component_id == "residue_chopping_pass")
    assert it2.total_net_pln == pytest.approx(3.5 * 180.0) and it2.quantity_status == "known"


def test_existing_weir_inspected_instead_of_new_construction(orch):
    fc = FieldContext(existing_retention_status="yes", existing_retention_types=["weir"])
    _, _, sel = _sel(orch, ["runoff_slowing"], fc=fc)
    comps = {i.component_id for i in orch.costs(sel.selection_id).items}
    assert "existing_weir_inspection" in comps and "weir_structure" not in comps


# --- 24. Brak potwierdzonego wzrostu plonu; 25. próg opłacalności zamiast ROI ---------------------------
def test_no_yield_increase_and_break_even_instead_of_roi(orch):
    _, _, sel = _sel(orch, ["crop_residues", "cover_crops", "contour_farming"])
    ce = orch.costs(sel.selection_id)
    pr = orch.profitability(ce.cost_estimate_id)
    assert pr.result_kind == "break_even_only"
    assert pr.annual_net_benefit_pln is None and pr.simple_payback_years is None
    assert pr.yield_effect["status"] == "not_estimated"
    be = pr.break_even
    expected = round(ce.totals.capex_known_net_pln / 10 + ce.totals.opex_annual_known_net_pln, 2)
    assert be["required_annual_benefit_pln"] == pytest.approx(expected)
    assert be["required_yield_increase_t_ha"] == pytest.approx(expected / (700 * 4.8), rel=1e-3)
    assert [case["threshold_pct"] for case in pr.sensitivity_cases] == [50, 100, 150]
    middle = pr.sensitivity_cases[1]
    assert middle["annual_gross_benefit_pln"] == expected
    assert middle["annual_net_benefit_pln"] == pytest.approx(expected - ce.totals.opex_annual_known_net_pln)
    assert middle["simple_payback_years"] == pytest.approx(10)


def test_yield_and_savings_scenario_uses_area_and_keeps_partial_costs_visible(orch):
    _, _, sel = _sel(orch, ["crop_residues", "contour_farming", "vegetative_strips"], crop="sugar_beet")
    ce = orch.costs(sel.selection_id)
    inputs = EconomicInputs(own_price_pln_t=600, scenario_yield_change_t_ha=0.5, scenario_annual_savings_pln=250)
    plan = orch.plan(sel.selection_id, economic_inputs=inputs)
    result = plan.section_1_profitability
    assert result["result_kind"] == "scenario"
    assert result["annual_net_benefit_pln"] == pytest.approx(0.5 * 600 * 4.8 + 250 - ce.totals.opex_annual_known_net_pln)
    assert result["break_even"]["required_yield_increase_t_ha"] is not None
    assert result["break_even"]["required_yield_increase_pct_of_baseline"] is None
    assert result["break_even"]["is_lower_bound"]
    assert result["simple_payback_years"] is None
    assert all(case["simple_payback_years"] is None for case in result["sensitivity_cases"])


def test_scenario_payback_only_with_positive_benefit_and_complete_capex(orch):
    _, _, sel = _sel(orch, ["crop_residues", "contour_farming"])  # wszystkie pozycje wycenione
    ce = orch.costs(sel.selection_id)
    assert ce.totals.capex_is_complete
    ok = orch.profitability(ce.cost_estimate_id, EconomicInputs(scenario_annual_benefit_pln={"crop_residues": 2000}))
    assert ok.result_kind == "scenario" and ok.simple_payback_years == pytest.approx(900 / (2000 - 864), rel=1e-2)
    neg = orch.profitability(ce.cost_estimate_id, EconomicInputs(scenario_annual_benefit_pln={"crop_residues": 500}))
    assert neg.annual_net_benefit_pln < 0 and neg.simple_payback_years is None


def test_overlapping_benefits_not_summed():
    total, warnings, used = combine_scenario_benefits(
        {"crop_residues": 1000, "cover_crops": 800, "vegetative_strips": 300},
        ["crop_residues", "cover_crops", "vegetative_strips"])
    assert total == 1300.0  # max(1000, 800) w grupie soil_water_retention + 300
    assert warnings and "nie zostały zsumowane" in warnings[0]


# --- 26–28. Dotacje -----------------------------------------------------------------------------------------
def test_unknown_beneficiary_needs_verification_never_granted(orch):
    _, _, sel = _sel(orch, ["crop_residues", "cover_crops"], as_of=TODAY)
    ce = orch.costs(sel.selection_id)
    fm = orch.funding(sel.selection_id, ce.cost_estimate_id)
    soil = next(m for m in fm.matches if m.program_id == "DEMO-SOIL-ANNUAL")
    assert soil.call_status_on_as_of == "open" and soil.match_status == "needs_verification"
    assert soil.beneficiary_eligibility == "UNKNOWN" and "is_active_farmer" in soil.missing_data
    assert "nie oznacza" in soil.disclaimer_pl and soil.is_demonstration
    full = BeneficiaryProfile(is_active_farmer=True, has_producer_number=True)
    soil2 = next(m for m in orch.funding(sel.selection_id, ce.cost_estimate_id, full).matches
                 if m.program_id == "DEMO-SOIL-ANNUAL")
    assert soil2.match_status == "possible"
    no = BeneficiaryProfile(is_active_farmer=False)
    soil3 = next(m for m in orch.funding(sel.selection_id, ce.cost_estimate_id, no).matches
                 if m.program_id == "DEMO-SOIL-ANNUAL")
    assert soil3.match_status == "not_eligible"


def test_closed_call(orch):
    _, _, sel = _sel(orch, ["runoff_slowing"], as_of=TODAY)
    ce = orch.costs(sel.selection_id)
    m = next(x for x in orch.funding(sel.selection_id, ce.cost_estimate_id).matches if x.program_id == "DEMO-RET-INV")
    assert m.call_status_on_as_of == "closed" and m.match_status == "needs_verification"


def test_historical_call_vs_current_status(orch):
    _, _, sel = _sel(orch, ["runoff_slowing"], as_of=HIST)
    ce = orch.costs(sel.selection_id)
    m = next(x for x in orch.funding(sel.selection_id, ce.cost_estimate_id).matches if x.program_id == "DEMO-RET-INV")
    assert m.call_status_on_as_of == "open" and m.call_status_now == "closed"
    assert m.is_historical_view and any("nie można dziś złożyć" in r for r in m.reasons_pl)


def test_future_calls_announced_or_unknown_never_open():
    calls = load_programs()["programs"]
    trees = next(p for p in calls if p["program_id"] == "DEMO-TREES")["calls"]
    soil = next(p for p in calls if p["program_id"] == "DEMO-SOIL-ANNUAL")["calls"]
    assert call_status(trees, date(2027, 3, 1), TODAY)[0] == "announced"  # w oknie, ale po granicy wiedzy
    assert call_status(soil, date(2027, 6, 1), TODAY)[0] == "unknown"     # brak ogłoszonego naboru
    assert call_status(trees, date(2019, 6, 15), TODAY)[0] == "unknown"   # w 2019 nie był ogłoszony
    assert call_status([], date(2026, 6, 1), TODAY)[0] == "unknown"


def test_double_funding_warning_and_no_summing(orch):
    _, _, sel = _sel(orch, ["retention_pond", "efficient_irrigation"], as_of=TODAY)
    ce = orch.costs(sel.selection_id)
    fm = orch.funding(sel.selection_id, ce.cost_estimate_id)
    assert fm.double_funding_warnings and "nie sumowano" in fm.double_funding_warnings[0]
    assert not hasattr(fm, "total_funding_pln")


# --- 29. Cykl zależności; deduplikacja zadań --------------------------------------------------------------
def test_dependency_cycle_detected():
    assert find_cycle(["A", "B", "C"], {"A": ["B"], "B": ["C"], "C": ["A"]}) is not None
    assert find_cycle(["A", "B"], {"B": ["A"]}) is None


def test_planner_refuses_cyclic_templates(orch):
    snap, rk, sel = _sel(orch, ["organic_matter"])
    ce = orch.costs(sel.selection_id)
    fm = orch.funding(sel.selection_id, ce.cost_estimate_id)
    tpl = copy.deepcopy(load_templates())
    t = {x["template_id"]: x for x in tpl["templates"]}
    t["T_CONFIRM_WORK_AREA"]["depends_on"] = ["T_EXEC_ORGANIC"]  # sztuczny cykl
    plan = build_plan(sel, rk.screening.facts, ce, fm, {s.method_id: s for s in rk.screening.methods}, templates=tpl)
    assert plan.status == "error" and plan.cycle_detected and plan.tasks == []


def test_plan_tasks_deduplicated_and_ordered(orch):
    snap, rk, sel = _sel(orch, ["organic_matter", "cover_crops", "reduced_tillage"])
    ce = orch.costs(sel.selection_id)
    fm = orch.funding(sel.selection_id, ce.cost_estimate_id)
    plan = build_plan(sel, rk.screening.facts, ce, fm, {s.method_id: s for s in rk.screening.methods})
    tids = [t.template_id for t in plan.tasks]
    assert len(tids) == len(set(tids)) and tids.count("T_SOIL_SAMPLING") == 1
    soil = next(t for t in plan.tasks if t.template_id == "T_SOIL_SAMPLING")
    assert set(soil.method_ids) == {"organic_matter", "cover_crops", "reduced_tillage"}
    pos = {t.task_id: t.order for t in plan.tasks}
    for t in plan.tasks:
        assert all(pos[d] < t.order for d in t.depends_on)  # zależności przed zadaniem
    assert all("20" not in t.timing_pl for t in plan.tasks)  # brak sztywnych dat


def test_unknown_drainage_puts_verification_first(orch):
    snap, rk, sel = _sel(orch, ["controlled_drainage"])
    fp = orch.plan(sel.selection_id)
    tasks = fp.section_4_implementation["tasks"]
    ids = [t["template_id"] for t in tasks]
    assert ids.index("T_VERIFY_DRAINAGE_EXISTENCE") < ids.index("T_VERIFY_DRAINAGE_CONDITION") < ids.index("T_EXEC_HYDRO_WORKS")
    assert tasks[ids.index("T_PERMITS")]["timing_pl"].endswith("(termin zależny od organu)")
