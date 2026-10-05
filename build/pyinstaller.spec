# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec for the Sentinel.

Builds `bot.py` and its imported modules (`rules.py`, `reaction_roles.py`,
`dashboard.py`), plus
its companion `installer.py` and dashboard UI, into a self-contained binary:

  Linux:   dist/sentinel/sentinel
  macOS:   dist/Sentinel.app/Contents/MacOS/sentinel
  Windows: dist/sentinel/sentinel.exe

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
def _read_version() -> str:
    """Version for this bundle, from the VERSION file at the project root.

    It is also bundled as a data file so `sentinel --version`
    reports the version of the build that is actually installed, not of
    whatever source tree happens to be around.
    """
    try:
        version = (PROJECT_ROOT / 'VERSION').read_text(encoding='utf-8').strip()
    except OSError:
        version = ''
    return version or '0.0.0+unknown'


VERSION = _read_version()

DATA_FILES = [
    # Bundle installer.py alongside the binary so the main entry point
    # can `import installer` at runtime, and ship the dashboard's web UI.
    (str(PROJECT_ROOT / 'installer.py'), '.'),
    (str(PROJECT_ROOT / 'dashboard.html'), '.'),
    (str(PROJECT_ROOT / 'VERSION'), '.'),
]

# Service-unit / launchd-plist templates, so `--install-service` works from
# the packaged binary too (bot.paths.resource_path looks for these under
# 'build/<os>/'). The app tree itself is read-only at install time, so these
# are read-only inputs - never write next to them.
for _rel in (
    ('build/linux/sentinel.service', 'build/linux'),
    ('build/linux/sentinel.desktop', 'build/linux'),
    ('build/macos/com.arena.sentinel.plist', 'build/macos'),
):
    _src = PROJECT_ROOT / _rel[0]
    if _src.exists():
        DATA_FILES.append((str(_src), _rel[1]))
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
        'aiohttp.web',
        'sqlite3',
        # bot.py's `import paths` is picked up by the analysis, but
        # installer.py is bundled as a data file (not analysed), so list the
        # module explicitly to be safe.
        'paths',
        # The shared /manage command group lives in its own module so
        # bot.py and rules.py can both import it.
        'command_tree',
        # Rules-Markdown renderer used by the dashboard's rules preview.
        'discord_markdown',
        'rules',
        # Reaction-role menus (dashboard-published posts + the reaction cog).
        'reaction_roles',
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
    name='sentinel',
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
    name='sentinel',
)

# On macOS, wrap the COLLECT output into a real .app bundle.
if IS_MACOS:
    app = BUNDLE(
        coll,
        name='Sentinel.app',
        icon=str(ICON_ICNS) if ICON_ICNS.exists() else None,
        bundle_identifier='com.arena.sentinel',
        info_plist={
            'CFBundleName': 'Sentinel',
            'CFBundleDisplayName': 'Sentinel',
            'CFBundleShortVersionString': VERSION,
            'CFBundleVersion': VERSION,
            'CFBundleExecutable': 'sentinel',
            'NSHighResolutionCapable': True,
            'LSMinimumSystemVersion': '10.13',
            # Show in Dock (False would make it a faceless background app).
            'LSUIElement': False,
            'NSHumanReadableCopyright': 'MIT License',
        },
    )
