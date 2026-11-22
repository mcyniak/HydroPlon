"""Pełny przepływ (test 30), unieważnianie (31), powtarzalność (32), API FastAPI i raport/PDF."""
from __future__ import annotations

import io
import re
from datetime import date

import pytest
from fastapi.testclient import TestClient
from pypdf import PdfReader

from app.api.deps import get_orchestrator
from app.engine.orchestrator import Orchestrator, PipelineSession
from app.main import app
from app.models.methods import FieldContext
from app.reports.report_builder import render_pdf, render_text
from app.services.parcel_service import parcel_cache
from app.store import ResultStore
from conftest import HIST, PARCEL_A, PARCEL_B, PARCEL_C, TODAY, strip_volatile


# --- 30. Scenariusz ze specyfikacji: działka A, kukurydza, drenaż = nie wiem, + metoda warunkowa -------------
def test_full_flow_parcel_a_maize_then_other_crop(orch):
    snap = orch.analyze(PARCEL_A, HIST)                                   # 1–2
    ca = orch.assess(snap.analysis_id, "maize_grain")                     # 3
    rk = orch.recommend(snap.analysis_id, ca.crop_assessment_id,
                        FieldContext(subsurface_drainage_status="unknown"))  # 4
    rec = [m.method_id for m in rk.recommended]                           # 5
    assert rec
    chosen = rec + ["reduced_tillage"]                                    # 6 (metoda warunkowa)
    sel = orch.select(snap.analysis_id, rk.ranking_run_id, chosen)
    assert sel.status in ("conditional", "ready")                         # 7 (relacje sprawdzone)
    assert any(r.relation_type == "dependency" for r in sel.method_relations)
    fp = orch.plan(sel.selection_id)                                      # 8–12
    assert fp.section_2_costs["totals"]["total_status"] in ("partial", "complete")
    assert fp.section_1_profitability["result_kind"] == "break_even_only"
    assert fp.section_3_funding["matches"]
    assert fp.section_4_implementation["tasks"] and fp.section_4_implementation["cycle_detected"] is None
    ids = fp.ids
    assert ids["analysis_id"] == snap.analysis_id and ids["crop_assessment_id"] == ca.crop_assessment_id
    assert ids["ranking_run_id"] == rk.ranking_run_id and ids["selection_id"] == sel.selection_id
    assert {"method_catalog", "price_catalog", "funding_catalog", "diagnosis", "crop_catalog"} <= set(fp.versions)
    assert fp.data_provenance["is_demonstration"] is True
    # ta sama działka, inna uprawa
    ca2 = orch.assess(snap.analysis_id, "potato")
    rk2 = orch.recommend(snap.analysis_id, ca2.crop_assessment_id)
    sel2 = orch.select(snap.analysis_id, rk2.ranking_run_id, [m.method_id for m in rk2.recommended] + ["reduced_tillage"])
    fp2 = orch.plan(sel2.selection_id)
    assert fp2.plan_id != fp.plan_id
    assert fp2.parcel_summary == fp.parcel_summary                       # te same dane działki
    assert fp2.crop_summary["crop_id"] == "potato"
    assert fp2.section_5_analysis["crop_assessment"]["crop_water_balance"] != \
        fp.section_5_analysis["crop_assessment"]["crop_water_balance"]


