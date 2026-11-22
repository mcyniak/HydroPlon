"""HydroPlon — demonstracja pełnego przepływu w trybie offline (fixture).

Użycie (z katalogu głównego repozytorium):
    .venv/Scripts/python scripts/demo_run.py                 # scenariusze domyślne
    .venv/Scripts/python scripts/demo_run.py --parcel B --crop winter_wheat --date 2019-06-15 --drainage no
    .venv/Scripts/python scripts/demo_run.py --parcel A --crop maize_grain --add reduced_tillage,runoff_slowing \
        --remove vegetative_strips --pond --weir --out demo_output

Wyniki: <out>/<scenariusz>/final_plan.json, report.md, report.pdf oraz <out>/summary.json.
Wszystkie dane są SYNTETYCZNE (source_type=fixture, is_demonstration=true).
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.config import settings  # noqa: E402
from app.engine.orchestrator import Orchestrator  # noqa: E402
from app.models.economics import BeneficiaryProfile, EconomicInputs  # noqa: E402
from app.models.methods import FieldContext  # noqa: E402
from app.reports.report_builder import render_pdf, render_text  # noqa: E402

PARCELS = {"A": "049901_2.0001.101", "B": "069901_2.0001.202/3", "C": "149901_2.0001.303", "D": "049901_2.0002.404/1"}


def run(orch: Orchestrator, name: str, out: Path, *, parcel: str, crop: str, as_of: date | None, fc: FieldContext,
        add: list[str], remove: list[str], econ: EconomicInputs | None = None) -> dict:
    t0 = time.perf_counter()
    r = orch.run_full(PARCELS.get(parcel, parcel), crop, as_of=as_of, field_context=fc, add_methods=add,
                      remove_methods=remove, economic_inputs=econ, beneficiary_profile=BeneficiaryProfile())
    elapsed = time.perf_counter() - t0
    fp, rk, snap = r["plan"], r["ranking"], r["analysis"]
    d = out / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "final_plan.json").write_text(json.dumps(fp.model_dump(mode="json"), ensure_ascii=False, indent=1), encoding="utf-8")
    (d / "report.md").write_text(render_text(fp), encoding="utf-8")
    (d / "report.pdf").write_bytes(render_pdf(fp))
    summary = {
        "scenario": name, "parcel_id": snap.parcel.parcel_id, "crop_id": crop,
        "as_of_date": snap.time_context.as_of_date.isoformat(), "time_mode": snap.time_context.mode,
        "field_context": fc.model_dump(mode="json"), "elapsed_s": round(elapsed, 3),
        "plan_id": fp.plan_id, "plan_status": fp.status, "module_statuses": snap.module_statuses,
        "diagnosis": [(p.problem_id, p.severity, p.confidence) for p in snap.diagnosis.problems],
        "diagnosis_constraints": [c.constraint_id for c in snap.diagnosis.constraints],
        "crop_assessment": {"status": r["crop_assessment"].status,
                            "pressure": r["crop_assessment"].indicative_climatic_pressure},
        "methods": {g: [(m.method_id, m.priority_class) for m in getattr(rk, g)]
                    for g in ("recommended", "conditional", "not_recommended")},
        "selected": [(m["method_id"], m["screening_status"], m["origin"]) for m in fp.selected_methods],
        "selection_status": r["selection"].status,
        "relations": [(x.method_a, x.method_b, x.relation_type) for x in r["selection"].method_relations],
        "costs": {k: fp.section_2_costs["totals"][k] for k in ("capex_known_net_pln", "opex_annual_known_net_pln",
                                                                 "total_status", "unpriced_item_ids")},
        "profitability": fp.section_1_profitability["headline_pl"],
        "funding": [(m["program_id"], m["match_status"], m["call_status_on_as_of"], m["call_status_now"])
                    for m in fp.section_3_funding["matches"]],
        "tasks_count": len(fp.section_4_implementation["tasks"]),
        "first_tasks": [t["title_pl"] for t in fp.section_4_implementation["tasks"][:5]],
        "outputs": [str((d / f).relative_to(out.parent)) for f in ("final_plan.json", "report.md", "report.pdf")],
    }
    return summary


def main() -> int:
    ap = argparse.ArgumentParser(description="HydroPlon — demo pełnego przepływu (offline)")
    ap.add_argument("--parcel", help="A/B/C/D albo identyfikator TERYT z fixture")
    ap.add_argument("--crop", default="maize_grain")
    ap.add_argument("--date", help="as_of_date RRRR-MM-DD (domyślnie dziś w Polsce)")
    ap.add_argument("--today", help="nadpisanie daty odniesienia (powtarzalność)")
    ap.add_argument("--drainage", default="unknown", choices=["yes", "no", "unknown"])
    ap.add_argument("--pond", action="store_true", help="deklaracja: istniejący zbiornik")
    ap.add_argument("--weir", action="store_true", help="deklaracja: zastawka na rowie")
    ap.add_argument("--add", default="", help="metody dodane ręcznie, po przecinku")
    ap.add_argument("--remove", default="", help="rekomendowane metody odznaczone, po przecinku")
    ap.add_argument("--yield-t-ha", type=float, help="własny plon bazowy t/ha")
    ap.add_argument("--price-pln-t", type=float, help="własna cena zł/t")
    ap.add_argument("--out", default=str(ROOT / "demo_output"))
    a = ap.parse_args()
    today = date.fromisoformat(a.today) if a.today else settings.today()
    orch = Orchestrator(mode="fixture", today=today)
    out = Path(a.out)
    types = [t for t, on in (("pond", a.pond), ("weir", a.weir)) if on]
    fc = FieldContext(subsurface_drainage_status=a.drainage,
                      existing_retention_status="yes" if types else "unknown", existing_retention_types=types)
    split = lambda s: [x.strip() for x in s.split(",") if x.strip()]  # noqa: E731
    econ = EconomicInputs(own_yield_t_ha=a.yield_t_ha, own_price_pln_t=a.price_pln_t)
    summaries = []
    if a.parcel:
        as_of = date.fromisoformat(a.date) if a.date else None
        summaries.append(run(orch, "custom", out, parcel=a.parcel, crop=a.crop, as_of=as_of, fc=fc,
                             add=split(a.add), remove=split(a.remove), econ=econ))
    else:
        hist = date(2019, 6, 15)
        summaries.append(run(orch, "1_A_maize_today", out, parcel="A", crop="maize_grain", as_of=None,
                             fc=FieldContext(), add=["reduced_tillage"], remove=[]))
        summaries.append(run(orch, "1b_A_potato_today", out, parcel="A", crop="potato", as_of=None,
                             fc=FieldContext(), add=["reduced_tillage"], remove=[]))
        summaries.append(run(orch, "2_A_maize_2019", out, parcel="A", crop="maize_grain", as_of=hist,
                             fc=FieldContext(), add=["reduced_tillage"], remove=[]))
        summaries.append(run(orch, "3_B_wheat_2019_drainage_no", out, parcel="B", crop="winter_wheat", as_of=hist,
                             fc=FieldContext(subsurface_drainage_status="no"), add=["cover_crops"], remove=[]))
        summaries.append(run(orch, "4_C_incomplete_data", out, parcel="C", crop="maize_grain", as_of=None,
                             fc=FieldContext(), add=["crop_residues", "cover_crops"], remove=[]))
        summaries.append(run(orch, "5_A_pond_weir_declared", out, parcel="A", crop="maize_grain", as_of=hist,
                             fc=FieldContext(existing_retention_status="yes", existing_retention_types=["pond", "weir"]),
                             add=["runoff_slowing", "retention_pond"], remove=[]))
    out.mkdir(parents=True, exist_ok=True)
    payload = {"generated_for_reference_today": today.isoformat(), "data_mode": "fixture",
               "note_pl": "Dane SYNTETYCZNE — wyłącznie test mechanizmów.", "scenarios": summaries}
    (out / "summary.json").write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    for s in summaries:
        print(f"\n=== {s['scenario']}  ({s['parcel_id']}, {s['crop_id']}, {s['as_of_date']} {s['time_mode']}) "
              f"— {s['elapsed_s']} s")
        print("  moduły:", s["module_statuses"])
        print("  diagnoza:", s["diagnosis"], "| ograniczenia:", s["diagnosis_constraints"])
        print("  uprawa:", s["crop_assessment"])
        print("  rekomendowane:", s["methods"]["recommended"])
        print("  niezalecane:", s["methods"]["not_recommended"])
        print("  wybrane:", s["selected"], "| status zestawu:", s["selection_status"])
        print("  koszty:", s["costs"])
        print("  opłacalność:", s["profitability"])
        print("  dotacje:", s["funding"])
        print(f"  zadań w planie: {s['tasks_count']}; plan_id={s['plan_id']}")
    print(f"\nZapisano: {out / 'summary.json'} oraz katalogi scenariuszy (final_plan.json, report.md, report.pdf).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
