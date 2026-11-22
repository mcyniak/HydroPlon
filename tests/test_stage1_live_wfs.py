"""Etap 1 — źródło live WFS EGiB i strategia ULDK → WFS (bez sieci: httpx.MockTransport)."""
from __future__ import annotations

import json

import httpx
import pytest

from app.services.adapters.live import LiveAdapters
from app.services.egib_wfs_service import EgibWfsClient
from app.services.errors import AmbiguousResultError, NotFoundError, SourceFormatError
from app.services.geometry_service import lonlat_to_2180
from app.services.parcel_service import build_parcel_profile
from app.services.uldk_service import LiveUldkClient

LON, LAT = 18.6047, 53.0102
PT = lonlat_to_2180(LON, LAT)
PID = "046301_1.0015.17/2"


def square(cx, cy, half=40.0, swap=False):
    ring = [(cx - half, cy - half), (cx + half, cy - half), (cx + half, cy + half), (cx - half, cy + half),
            (cx - half, cy - half)]
    if swap:
        ring = [(y, x) for x, y in ring]
    return {"type": "Polygon", "coordinates": [[list(p) for p in ring]]}


def feature(pid, geom):
    return {"type": "Feature", "properties": {"ID_DZIALKI": pid, "NUMER_DZIALKI": pid.rsplit(".", 1)[1],
                                             "NAZWA_OBREBU": "Obręb 15", "NAZWA_GMINY": "Toruń", "DATA": "2026-11-19"},
            "geometry": geom}


def wfs(features_for_request):
    def handler(request: httpx.Request):
        return httpx.Response(200, text=json.dumps({"type": "FeatureCollection",
                                                    "features": features_for_request(request)}))
    return EgibWfsClient(transport=httpx.MockTransport(handler), max_retries=0)


def test_by_point_bbox_uses_urn_with_north_east_order():
    seen = {}

    def feats(req):
        seen["bbox"] = req.url.params["BBOX"]
        return [feature(PID, square(PT.x, PT.y))]
    raw = wfs(feats).fetch_by_xy(LON, LAT)
    n, e = (float(v) for v in seen["bbox"].split(",")[:2])
    assert abs(n - PT.y) < 1 and abs(e - PT.x) < 1 and seen["bbox"].endswith("urn:ogc:def:crs:EPSG::2180")
    assert raw.teryt_id == PID and raw.source_type == "live" and not raw.is_demonstration
    assert raw.administrative_location["voivodeship"] == "kujawsko-pomorskie"


def test_swapped_axes_in_response_are_detected_and_fixed():
    raw = wfs(lambda req: [feature(PID, square(PT.x, PT.y, swap=True))]).fetch_by_xy(LON, LAT)
    prof = build_parcel_profile(raw)
    assert prof.parcel_id == PID and abs(prof.metrics.area_m2 - 6400) < 1
    assert abs(prof.coordinates.point_on_surface_lon - LON) < 0.01  # geometria trafiła we właściwe miejsce


def test_feature_not_covering_point_is_rejected():
    far = square(PT.x + 5000, PT.y + 5000)
    with pytest.raises(NotFoundError):
        wfs(lambda req: [feature(PID, far)]).fetch_by_xy(LON, LAT)


def test_point_on_shared_boundary_is_ambiguous():
    a = square(PT.x - 40, PT.y)
    b = square(PT.x + 40, PT.y)
    with pytest.raises(AmbiguousResultError):
        wfs(lambda req: [feature(PID, a), feature("046301_1.0015.18", b)]).fetch_by_xy(LON, LAT)


