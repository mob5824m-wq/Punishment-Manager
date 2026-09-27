"""
Punishment Manager - runtime filesystem locations.

Why this module exists
----------------------
Packaged builds used to derive every path from ``Path(__file__).resolve().parent``.
For a PyInstaller build that is the *read-only application tree*
(``/opt/punishment-manager/_internal`` on Linux, ``C:\\Program Files\\Punishment
Manager`` on Windows), so the bot died at import time with::

    PermissionError: [Errno 13] Permission denied: '/opt/punishment-manager/_internal/data'

Writable state (the SQLite db and the log file) and the user's config now live
in a platform-appropriate location chosen once, at import, here.

Data directory, first usable candidate wins
    1. ``$PUNISHMENT_MANAGER_DATA`` (or ``$PUNISHMENT_MANAGER_HOME``)
    2. running from source: ``<repo>/data``  (unchanged dev behaviour)
    3. packaged builds: the platform state dir
       Linux   ``/var/lib/punishment-manager`` (the .deb's state dir),
               ``$XDG_STATE_HOME/punishment-manager``,
               ``~/.local/state/punishment-manager``, ``~/.local/share/...``
       macOS   ``~/Library/Application Support/Punishment Manager``
       Windows ``%LOCALAPPDATA%\\Punishment Manager``, ``%APPDATA%\\...``
    4. packaged builds: ``<dir containing the executable>/data`` so portable /
       unzipped installs keep working
    5. a temp dir - the bot still starts, and says where it put its files

Config file, first that exists wins (so the .deb's read-only
``/etc/punishment-manager/config.json`` is honoured), otherwise created in the
data dir. A config found in a read-only place is *copied on write* into the
data dir, which then takes precedence on later runs.

Everything here is standard-library only and import-safe: it never raises, and
never writes outside a directory it has verified is writable. Call ``describe()``
or run ``punishment-manager --paths`` to see the resolved locations.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import sys
import tempfile
from pathlib import Path
from typing import Any, Iterable, Optional

__all__ = [
    "APP_NAME",
    "app_version",
    "APP_TITLE",
    "BUNDLE_DIR",
    "APP_DIR",
    "DATA_DIR",
    "CONFIG_PATH",
    "DB_PATH",
    "LOG_PATH",
    "config_path",
    "config_write_path",
    "data_dir",
    "describe",
    "ensure_writable",
    "is_frozen",
    "load_config_dict",
    "resource_path",
    "startup_notes",
    "write_config",
]

APP_NAME = "punishment-manager"          # lower-case, used for unix dirs
APP_TITLE = "Punishment Manager"          # display name, used for win/mac dirs

# Directory under which the shipped service-unit / launchd-plist resources
# are looked up in a packaged install (see build/pyinstaller.spec).
_RESOURCE_ROOTS_POSIX = ("/usr/share/punishment-manager", "/usr/local/share/punishment-manager")


# --------------------------------------------------------------------------- #
# Frozen / bundled locations
# --------------------------------------------------------------------------- #
def is_frozen() -> bool:
    """True when running a PyInstaller (or similar) bundle, not source."""
    return bool(getattr(sys, "frozen", False))


def bundle_dir() -> Optional[Path]:
    """PyInstaller's read-only resource dir (``sys._MEIPASS``), else None."""
    meipass = getattr(sys, "_MEIPASS", None)
    if not meipass:
        return None
    try:
        return Path(meipass).resolve()
    except OSError:
        return Path(meipass)


def _source_dir() -> Path:
    """Repo root when running from source (dir containing bot.py/installer.py)."""
    main_file = getattr(sys.modules.get("__main__", None), "__file__", None)
    if main_file:
        try:
            cand = Path(main_file).resolve().parent
        except OSError:
            cand = None
        if cand is not None and ((cand / "bot.py").is_file() or (cand / "installer.py").is_file()):
            return cand
    here = Path(__file__).resolve().parent
    if (here / "bot.py").is_file() or (here / "installer.py").is_file():
        return here
    return Path.cwd()


