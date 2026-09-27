# Plik konfiguracji PyInstaller – budowa: pyinstaller packaging/typerbot.spec
# Wynik: dist/TyperBot/TyperBot.exe (folder z aplikacją, bez instalacji Pythona).
import sys

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

# Backend magazynu kluczy: na Windows Menedżer poświadczeń, gdzie indziej – tylko do testu budowy.
keyring_backends = (["keyring.backends.Windows"] if sys.platform == "win32"
                    else ["keyring.backends.fail", "keyring.backends.null"])
hidden = keyring_backends + collect_submodules("typerbot") + ["scipy.special._cdflib", "scipy.optimize"]
datas = collect_data_files("tzdata")            # strefy czasowe (Windows nie ma ich w systemie)

a = Analysis(
    ["typerbot_app.py"],
    pathex=[".."],
    hiddenimports=hidden,
    datas=datas,
    excludes=["tkinter", "matplotlib", "pandas", "IPython", "pytest",
              "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets", "PySide6.Qt3DCore", "PySide6.QtQuick",
              "PySide6.QtQml", "PySide6.QtMultimedia", "PySide6.QtPdf"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="TyperBot",
    console=False,          # okno aplikacji bez konsoli
    upx=False,
)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="TyperBot")
