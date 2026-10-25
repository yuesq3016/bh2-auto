@echo off
rem Install Python dependencies for simauto.
rem NOTE: keep this file ASCII-only; cmd.exe parses .cmd by byte offset.
setlocal
set "PROJ=%~dp0"
if "%PROJ:~-1%"=="\" set "PROJ=%PROJ:~0,-1%"

set "PY="
for %%P in (python.exe) do if not defined PY if exist "%%~$PATH:P" set "PY=%%~$PATH:P"
if not defined PY if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" set "PY=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
if not defined PY if exist "%LOCALAPPDATA%\Programs\Python\Python311\python.exe" set "PY=%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
if not defined PY if exist "C:\Python312\python.exe" set "PY=C:\Python312\python.exe"

if not defined PY goto nopython

echo Using: %PY%
echo.
"%PY%" -m pip install -r "%PROJ%\requirements.txt"
if errorlevel 1 goto mirror

echo.
echo [OK] Dependencies installed. Now double-click 启动.cmd
pause
exit /b 0

:mirror
echo.
echo Default index failed, retrying with the Tsinghua mirror...
"%PY%" -m pip install -r "%PROJ%\requirements.txt" -i https://pypi.tuna.tsinghua.edu.cn/simple
if errorlevel 1 goto failed
echo.
echo [OK] Dependencies installed. Now double-click 启动.cmd
pause
exit /b 0

:failed
echo.
echo [ERROR] pip install failed. Install Python 3.11/3.12 from python.org first,
echo         then run this file again.
pause
exit /b 1

:nopython
echo [ERROR] Python 3.11/3.12 not found.
echo Install it from https://www.python.org/downloads/ (check "Add to PATH"),
echo then run this file again.
pause
exit /b 1
