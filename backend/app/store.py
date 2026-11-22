"""Repozytorium wyników (w pamięci) z jawnymi zależnościami.

W prototypie zastępuje tabele PostGIS (`analysis_runs`, `crop_assessments`,
`plan_snapshots` ...). Interfejs (put/get/invalidate) jest niezależny od
nośnika, więc implementację Postgres można podstawić bez zmiany silników.

Każdy rekord ma rodziców; unieważnienie rekordu kaskadowo unieważnia wszystkie
wyniki zależne (specyfikacja §25: "unieważniać nieaktualne wyniki zależne").
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from app.config import TZ
from app.services.errors import NotFoundError


@dataclass
class Record:
    record_id: str
    kind: str
    payload: Any
    parents: tuple[str, ...]
    created_at: str
    valid: bool = True
    invalidated_reason: str | None = None
    children: set[str] = field(default_factory=set)


class ResultStore:
    def __init__(self) -> None:
        self._records: dict[str, Record] = {}
        self._lock = threading.RLock()

    def put(self, record_id: str, kind: str, payload: Any, parents: tuple[str, ...] = ()) -> Record:
        """Zapis idempotentny: ten sam identyfikator (te same wejścia) nie tworzy duplikatu.

        Jeśli rekord istniał, ale został unieważniony, ponowne obliczenie z tymi
        samymi wejściami przywraca go jako aktualny (deterministyczny wynik).
        """
        with self._lock:
            existing = self._records.get(record_id)
            if existing is not None:
                existing.valid = True
                existing.invalidated_reason = None
                existing.payload = payload
                return existing
            rec = Record(record_id, kind, payload, tuple(parents), datetime.now(TZ).isoformat(timespec="seconds"))
            self._records[record_id] = rec
            for p in parents:
                if p in self._records:
                    self._records[p].children.add(record_id)
            return rec

    def get_record(self, record_id: str, kind: str | None = None) -> Record:
        with self._lock:
            rec = self._records.get(record_id)
        if rec is None or (kind and rec.kind != kind):
            raise NotFoundError(f"Nie znaleziono wyniku {kind or ''} o identyfikatorze {record_id}",
                                details={"record_id": record_id, "kind": kind})
        return rec

    def get(self, record_id: str, kind: str | None = None, *, require_valid: bool = False) -> Any:
        rec = self.get_record(record_id, kind)
        if require_valid and not rec.valid:
            from app.services.errors import ConsistencyError
            raise ConsistencyError(
                f"Wynik {record_id} został unieważniony ({rec.invalidated_reason}); przelicz go ponownie.",
                details={"record_id": record_id})
        return rec.payload

    def is_valid(self, record_id: str) -> bool:
        with self._lock:
            rec = self._records.get(record_id)
            return bool(rec and rec.valid)

    def invalidate(self, record_id: str, reason: str) -> list[str]:
        """Unieważnia rekord i wszystkie zależne; zwraca listę unieważnionych identyfikatorów."""
        out: list[str] = []
        with self._lock:
            stack = [record_id]
            while stack:
                rid = stack.pop()
                rec = self._records.get(rid)
                if rec is None or not rec.valid:
                    continue
                rec.valid = False
                rec.invalidated_reason = reason
                out.append(rid)
                stack.extend(sorted(rec.children))
        return out

    def clear(self) -> None:
        with self._lock:
            self._records.clear()

    def __len__(self) -> int:
        return len(self._records)


store = ResultStore()
