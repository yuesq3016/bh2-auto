@echo off
REM simauto launcher - works from any directory.
REM
REM The project is not installed into site-packages (this machine's Python
REM cannot be written to from the sandbox that created it), so this launcher
REM does two things instead: put the project root on PYTHONPATH and run the
REM module with the project root as the working directory, so relative paths
REM in configs (templates/, sessions/) keep working.
setlocal
set "PROJECT=%~dp0.."
set "PYTHONPATH=%PROJECT%;%PYTHONPATH%"
cd /d "%PROJECT%"
"%LOCALAPPDATA%\Programs\Python\Python312\python.exe" -m simauto %*