def test_report_text_and_pdf_from_same_plan(orch):
    r = orch.run_full(PARCEL_A, "maize_grain", as_of=HIST)
    fp = r["plan"]
    txt = render_text(fp)
    headers = ["## Diagnoza wodna działki i uprawy", "### Stan niedoboru wody", "### Przyczyny i czynniki ryzyka",
               "## Zalecane działania i uzasadnienie", "## 1. Opłacalność", "## 2. Koszty",
               "## 3. Możliwe dofinansowania", "## 4. Plan wdrożenia", "## 5. Szczegóły analizy"]
    positions = [txt.index(h) for h in headers]
    assert positions == sorted(positions)
    assert fp.plan_id in txt and "SYNTETYCZNE" in txt
    assert not re.search(r"(?:= |: )0,00 zł", txt)  # żadna pozycja ani suma nie jest fikcyjnym zerem
    pdf = render_pdf(fp)
    assert pdf[:4] == b"%PDF" and len(pdf) > 5000
    report = PdfReader(io.BytesIO(pdf))
    pdf_text = []
    for index, page in enumerate(report.pages, start=1):
        text_starts = []

        def collect_text_start(text, _cm, tm, _font, _size):
            if text.strip():
                text_starts.append(tm[4])

        text = page.extract_text(visitor_text=collect_text_start)
        pdf_text.append(text)
        assert text_starts and all(45 <= x <= float(page.mediabox.width) - 45 for x in text_starts)
        assert f"Strona {index} / {len(report.pages)}" in text
        assert page.images
    assert len(report.outline) >= 7

    first_page = " ".join(pdf_text[0].split())
    pdf_text = " ".join(" ".join(pdf_text).split())
    assert "Stan niedoboru wody" in first_page and "Przyczyny i czynniki ryzyka" in first_page
    assert "Ostatnia obserwacja IMGW w analizie 15.06.2019" in first_page
    assert "Ostatnia użyteczna scena Sentinel-2 11.06.2019" in first_page
    fonts = report.pages[0]["/Resources"]["/Font"].values()
    assert any("Literata" in font.get_object()["/BaseFont"] for font in fonts)
    pdf_positions = [pdf_text.index(header.lstrip("# ")) for header in headers]
    assert pdf_positions == sorted(pdf_positions)
    assert fp.plan_id in pdf_text
    assert "Wersje danych i reguł" not in pdf_text
    assert "Wersje danych i reguł" in txt
    for line in (
        "Stan niedoboru wody",
        "Dane wieloletnie wskazują na ryzyko niedoboru",
        "Przyczyny i czynniki ryzyka",
        "Zalecane działania i uzasadnienie",
        "Retencja · Pasy roślinne i zadarnienie",
        "Agrotechnika · Pozostawianie resztek pożniwnych",
        "Podstawa w danych: ryzyko spływu i erozji.",
        "Źródła danych",
        "Ograniczenia i braki danych",
        "Pewność rozpoznania okresowych niedoborów: wysoka.",
        "Ostatnia obserwacja IMGW w analizie: 15.06.2019.",
        "Ostatnia użyteczna scena Sentinel-2: 11.06.2019.",
    ):
        assert line in txt and line in pdf_text


def test_water_report_shows_missing_data_and_conditional_irrigation(orch):
    snapshot = orch.analyze(PARCEL_C, HIST)
    assessment = orch.assess(snapshot.analysis_id, "maize_grain")
    ranking = orch.recommend(snapshot.analysis_id, assessment.crop_assessment_id, FieldContext())
    selection = orch.select(snapshot.analysis_id, ranking.ranking_run_id, ["crop_residues", "efficient_irrigation"])
    plan = orch.plan(selection.selection_id)
    report = plan.section_5_analysis["water_report"]
    sections = {section["title"]: section["entries"] for section in report["sections"]}
    assert all("Brak danych" in entry["text"] and "mm" not in entry["text"]
               for entry in sections["Stan niedoboru wody"])
    assert report["dates"][2]["text"] == "brak danych"
    actions = sections["Zalecane działania i uzasadnienie"]
    irrigation = next(entry for entry in actions if entry["label"].startswith("Nawadnianie (opcjonalne) · warunkowa"))
    assert "Brak danych do potwierdzenia potrzeby" in irrigation["text"]
    assert "prawa do poboru wody" in irrigation["text"]
    assert any(entry["label"] == "Gleba" and "zdolność gleby do zatrzymywania wody" in entry["text"]
               for entry in sections["Ograniczenia i braki danych"])

    # Raporty generowane ze starszych obiektów planu też pokazują braki.
    plan.section_5_analysis.pop("water_report")
    first_page = " ".join(PdfReader(io.BytesIO(render_pdf(plan))).pages[0].extract_text().split())
    assert "Brak danych do oceny bilansu" in first_page
    assert "Ostatnia użyteczna scena Sentinel-2 brak danych" in first_page
    assert "Nawadnianie (opcjonalne) · warunkowa" in first_page
    text = render_text(plan)
    assert "Ostatnia użyteczna scena Sentinel-2: brak danych" in text
    assert "Pewność rozpoznania okresowych niedoborów: nieustalona" in text
    assert "brak danych mm" not in text


