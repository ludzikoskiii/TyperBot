@echo off
rem Instalacja TyperBota (jednorazowo, kilka minut): srodowisko .venv i biblioteki z requirements.txt.
rem Dziala z kazdym 64-bitowym Pythonem 3.11 lub nowszym. Parametr "bez-pauzy" uzywa zbuduj_exe.bat.
setlocal
cd /d "%~dp0"
set "PAUZA=1"
if /i "%~1"=="bez-pauzy" set "PAUZA=0"
set "PY="

if not exist .venv\Scripts\python.exe (
    for %%C in ("py -3.13" "py -3.12" "py -3.11" "py -3" "python") do (
        if not defined PY (
            %%~C -c "import sys, struct; sys.exit(not (sys.version_info >= (3, 11) and struct.calcsize('P') == 8))" >nul 2>&1 && set "PY=%%~C"
        )
    )
    if not defined PY (
        echo.
        echo Nie znaleziono 64-bitowego Pythona 3.11 lub nowszego.
        echo Zainstaluj go z https://www.python.org/downloads/windows/
        echo i w instalatorze zaznacz opcje "Add python.exe to PATH".
        if "%PAUZA%"=="1" pause
        exit /b 1
    )
)
if not exist .venv\Scripts\python.exe (
    echo Tworzenie srodowiska .venv: %PY%
    %PY% -m venv .venv
)
if not exist .venv\Scripts\python.exe (
    echo.
    echo Nie udalo sie utworzyc srodowiska .venv - skopiuj komunikat bledu powyzej.
    if "%PAUZA%"=="1" pause
    exit /b 1
)

echo Instalacja bibliotek - to potrwa kilka minut...
.venv\Scripts\python.exe -m pip install --disable-pip-version-check -r requirements.txt
if errorlevel 1 (
    echo.
    echo Instalacja bibliotek nie powiodla sie - skopiuj komunikat bledu powyzej.
    if "%PAUZA%"=="1" pause
    exit /b 1
)
echo.
echo Gotowe. Aplikacje uruchamiasz dwuklikiem na uruchom.bat
if "%PAUZA%"=="1" pause
exit /b 0
