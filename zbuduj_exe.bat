@echo off
rem Budowa TyperBot.exe (folder dist\TyperBot). Uruchom w folderze projektu.
cd /d "%~dp0"
call instaluj.bat bez-pauzy
if errorlevel 1 (
    pause
    exit /b 1
)
.venv\Scripts\python.exe -m pip install --disable-pip-version-check pyinstaller
cd packaging
..\.venv\Scripts\python.exe -m PyInstaller --noconfirm --distpath ..\dist --workpath ..\build typerbot.spec
cd ..
echo.
echo Test zbudowanej aplikacji:
dist\TyperBot\TyperBot.exe --self-test
echo.
echo Gotowe: dist\TyperBot\TyperBot.exe
pause
