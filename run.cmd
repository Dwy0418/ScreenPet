@echo off
rem Start the pet with pythonw: no console window pops up (GUI subsystem).
rem PET_LOG tells the app to append its logs to run.out (there is no console to print to).
rem NOTE: keep this file ASCII-only - cmd reads .cmd as ANSI (GBK) and UTF-8 Chinese breaks it.
rem To watch live logs instead, run "python main.py" (or "python main.py --console") in a terminal.
cd /d "%~dp0"

set "PET_LOG=1"
set "PYW=pythonw"
where pythonw >nul 2>nul || set "PYW=python"
start "" %PYW% main.py %*
