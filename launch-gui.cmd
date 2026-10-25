@echo off
rem ===========================================================================
rem  Honkai Gakuen 2 automation - GUI launcher (called by the desktop shortcut)
rem
rem  IMPORTANT: this file must stay 100%% ASCII.  cmd.exe reads .cmd files by
rem  byte offset using the system ANSI code page, so ANY non-ASCII byte in a
rem  comment makes it miscount the following lines and the script fails with
rem  bizarre errors like "'onkai' is not recognized" (a command parsed from
rem  the middle of a word).  Measured the hard way.
rem ===========================================================================
setlocal
set "PROJ=%~dp0"
if "%PROJ:~-1%"=="\" set "PROJ=%PROJ:~0,-1%"

rem --- locate python: PATH first, then common install locations ------------
set "PY="
for %%P in (python.exe) do if not defined PY if exist "%%~$PATH:P" set "PY=%%~$PATH:P"
if not defined PY if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" set "PY=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
if not defined PY if exist "%LOCALAPPDATA%\Programs\Python\Python311\python.exe" set "PY=%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
if not defined PY if exist "C:\Python312\python.exe" set "PY=C:\Python312\python.exe"

if not defined PY (
  echo [ERROR] Python 3.11/3.12 not found.
  echo Install Python or edit this file and set the PY variable explicitly.
  pause
  exit /b 1
)

set "PYTHONPATH=%PROJ%"
set "PYTHONIOENCODING=utf-8"
cd /d "%PROJ%"

"%PY%" -m simauto gui
if errorlevel 1 (
  echo.
  echo [ERROR] GUI failed to start. See the message above.
  pause
)
endlocal
