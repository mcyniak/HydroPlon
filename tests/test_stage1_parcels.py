"""Etap 1 — identyfikator TERYT, parser ULDK, geometria, cache, oba endpointy (testy 1–3 specyfikacji + §42.1)."""
from __future__ import annotations

import httpx
import pytest

from app.services.errors import (
    AmbiguousResultError,
    InputValidationError,
    NotFoundError,
    SourceError,
    SourceFormatError,
    SourceTimeoutError,
)
from app.services.geometry_service import parse_and_repair, process_parcel_geometry
from app.services.parcel_service import parcel_cache
from app.services.uldk_service import LiveUldkClient, normalize_teryt_id, parse_uldk_response, validate_lon_lat
from conftest import PARCEL_A, PARCEL_D

SQUARE_100M = "POLYGON((500000 500000, 500100 500000, 500100 500100, 500000 500100, 500000 500000))"


# --- 1. Poprawność identyfikatora -----------------------------------------------------------------
@pytest.mark.parametrize("raw,expected", [
    ("040301_1.0005.124/2", "040301_1.0005.124/2"),
    ("  040301_1.0005.124/2 ", "040301_1.0005.124/2"),
    ("146501_1.0001.AR_12.45", "146501_1.0001.AR_12.45"),
    ("146501_1.0001.ar_12.45", "146501_1.0001.AR_12.45"),
    ("040301_1.0005.12 4/2", "040301_1.0005.124/2"),
])
def test_valid_teryt_ids_normalized(raw, expected):
    assert normalize_teryt_id(raw) == expected


@pytest.mark.parametrize("raw", ["", "abc", "040301_1.0005.", "040301-1.0005.124", "040301_1.0005.124/2/3",
                                 "040301_1.0005.12a", "040301_1.0005.124;DROP", "0403_1.0005.124"])
def test_invalid_teryt_ids_rejected(raw):
    with pytest.raises(InputValidationError):
        normalize_teryt_id(raw)


# --- Kolejność lon/lat i obwiednia Polski -----------------------------------------------------------
def test_lon_lat_order_and_bbox():
    assert validate_lon_lat(18.6, 53.0) == (18.6, 53.0)
    assert validate_lon_lat(18.5, 54.83)  # północ Polski (Jastrzębia Góra ~54,83°N)
    with pytest.raises(InputValidationError, match="zamienioną kolejność"):
        validate_lon_lat(53.0, 18.6)
    with pytest.raises(InputValidationError):
        validate_lon_lat(2.35, 48.85)  # Paryż


def test_live_client_sends_lon_lat_4326_order():
    seen = {}

    def handler(request: httpx.Request):
        seen["xy"] = request.url.params["xy"]
        return httpx.Response(200, text="-1 brak\n")
    client = LiveUldkClient(transport=httpx.MockTransport(handler), max_retries=0)
    with pytest.raises(NotFoundError):
        client.fetch_by_xy(18.6, 53.0)
    assert seen["xy"] == "18.6000000,53.0000000,4326"


# --- Parser ULDK --------------------------------------------------------------------------------------
def _resp(wkt=SQUARE_100M, teryt="040301_1.0005.124/2", n=8):
    fields = [f"SRID=2180;{wkt}", teryt, "woj", "pow", "gm", "obr", "124/2", "EGIB"][:n]
    return "0\n" + "|".join(fields) + "\n"


def test_parser_ok_strips_srid_prefix():
    raw = parse_uldk_response(_resp(), source_type="live", is_demonstration=False)
    assert raw.raw_wkt_2180.startswith("POLYGON")
    assert raw.administrative_location["parcel_number"] == "124/2"


@pytest.mark.parametrize("text,exc", [
    ("-1 brak wynikow\n", NotFoundError),
    ("", SourceFormatError),
    ("0\n", SourceFormatError),
    ("2 nieznany status\n", SourceFormatError),
    ("00\n" + _resp().split("\n", 1)[1], SourceFormatError),   # status != dokładnie "0"
    (_resp(n=3), SourceFormatError),                          # zmiana schematu pól
    (_resp().replace("SRID=2180", "SRID=4326"), SourceFormatError),
    (_resp() + _resp().split("\n", 1)[1], AmbiguousResultError),
    (_resp(wkt="POINT(1 2)"), SourceFormatError),
])
def test_parser_errors(text, exc):
    with pytest.raises(exc):
        parse_uldk_response(text, source_type="live", is_demonstration=False)


def test_fixture_malformed_uldk_response(orch):
    with pytest.raises(SourceFormatError):
        orch.parcel_by_id("049901_2.0001.999")


def test_fixture_parcel_not_found(orch):
    with pytest.raises(NotFoundError):
        orch.parcel_by_id("049901_2.0001.998")


# --- Brak odpowiedzi / błędy usługi -------------------------------------------------------------------
def test_live_timeout_and_server_error_are_distinguished():
    def timeout(request):
        raise httpx.ReadTimeout("t", request=request)
    calls = {"n": 0}

    def err500(request):
        calls["n"] += 1
        return httpx.Response(503, text="down")
    with pytest.raises(SourceTimeoutError):
        LiveUldkClient(transport=httpx.MockTransport(timeout), max_retries=1).fetch_by_id("040301_1.0005.124/2")
    with pytest.raises(SourceError) as e:
        LiveUldkClient(transport=httpx.MockTransport(err500), max_retries=1).fetch_by_id("040301_1.0005.124/2")
    assert calls["n"] == 2  # ograniczone ponowienie: 1 próba + 1 retry
    assert not isinstance(e.value, SourceTimeoutError)


