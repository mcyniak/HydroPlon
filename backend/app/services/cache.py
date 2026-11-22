"""Wspólny, ograniczony cache wyników źródeł live (SQLite, biblioteka standardowa).

* powstaje wyłącznie podczas normalnego korzystania z aplikacji (brak wstępnego ładowania działek),
* klucz = (przestrzeń nazw, klucz, wersja źródła/algorytmu) — zmiana wersji unieważnia stare wpisy,
* limit rozmiaru (domyślnie 64 MB) z usuwaniem najdawniej używanych wpisów (LRU) i TTL per przestrzeń,
* wyniki różnych działek i dat nie mieszają się, bo działka/geometria/scena są częścią klucza.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from app.config import APP_DIR

DEFAULT_PATH = APP_DIR / "data_cache" / "live_cache.sqlite"
MAX_BYTES = 64 * 1024 * 1024
TTL_S = {"soilgrids": 180 * 86400, "terrain": 180 * 86400, "s2scene": 365 * 86400, "s2search": 6 * 3600,
         "hydro": 30 * 86400, "flood": 30 * 86400}
_lock = threading.Lock()
_path: Path = DEFAULT_PATH
_stats = {"hits": 0, "misses": 0, "puts": 0, "evictions": 0}


def configure(path: Path | str) -> None:
    global _path
    _path = Path(path)


def _con() -> sqlite3.Connection:
    _path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(_path, timeout=10)
    con.execute("""CREATE TABLE IF NOT EXISTS cache(ns TEXT, key TEXT, version TEXT, created REAL, accessed REAL,
                   size INTEGER, value TEXT, PRIMARY KEY(ns, key, version))""")
    return con


def get(ns: str, key: str, version: str) -> Any | None:
    now = time.time()
    with _lock:
        con = _con()
        try:
            row = con.execute("SELECT value, created FROM cache WHERE ns=? AND key=? AND version=?",
                              (ns, key, version)).fetchone()
            if row is None or now - row[1] > TTL_S.get(ns, 30 * 86400):
                _stats["misses"] += 1
                return None
            con.execute("UPDATE cache SET accessed=? WHERE ns=? AND key=? AND version=?", (now, ns, key, version))
            con.commit()
            _stats["hits"] += 1
            return json.loads(row[0])
        finally:
            con.close()


def put(ns: str, key: str, version: str, value: Any) -> None:
    payload = json.dumps(value, ensure_ascii=False, default=float)
    now = time.time()
    with _lock:
        con = _con()
        try:
            con.execute("DELETE FROM cache WHERE ns=? AND key=? AND version<>?", (ns, key, version))  # stare wersje
            con.execute("INSERT OR REPLACE INTO cache VALUES (?,?,?,?,?,?,?)",
                        (ns, key, version, now, now, len(payload), payload))
            total = con.execute("SELECT COALESCE(SUM(size),0) FROM cache").fetchone()[0]
            if total > MAX_BYTES:
                for ns_, key_, ver_, size_ in con.execute(
                        "SELECT ns, key, version, size FROM cache ORDER BY accessed").fetchall():
                    if total <= MAX_BYTES * 0.8:
                        break
                    con.execute("DELETE FROM cache WHERE ns=? AND key=? AND version=?", (ns_, key_, ver_))
                    total -= size_
                    _stats["evictions"] += 1
            con.commit()
            _stats["puts"] += 1
        finally:
            con.close()


def stats() -> dict:
    with _lock:
        con = _con()
        try:
            n, size = con.execute("SELECT COUNT(*), COALESCE(SUM(size),0) FROM cache").fetchone()
        finally:
            con.close()
    return {**_stats, "entries": n, "bytes": size, "max_bytes": MAX_BYTES, "path": str(_path)}