def app_dir() -> Path:
    """Where the executable lives (frozen) or where the sources live."""
    if is_frozen():
        try:
            return Path(sys.executable).resolve().parent
        except OSError:
            return Path(sys.executable).parent
    return _source_dir()


def resource_path(*parts: str) -> Optional[Path]:
    """Locate a read-only shipped resource (unit file, plist, .desktop).

    Looks in the PyInstaller bundle, then next to the sources/executable,
    then in the FHS share dir the .deb installs into. Returns None if absent.
    """
    roots: list[Path] = []
    bundled = bundle_dir()
    if bundled is not None:
        roots.append(bundled)
    roots.append(APP_DIR)
    if not is_frozen():
        roots.append(Path(__file__).resolve().parent)
    if os.name != "nt":
        roots.extend(Path(r) for r in _RESOURCE_ROOTS_POSIX)
    for root in roots:
        try:
            cand = root.joinpath(*parts)
        except (OSError, ValueError):
            continue
        if cand.is_file():
            return cand
    return None


# --------------------------------------------------------------------------- #
# Writability helpers
# --------------------------------------------------------------------------- #
def _home() -> Optional[Path]:
    try:
        return Path.home()
    except (RuntimeError, OSError):  # no HOME/PASSWORD entry (odd service env)
        return None


def _nearest_existing(path: Path) -> Optional[Path]:
    probe = path
    while not probe.exists() and probe.parent != probe:
        probe = probe.parent
    return probe if probe.exists() else None


def _dir_usable(path: Path) -> bool:
    """True if `path` is a writable dir or can be created by us."""
    if path.exists():
        return path.is_dir() and os.access(path, os.W_OK | os.X_OK)
    ancestor = _nearest_existing(path)
    return ancestor is not None and os.access(ancestor, os.W_OK | os.X_OK)


def ensure_writable(path: Path) -> Optional[Path]:
    """Create `path` if needed. Return it, or None when it isn't usable."""
    path = Path(path).expanduser()
    if not _dir_usable(path):
        return None
    try:
        path.mkdir(parents=True, exist_ok=True)
    except (OSError, ValueError):
        return None
    if path.is_dir() and os.access(path, os.W_OK):
        return path
    return None


def _dedupe(paths: Iterable[Path]) -> list[Path]:
    seen: set[str] = set()
    out: list[Path] = []
    for p in paths:
        try:
            key = str(Path(p).expanduser())
        except (OSError, ValueError):
            continue
        if key not in seen:
            seen.add(key)
            out.append(Path(key))
    return out


def _env_path(name: str) -> Optional[Path]:
    raw = os.environ.get(name, "").strip()
    return Path(raw).expanduser() if raw else None


# --------------------------------------------------------------------------- #
# Data directory
# --------------------------------------------------------------------------- #
def _platform_state_dirs() -> list[Path]:
    home = _home()
    out: list[Path] = []

    if os.name == "nt":
        for var in ("LOCALAPPDATA", "APPDATA"):
            base = os.environ.get(var, "").strip()
            if base:
                out.append(Path(base) / APP_TITLE)
        if home is not None:
            out.append(home / "AppData" / "Local" / APP_TITLE)
            out.append(home / f".{APP_NAME}")
        return out

    if sys.platform == "darwin":
        if home is not None:
            out.append(home / "Library" / "Application Support" / APP_TITLE)
            out.append(home / f".{APP_NAME}")
        return out

    # Linux / other posix. The .deb's state dir is first so the systemd
    # service (ProtectHome=yes, ProtectSystem=strict) lands somewhere the
    # unit already lists in ReadWritePaths.
    if os.path.isdir(f"/var/lib/{APP_NAME}") or os.access("/var/lib", os.W_OK):
        out.append(Path(f"/var/lib/{APP_NAME}"))
    xdg = os.environ.get("XDG_STATE_HOME", "").strip() or os.environ.get("XDG_DATA_HOME", "").strip()
    if xdg:
        out.append(Path(xdg).expanduser() / APP_NAME)
    if home is not None:
        out.append(home / ".local" / "state" / APP_NAME)
        out.append(home / ".local" / "share" / APP_NAME)
        out.append(home / f".{APP_NAME}")
    return out


