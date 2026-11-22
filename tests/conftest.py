"""Wspólne fixtury testów prototypu HydroPlon (tryb fixture, stała data odniesienia)."""
from __future__ import annotations

from datetime import date

import pytest

from app.engine.orchestrator import Orchestrator
from app.services.parcel_service import parcel_cache
from app.store import ResultStore

TODAY = date(2026, 11, 21)
HIST = date(2019, 6, 15)
PARCEL_A = "049901_2.0001.101"
PARCEL_B = "069901_2.0001.202/3"
PARCEL_C = "149901_2.0001.303"
PARCEL_D = "049901_2.0002.404/1"
VOLATILE_KEYS = {"generated_at", "fetched_at", "created_at", "from_cache"}


@pytest.fixture()
def orch() -> Orchestrator:
    parcel_cache.clear()
    return Orchestrator(mode="fixture", result_store=ResultStore(), today=TODAY)


def strip_volatile(obj):
    """Usuwa pola czasu wygenerowania (jedyne dopuszczalne różnice między powtórzeniami)."""
    if isinstance(obj, dict):
        return {k: strip_volatile(v) for k, v in obj.items() if k not in VOLATILE_KEYS}
    if isinstance(obj, list):
        return [strip_volatile(v) for v in obj]
    return obj


def chain(orch: Orchestrator, parcel_id: str, crop_id: str, as_of=HIST, fc=None):
    snap = orch.analyze(parcel_id, as_of)
    ca = orch.assess(snap.analysis_id, crop_id)
    rk = orch.recommend(snap.analysis_id, ca.crop_assessment_id, fc)
    return snap, ca, rk


def status_of(rk, method_id: str) -> str:
    return next(m.screening_status for m in rk.all_methods() if m.method_id == method_id)


def screening_of(rk, method_id: str):
    return next(s for s in rk.screening.methods if s.method_id == method_id)
