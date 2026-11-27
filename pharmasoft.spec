# PyInstaller build recipe. Build with:  powershell -File build.ps1
# Output: dist\DrShafiqAqilClinic\DrShafiqAqilClinic.exe (+ _internal folder), zipped by build.ps1.
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files

ROOT = Path(SPECPATH)
APP = ROOT / "labapp"

datas = [
    (str(APP / "templates"), "labapp/templates"),
    (str(APP / "static"), "labapp/static"),
    (str(APP / "schema.sql"), "labapp"),
    (str(APP / "seed_tests.sql"), "labapp"),
    (str(APP / "test_definitions"), "labapp/test_definitions"),
]
# Migrations are loaded from files at runtime, so ship them as data (not as compiled modules).
datas += [(str(p), "labapp/migrations") for p in (APP / "migrations").glob("[0-9][0-9][0-9]_*.*")]
datas += collect_data_files("playwright")   # Playwright driver (node.exe + JS); browsers are NOT bundled
datas += collect_data_files("barcode")

a = Analysis(
    [str(ROOT / "run.py")],
    pathex=[str(ROOT)],
    datas=datas,
    hiddenimports=["labapp.catalog"],        # imported only by a migration file
    excludes=["tkinter", "unittest", "tests", "PyInstaller"],
)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="DrShafiqAqilClinic", console=True)
coll = COLLECT(exe, a.binaries, a.datas, name="DrShafiqAqilClinic")