# --- 31. Zmiana danych wejściowych unieważnia stary wynik -------------------------------------------------
def test_session_invalidation_and_manual_selection_preserved(orch):
    s = PipelineSession(orch)
    s.set_parcel(PARCEL_A, HIST)
    s.set_crop("maize_grain")
    plan1 = s.build_plan()
    old_analysis, old_ranking, old_sel = s.analysis_id, s.ranking_run_id, s.selection_id
    assert s.is_current(plan1.plan_id)
    # ręczna zmiana metod: zachowana przy ponownym rankingu z TYMI SAMYMI danymi
    s.set_methods(["crop_residues", "reduced_tillage"])
    assert not s.is_current(plan1.plan_id) and plan1.plan_id in s.stale_ids
    s.set_field_context(FieldContext())  # bez zmiany deklaracji
    assert s.selected_method_ids == ["crop_residues", "reduced_tillage"] and s.has_manually_edited_methods
    # zmiana FieldContext => nowy ranking, reset do domyślnych rekomendacji
    s.set_field_context(FieldContext(subsurface_drainage_status="no"))
    assert old_ranking in s.stale_ids and s.ranking_run_id != old_ranking
    assert not s.has_manually_edited_methods
    # zmiana uprawy => nowa ocena, nowy ranking
    s.set_crop("winter_wheat")
    assert s.crop_assessment_id and s.ranking_run_id not in (old_ranking,)
    # zmiana daty => nowa analiza; stare identyfikatory unieważnione
    s.set_parcel(PARCEL_A, TODAY)
    assert old_analysis in s.stale_ids and s.analysis_id != old_analysis
    plan2 = s.build_plan()
    assert plan2.ids["analysis_id"] == s.analysis_id and plan2.plan_id != plan1.plan_id
    assert old_sel in s.stale_ids


def test_store_cascade_invalidation(orch):
    r = orch.run_full(PARCEL_A, "maize_grain", as_of=HIST)
    aid, pid = r["analysis"].analysis_id, r["plan"].plan_id
    gone = orch.store.invalidate(aid, "test: nowa wersja danych źródłowych")
    assert pid in gone and not orch.store.is_valid(pid)
    from app.services.errors import ConsistencyError
    with pytest.raises(ConsistencyError):
        orch.store.get(pid, "plan", require_valid=True)


# --- 32. Te same dane i wersje => ten sam wynik -------------------------------------------------------------
def test_reproducible_results_with_fresh_stores():
    outs = []
    for _ in range(2):
        parcel_cache.clear()
        o = Orchestrator(mode="fixture", result_store=ResultStore(), today=TODAY)
        r = o.run_full(PARCEL_B, "winter_wheat", as_of=HIST, add_methods=["cover_crops"])
        outs.append(strip_volatile(r["plan"].model_dump(mode="json")))
    assert outs[0]["plan_id"] == outs[1]["plan_id"]
    assert outs[0] == outs[1]


def test_different_inputs_give_different_ids(orch):
    a = orch.run_full(PARCEL_A, "maize_grain", as_of=HIST)["plan"]
    b = orch.run_full(PARCEL_A, "maize_grain", as_of=date(2020, 6, 15))["plan"]
    assert a.plan_id != b.plan_id and a.ids["analysis_id"] != b.ids["analysis_id"]


