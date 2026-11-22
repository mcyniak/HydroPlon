from functools import partial
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from app.api.deps import get_orchestrator
from app.main import app
from app.services import address_service


@pytest.fixture
def address_client(monkeypatch):
    adapters = SimpleNamespace(mode="live")
    monkeypatch.setitem(app.dependency_overrides, get_orchestrator, lambda: SimpleNamespace(adapters=adapters))
    return TestClient(app), adapters


@pytest.mark.parametrize("query, candidates", [
    ("Marki, Andersa 1", ["Marki, Andersa 1"]),
    ("ul. Andersa 1, 05-270 Marki", ["Marki, Andersa 1"]),
    ("Andersa 1 Marki", ["Marki, Andersa 1"]),
    ("Marki Andersa 1", ["Marki Andersa 1", "Marki, Andersa 1"]),
    ("Królewskie Brzeziny 13", ["Królewskie Brzeziny 13"]),
])
def test_address_formats_and_wgs84(address_client, monkeypatch, query, candidates):
    calls = []

    def response(request):
        assert request.url.params["srid"] == "4326"
        calls.append(request.url.params["address"])
        results = {"1": {"city": "Marki", "street": "Andersa", "number": "1", "x": "21.1179", "y": "52.3426"}}
        return httpx.Response(200, json={"type": "address", "results": results if calls[-1] == candidates[-1] else None})

    monkeypatch.setattr(address_service, "Client", partial(httpx.Client, transport=httpx.MockTransport(response)))
    client, _ = address_client
    result = client.get("/api/parcels/address-search", params={"q": query})

    assert result.status_code == 200
    assert result.json() == [{"label": "Marki, Andersa 1", "lat": 52.3426, "lon": 21.1179, "kind": "address"}]
    assert calls == candidates


@pytest.mark.parametrize("status, payload, expected_status, expected_body", [
    (200, {"type": "city", "results": None}, 200, []),
    (200, {"type": "city", "results": []}, 502, "upstream_bad_format"),
    (503, {}, 502, "upstream_error"),
])
def test_empty_and_failed_address_results(address_client, monkeypatch, status, payload, expected_status, expected_body):
    transport = httpx.MockTransport(lambda _: httpx.Response(status, json=payload))
    monkeypatch.setattr(address_service, "Client", partial(httpx.Client, transport=transport))
    client, _ = address_client
    response = client.get("/api/parcels/address-search", params={"q": "Marki"})

    assert response.status_code == expected_status
    assert (response.json() if expected_status == 200 else response.json()["error_code"]) == expected_body


def test_fixture_address_search_stays_offline(address_client, monkeypatch):
    def unexpected_client(**kwargs):
        pytest.fail("W trybie fixture nie wolno wywołać usługi geokodowania.")

    monkeypatch.setattr(address_service, "Client", unexpected_client)
    client, adapters = address_client
    adapters.mode = "fixture"

    assert client.get("/api/parcels/address-search", params={"q": "Marki"}).status_code == 422
