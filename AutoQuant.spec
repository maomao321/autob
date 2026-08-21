# -*- mode: python ; coding: utf-8 -*-
from pathlib import Path
import sys


project_root = Path(SPECPATH)
dependency_root = project_root / ".packaging"
is_windows = sys.platform == "win32"
is_macos = sys.platform == "darwin"

a = Analysis(
    [str(project_root / "autoquant" / "__main__.py")],
    pathex=[str(project_root), str(dependency_root)],
    binaries=[],
    datas=[],
    hiddenimports=["websocket"],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="AutoQuant",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    version=str(project_root / "packaging" / "version_info.txt") if is_windows else None,
)

if is_macos:
    app = BUNDLE(
        exe,
        name="AutoQuant.app",
        icon=None,
        bundle_identifier="com.autoquant.desktop",
        info_plist={
            "CFBundleDisplayName": "AutoQuant",
            "CFBundleName": "AutoQuant",
            "CFBundleShortVersionString": "0.5.0",
            "CFBundleVersion": "0.5.0",
            "NSHighResolutionCapable": True,
        },
    )
