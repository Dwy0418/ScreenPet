@echo off
rem Double-click this to build the Windows app (dist\ScreenPet\ScreenPet.exe).
rem NOTE: keep this file ASCII-only - cmd reads .cmd as ANSI (GBK) and UTF-8 Chinese breaks it.
cd /d "%~dp0"

set "PY=python"
where python >nul 2>nul || set "PY=py"

%PY% -c "import PyInstaller" >nul 2>nul || (
  echo [build] Installing PyInstaller ^(build machine only^)...
  %PY% -m pip install -r requirements-dev.txt || goto :fail
)

%PY% tools\build.py %*
if errorlevel 1 goto :fail
echo.
echo [build] Done. Look in the dist\ folder.
pause
exit /b 0

:fail
echo.
echo [build] Build failed. See the messages above.
pause
exit /b 1
