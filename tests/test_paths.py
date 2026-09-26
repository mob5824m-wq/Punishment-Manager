"""
Tests for runtime path resolution (paths.py).

These exist because packaged builds used to derive every path from
``Path(__file__).parent`` and died at import time with::

    PermissionError: [Errno 13] Permission denied:
        '/opt/punishment-manager/_internal/data'

so the core assertion everywhere is: *with a read-only app directory, the bot
still resolves a writable data dir and starts*.

Run with either:
    python -m pytest tests/test_paths.py
    python tests/test_paths.py
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

# These cases lean on POSIX permission bits. Root ignores them and Windows
# chmod() only toggles the read-only attribute on files (not directories), so
# skip them there instead of "testing" nothing.
AS_ROOT = os.name != "nt" and hasattr(os, "geteuid") and os.geteuid() == 0
NEEDS_POSIX_PERMS = AS_ROOT or os.name == "nt"
PERMS_SKIP_REASON = (
    "read-only directory fixtures need POSIX permission bits and a non-root uid"
)

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


class _Sandbox:
    """A throwaway HOME + a read-only 'installed app' directory."""

    def __init__(self, *, frozen: bool = False, app_writable: bool = False) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="pm-paths-test-"))
        self.home = self.tmp / "home"
        self.app = self.tmp / "opt" / "punishment-manager"
        self.bundle = self.app / "_internal"
        for d in (self.home, self.app, self.bundle):
            d.mkdir(parents=True)
        for name in ("bot.py", "paths.py", "installer.py"):
            shutil.copy2(REPO_ROOT / name, self.bundle / name)
        if not app_writable:
            self.lock_readonly(self.app)
            self.lock_readonly(self.bundle)
        self.frozen = frozen
        self.env = {
            "HOME": str(self.home),
            "USERPROFILE": str(self.home),
            "APPDATA": str(self.home / "AppData" / "Roaming"),
            "LOCALAPPDATA": str(self.home / "AppData" / "Local"),
            "XDG_STATE_HOME": str(self.home / ".local" / "state"),
            "XDG_DATA_HOME": str(self.home / ".local" / "share"),
            "XDG_CONFIG_HOME": str(self.home / ".config"),
            "PATH": os.environ.get("PATH", ""),
            # Don't let the ambient (dev) config/data leak into the test.
            "PUNISHMENT_MANAGER_HOME": "",
            "PUNISHMENT_MANAGER_DATA": "",
            "PUNISHMENT_MANAGER_CONFIG": "",
            "PYTHONIOENCODING": "utf-8",
        }

    @staticmethod
    def lock_readonly(path: Path) -> None:
        try:
            path.chmod(0o555)
        except OSError:  # e.g. a filesystem without POSIX modes
            pass

    def unlock(self) -> None:
        for d in (self.app, self.bundle):
            try:
                d.chmod(0o755)
            except OSError:
                pass

    def cleanup(self) -> None:
        self.unlock()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_python(self, code: str, *, extra_env: dict | None = None) -> subprocess.CompletedProcess:
        """Run `code` in a subprocess with this sandbox's env.

        For the frozen case, sys.frozen / _MEIPASS / sys.executable are
        patched before the imports so paths.py believes it is running from
        /opt/punishment-manager/_internal like a PyInstaller onedir build.
        """
        prelude = ""
        if self.frozen:
            prelude = (
                "import sys\n"
                f"sys.frozen = True\n"
                f"sys._MEIPASS = {str(self.bundle)!r}\n"
                f"sys.executable = {str(self.app / 'punishment-manager')!r}\n"
                f"sys.argv = [{str(self.app / 'punishment-manager')!r}]\n"
            )
        env = {**self.env, **(extra_env or {})}
        env["PYTHONPATH"] = str(self.bundle)
        return subprocess.run(
            [sys.executable, "-c", prelude + code],
            capture_output=True, text=True, env=env, cwd=str(self.home),
        )

    def write_config(self, cfg: dict, *, where: Path) -> Path:
        target = where / "config.json"
        target.write_text(json.dumps(cfg), encoding="utf-8")
        return target


class ReadOnlyAppDirTests(unittest.TestCase):
    """The reported crash: data/ next to the executable is not writable."""

    def setUp(self) -> None:
        if NEEDS_POSIX_PERMS:
            self.skipTest(PERMS_SKIP_REASON)
        self.sb = _Sandbox()

    def tearDown(self) -> None:
        self.sb.cleanup()

    def test_paths_module_imports_and_picks_writable_dir(self) -> None:
        code = (
            "import paths, os\n"
            "print(paths.DATA_DIR)\n"
            "assert os.access(paths.DATA_DIR, os.W_OK), paths.DATA_DIR\n"
            "assert not str(paths.DATA_DIR).startswith("
            f"{str(self.sb.bundle)!r}), paths.DATA_DIR\n"
        )
        res = self.sb.run_python(code)
        self.assertEqual(res.returncode, 0, msg=res.stderr or res.stdout)
        chosen = Path(res.stdout.strip().splitlines()[0])
        self.assertTrue(chosen.is_dir())

    def test_bot_import_does_not_raise_permissionerror(self) -> None:
        # `import bot` used to die inside pathlib.mkdir at line ~35.
        code = (
            "import bot\n"
            "print('OK', bot.DB_PATH, bot.LOG_PATH, bot.paths.config_path())\n"
        )
        res = self.sb.run_python(code)
        self.assertEqual(res.returncode, 0, msg=res.stderr or res.stdout)
        self.assertIn("OK", res.stdout)
        self.assertNotIn("PermissionError", res.stderr + res.stdout)

    def test_cli_paths_flag_reports_locations(self) -> None:
        res = subprocess.run(
            [sys.executable, str(self.sb.bundle / "bot.py"), "--paths"],
            capture_output=True, text=True, env={**self.sb.env, "PYTHONPATH": str(self.sb.bundle)},
            cwd=str(self.sb.home),
        )
        self.assertEqual(res.returncode, 0, msg=res.stderr or res.stdout)
        for key in ("data dir", "database", "log file", "config (read)", "config (write)"):
            self.assertIn(key, res.stdout)

    def test_nothing_is_written_into_the_readonly_app_dir(self) -> None:
        code = "import bot, paths; paths.write_config({'bot_token': 'x'}); print('OK')"
        res = self.sb.run_python(code)
        self.assertEqual(res.returncode, 0, msg=res.stderr or res.stdout)
        self.sb.unlock()
        leftovers = sorted(
            p.name for p in self.sb.bundle.iterdir()
            if p.name not in {"bot.py", "paths.py", "installer.py", "__pycache__"}
        )
        self.assertEqual(leftovers, [])


class FrozenBuildTests(unittest.TestCase):
    """A PyInstaller-style install: app tree read-only, no source tree."""

    def setUp(self) -> None:
        if NEEDS_POSIX_PERMS:
            self.skipTest(PERMS_SKIP_REASON)
        self.sb = _Sandbox(frozen=True)

    def tearDown(self) -> None:
        self.sb.cleanup()

    def test_data_dir_is_a_platform_state_dir(self) -> None:
        code = "import paths; print(paths.DATA_DIR); print(paths.is_frozen())"
        res = self.sb.run_python(code)
        self.assertEqual(res.returncode, 0, msg=res.stderr or res.stdout)
        lines = res.stdout.split("\n")
        chosen = Path(lines[0])
        self.assertEqual(lines[1], "True")
        self.assertNotIn("_internal", str(chosen))
        self.assertTrue(os.access(chosen, os.W_OK))

    def test_app_dir_points_at_the_executable_not_the_bundle(self) -> None:
        code = "import paths; print(paths.APP_DIR)"
        res = self.sb.run_python(code)
        self.assertEqual(res.returncode, 0, msg=res.stderr or res.stdout)
        self.assertEqual(Path(res.stdout.strip()), self.sb.app)

    def test_legacy_db_next_to_executable_is_carried_over(self) -> None:
        # Pre-fix portable installs kept their history in <app dir>/data.
        self.sb.unlock()
        legacy = self.sb.bundle / "data"
        legacy.mkdir()
        (legacy / "punishments.db").write_text("legacy db", encoding="utf-8")
        self.sb.lock_readonly(self.sb.bundle)
        self.sb.lock_readonly(self.sb.app)
        code = "import paths; print(paths.DB_PATH)"
        res = self.sb.run_python(code)
        self.assertEqual(res.returncode, 0, msg=res.stderr or res.stdout)
        new_db = Path(res.stdout.strip())
        self.assertEqual(new_db.read_text(encoding="utf-8"), "legacy db")

    def test_system_config_is_read_but_saved_to_the_data_dir(self) -> None:
        if NEEDS_POSIX_PERMS:
            self.skipTest(PERMS_SKIP_REASON)
        # Mirrors the .deb: config shipped in a directory the service can
        # read but not write (here: read-only parent, mode 0644 file).
        self.sb.unlock()
        etc = self.sb.tmp / "etc-state"
        etc.mkdir()
        self.sb.write_config({"bot_token": "from-system", "server_id": 7}, where=etc)
        (etc / "config.json").chmod(0o444)
        etc.chmod(0o555)
        code = (
            "import json, os, paths\n"
            "os.chmod(paths.config_path(), 0o444)\n"
            "cfg = paths.load_config_dict()\n"
            "print(cfg['bot_token'])\n"
            "cfg['bot_token'] = 'updated'\n"
            "written = paths.write_config(cfg)\n"
            "print(written)\n"
            "print(paths.config_path())\n"
            "print(json.dumps(open(written).read() and 'ok'))\n"
        )
        res = self.sb.run_python(
            code, extra_env={"PUNISHMENT_MANAGER_CONFIG": str(etc / "config.json")}
        )
        self.assertEqual(res.returncode, 0, msg=res.stderr or res.stdout)
        lines = res.stdout.splitlines()
        self.assertEqual(lines[0], "from-system")
        self.assertNotIn(str(etc / "config.json"), lines[1])  # copy-on-write
        self.assertEqual(lines[1], lines[2])                    # now read back
        self.assertTrue(Path(lines[1]).is_file())
        # The read-only original must be untouched.
        self.assertEqual(json.loads((etc / "config.json").read_text())["bot_token"], "from-system")


class EnvOverrideTests(unittest.TestCase):
    def setUp(self) -> None:
        self.sb = _Sandbox(app_writable=True)

    def tearDown(self) -> None:
        self.sb.cleanup()

    def test_home_override(self) -> None:
        base = self.sb.home / "pm-state"
        code = "import paths; print(paths.DATA_DIR)"
        res = self.sb.run_python(code, extra_env={"PUNISHMENT_MANAGER_HOME": str(base)})
        self.assertEqual(res.returncode, 0, msg=res.stderr or res.stdout)
        self.assertEqual(Path(res.stdout.strip()), base)
        self.assertTrue(base.is_dir())

    def test_per_user_state_dir_is_private(self) -> None:
        # The db holds punishment reasons + user ids; on a shared machine the
        # per-user state dir must not be world-readable.
        base = self.sb.home / "pm-state"
        code = (
            "import os, paths, stat\n"
            "print(oct(stat.S_IMODE(os.stat(paths.DATA_DIR).st_mode)))\n"
        )
        res = self.sb.run_python(code, extra_env={"PUNISHMENT_MANAGER_HOME": str(base)})
        self.assertEqual(res.returncode, 0, msg=res.stderr or res.stdout)
        if os.name != "nt":
            self.assertEqual(res.stdout.strip(), "0o700")

    def test_data_and_config_overrides(self) -> None:
        data = self.sb.home / "data-dir"
        cfg = self.sb.home / "custom-config.json"
        cfg.write_text(json.dumps({"bot_token": "abc"}), encoding="utf-8")
        code = (
            "import paths\n"
            "print(paths.DATA_DIR)\n"
            "print(paths.config_path())\n"
            "print(paths.load_config_dict()['bot_token'])\n"
        )
        res = self.sb.run_python(code, extra_env={
            "PUNISHMENT_MANAGER_DATA": str(data),
            "PUNISHMENT_MANAGER_CONFIG": str(cfg),
        })
        self.assertEqual(res.returncode, 0, msg=res.stderr or res.stdout)
        lines = res.stdout.splitlines()
        self.assertEqual(Path(lines[0]), data)
        self.assertEqual(Path(lines[1]), cfg)
        self.assertEqual(lines[2], "abc")

    def test_source_run_keeps_repo_relative_paths(self) -> None:
        """Dev checkouts must still use <repo>/data and <repo>/config.json."""
        code = (
            "import paths\n"
            "print(paths.DATA_DIR)\n"
            "print(paths.config_path())\n"
        )
        res = self.sb.run_python(code)
        self.assertEqual(res.returncode, 0, msg=res.stderr or res.stdout)
        lines = res.stdout.splitlines()
        self.assertEqual(Path(lines[0]), self.sb.bundle / "data")
        self.assertEqual(Path(lines[1]), self.sb.bundle / "config.json")


class WriteSafetyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.sb = _Sandbox(app_writable=True)

    def tearDown(self) -> None:
        self.sb.cleanup()

    def test_config_saved_atomically_with_private_mode(self) -> None:
        code = (
            "import json, os, paths, stat\n"
            "target = paths.write_config({'bot_token': 't', 'server_id': 1})\n"
            "st = os.stat(target)\n"
            "print(target)\n"
            "print(oct(stat.S_IMODE(st.st_mode)))\n"
            "print(json.load(open(target))['server_id'])\n"
            "print([p for p in target.parent.iterdir() if '.tmp' in p.name])\n"
        )
        res = self.sb.run_python(code)
        self.assertEqual(res.returncode, 0, msg=res.stderr or res.stdout)
        lines = res.stdout.splitlines()
        self.assertTrue(Path(lines[0]).is_file())
        if os.name != "nt":
            self.assertIn("600", lines[1])
        self.assertEqual(lines[2], "1")
        self.assertEqual(lines[3], "[]")  # no temp-file litter

    def test_unwritable_target_reports_actionable_error(self) -> None:
        if NEEDS_POSIX_PERMS:
            self.skipTest(PERMS_SKIP_REASON)
        # A config location we cannot write must say what to do about it,
        # not print a bare traceback (which is how this bug was reported).
        code = (
            "import paths\n"
            "locked = paths.DATA_DIR / 'locked'\n"
            "locked.mkdir(parents=True, exist_ok=True)\n"
            "locked.chmod(0o555)\n"
            "try:\n"
            "    paths._write_json(locked / 'config.json', {})\n"
            "    print('no error')\n"
            "except PermissionError as exc:\n"
            "    print('PUNISHMENT_MANAGER_CONFIG' in str(exc))\n"
            "finally:\n"
            "    locked.chmod(0o755)\n"
        )
        res = self.sb.run_python(code)
        self.assertEqual(res.returncode, 0, msg=res.stderr or res.stdout)
        self.assertEqual(res.stdout.strip(), "True")

    def test_readonly_config_redirects_to_the_data_dir(self) -> None:
        if NEEDS_POSIX_PERMS:
            self.skipTest(PERMS_SKIP_REASON)
        # Saves never fail just because the config that was read is read-only:
        # they land in the data dir, which then wins on the next run.
        code = (
            "import json, os, paths\n"
            "cfg_file = paths.config_path()\n"
            "cfg = paths.load_config_dict()\n"
            "cfg['bot_token'] = 'saved'\n"
            "os.chmod(cfg_file, 0o444)\n"
            "written = paths.write_config(cfg)\n"
            "print(written != cfg_file, json.load(open(written))['bot_token'])\n"
        )
        target = self.sb.app / "config.json"
        target.write_text(json.dumps({"bot_token": "read-only"}), encoding="utf-8")
        res = self.sb.run_python(code)
        self.assertEqual(res.returncode, 0, msg=res.stderr or res.stdout)
        self.assertEqual(res.stdout.strip(), "True saved")


def main() -> int:
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromModule(sys.modules[__name__])
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