def test_by_id_confirms_orientation_with_point_query():
    geom_swapped = square(PT.x, PT.y, swap=True)  # serwer odsyła osie zamienione

    def feats(req):
        if "FILTER" in req.url.params:
            assert PID in req.url.params["FILTER"]
            return [feature(PID, geom_swapped)]
        # zapytanie weryfikujące: realistycznie — tylko gdy BBOX (N,E) trafia w prawdziwe położenie działki
        n0, e0, n1, e1 = (float(v) for v in req.url.params["BBOX"].split(",")[:4])
        hit = (e0 <= PT.x + 40 and e1 >= PT.x - 40 and n0 <= PT.y + 40 and n1 >= PT.y - 40)
        return [feature(PID, geom_swapped)] if hit else []
    raw = wfs(feats).fetch_by_id(PID)
    assert raw.teryt_id == PID
    assert abs(build_parcel_profile(raw).coordinates.point_on_surface_lat - LAT) < 0.01


def test_non_json_wfs_exception_is_format_error():
    client = EgibWfsClient(transport=httpx.MockTransport(
        lambda r: httpx.Response(200, text="<ows:ExceptionReport>err</ows:ExceptionReport>")), max_retries=0)
    with pytest.raises(SourceFormatError):
        client.fetch_by_xy(LON, LAT)


def test_live_adapter_falls_back_to_wfs_when_uldk_rate_limited_and_says_so():
    calls = {"uldk": 0}

    def uldk_handler(req):
        calls["uldk"] += 1
        return httpx.Response(302, headers={"location": "https://integracja01.gugik.gov.pl/resp/uldk.php"})
    ad = LiveAdapters(uldk_client=LiveUldkClient(transport=httpx.MockTransport(uldk_handler), max_retries=0),
                      wfs_client=wfs(lambda req: [feature(PID, square(PT.x, PT.y))]), parcel_source="auto")
    p1 = build_parcel_profile(ad.parcel_by_xy(LON, LAT))
    assert ad.last_parcel_source == "wfs" and p1.source.source_type == "live" and not p1.source.is_demonstration
    assert any("limitu zapytań" in n for n in p1.notices) and any("WFS EGiB" in n for n in p1.notices)
    ad.parcel_by_xy(LON, LAT)
    assert calls["uldk"] == 1  # po wykryciu blokady ULDK jest czasowo pomijane


def test_uldk_only_strategy_does_not_fall_back():
    from app.services.errors import SourceRateLimitedError
    ad = LiveAdapters(uldk_client=LiveUldkClient(transport=httpx.MockTransport(
        lambda r: httpx.Response(302, headers={"location": "https://integracja01.gugik.gov.pl/resp/uldk.php"})),
        max_retries=0), wfs_client=wfs(lambda req: []), parcel_source="uldk")
    with pytest.raises(SourceRateLimitedError):
        ad.parcel_by_id(PID)


def test_uldk_not_found_does_not_trigger_second_source():
    ad = LiveAdapters(uldk_client=LiveUldkClient(transport=httpx.MockTransport(
        lambda r: httpx.Response(200, text="-1 brak\n")), max_retries=0),
        wfs_client=wfs(lambda req: pytest.fail("WFS nie powinien być wywołany")), parcel_source="auto")
    with pytest.raises(NotFoundError):
        ad.parcel_by_id(PID)


@pytest.mark.parametrize("has_parcel", [True, False])
def test_map_click_checks_wfs_when_uldk_has_no_parcel(has_parcel):
    ad = LiveAdapters(
        uldk_client=LiveUldkClient(transport=httpx.MockTransport(
            lambda r: httpx.Response(200, text="-1 brak\n")), max_retries=0),
        wfs_client=wfs(lambda req: [feature(PID, square(PT.x, PT.y))] if has_parcel else []),
        parcel_source="auto",
    )
    if not has_parcel:
        with pytest.raises(NotFoundError):
            ad.parcel_by_xy(LON, LAT)
        return

    profile = build_parcel_profile(ad.parcel_by_xy(LON, LAT))
    assert profile.parcel_id == PID
    assert profile.source.source_type == "live" and not profile.source.is_demonstration
    assert "WFS EGiB" in profile.source.service
    assert any("ULDK nie zwróciło działki" in notice for notice in profile.notices)
