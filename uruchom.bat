@echo off
rem Uruchomienie TyperBota z wirtualnego srodowiska Pythona (bez okna konsoli).
cd /d "%~dp0"
if not exist .venv\Scripts\pythonw.exe (
    echo Najpierw zainstaluj biblioteki: dwuklik na instaluj.bat
    pause
    exit /b 1
)
start "" .venv\Scripts\pythonw.exe -m typerbot
