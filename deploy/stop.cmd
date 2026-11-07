@echo off
REM Stop the service listening on PORT (default 8000).

if "%PORT%"=="" set PORT=8000

set FOUND=0
for /f "tokens=5" %%p in ('netstat -ano ^| findstr ":%PORT% .*LISTENING"') do (
  set FOUND=1
  echo Stopping PID %%p on port %PORT% ...
  taskkill /F /PID %%p >nul 2>&1
)

if "%FOUND%"=="0" echo No service is listening on port %PORT%.