def data_dir_candidates() -> list[Path]:
    """Ordered, de-duplicated data-dir candidates (writable or creatable)."""
    override = _env_path("PUNISHMENT_MANAGER_DATA")
    if override is None:
        home_override = _env_path("PUNISHMENT_MANAGER_HOME")
        override = home_override if home_override is not None else None
    cands: list[Path] = []
    if override is not None:
        cands.append(override)
    else:
        if not is_frozen():
            # Source checkout: keep the historical ./data behaviour.
            cands.append(APP_DIR / "data")
        cands.extend(_platform_state_dirs())
        # Portable / unzipped installs where the app dir *is* writable.
        cands.append(APP_DIR / "data")
    return _dedupe(cands)


def _tighten_private_dir(data_dir: Path) -> None:
    """Keep a per-user state dir 0700, not the umask default 0755.

    The db (punishment reasons, user ids) and the log move out of a private
    repo checkout onto shared machines, so don't leave them readable by every
    local user. Only ever applies to a directory we own inside $HOME: shared
    system state dirs like /var/lib/punishment-manager are group-managed by
    the package and must keep their permissions.
    """
    if os.name == "nt":
        return
    home = _home()
    if home is None:
        return
    try:
        st = data_dir.stat()
        inside_home = str(data_dir).startswith(f"{home}{os.sep}")
    except OSError:
        return
    if not inside_home or st.st_uid != os.getuid():
        return
    if stat.S_IMODE(st.st_mode) & 0o077:
        try:
            data_dir.chmod(0o700)
        except OSError:
            pass


def _pick_data_dir() -> tuple[Path, list[str]]:
    notes: list[str] = []
    for cand in data_dir_candidates():
        ready = ensure_writable(cand)
        if ready is not None:
            _tighten_private_dir(ready)
            _import_legacy_state(ready, notes)
            return ready, notes
        if cand.exists():
            notes.append(
                f"{cand} exists but is not writable by uid "
                f"{os.getuid() if hasattr(os, 'getuid') else 'this user'}; using another location."
            )
        else:
            notes.append(f"{cand} could not be created; using another location.")
    fallback = ensure_writable(Path(tempfile.gettempdir()) / APP_NAME)
    if fallback is not None:
        notes.append(
            f"WARNING: no per-user state directory was usable, so data and logs are in "
            f"{fallback} (temporary!). Point PUNISHMENT_MANAGER_DATA at a writable directory."
        )
        return fallback, notes
    # Truly nothing: leave paths pointed at the first candidate and let the
    # caller's own error surface with a clear message.
    cands = data_dir_candidates()
    target = cands[0] if cands else Path(tempfile.gettempdir()) / APP_NAME
    notes.append(
        f"WARNING: cannot create a writable data directory (tried: {', '.join(str(c) for c in cands)}). "
        "Set PUNISHMENT_MANAGER_DATA to a writable path."
    )
    return target, notes


def _legacy_data_dirs() -> list[Path]:
    """Places a pre-fix build may have kept its state in."""
    out: list[Path] = []
    bundled = BUNDLE_DIR
    if bundled is not None:
        out.append(bundled / "data")   # Path(__file__).parent in an onedir build
    out.append(APP_DIR / "data")       # ...or a portable/unzipped install
    return out


def _import_legacy_state(data_dir: Path, notes: list[str]) -> None:
    """One-off copy of an older db that lived next to the executable.

    Pre-fix builds put (or tried to put) everything in ``<app dir>/data``.
    Upgrading shouldn't silently drop punishment history, so copy the db over
    when the new location has none.
    """
    new_db = data_dir / "punishments.db"
    if not is_frozen() or new_db.exists():
        return  # source runs already use <repo>/data as the first candidate
    legacy_db = next((p for p in (d / "punishments.db" for d in _legacy_data_dirs()) if p.is_file()), None)
    if legacy_db is None:
        return
    try:
        new_db.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(legacy_db, new_db)
        notes.append(f"Imported existing database from {legacy_db}.")
    except OSError:
        pass  # best effort only