# --- 2./3. Geometria i powierzchnia -------------------------------------------------------------------
def test_polygon_area_in_2180():
    g = process_parcel_geometry(SQUARE_100M)
    assert g["metrics"]["area_m2"] == 10000.0
    assert g["metrics"]["area_ha"] == 1.0
    assert g["metrics"]["perimeter_m"] == 400.0
    assert not g["metrics"]["is_multipart"]
    lon, lat = g["coordinates"]["centroid_lon"], g["coordinates"]["centroid_lat"]
    assert 14 < lon < 24.5 and 49 < lat < 55  # oś: lon, lat


def test_multipolygon_kept_whole_and_area_summed():
    mp = ("MULTIPOLYGON(((500000 500000, 500100 500000, 500100 500100, 500000 500100, 500000 500000)),"
          "((500200 500000, 500300 500000, 500300 500050, 500200 500050, 500200 500000)))")
    g = process_parcel_geometry(mp)
    assert g["metrics"]["is_multipart"] and g["metrics"]["part_count"] == 2
    assert g["metrics"]["area_m2"] == 15000.0
    assert g["geojson_4326"]["type"] == "MultiPolygon"


def test_centroid_vs_point_on_surface_for_u_shape():
    u = ("POLYGON((500000 500000, 500300 500000, 500300 500300, 500200 500300, 500200 500100, "
         "500100 500100, 500100 500300, 500000 500300, 500000 500000))")
    c = process_parcel_geometry(u)["coordinates"]
    assert c["centroid_inside_parcel"] is False  # centroid poza działką w kształcie U
    from shapely import wkt
    from shapely.geometry import Point
    assert wkt.loads(u).covers(Point(c["point_on_surface_x_2180"], c["point_on_surface_y_2180"]))


def test_invalid_bowtie_polygon_is_repaired():
    bowtie = "POLYGON((500000 500000, 500100 500100, 500100 500000, 500000 500100, 500000 500000))"
    geom, repaired = parse_and_repair(bowtie)
    assert repaired and geom.is_valid and geom.area > 0


@pytest.mark.parametrize("bad", ["POLYGON((1 2, 3", "LINESTRING(500000 500000, 500100 500100)",
                                 "POLYGON((18 52, 18.1 52, 18.1 52.1, 18 52.1, 18 52))"])
def test_invalid_geometry_rejected(bad):
    with pytest.raises(SourceFormatError):
        parse_and_repair(bad)


def test_small_parcel_not_rounded_to_zero():
    tiny = "POLYGON((500000 500000, 500010 500000, 500010 500010, 500000 500010, 500000 500000))"  # 100 m2
    assert process_parcel_geometry(tiny)["metrics"]["area_ha"] == 0.01


# --- Endpointy: ten sam ParcelProfile; cache --------------------------------------------------------
def test_by_id_and_by_point_return_identical_profile(orch):
    by_id = orch.parcel_by_id(PARCEL_A)
    p = by_id.coordinates
    parcel_cache.clear()
    by_pt = orch.parcel_by_point(p.point_on_surface_lon, p.point_on_surface_lat)
    assert by_id.model_dump(exclude={"source", "from_cache"}) == by_pt.model_dump(exclude={"source", "from_cache"})
    assert by_id.source.is_demonstration is True and by_id.source.source_type == "fixture"


def test_point_outside_any_parcel(orch):
    with pytest.raises(NotFoundError):
        orch.parcel_by_point(16.0, 50.5)


def test_fixture_multipolygon_parcel(orch):
    d = orch.parcel_by_id(PARCEL_D)
    assert d.metrics.is_multipart and d.metrics.part_count == 2
    assert d.geojson_feature["geometry"]["type"] == "MultiPolygon"


def test_cache_hit_and_expiry(orch):
    parcel_cache.clear()
    first = orch.parcel_by_id(PARCEL_A)
    second = orch.parcel_by_id(PARCEL_A)
    assert first.from_cache is False and second.from_cache is True
    assert first.model_dump(exclude={"from_cache"}) == second.model_dump(exclude={"from_cache"})
    parcel_cache.expire_all()
    third = orch.parcel_by_id(PARCEL_A)
    assert third.from_cache is False
    assert third.geometry_version == first.geometry_version


def test_live_redirect_is_not_followed_and_decoy_payload_rejected():
    def redirect(request):
        return httpx.Response(302, headers={"location": "https://integracja01.gugik.gov.pl/resp/uldk.php"})
    with pytest.raises(SourceError) as e:
        LiveUldkClient(transport=httpx.MockTransport(redirect), max_retries=0).fetch_by_id("040301_1.0005.124/2")
    assert e.value.details["redirect_host"] == "integracja01.gugik.gov.pl"
    decoy = "0\n0101000020E6100000000000000080564000000000008056400101010101\n"  # obserwowana pozorna odpowiedź
    with pytest.raises(SourceFormatError):
        parse_uldk_response(decoy, source_type="live", is_demonstration=False)
