"""
Punishment Manager - interactive installer.

Asks the user for:
  - bot_token          (the Discord bot token)
  - server_id          (the single server this bot will run in)
  - punish_role_id     (the role given during punishment)
  - post_role_id       (the role given after the timer expires)
  - staff_role_id      (optional; users with this role cannot be punished;
                        admins and mods are always protected)
  - staff_channel_id   (optional; where staff get embed notifications)
  - dm_user            (whether to DM the punished user an embed)

Writes everything to config.json in the project root. Re-runnable: any
field the user skips is left as it was.

Cross-platform: uses only the standard library, no `readline` magic.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Optional


BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"


# --------------------------------------------------------------------------- #
# Terminal helpers
# --------------------------------------------------------------------------- #
def _is_windows() -> bool:
    return os.name == "nt"


def _prompt(label: str, default: Optional[str] = None, *, hidden: bool = False) -> str:
    """Read a line of input from stdin. On Windows, hide input with msvcrt
    when `hidden` is True so the bot token isn't echoed to the terminal.
    """
    suffix = f" [{default}]" if default else ""
    sys.stdout.write(f"{label}{suffix}: ")
    sys.stdout.flush()
    if hidden and _is_windows():
        import msvcrt  # type: ignore[import-not-found]
        buf: list[str] = []
        while True:
            ch = msvcrt.getwch()
            if ch in ("\r", "\n"):
                sys.stdout.write("\n")
                break
            if ch == "\b":
                if buf:
                    buf.pop()
                    sys.stdout.write("\b \b")
                continue
            if ch == "\x03":  # Ctrl-C
                raise KeyboardInterrupt
            buf.append(ch)
            sys.stdout.write("*")
            sys.stdout.flush()
        return "".join(buf)
    if hidden:
        try:
            import termios, tty  # type: ignore[import-not-found]
            fd = sys.stdin.fileno()
            old = termios.tcgetattr(fd)
            try:
                tty.setecho(False)
                value = sys.stdin.readline().rstrip("\n")
            finally:
                termios.tcsetattr(fd, termios.TCSADRAIN, old)
            sys.stdout.write("\n")
            return value
        except Exception:
            pass
    return sys.stdin.readline().rstrip("\n")


def _confirm(label: str, default: bool = True) -> bool:
    suffix = "Y/n" if default else "y/N"
    while True:
        sys.stdout.write(f"{label} [{suffix}]: ")
        sys.stdout.flush()
        ans = sys.stdin.readline().strip().lower()
        if not ans:
            return default
        if ans in ("y", "yes"):
            return True
        if ans in ("n", "no"):
            return False


_SNOWFLAKE_RE = re.compile(r"^\d{17,20}$")


def _parse_snowflake(label: str, raw: str) -> Optional[int]:
    raw = raw.strip()
    if not raw:
        return None
    # Allow <@12345>, <@&12345>, <#12345>, or plain number.
    m = re.search(r"\d{17,20}", raw)
    if not m:
        print(f"  (Skipped {label}: not a valid Discord id)")
        return None
    return int(m.group(0))


# --------------------------------------------------------------------------- #
# Installer
# --------------------------------------------------------------------------- #
WELCOME = """
Punishment Manager - first-time setup
=====================================

I'll write a few values to config.json. Anything you skip will be left
as it is, so this is safe to re-run.

How to find the values:
  - bot_token:        Discord Developer Portal -> your app -> Bot -> Token
  - server_id:        right-click your server icon -> Copy Server ID
                      (enable Developer Mode in Settings -> Advanced first)
  - role ids:         right-click the role -> Copy Role ID
  - staff_channel_id: right-click the channel -> Copy Channel ID
  - staff_role_id:    role given to staff; members with it cannot be
                      punished (admins and mods are always protected)
  - press Enter on a prompt to keep the existing value
