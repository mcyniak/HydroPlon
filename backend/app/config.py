"""Konfiguracja prototypu HydroPlon.

Tryb pracy:
  * ``fixture`` (domyślny) — wyłącznie syntetyczne, jawnie oznaczone dane offline,
  * ``live`` — jawne adaptery usług zewnętrznych; adapter niewdrożony zwraca
    status ``unavailable`` z powodem ``adapter_not_implemented``. Nie ma cichego
    przełączenia live -> fixture.

Data "dzisiaj" jest liczona w strefie Europe/Warsaw (korekta I04) i może zostać
nadpisana zmienną ``HYDROPLON_TODAY`` (powtarzalne testy i demo).
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

APP_DIR = Path(__file__).resolve().parent
DATA_DIR = APP_DIR / "data"
FIXTURES_DIR = APP_DIR / "fixtures"
TZ = ZoneInfo("Europe/Warsaw")

APP_VERSION = "0.1.0-prototype"
VALID_MODES = ("fixture", "live")


def _env_date(name: str) -> date | None:
    raw = os.getenv(name)
    if not raw:
        return None
    return date.fromisoformat(raw)


@dataclass(frozen=True)
class Settings:
    mode: str = "fixture"
    today_override: date | None = None
    uldk_url: str = "https://uldk.gugik.gov.pl/"
    uldk_timeout_s: float = 8.0
    uldk_max_retries: int = 1
    max_wkt_chars: int = 2_000_000
    max_vertices: int = 50_000
    finance_horizon_years: int = 10
    analysis_budget_s: float = 28.0
    extra: dict = field(default_factory=dict)

    def today(self) -> date:
        """Lokalna data w Polsce (nie UTC)."""
        if self.today_override:
            return self.today_override
        return datetime.now(TZ).date()


def load_settings() -> Settings:
    mode = os.getenv("HYDROPLON_MODE", "fixture").strip().lower()
    if mode not in VALID_MODES:
        raise ValueError(f"HYDROPLON_MODE musi być jednym z {VALID_MODES}, otrzymano {mode!r}")
    return Settings(mode=mode, today_override=_env_date("HYDROPLON_TODAY"))


settings = load_settings()
