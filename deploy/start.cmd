@echo off
REM Production start: single Uvicorn worker.
REM Set HOST=0.0.0.0 before running to allow LAN access.
REM Single worker on purpose: embedding + reranker stay in VRAM; multiple workers would load both twice.
REM Logs go to ..\logs\server.log (kept outside the code tree).

cd /d "%~dp0.."
set ROOT=%~dp0..\..

if "%HOST%"=="" set HOST=127.0.0.1
if "%PORT%"=="" set PORT=8000

if not exist ".venv\Scripts\python.exe" (
  echo [ERROR] .venv not found. Create it and install requirements.txt first.
  exit /b 1
)

if not exist "%ROOT%\logs" mkdir "%ROOT%\logs"

echo [1/2] Checking port %PORT% ...
for /f "tokens=5" %%p in ('netstat -ano ^| findstr ":%PORT% .*LISTENING"') do (
  echo [ERROR] Port %PORT% is already in use by PID %%p
  echo         Run deploy\stop.cmd first.
  exit /b 1
)

echo [2/2] Starting http://%HOST%:%PORT% ...
.venv\Scripts\python.exe -m uvicorn app.main:app ^
  --host %HOST% --port %PORT% ^
  --workers 1 ^
  --timeout-keep-alive 65 ^
  --log-level info >> "%ROOT%\logs\server.log" 2>&1

echo Server stopped. See %ROOT%\logs\server.log
