"""Katalog programów wsparcia z dokumentów urzędowych (tryb live) i kroki planu z terminami programów."""
from __future__ import annotations

import json
import re
from datetime import date

import pytest

from app.config import DATA_DIR
from app.engine import funding_matcher
from app.engine.funding_matcher import call_status
from app.engine.implementation_planner import next_window
from app.engine.method_catalog import methods_by_id
from app.models.economics import BeneficiaryProfile
from conftest import PARCEL_A, TODAY, chain

REAL = json.loads((DATA_DIR / "funding_programs.json").read_text(encoding="utf-8"))
OFFICIAL = re.compile(r"^https://(www\.)?(gov\.pl|cdr\.gov\.pl|bgk\.pl)/")


@pytest.fixture()
def real_catalog(monkeypatch):
    """Orkiestrator testów działa w trybie fixture — podstawiamy katalog z dokumentów urzędowych."""
    monkeypatch.setitem(funding_matcher.CATALOG_FILES, "fixture", "funding_programs.json")


def test_real_catalog_is_sourced_and_not_demonstration():
    assert REAL["is_demonstration"] is False and REAL["catalog_verified_at"]
    known_methods = set(methods_by_id())
    phases = set(range(1, 9))
    for p in REAL["programs"]:
        text = json.dumps(p, ensure_ascii=False)
        assert "DEMO" not in text and "fikcyj" not in text.lower(), p["program_id"]
        assert p["source_urls"] and all(OFFICIAL.match(u) for u in p["source_urls"]), p["program_id"]
        assert OFFICIAL.match(p["official_source_url"])
        assert set(p["eligible_methods"]) <= known_methods
        assert set(p.get("method_details", {})) <= set(p["eligible_methods"])
        for c in p["calls"]:
            assert c["basis_pl"]  # każdy termin ma podstawę
            if c["valid_to"]:
                assert date.fromisoformat(c["valid_from"]) <= date.fromisoformat(c["valid_to"])
        for st in p.get("plan_steps", []):
            assert st["phase"] in phases and st["title_pl"] and st["timing_pl"]
            assert set(st.get("method_ids", [])) <= set(p["eligible_methods"])
            if "window" in st:
                next_window(st["window"]["from"], st["window"]["to"], TODAY)


def test_next_window_handles_ongoing_future_and_new_year():
    assert next_window("03-15", "05-15", date(2026, 11, 22)) == (date(2027, 3, 15), date(2027, 5, 15))
    assert next_window("03-15", "05-15", date(2026, 4, 1)) == (date(2026, 3, 15), date(2026, 5, 15))
    # okno przez Nowy Rok (utrzymanie międzyplonu do 15 lutego)
    assert next_window("10-01", "02-15", date(2026, 12, 10)) == (date(2026, 10, 1), date(2027, 2, 15))
    assert next_window("10-01", "02-15", date(2026, 3, 1)) == (date(2026, 10, 1), date(2027, 2, 15))


def test_continuous_call_without_end_date_is_open():
    calls = [{"call_id": "C", "valid_from": "2024-03-01", "valid_to": None, "known_from": "2024-03-01", "known_to": None}]
    assert call_status(calls, date(2026, 11, 22), date(2026, 11, 22))[0] == "open"
    assert call_status(calls, date(2023, 6, 1), date(2026, 11, 22))[0] == "unknown"  # przed startem, brak zapowiedzi


def test_real_programs_matched_with_estimates_and_dated_plan_steps(orch, real_catalog):
    snap, _, rk = chain(orch, PARCEL_A, "maize_grain", as_of=TODAY)
    sel = orch.select(snap.analysis_id, rk.ranking_run_id, ["cover_crops", "reduced_tillage", "windbreaks"])
    fp = orch.plan(sel.selection_id, beneficiary_profile=BeneficiaryProfile())
    f = fp.section_3_funding
    assert f["catalog_is_demonstration"] is False and f["catalog_version"] == REAL["version"]
    by_code = {m["code"]: m for m in f["matches"]}
    eco = by_code["I.4.2"]
    assert set(eco["method_ids"]) == {"cover_crops", "reduced_tillage"}
    assert any("5 pkt/ha" in e for e in eco["estimate_pl"]) and any("3 pkt/ha" in e for e in eco["estimate_pl"])
    assert eco["call_status_now"] == "announced"          # kampania 2027 wg reguły corocznej
    assert by_code["I.10.12"]["call_status_now"] == "closed"  # nabór 1.06–31.07.2026 zakończony
    assert "I.4.5" not in by_code                          # retencja na TUZ tylko dla łąk i pastwisk
    tasks = {t["template_id"]: t for t in fp.section_4_implementation["tasks"]}
    camp = tasks["ARIMR_CAMPAIGN_APPLY"]
    assert (camp["window_from"], camp["window_to"]) == ("2027-03-15", "2027-05-15")
    assert [t["template_id"] for t in fp.section_4_implementation["tasks"]].count("ARIMR_CAMPAIGN_APPLY") == 1
    assert tasks["ECO_COVER_SOW"]["window_from"] == "2027-07-01"
    # utrzymanie międzyplonu liczone od najbliższego siewu, nie od dziś
    assert (tasks["ECO_COVER_KEEP"]["window_from"], tasks["ECO_COVER_KEEP"]["window_to"]) == ("2027-10-01", "2028-02-15")
    assert "nie jest jeszcze ogłoszony" in tasks["TREES_APPLY"]["window_note_pl"]
    assert any("I.10.12" in p for p in tasks["TREES_APPLY"]["program_ids"])


def test_grassland_gets_water_retention_ecoscheme(orch, real_catalog):
    snap, _, rk = chain(orch, PARCEL_A, "grassland", as_of=TODAY)
    sel = orch.select(snap.analysis_id, rk.ranking_run_id, ["runoff_slowing"])
    fp = orch.plan(sel.selection_id)
    codes = {m["code"] for m in fp.section_3_funding["matches"]}
    assert "I.4.5" in codes
    assert "ECO_TUZ_FLOOD_CHECK" in {t["template_id"] for t in fp.section_4_implementation["tasks"]}