# --- API (FastAPI TestClient) --------------------------------------------------------------------------------
@pytest.fixture()
def client(orch):
    app.dependency_overrides[get_orchestrator] = lambda: orch
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_api_full_chain(client):
    p = client.get("/api/parcels/by-id", params={"id": PARCEL_A}).json()
    pt = client.get("/api/parcels/by-point", params={"lat": p["coordinates"]["point_on_surface_lat"],
                                                     "lon": p["coordinates"]["point_on_surface_lon"]}).json()
    assert pt["parcel_id"] == p["parcel_id"] and pt["metrics"] == p["metrics"]
    an = client.post("/api/analysis", json={"teryt_id": PARCEL_A, "target_date": "2019-06-15"}).json()
    assert an["time_context"]["as_of_date"] == "2019-06-15"  # aliasy teryt_id/target_date
    crops = client.get("/api/crops").json()
    assert crops["count"] == 8 and crops["assessable_count"] == 8
    ca = client.post("/api/crops/assess", json={"analysis_id": an["analysis_id"], "crop_id": "maize_grain"}).json()
    rk = client.post("/api/recommendations", json={"analysis_id": an["analysis_id"],
                                                   "crop_assessment_id": ca["crop_assessment_id"],
                                                   "field_context": {"subsurface_drainage_status": "nie_wiem"}}).json()
    assert len(rk["recommended"]) + len(rk["conditional"]) + len(rk["not_recommended"]) == 11
    sel = client.post("/api/selections", json={"analysis_snapshot_id": an["analysis_id"],
                                               "ranking_run_id": rk["ranking_run_id"]}).json()
    ce = client.post("/api/costs/estimate", json={"selection_id": sel["selection_id"]}).json()
    pr = client.post("/api/profitability/evaluate", json={"cost_estimate_id": ce["cost_estimate_id"]}).json()
    fm = client.post("/api/funding/match", json={"selection_id": sel["selection_id"],
                                                "cost_estimate_id": ce["cost_estimate_id"]}).json()
    assert pr["result_kind"] == "break_even_only" and fm["catalog_is_demonstration"] is True
    plan = client.post("/api/plans", json={"selection_id": sel["selection_id"]}).json()
    assert client.get(f"/api/plans/{plan['plan_id']}").json()["plan_id"] == plan["plan_id"]
    rep = client.get(f"/api/plans/{plan['plan_id']}/report")
    assert rep.status_code == 200 and plan["plan_id"] in rep.text
    pdf = client.get(f"/api/plans/{plan['plan_id']}/pdf")
    assert pdf.status_code == 200 and pdf.content[:4] == b"%PDF"


def test_api_errors_are_structured(client):
    r = client.get("/api/parcels/by-id", params={"id": "zly-identyfikator"})
    assert r.status_code == 400 and r.json()["error_code"] == "invalid_input" and r.json()["request_id"]
    r = client.get("/api/parcels/by-id", params={"id": "049901_2.0001.998"})
    assert r.status_code == 404
    r = client.get("/api/parcels/by-id", params={"id": "049901_2.0001.999"})
    assert r.status_code == 502 and r.json()["error_code"] == "upstream_bad_format"
    r = client.get("/api/parcels/by-point", params={"lat": 18.6, "lon": 53.0})
    assert r.status_code == 400
    r = client.post("/api/crops/assess", json={"analysis_id": "an-nieistnieje", "crop_id": "maize_grain"})
    assert r.status_code == 404
    r = client.post("/api/analysis", json={"parcel_id": PARCEL_A, "as_of_date": "nie-data"})
    assert r.status_code == 422 and r.json()["error_code"] == "validation_error"
    r = client.post("/api/analysis", json={"parcel_id": PARCEL_A, "geometry": "POLYGON(...)"})
    assert r.status_code == 422  # nie przyjmujemy geometrii z przeglądarki


