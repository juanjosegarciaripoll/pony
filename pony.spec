# PyInstaller spec file for Pony Express.
#
# Prerequisites:
#   uv run mkdocs build --strict   (produces site/)
#   uv run pyinstaller pony.spec --noconfirm --clean
#
# Or use the build script which handles both steps:
#   uv run python scripts/build.py

import sys
from pathlib import Path

block_cipher = None

site_dir = Path("site")
datas: list[tuple[str, str]] = [("config-sample.toml", ".")]
if site_dir.exists():
    datas.append(("site", "site"))
else:
    print(
        "WARNING: site/ not found. "
        "Run 'uv run mkdocs build --strict' before PyInstaller "
        "to bundle the offline documentation."
    )

a = Analysis(
    ["src/pony/__main__.py"],
    pathex=[],
    binaries=[],
    datas=datas,
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

icon: str | None = None
if sys.platform == "win32":
    icon = "icons/pony-express.ico"
elif sys.platform == "darwin":
    icon = "icons/pony-express.icns"

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="pony",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=icon,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="pony",
)
