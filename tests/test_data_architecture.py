"""Architektura danych: cache, łańcuch dostawców satelitarnych, adapter CDSE (opcjonalny), hydrologia, budżet czasu.
Wszystko offline (atrapy odpowiedzi / monkeypatch) — testy nie wychodzą do sieci."""
from __future__ import annotations

import json
import time
from datetime import date

import httpx
import numpy as np
import pytest
from shapely import wkt as shp_wkt

from app.services import cache
from app.services.adapters import live_hydrology as lh
from app.services.adapters import satellite_providers as sp
from app.services.errors import SourceRateLimitedError, SourceTimeoutError

SQUARE = "POLYGON((500000 500000, 500200 500000, 500200 500200, 500000 500200, 500000 500000))"


@pytest.fixture()
def tmp_cache(tmp_path):
    old = cache._path
    cache.configure(tmp_path / "c.sqlite")
    yield
    cache.configure(old)


# ------------------------------------------------------------------------------------------------ cache
def test_cache_versioning_and_isolation(tmp_cache):
    cache.put("terrain", "parcelA", "v1", {"slope": 1})
    assert cache.get("terrain", "parcelA", "v1") == {"slope": 1}
    assert cache.get("terrain", "parcelA", "v2") is None          # nowa wersja algorytmu => brak trafienia
    assert cache.get("terrain", "parcelB", "v1") is None          # inna działka
    cache.put("terrain", "parcelA", "v2", {"slope": 2})
    assert cache.get("terrain", "parcelA", "v1") is None          # stara wersja usunięta przy zapisie nowej


def test_cache_size_limit_evicts_lru(tmp_cache, monkeypatch):
    monkeypatch.setattr(cache, "MAX_BYTES", 5_000)
    for i in range(20):
        cache.put("s2scene", f"k{i}", "v", {"blob": "x" * 500})
    st = cache.stats()
    assert st["bytes"] <= 5_000 and st["evictions"] > 0
    assert cache.get("s2scene", "k19", "v") is not None and cache.get("s2scene", "k0", "v") is None


def test_cache_ttl(tmp_cache, monkeypatch):
    cache.put("s2search", "q", "1", [1])
    monkeypatch.setitem(cache.TTL_S, "s2search", -1)
    assert cache.get("s2search", "q", "1") is None


# ------------------------------------------------------------------------------------------------ satelita
def test_scene_model_rejects_impossible_ndvi():
    with pytest.raises(ValueError):
        sp.SceneIndices(acquisition_date="2026-11-15", scene_id="x", provider="p", valid_parcel_pixels_pct=100,
                        ndvi_median=2.97)


class FakeProvider:
    def __init__(self, name, behaviour, available=True):
        self.name, self.requires_auth, self.behaviour, self._available, self.calls = name, False, behaviour, available, 0

    def available(self):
        return (self._available, "test")

    def scenes(self, q, deadline):
        self.calls += 1
        if self.behaviour == "ok":
            return [sp.SceneIndices(acquisition_date="2019-06-10", scene_id="s", provider=self.name,
                                    valid_parcel_pixels_pct=95, ndvi_median=0.6, ndmi_median=0.15)], []
        if self.behaviour == "429":
            raise SourceRateLimitedError("limit")
        if self.behaviour == "slow":
            time.sleep(0.3)
            raise SourceTimeoutError("timeout")


Q = sp.Query(18.4, 52.8, SQUARE, date(2019, 6, 15))


def test_fallback_uses_next_provider_only_after_failure_and_once():
    a, b, c = FakeProvider("a", "429"), FakeProvider("b", "ok"), FakeProvider("c", "ok")
    res = sp.fetch_with_fallback(Q, budget_s=5, providers=[a, b, c])
    assert res["provider"] == "b" and a.calls == b.calls == 1 and c.calls == 0
    assert [x["status"] for x in res["attempts"]] == ["error", "ok"]


