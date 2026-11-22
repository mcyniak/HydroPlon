@echo off
setlocal
pushd "%~dp0" || exit /b 1

if not exist "backend\app\main.py" (
    echo Rozpakuj cale repozytorium i uruchom ten plik z katalogu HydroPlon.
    goto failed
)

set "HYDROP_LAUNCH_PORT=8000"
set "HYDROP_LAUNCH_BASE=http://127.0.0.1:%HYDROP_LAUNCH_PORT%"
set "HYDROP_LAUNCH_URL=%HYDROP_LAUNCH_BASE%/app"
set "HYDROP_LAUNCH_CHECK=try { $health = Invoke-RestMethod -Uri ($env:HYDROP_LAUNCH_BASE + '/api/health') -TimeoutSec 2; $health.status -eq 'ok' -and $health.data_mode -eq 'live' -and $health.app_version } catch { $false }"
powershell.exe -NoProfile -NonInteractive -Command "if (& { %HYDROP_LAUNCH_CHECK% }) { exit 0 }; exit 1"
if not errorlevel 1 (
    start "" "%HYDROP_LAUNCH_URL%"
    goto finished
)

set "HYDROP_LAUNCH_PYTHON=.venv\Scripts\python.exe"
if not exist "%HYDROP_LAUNCH_PYTHON%" (
    echo Tworze srodowisko Pythona dla HydroPlon...
    call :create_venv
    if errorlevel 1 goto failed
)

fc /b requirements.txt ".venv\hydroplon-requirements.txt" >nul 2>&1
if errorlevel 1 (
    echo Instaluje zaleznosci HydroPlon. To moze potrwac kilka minut.
    "%HYDROP_LAUNCH_PYTHON%" -X utf8 -m pip install --disable-pip-version-check --no-input --quiet -r requirements.txt
    if errorlevel 1 goto failed
    copy /y requirements.txt ".venv\hydroplon-requirements.txt" >nul
)

set "HYDROPLON_MODE=live"
set "PYTHONUTF8=1"
echo Uruchamiam HydroPlon. Zamknij to okno, aby zatrzymac serwer.
start "" /b powershell.exe -NoProfile -NonInteractive -Command "$deadline = (Get-Date).AddSeconds(30); do { if (& { %HYDROP_LAUNCH_CHECK% }) { Start-Process $env:HYDROP_LAUNCH_URL; exit }; Start-Sleep -Milliseconds 400 } while ((Get-Date) -lt $deadline)"
"%HYDROP_LAUNCH_PYTHON%" -m uvicorn app.main:app --app-dir backend --port %HYDROP_LAUNCH_PORT%
if errorlevel 1 goto failed
goto finished

:create_venv
where /q py.exe
if not errorlevel 1 (
    py -3.13 -m venv .venv
    if not errorlevel 1 exit /b 0
)
python -c "import sys; sys.exit(sys.version_info[:2] != (3, 13))" >nul 2>&1
if errorlevel 1 (
    echo Zainstaluj Python 3.13 i uruchom ten plik ponownie.
    exit /b 1
)
python -m venv .venv
exit /b %errorlevel%

:failed
echo Nie udalo sie uruchomic HydroPlon. Szczegoly sa powyzej.
pause
popd
exit /b 1

:finished
popd
exit /b 0
