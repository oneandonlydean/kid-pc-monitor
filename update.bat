@echo off
setlocal EnableExtensions
title Kid PC Monitor - Update

rem ============================================================
rem  Kid PC Monitor - one-click updater (run on the kid's PC)
rem
rem  Double-click to update the agent to the latest code:
rem    - pulls the newest code (git pull)
rem    - redeploys it to C:\ProgramData\KidPCMonitor
rem    - restarts the agent
rem
rem  Optional:
rem    update.bat auto       turn ON automatic updates (startup + daily)
rem    update.bat auto-off   turn OFF automatic updates
rem ============================================================

set "MODE=%~1"

rem --- Self-elevate to Administrator if needed ---
net session >nul 2>&1
if %errorlevel% neq 0 (
    echo Requesting administrator privileges...
    if "%~1"=="" (
        powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    ) else (
        powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs -ArgumentList '%*'"
    )
    exit /b
)

pushd "%~dp0"

echo(
echo ==================================================
echo   Kid PC Monitor - Update
echo ==================================================
echo(

rem --- Find Python ---
set "PYCMD="
where py >nul 2>&1 && set "PYCMD=py -3"
if not defined PYCMD (
    where python >nul 2>&1 && set "PYCMD=python"
)
if not defined PYCMD (
    echo [ERROR] Python was not found on PATH. Install Python and try again.
    goto :done
)

if /i "%MODE%"=="auto-off" (
    %PYCMD% "%~dp0scripts\install.py" --disable-auto-update
    goto :done
)

if /i "%MODE%"=="auto" (
    %PYCMD% "%~dp0scripts\install.py" --enable-auto-update
    echo(
    echo Running a one-time update now as well...
    echo(
)

%PYCMD% "%~dp0scripts\install.py" --update --pull

:done
popd
echo(
pause
