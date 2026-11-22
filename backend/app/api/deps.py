"""Wspólne zależności API: jeden orkiestrator na proces (tryb z konfiguracji)."""
from __future__ import annotations

from functools import lru_cache

from app.config import settings
from app.engine.orchestrator import Orchestrator


@lru_cache(maxsize=1)
def get_orchestrator() -> Orchestrator:
    return Orchestrator(mode=settings.mode, today=settings.today_override)
