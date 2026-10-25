@echo off
rem 启动图形界面(仿 MAA 前端)
setlocal
set PY="C:\Users\14520\AppData\Local\Programs\Python\Python312\python.exe"
set HERE=%~dp0
cd /d "%HERE%"
%PY% -m simauto gui
if errorlevel 1 (
  echo.
  echo 启动失败。请检查上面的错误信息。
  pause
)
endlocal
