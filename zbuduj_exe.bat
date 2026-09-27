@echo off
rem Budowa TyperBot.exe (folder dist\TyperBot). Uruchom w folderze projektu.
if not exist .venv (
    py -3.12 -m venv .venv
)
call .venv\Scripts\activate.bat
pip install -r requirements.txt pyinstaller
cd packaging
pyinstaller --noconfirm --distpath ..\dist --workpath ..\build typerbot.spec
cd ..
echo.
echo Test zbudowanej aplikacji:
dist\TyperBot\TyperBot.exe --self-test
echo.
echo Gotowe: dist\TyperBot\TyperBot.exe
pause
