# Instalacja i uruchomienie HydroPlon

[Strona projektu](../README.md) · [Instrukcja obsługi](INSTRUKCJA.md)

## Wymagania

- [Python 3.13](https://www.python.org/downloads/) i przeglądarka.
- Całe repozytorium HydroPlon. Pobierz je z GitHuba przez **Code → Download ZIP** i rozpakuj albo użyj poniższych poleceń.

```bash
git clone https://github.com/mcyniak/HydroPlon.git
cd HydroPlon
```

Pierwsza instalacja zależności wymaga internetu. Przeglądarka pobiera z sieci bibliotekę mapy i jej podkład. Tryb rzeczywisty łączy się również z usługami danych.

## Windows

Otwórz [`Uruchom HydroPlon.cmd`](../Uruchom%20HydroPlon.cmd) w katalogu projektu. Przy pierwszym uruchomieniu skrypt tworzy środowisko `.venv` i instaluje zależności. Następnie włącza tryb rzeczywisty i otwiera [aplikację w przeglądarce](http://127.0.0.1:8000/app). Zamknij okno skryptu, żeby zatrzymać serwer.

Poniższe polecenia uruchamiają serwer w trybie rzeczywistym z PowerShella.

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
$env:HYDROPLON_MODE = "live"
.\.venv\Scripts\python.exe -m uvicorn app.main:app --app-dir backend --port 8000
```

## macOS i Linux

Uruchom poniższe polecenia w terminalu z katalogu projektu.

```bash
python3.13 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
HYDROPLON_MODE=live .venv/bin/python -m uvicorn app.main:app --app-dir backend --port 8000
```

Wejdź na [127.0.0.1:8000/app](http://127.0.0.1:8000/app). Serwer zatrzymasz skrótem `Ctrl+C`. Przy zajętym porcie zmień `--port 8000` na wolny numer i użyj go także w adresie przeglądarki.

## Tryb testowy

Na **Windows** po zainstalowaniu zależności uruchom z PowerShella `powershell -File scripts\start_fixture.ps1`.

Na **macOS i Linux** użyj polecenia startowego z poprzedniej sekcji z `HYDROPLON_MODE=fixture`.

Otwórz [przykładową działkę A z datą 15.06.2019](http://127.0.0.1:8000/app?parcel=049901_2.0001.101&date=2019-06-15) i przejdź do wyboru uprawy. Możesz też użyć przycisków **Działki testowe** w kreatorze. Tryb testowy wykorzystuje syntetyczne dane i działki A–D.

## Narzędzia deweloperskie

- **Testy** — uruchom `.\.venv\Scripts\python.exe -m pytest -q` na Windows albo `.venv/bin/python -m pytest -q` na macOS i Linux.
- **Style** — do zmiany plików Sass użyj Node.js i poleceń `npm ci` oraz `npm run css`. Do samego uruchomienia aplikacji wystarcza CSS zapisany w repozytorium.
- **API** — po uruchomieniu serwera otwórz [Swagger](http://127.0.0.1:8000/docs), [status aplikacji](http://127.0.0.1:8000/api/health) lub [status źródeł](http://127.0.0.1:8000/api/system/sources).
- **Dane IMGW** — repozytorium zawiera kopię dobowego archiwum. Aby ją odświeżyć, uruchom `scripts/import_imgw.py` Pythonem ze środowiska `.venv`.
