"""Identyfikatory adresowane treścią.

Identyfikator = prefiks + skrót SHA-256 kanonicznego JSON-a wejść i wersji reguł.
Te same wejścia i wersje -> ten sam identyfikator (powtarzalność, test 32).
Inne wejścia -> inny identyfikator, więc stary wynik nie może zostać użyty
dla nowego zestawu danych (test 31).
"""
from __future__ import annotations

import hashlib
import json
from datetime import date, datetime
from typing import Any


def _default(value: Any) -> Any:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, (set, frozenset)):
        return sorted(value)
    raise TypeError(f"Nie można serializować {type(value).__name__}")


def canonical_json(payload: Any) -> str:
    return json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=_default)


def content_hash(payload: Any, length: int = 16) -> str:
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()[:length]


def make_id(prefix: str, payload: Any) -> str:
    return f"{prefix}-{content_hash(payload)}"
