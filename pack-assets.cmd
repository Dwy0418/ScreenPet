@echo off
rem Double-click this to pack the pet's frames into a shareable zip.
rem It runs the whole pipeline: download -> cut out -> classify -> zip.
rem
rem   pack-assets.cmd                          source image -> frames -> zip
rem   pack-assets.cmd --url <image url>        download the source image first
rem   pack-assets.cmd --no-cutout              skip regeneration, pack what is there
rem   pack-assets.cmd --list                   classify + check only, no zip
rem
rem Output: dist\桌宠素材包-v<version>.zip  (frames + README.txt + 清单.txt)
rem NOTE: keep this file ASCII-only - cmd reads .cmd as ANSI (GBK) and UTF-8 Chinese breaks it.
cd /d "%~dp0"

set "PY=python"
where python >nul 2>nul || set "PY=py"

%PY% tools\pack_assets.py %*
if errorlevel 1 goto :fail
echo.
echo [pack] Done. Look in the dist\ folder.
pause
exit /b 0

:fail
echo.
echo [pack] Failed. See the messages above.
pause
exit /b 1
