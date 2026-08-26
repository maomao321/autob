# -*- mode: python ; coding: utf-8 -*-
from pathlib import Path
import sys


project_root = Path(SPECPATH)
dependency_root = project_root / ".packaging"
is_windows = sys.platform == "win32"
is_macos = sys.platform == "darwin"
icon_png = project_root / "packaging" / "assets" / "autoquant-icon.png"
icon_windows = project_root / "packaging" / "assets" / "autoquant-icon.ico"
icon_macos = project_root / "packaging" / "assets" / "autoquant-icon.icns"

a = Analysis(
    [str(project_root / "frontend" / "autoquant_frontend" / "__main__.py")],
    pathex=[
        str(project_root / "frontend"),
        str(project_root / "backend"),
        str(project_root / "shared"),
        str(dependency_root),
    ],
    binaries=[],
    datas=[(str(icon_png), "assets")],
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
    icon=str(icon_windows) if is_windows else None,
)

if is_macos:
    app = BUNDLE(
        exe,
        name="AutoQuant.app",
        icon=str(icon_macos),
        bundle_identifier="com.autoquant.desktop",
        info_plist={
            "CFBundleDisplayName": "AutoQuant",
            "CFBundleName": "AutoQuant",
            "CFBundleShortVersionString": "0.5.0",
            "CFBundleVersion": "0.5.0",
            "NSHighResolutionCapable": True,
        },
    )