def test_unavailable_provider_is_skipped_and_budget_respected():
    skip, slow, never = FakeProvider("cdse", "ok", available=False), FakeProvider("s", "slow"), FakeProvider("n", "ok")
    res = sp.fetch_with_fallback(Q, budget_s=0.2, providers=[skip, slow, never])
    assert res["provider"] is None and res["scenes"] == []
    assert res["attempts"][0]["status"] == "skipped" and never.calls == 0


def test_cdse_adapter_disabled_without_user_credentials(monkeypatch):
    monkeypatch.delenv("CDSE_CLIENT_ID", raising=False)
    monkeypatch.delenv("CDSE_CLIENT_SECRET", raising=False)
    ok, why = sp.CdseStatisticalProvider().available()
    assert ok is False and "OAuth" in why


def test_cdse_adapter_parses_statistics_and_blocks_on_429(monkeypatch):
    monkeypatch.setenv("CDSE_CLIENT_ID", "user-own-id")
    monkeypatch.setenv("CDSE_CLIENT_SECRET", "user-own-secret")
    state = {"stat_status": 200}
    stats = {"data": [{"interval": {"from": "2019-06-12T00:00:00Z"}, "outputs": {
        "ndvi": {"bands": {"B0": {"stats": {"sampleCount": 100, "noDataCount": 10,
                                            "percentiles": {"25.0": 0.5, "50.0": 0.6, "75.0": 0.7}}}}},
        "ndmi": {"bands": {"B0": {"stats": {"sampleCount": 100, "noDataCount": 10,
                                            "percentiles": {"25.0": 0.1, "50.0": 0.15, "75.0": 0.2}}}}}}}]}

    def handler(req: httpx.Request):
        if "openid-connect/token" in str(req.url):
            assert b"user-own-secret" in req.content
            return httpx.Response(200, json={"access_token": "tok", "expires_in": 600})
        assert req.headers["Authorization"] == "Bearer tok"
        if state["stat_status"] == 429:
            return httpx.Response(429, headers={"Retry-After": "120"})
        return httpx.Response(200, json=stats)
    prov = sp.CdseStatisticalProvider(transport=httpx.MockTransport(handler))
    scenes, _ = prov.scenes(Q, time.monotonic() + 10)
    assert scenes[0].ndvi_median == 0.6 and scenes[0].valid_parcel_pixels_pct == 90.0
    state["stat_status"] = 429
    with pytest.raises(SourceRateLimitedError):
        prov.scenes(Q, time.monotonic() + 10)
    assert prov.available()[0] is False  # limit wyczerpany => adapter wstrzymany, bez bombardowania usługi


# ------------------------------------------------------------------------------------------------ hydrologia
def test_water_features_distances_from_wms_masks(monkeypatch):
    geom = shp_wkt.loads(SQUARE)

    def fake(layers, bbox, w, h):
        m = np.zeros((h, w), bool)
        if layers == "row":  # pionowa linia 10 m na wschód od działki
            col = int((500210 - bbox[0]) / (bbox[2] - bbox[0]) * w)
            m[:, col] = True
        if layers == "WPow":
            return None   # warstwa niedostępna
        return m
    monkeypatch.setattr(lh, "_wms_group", fake)
    res = lh.water_features(geom)
    d = {f["bdot_code"]: f["distance_m"] for f in res["features"]}
    assert 8 <= d["SWRM"] <= 13 and "SWRS" not in d
    assert res["status"] == "partial" and res["failed_groups"] == ["PTWP"]


def test_hydrology_service_marks_failed_groups_unknown():
    from app.services.adapters.fixture import FixtureAdapters
    from app.services.hydrology_service import compute_hydrology
    from app.services.parcel_service import build_parcel_profile
    parcel = build_parcel_profile(FixtureAdapters().parcel_by_id("049901_2.0001.101"))
    raw = {"source_type": "live", "is_demonstration": False, "version": "t", "search_buffer_m": 500,
           "record": {"features_layer_status": "ok", "features": [], "failed_groups": ["SWRM", "SWRS"],
                      "flood_layer_status": "unavailable", "flood_zones": None, "pixel_size_m": 2.0}}
    h = compute_hydrology(raw, parcel, "current")
    assert h["status"] == "partial"
    assert h["nearby_water_features"]["has_adjacent_ditch_or_stream"] is None  # nie "False"
    assert h["flood_and_basin_context"]["in_flood_hazard_zone"] is None