def test_api_mixing_ids_returns_409(client):
    a = client.post("/api/analysis", json={"parcel_id": PARCEL_A, "as_of_date": "2019-06-15"}).json()
    b = client.post("/api/analysis", json={"parcel_id": PARCEL_B, "as_of_date": "2019-06-15"}).json()
    ca = client.post("/api/crops/assess", json={"analysis_id": a["analysis_id"], "crop_id": "maize_grain"}).json()
    r = client.post("/api/recommendations", json={"analysis_id": b["analysis_id"],
                                                  "crop_assessment_id": ca["crop_assessment_id"]})
    assert r.status_code == 409 and r.json()["error_code"] == "inconsistent_identifiers"


def test_api_empty_selection_cannot_make_plan(client):
    an = client.post("/api/analysis", json={"parcel_id": PARCEL_A, "as_of_date": "2019-06-15"}).json()
    ca = client.post("/api/crops/assess", json={"analysis_id": an["analysis_id"], "crop_id": "maize_grain"}).json()
    rk = client.post("/api/recommendations", json={"analysis_id": an["analysis_id"],
                                                   "crop_assessment_id": ca["crop_assessment_id"]}).json()
    sel = client.post("/api/selections", json={"analysis_id": an["analysis_id"], "ranking_run_id": rk["ranking_run_id"],
                                               "selected_method_ids": []}).json()
    assert sel["status"] == "empty"
    r = client.post("/api/plans", json={"selection_id": sel["selection_id"]})
    assert r.status_code == 422


def test_api_demo_and_sources(client):
    src = client.get("/api/system/sources").json()
    assert src["data_mode"] == "fixture" and all(a["source_type"] == "fixture" for a in src["adapters"])
    demo = client.post("/api/demo/run", json={"parcel_id": PARCEL_A, "crop_id": "maize_grain",
                                              "as_of_date": "2019-06-15", "add_methods": ["reduced_tillage"]}).json()
    assert demo["plan_id"].startswith("plan-") and "reduced_tillage" in demo["selected"]


def test_panel_served(client):
    r = client.get("/panel")
    assert r.status_code == 200 and "panel testowy" in r.text and "/api/demo/run" in r.text


def test_map_page_and_fixture_parcels(client):
    r = client.get("/mapa")
    assert r.status_code == 200 and "leaflet@1.9.4" in r.text and "/api/parcels/by-point" in r.text
    fx = client.get("/api/system/fixture-parcels").json()
    assert {f["properties"]["label"] for f in fx["features"]} == {"A", "B", "C", "D"}


def test_wizard_app_served_and_root_redirects(client):
    r = client.get("/app")
    assert r.status_code == 200
    for needle in ('id="screen-1"', 'id="screen-4"', "/static/app.css", "/static/app.js"):
        assert needle in r.text, needle
    css, js = client.get("/static/app.css"), client.get("/static/app.js")
    assert css.status_code == 200 and "text/css" in css.headers["content-type"]
    assert "#0e6548" in css.text and "#eac24f" in css.text and "Literata" in css.text
    assert js.status_code == 200 and "javascript" in js.headers["content-type"]
    for needle in ("/api/parcels/by-id", "/api/parcels/by-point", "/api/parcels/address-search",
                   "/api/analysis", "/api/crops/assess", "/api/recommendations", "/api/selections", "/api/plans"):
        assert needle in js.text, needle
    assert 'id="nav-pdf"' in r.text and "/pdf" in js.text
    assert 'src="/static/logo-hydroplon.svg"' in r.text
    root = client.get("/", follow_redirects=False)
    assert root.status_code in (302, 307) and root.headers["location"] == "/app"


def test_demo_run_without_any_method_returns_analysis_not_500(client):
    r = client.post("/api/demo/run", json={"parcel_id": "149901_2.0001.303", "crop_id": "maize_grain"})
    d = r.json()
    assert r.status_code == 200 and d["plan_id"] is None and d["status"] == "no_plan"
    assert len(sum(d["methods"].values(), [])) == 11
