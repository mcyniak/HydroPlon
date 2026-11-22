"""Status systemu, źródeł danych i skrót demonstracyjny całego przepływu."""
from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import Field

from app.api.deps import get_orchestrator
from app.config import APP_DIR, APP_VERSION, settings
from app.engine.orchestrator import Orchestrator
from app.models.common import StrictModel
from app.models.economics import BeneficiaryProfile, EconomicInputs
from app.models.methods import FieldContext
from app.services.errors import UnprocessableError

router = APIRouter(tags=["System i demo"])


@router.get("/panel", include_in_schema=False)
def panel() -> HTMLResponse:
    """Prosty panel testowy (nie docelowy frontend)."""
    return HTMLResponse((APP_DIR / "static" / "panel.html").read_text(encoding="utf-8"))


@router.get("/", include_in_schema=False)
def root() -> RedirectResponse:
    return RedirectResponse("/app")


@router.get("/app", include_in_schema=False)
def app_page() -> HTMLResponse:
    """Docelowy kreator HydroPlon (4 kroki: działka, uprawa, metody, raport) — spec. GRAND FINALE v7, część VIII.
    Do adresów app.css / app.js dopisujemy wersję (czas modyfikacji), żeby przeglądarka nie trzymała starych plików."""
    static = APP_DIR / "static"
    html = (static / "app.html").read_text(encoding="utf-8")
    for name in ("app.css", "app.js"):
        html = html.replace(f"/static/{name}\"", f"/static/{name}?v={int((static / name).stat().st_mtime)}\"")
    return HTMLResponse(html, headers={"Cache-Control": "no-cache"})


@router.get("/mapa", include_in_schema=False)
def map_page() -> HTMLResponse:
    """Mapa testowa: wybór działki (klik / ID) i ręczne przejście całego przepływu (nie docelowy frontend)."""
    return HTMLResponse((APP_DIR / "static" / "mapa.html").read_text(encoding="utf-8"))


@router.get("/api/system/fixture-parcels")
def fixture_parcels(orch: Orchestrator = Depends(get_orchestrator)) -> dict:
    """Obrysy działek testowych (tylko tryb fixture), żeby na mapie było widać, gdzie klikać."""
    if orch.adapters.mode != "fixture":
        return {"type": "FeatureCollection", "features": [], "note_pl": "Tryb live — brak działek testowych."}
    from app.services.adapters.fixture import _load
    feats = []
    for pid, rec in _load("parcels.json")["parcels"].items():
        prof = orch.parcel_by_id(pid)
        feats.append({"type": "Feature", "geometry": prof.geojson_feature["geometry"],
                      "properties": {"parcel_id": pid, "label": rec["label"], "scenario_pl": rec["scenario_pl"],
                                     "area_ha": prof.metrics.area_ha}})
    return {"type": "FeatureCollection", "features": feats}


@router.get("/api/health")
def health(orch: Orchestrator = Depends(get_orchestrator)) -> dict:
    return {"status": "ok", "app_version": APP_VERSION, "data_mode": orch.adapters.mode,
            "reference_today": (orch.today or settings.today()).isoformat()}


@router.get("/api/system/sources")
def sources(orch: Orchestrator = Depends(get_orchestrator)) -> dict:
    return {"data_mode": orch.adapters.mode,
            "note_pl": ("Tryb fixture: wyłącznie syntetyczne dane offline." if orch.adapters.mode == "fixture" else
                        "Tryb live: adaptery niewdrożone zwracają moduł 'unavailable' — bez przełączania na fixture."),
            "adapters": [a.__dict__ for a in orch.adapters.info()]}


class DemoRunRequest(StrictModel):
    parcel_id: str = "049901_2.0001.101"
    crop_id: str = "maize_grain"
    as_of_date: date | None = None
    field_context: FieldContext = Field(default_factory=FieldContext)
    add_methods: list[str] = Field(default_factory=list)
    remove_methods: list[str] = Field(default_factory=list)
    economic_inputs: EconomicInputs = Field(default_factory=EconomicInputs)
    beneficiary_profile: BeneficiaryProfile = Field(default_factory=BeneficiaryProfile)


@router.post("/api/demo/run")
def demo_run(req: DemoRunRequest, orch: Orchestrator = Depends(get_orchestrator)) -> dict:
    """Cały przepływ jednym wywołaniem (dla testera). Zwraca identyfikatory i skrót wyników."""
    try:
        r = orch.run_full(req.parcel_id, req.crop_id, as_of=req.as_of_date, field_context=req.field_context,
                          add_methods=req.add_methods, remove_methods=req.remove_methods,
                          economic_inputs=req.economic_inputs, beneficiary_profile=req.beneficiary_profile)
    except UnprocessableError as exc:
        # Np. brak metod rekomendowanych (dane niedostępne) i brak ręcznie dodanych — plan nie powstaje,
        # ale tester widzi analizę i oceny metod.
        snap = orch.analyze(req.parcel_id, req.as_of_date)
        ca = orch.assess(snap.analysis_id, req.crop_id)
        rk = orch.recommend(snap.analysis_id, ca.crop_assessment_id, req.field_context)
        return {"plan_id": None, "status": "no_plan", "message_pl": exc.message_pl,
                "hint_pl": "Dodaj metody ręcznie (add_methods), np. spośród warunkowych.",
                "analysis_id": snap.analysis_id, "module_statuses": snap.module_statuses,
                "diagnosis_problems": [(p.problem_id, p.severity, p.confidence) for p in snap.diagnosis.problems],
                "methods": {g: [(m.method_id, m.priority_class) for m in getattr(rk, g)]
                            for g in ("recommended", "conditional", "not_recommended")},
                "warnings": snap.warnings}
    fp, rk = r["plan"], r["ranking"]
    return {
        "plan_id": fp.plan_id, "ids": fp.ids, "status": fp.status,
        "report_url": f"/api/plans/{fp.plan_id}/report", "pdf_url": f"/api/plans/{fp.plan_id}/pdf",
        "diagnosis_problems": [(p.problem_id, p.severity, p.confidence) for p in r["analysis"].diagnosis.problems],
        "methods": {g: [(m.method_id, m.priority_class) for m in getattr(rk, g)]
                    for g in ("recommended", "conditional", "not_recommended")},
        "selected": [m["method_id"] for m in fp.selected_methods],
        "costs_totals": fp.section_2_costs["totals"],
        "profitability_headline": fp.section_1_profitability["headline_pl"],
        "funding": [(m["program_id"], m["match_status"], m["call_status_on_as_of"], m["call_status_now"])
                    for m in fp.section_3_funding["matches"]],
        "tasks": [t["title_pl"] for t in fp.section_4_implementation["tasks"]],
        "warnings": fp.warnings,
    }