class FakeResp:
    def __init__(self, status=200, ctype="application/json", content=b"", js=None):
        self.status_code, self.headers, self.content, self._js = status, {"content-type": ctype}, content, js

    def json(self):
        return self._js


def test_flood_zones_scenarios_from_point_queries(monkeypatch):
    geom = shp_wkt.loads(SQUARE)
    monkeypatch.setattr(lh, "_decode_alpha", lambda png: np.ones((110, 110), bool))  # sito: strefa na całej działce

    def fake_get(url, params=None, timeout=None):
        if params["REQUEST"] == "GetMap":
            return FakeResp(ctype="image/png", content=b"png")
        x = float(params["BBOX"].split(",")[0]) + 5
        props = [{"loo_qualitativelikelihood": "scenariusz Q 0,2% (raz na 500 lat)", "loo_ql_returnperiod": "500"}]
        if x < 500100:  # zachodnia połowa także w Q1%
            props.append({"loo_qualitativelikelihood": "scenariusz Q 1% (raz na 100 lat)", "loo_ql_returnperiod": "100"})
        return FakeResp(js={"features": [{"properties": p} for p in props]})
    monkeypatch.setattr(lh._http, "get", fake_get)
    fz = lh.flood_zones(geom)
    shares = {z["return_period_years"]: z["share_of_parcel_pct"] for z in fz["zones"]}
    assert shares["500"] == 100.0 and 30 <= shares["100"] <= 70


def test_flood_zones_unavailable_is_not_false(monkeypatch):
    def boom(*a, **k):
        raise httpx.ConnectError("x")
    monkeypatch.setattr(lh._http, "get", boom)
    fz = lh.flood_zones(shp_wkt.loads(SQUARE))
    assert fz["status"] == "unavailable"


# ------------------------------------------------------------------------------------------------ budżet czasu
def test_analysis_time_budget_marks_slow_module(monkeypatch, orch):
    from app.config import settings
    from app.services.adapters.fixture import FixtureAdapters
    object.__setattr__(settings, "analysis_budget_s", 0.5)
    slow = FixtureAdapters.satellite_raw

    def slow_sat(self, *a, **k):
        time.sleep(1.5)
        return slow(self, *a, **k)
    monkeypatch.setattr(FixtureAdapters, "satellite_raw", slow_sat)
    try:
        t = time.perf_counter()
        s = orch.analyze("049901_2.0001.101", date(2019, 6, 15))
        assert time.perf_counter() - t < 1.4
        assert s.module_statuses["satellite"] == "unavailable"
        assert s.modules.satellite.metadata.unavailable_reason == "analysis_time_budget"
        assert s.module_statuses["soil"] == "ok" and s.timings_s["satellite"] is None
    finally:
        object.__setattr__(settings, "analysis_budget_s", 28.0)


def test_crop_change_does_not_refetch_spatial_data(orch, monkeypatch):
    from app.services.adapters.fixture import FixtureAdapters
    s = orch.analyze("049901_2.0001.101", date(2019, 6, 15))
    calls = {"n": 0}
    orig = FixtureAdapters.terrain_raw

    def counting(self, *a, **k):
        calls["n"] += 1
        return orig(self, *a, **k)
    monkeypatch.setattr(FixtureAdapters, "terrain_raw", counting)
    for crop in ("maize_grain", "winter_wheat", "potato"):
        ca = orch.assess(s.analysis_id, crop)
        orch.recommend(s.analysis_id, ca.crop_assessment_id)
    assert calls["n"] == 0
