@echo off
rem Uruchomienie TyperBota z wirtualnego srodowiska Pythona (bez okna konsoli).
cd /d %~dp0
if not exist .venv (
    echo Najpierw zainstaluj zaleznosci - instrukcja w README.md
    pause
    exit /b 1
)
start "" .venv\Scripts\pythonw.exe -m typerbot
