@echo off
rem Stop the running pet, then start it again with the current code.
rem
rem Why stop first:
rem   1. the pet reads the sources at startup - edits do nothing while it runs;
rem   2. it writes config/memory back on exit, so editing while it runs gets lost;
rem   3. it holds the global hotkeys exclusively, which makes the hotkey checks
rem      in tools\smoke_test.py fail with "registered but no key received".
rem
rem   restart.cmd            stop + start
rem   restart.cmd --stop     stop only (run this before tools\smoke_test.py)
rem   restart.cmd --start    start only
rem   restart.cmd --status   just show whether it is running
rem
rem NOTE: keep this file ASCII-only - cmd reads .cmd as ANSI (GBK) and UTF-8 Chinese breaks it.
setlocal
cd /d "%~dp0"

set "PY=python"
where python >nul 2>nul || set "PY=py"
%PY% "tools\restart_pet.py" %*