# --------------------------------------------------------------------------- #
# Config file
# --------------------------------------------------------------------------- #
def system_config_dir() -> Optional[Path]:
    """Packaged read-only system config location (``/etc/punishment-manager``)."""
    if os.name == "nt":
        base = os.environ.get("PROGRAMDATA", "").strip()
        return Path(base) / APP_TITLE if base else None
    if sys.platform == "darwin":
        return Path("/Library/Application Support") / APP_TITLE
    return Path(f"/etc/{APP_NAME}")


def config_candidates(data_dir: Path) -> list[Path]:
    """Ordered config.json candidates; the first that exists is read."""
    override = _env_path("PUNISHMENT_MANAGER_CONFIG")
    if override is not None:
        return [override]
    cands = [data_dir / "config.json"]
    sysdir = system_config_dir()
    if sysdir is not None:
        cands.append(sysdir / "config.json")
    cands.append(APP_DIR / "config.json")
    return _dedupe(cands)


def _pick_config_path(data_dir: Path) -> Path:
    cands = config_candidates(data_dir)
    for cand in cands:
        if cand.is_file() and os.access(cand, os.R_OK):
            return cand
    # Nothing exists yet: create it in the data dir, except for a source
    # checkout where ./config.json next to bot.py is what everyone expects.
    if not is_frozen():
        repo_cfg = APP_DIR / "config.json"
        if ensure_writable(repo_cfg.parent) is not None:
            return repo_cfg
    return data_dir / "config.json"


def config_path() -> Path:
    """The config file currently being read (mutable: see write_config)."""
    return CONFIG_PATH


def config_write_path() -> Path:
    """Where config.json should be written.

    Normally this is ``config_path()``. When the file that was read is
    read-only for us (e.g. the packaged ``/etc`` config, mode 0640 and
    root-owned), the writable copy in the data dir is used instead.
    """
    cfg = CONFIG_PATH
    if cfg.is_file():
        if os.access(cfg, os.W_OK):
            return cfg
    elif ensure_writable(cfg.parent) is not None:
        return cfg
    return DATA_DIR / "config.json"


def load_config_dict(default: Optional[dict] = None) -> dict[str, Any]:
    """Read config.json, creating `default` (or {}) at a writable location."""
    cfg_path = CONFIG_PATH
    if not cfg_path.is_file():
        target = config_write_path()
        _write_json(target, dict(default or {}))
        _adopt(target)
        return dict(default or {})
    try:
        with cfg_path.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Could not read config file {cfg_path}: {exc}") from exc
    if not isinstance(data, dict):
        raise RuntimeError(f"Config file {cfg_path} must contain a JSON object.")
    return data


def write_config(cfg: dict[str, Any]) -> Path:
    """Atomically write config.json to a writable location; returns it.

    Reads may come from a read-only location (the packaged
    ``/etc/punishment-manager/config.json``). In that case the first save
    copies the config into the writable data dir, which then takes
    precedence for later runs.
    """
    read_from = CONFIG_PATH
    target = config_write_path()
    _write_json(target, cfg)
    if target != read_from:
        _adopt(target)
        _NOTES.append(
            f"Config was read from the read-only {read_from}; wrote the updated "
            f"copy to {target}, which is used from now on."
        )
    return target


def _write_json(target: Path, cfg: dict[str, Any]) -> None:
    target = Path(target)
    if ensure_writable(target.parent) is None:
        raise PermissionError(
            f"Cannot write {target} - its directory is not writable. "
            f"Set PUNISHMENT_MANAGER_CONFIG (or PUNISHMENT_MANAGER_DATA) to a writable path."
        )
    prev = target.stat() if target.exists() else None
    tmp = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    try:
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
            f.write("\n")
        os.replace(tmp, target)
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
    if os.name != "nt":
        _secure(target, prev)


