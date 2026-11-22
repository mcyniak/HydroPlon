# HydroPlon — serwer w trybie FIXTURE: działki testowe A–D i syntetyczne dane wszystkich modułów (offline).
# Mapa testowa: http://127.0.0.1:8000/mapa   Panel: http://127.0.0.1:8000/panel   Swagger: http://127.0.0.1:8000/docs
Set-Location (Split-Path $PSScriptRoot -Parent)
$env:HYDROPLON_MODE = "fixture"
$env:PYTHONUTF8 = "1"
.\.venv\Scripts\python -m uvicorn app.main:app --app-dir backend --port 8000
