# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec for the Punishment Manager.

Builds `bot.py` (and its companion `installer.py`) into a single
self-contained binary on the host platform:

  Linux:   dist/punishment-manager/punishment-manager
  macOS:   dist/Punishment Manager.app/Contents/MacOS/punishment-manager
  Windows: dist/punishment-manager/punishment-manager.exe

The native installer scripts (build_macos.sh, build_windows.bat,
build_linux.sh) wrap that output into .dmg / .exe / .deb.

Run from the project root:

    pyinstaller --noconfirm --clean build/pyinstaller.spec

The result is platform-specific; you cannot cross-compile. Build each
artifact on its own OS (or in CI).
"""

import sys
from pathlib import Path

# Block-cipher for bytecode obfuscation. Keep off for debug builds.
block_cipher = None

# Project root (this file lives in build/).
PROJECT_ROOT = Path(SPECPATH).resolve().parent

# Source files (relative to project root).
SOURCES = [
    str(PROJECT_ROOT / 'bot.py'),
]
DATA_FILES = [
    # Bundle installer.py alongside the binary so the main entry point
    # can `import installer` at runtime.
    (str(PROJECT_ROOT / 'installer.py'), '.'),
]
ICON_PNG = PROJECT_ROOT / 'build' / 'icon.png'
ICON_ICO = PROJECT_ROOT / 'build' / 'icon.ico'
ICON_ICNS = PROJECT_ROOT / 'build' / 'icon.icns'

# Platform-specific target.
IS_WINDOWS = sys.platform.startswith('win')
IS_MACOS = sys.platform == 'darwin'
IS_LINUX = sys.platform.startswith('linux')

if IS_WINDOWS:
    icon_path = str(ICON_ICO) if ICON_ICO.exists() else None
elif IS_MACOS:
    icon_path = str(ICON_ICNS) if ICON_ICNS.exists() else None
else:
    icon_path = None  # Linux doesn't embed icons in the ELF binary

a = Analysis(
    SOURCES,
    pathex=[str(PROJECT_ROOT)],
    binaries=[],
    datas=DATA_FILES,
    hiddenimports=[
        'discord',
        'discord.app_commands',
        'discord.ext.commands',
        'discord.ext.tasks',
        'aiohttp',
        'sqlite3',
        # installer.py is bundled as a data file; import it via importlib.
    ],
    hookspath=[],
    runtime_hooks=[],
    excludes=[
        # Trim the binary by skipping unused stdlib modules.
        'tkinter', 'unittest', 'pydoc', 'doctest',
        'xml', 'xmlrpc', 'pdb',
    ],
    cipher=block_cipher,
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='punishment-manager',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,  # UPX often trips antivirus; leave off
    console=True,  # CLI app on every platform
    disable_windowed_traceback=False,
    target_arch=None,  # let PyInstaller pick the host arch
    codesign_identity=None,
    entitlements_file=None,
    icon=icon_path,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='punishment-manager',
)

# On macOS, wrap the COLLECT output into a real .app bundle.
if IS_MACOS:
    app = BUNDLE(
        coll,
        name='Punishment Manager.app',
        icon=str(ICON_ICNS) if ICON_ICNS.exists() else None,
        bundle_identifier='com.arena.punishment-manager',
        info_plist={
            'CFBundleName': 'Punishment Manager',
            'CFBundleDisplayName': 'Punishment Manager',
            'CFBundleShortVersionString': '1.0.0',
            'CFBundleVersion': '1.0.0',
            'CFBundleExecutable': 'punishment-manager',
            'NSHighResolutionCapable': True,
            'LSMinimumSystemVersion': '10.13',
            # Show in Dock (False would make it a faceless background app).
            'LSUIElement': False,
            'NSHumanReadableCopyright': 'MIT License',
        },
    )