def _secure(target: Path, prev: Optional[os.stat_result]) -> None:
    """Keep the token private, but readable by whoever has to read it.

    * a pre-existing config keeps its owner and mode across a rewrite
      (logrotate-style: replacing the file must not change who can read it);
    * otherwise 0600, so the token isn't world-readable on multi-user hosts;
    * but when the file lives in a directory we don't own - root running
      `punishment-manager --install` into the service's state dir, or a config
      in a group-owned /etc dir - hand it to that owner/group, or the service
      user can't read its own config back.
    """
    try:
        dir_st = target.parent.stat()
    except OSError:
        return
    chown = getattr(os, "chown", None)
    try:
        if prev is not None:
            if chown and prev.st_uid != os.getuid():
                chown(target, prev.st_uid, prev.st_gid)
            os.chmod(target, stat.S_IMODE(prev.st_mode))
            return
        os.chmod(target, 0o600)
        if chown and dir_st.st_uid != os.getuid():
            chown(target, dir_st.st_uid, dir_st.st_gid)
            os.chmod(target, 0o600)
        elif dir_st.st_gid != os.getgid():
            # We own the file but the dir belongs to a service group.
            os.chmod(target, 0o640)
    except OSError:
        pass  # best effort: never fail a save over a chmod


def _adopt(target: Path) -> bool:
    """Point this process at `target` for subsequent reads. True if moved."""
    global CONFIG_PATH
    before = CONFIG_PATH
    CONFIG_PATH = target
    return before != target


def app_version() -> str:
    """Version of *this* build, from the VERSION file.

    Packaged builds read the copy bundled by build/pyinstaller.spec, source
    runs read the repo file - so `--version` describes what is installed
    rather than whatever checkout happens to be nearby.
    """
    found = resource_path("VERSION")
    if found is not None:
        try:
            text = found.read_text(encoding="utf-8").strip()
        except OSError:
            text = ""
        if text:
            return text
    return "0.0.0+unknown"


# --------------------------------------------------------------------------- #
# Module-level resolution (import-time, never raises)
# --------------------------------------------------------------------------- #
BUNDLE_DIR = bundle_dir()
APP_DIR = app_dir()

_NOTES: list[str] = []
try:
    DATA_DIR, _notes = _pick_data_dir()
    _NOTES.extend(_notes)
except Exception as exc:  # pragma: no cover - defensive: import must not fail
    DATA_DIR = Path(tempfile.gettempdir()) / APP_NAME
    _NOTES.append(f"WARNING: data directory resolution failed ({exc}); using {DATA_DIR}.")

try:
    CONFIG_PATH = _pick_config_path(DATA_DIR)
except Exception as exc:  # pragma: no cover
    CONFIG_PATH = DATA_DIR / "config.json"
    _NOTES.append(f"WARNING: config resolution failed ({exc}); using {CONFIG_PATH}.")

DB_PATH = DATA_DIR / "punishments.db"
LOG_PATH = DATA_DIR / "bot.log"


def data_dir() -> Path:
    return DATA_DIR


def startup_notes() -> list[str]:
    """Human-readable notes about the choices made above (log these)."""
    return list(_NOTES)


_NOTES_REPORTED = 0


def new_startup_notes() -> list[str]:
    """Notes not yet handed out, so callers can log them without duplicates.

    paths.py resolves everything before logging is configured, so the notes
    are drained here: once right after the log handlers exist, and again on
    every save (a redirected config write appends a note at that point).
    """
    global _NOTES_REPORTED
    out = _NOTES[_NOTES_REPORTED:]
    _NOTES_REPORTED = len(_NOTES)
    return out


def describe() -> str:
    """Multi-line summary of resolved locations, for `--paths` / debugging."""
    lines = [
        f"mode          : {'packaged' if is_frozen() else 'source'}",
        f"app dir       : {APP_DIR}  (read-only in packaged builds)",
    ]
    if BUNDLE_DIR is not None:
        lines.append(f"bundle dir    : {BUNDLE_DIR}")
    lines += [
        f"data dir      : {DATA_DIR}",
        f"database      : {DB_PATH}",
        f"log file      : {LOG_PATH}",
        f"config (read) : {CONFIG_PATH}{'  [exists]' if CONFIG_PATH.is_file() else '  [not created yet]'}",
        f"config (write): {config_write_path()}",
    ]
    if _NOTES:
        lines.append("notes:")
        lines.extend(f"  - {n}" for n in _NOTES)
    return "\n".join(lines)