"""


def _load_existing() -> dict:
    if not CONFIG_PATH.exists():
        return {}
    try:
        with CONFIG_PATH.open("r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        print(f"  (Existing {CONFIG_PATH} is unreadable; starting fresh.)")
        return {}


def _save(cfg: dict) -> None:
    with CONFIG_PATH.open("w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
    # Also chmod 600 so the token isn't world-readable on multi-user systems.
    try:
        os.chmod(CONFIG_PATH, 0o600)
    except (OSError, NotImplementedError):
        pass
    print(f"  Wrote {CONFIG_PATH}")


def _show_current(label: str, value: Any) -> str:
    if value in (None, "", 0):
        return "(not set)"
    if label == "bot_token" and isinstance(value, str) and len(value) > 8:
        return f"{value[:4]}...{value[-4:]}"
    return str(value)


def run_installer() -> int:
    print(WELCOME)

    cfg = _load_existing()

    # 1. bot_token -------------------------------------------------- #
    cur = cfg.get("bot_token") or cfg.get("token") or ""
    val = _prompt(
        "Bot token",
        default=_show_current("bot_token", cur) if cur else None,
        hidden=True,
    )
    if val:
        cfg["bot_token"] = val
        cfg["token"] = val  # keep legacy key in sync for back-compat

    # 2. server_id -------------------------------------------------- #
    cur = cfg.get("server_id")
    val = _prompt(
        "Server (guild) ID",
        default=_show_current("server_id", cur) if cur else None,
    )
    parsed = _parse_snowflake("server_id", val) if val else None
    if parsed is not None:
        cfg["server_id"] = parsed

    # 3-4. punish + post role ids ---------------------------------- #
    for key, label in [
        ("punish_role_id", "Punish role ID (given during punishment)"),
        ("post_role_id",   "Post-punish role ID (given after the timer)"),
    ]:
        cur = cfg.get(key)
        val = _prompt(
            label,
            default=_show_current(key, cur) if cur else None,
        )
        parsed = _parse_snowflake(key, val) if val else None
        if parsed is not None:
            cfg[key] = parsed

    # 5. staff role (optional) ------------------------------------- #
    cur = cfg.get("staff_role_id")
    val = _prompt(
        "Staff role ID (members with this role cannot be punished, optional)",
        default=_show_current("staff_role_id", cur) if cur else None,
    )
    parsed = _parse_snowflake("staff_role_id", val) if val else None
    if parsed is not None:
        cfg["staff_role_id"] = parsed

    # 6. staff channel --------------------------------------------- #
    cur = cfg.get("staff_channel_id") or cfg.get("log_channel_id")
    val = _prompt(
        "Staff channel ID (for embed notifications, optional)",
        default=_show_current("staff_channel_id", cur) if cur else None,
    )
    parsed = _parse_snowflake("staff_channel_id", val) if val else None
    if parsed is not None:
        cfg["staff_channel_id"] = parsed
        cfg["log_channel_id"] = parsed  # legacy key

    # 7. dm user ---------------------------------------------------- #
    cur = cfg.get("dm_user", True)
    if _confirm(
        "DM the punished user an embed about their punishment",
        default=bool(cur),
    ):
        cfg["dm_user"] = True
    else:
        cfg["dm_user"] = False

    # ---- summary ---- #
    print("\nNew configuration:")
    summary_keys = (
        "bot_token", "server_id",
        "punish_role_id", "post_role_id", "staff_role_id",
        "staff_channel_id", "dm_user",
    )
    print(json.dumps(
        {k: cfg[k] for k in summary_keys if k in cfg},
        indent=2,
    ))

    if not _confirm("\nSave this configuration?", default=True):
        print("Aborted. No files were changed.")
        return 1

    _save(cfg)
    print(
        "\nDone. Run the bot with:\n"
        "  ./scripts/run_mac.sh    (macOS)\n"
        "  ./scripts/run_linux.sh  (Linux)\n"
        "  scripts\\run_windows.bat (Windows)\n"
    )
    return 0


if __name__ == "__main__":
    try:
        sys.exit(run_installer())
    except KeyboardInterrupt:
        print("\nCancelled.")
        sys.exit(130)
