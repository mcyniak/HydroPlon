"""Lokalizacja adresu na mapie przez publiczną usługę UUG GUGiK."""
from __future__ import annotations

import re
from collections.abc import Iterator

from httpx import Client, HTTPError, TimeoutException
from pydantic import ValidationError

from app.models.parcel import AddressMatch
from app.services.errors import InputValidationError, SourceError, SourceFormatError, SourceTimeoutError

UUG_URL = "https://services.gugik.gov.pl/uug/"
HOUSE_NUMBER = r"\d+[a-z]?(?:[/-]\d+[a-z]?)?"


def _address_queries(query: str) -> Iterator[str]:
    # UUG rozdziela miejscowość i ulicę przecinkiem. W adresie wiejskim przecinek nie jest potrzebny.
    parts = [" ".join(part.split()) for part in query.split(",") if part.strip()]
    if len(parts) == 2:
        city, street = parts
        if re.search(rf"\s{HOUSE_NUMBER}$", city, re.IGNORECASE):
            city, street = street, city
        yield f"{city}, {_street_name(street)}"
        return

    if len(parts) == 1:
        address = parts[0]
        street_first = re.fullmatch(rf"(.+\s{HOUSE_NUMBER})\s+([^\d]+)", address, re.IGNORECASE)
        if street_first:
            street, city = street_first.groups()
            yield f"{city}, {_street_name(street)}"
        yield address
        words = address.split()
        if re.fullmatch(HOUSE_NUMBER, words[-1], re.IGNORECASE):
            for split in range(1, min(4, len(words) - 1)):
                city, street = " ".join(words[:split]), " ".join(words[split:])
                yield f"{city}, {_street_name(street)}"


def _street_name(street: str) -> str:
    return re.sub(r"^(?:ul\.\s*|ulica\s+)", "", street, flags=re.IGNORECASE)


def _parse_matches(payload: dict) -> list[AddressMatch]:
    try:
        kind = payload["type"]
        results = payload["results"]
        if results is None:
            return []
        matches = []
        for row in results.values():
            city = " ".join(filter(None, (row.get("code"), row["city"])))
            street = " ".join(filter(None, (row.get("street"), row.get("number"))))
            district = row.get("jednostka", "").strip("{}").split(",")
            county = row.get("county") or (district[2] if len(district) == 4 else None)
            label = ", ".join(filter(None, (city, street, row.get("citypart"), f"pow. {county}" if county else None)))
            matches.append(AddressMatch(label=label, lat=row["y"], lon=row["x"], kind=kind))
        return matches
    except (AttributeError, KeyError, TypeError, ValidationError) as exc:
        raise SourceFormatError("Geoportal zwrócił niepoprawne dane adresowe.") from exc


def search_address(query: str) -> list[AddressMatch]:
    query = re.sub(r"\b\d{2}-\d{3}\b", " ", query)
    query = re.sub(r",\s*Polska\s*$", "", query, flags=re.IGNORECASE)
    queries = list(_address_queries(query))
    if not queries or len("".join(re.findall(r"\w", query))) < 3:
        raise InputValidationError("Wpisz miejscowość lub adres w formacie: miejscowość, ulica i numer.")

    try:
        with Client(timeout=12.0) as client:
            for address in queries:
                response = client.get(UUG_URL, params={"request": "GetAddress", "address": address, "srid": 4326})
                response.raise_for_status()
                matches = _parse_matches(response.json())
                if matches:
                    return matches
    except TimeoutException as exc:
        raise SourceTimeoutError("Geoportal nie odpowiedział na wyszukiwanie adresu.") from exc
    except HTTPError as exc:
        raise SourceError("Wyszukiwanie adresów w Geoportalu jest chwilowo niedostępne.") from exc
    except ValueError as exc:
        raise SourceFormatError("Geoportal zwrócił niepoprawne dane adresowe.") from exc
    return []
