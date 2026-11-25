# PyInstaller spec for the bundled helper (onedir, wrapped as a background-only .app).
# Build with packaging/build-helper.sh; do not run directly from the dev venv.
# ruff: noqa
import os

from PyInstaller.utils.hooks import collect_all, collect_submodules

VERSION = os.environ["JEV_VERSION"]
BUNDLE_ID = "com.soderlind.JevMailTriage.Helper"

datas, binaries, hiddenimports = [], [], []
for package in ("lingua", "typesafe_sdk", "truststore", "rich"):
    d, b, h = collect_all(package)
    datas += d
    binaries += b
    hiddenimports += h
hiddenimports += collect_submodules("keyring.backends")
hiddenimports += collect_submodules("jev_mail_triage")
hiddenimports += ["pydantic", "pydantic_core"]

a = Analysis(
    ["helper_entry.py"],
    pathex=["../src"],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=["tkinter", "_tkinter", "pytest", "IPython"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="jev-mail-triage-helper",
    console=True,
    strip=False,
    upx=False,
    target_arch="arm64",
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="jev-mail-triage-helper")
app = BUNDLE(
    coll,
    name="JevMailTriageHelper.app",
    bundle_identifier=BUNDLE_ID,
    version=VERSION,
    info_plist={
        "CFBundleShortVersionString": VERSION,
        "CFBundleVersion": VERSION,
        "LSBackgroundOnly": True,
        "LSUIElement": True,
        "NSHumanReadableCopyright": "Per Søderlind",
    },
)
