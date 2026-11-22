# HydroPlon - serwer w trybie LIVE: dzialki z GUGiK (ULDK, przy blokadzie WFS EGiB),
# analiza z publicznych zrodel (SoilGrids, NMT GUGiK, BDOT10k, MZP, IMGW, Sentinel-2 Earth Search).
# Mapa testowa: http://127.0.0.1:8000/mapa   Swagger: http://127.0.0.1:8000/docs
Set-Location (Split-Path $PSScriptRoot -Parent)
$env:HYDROPLON_MODE = "live"
$env:PYTHONUTF8 = "1"
.\.venv\Scripts\python -m uvicorn app.main:app --app-dir backend --port 8000
