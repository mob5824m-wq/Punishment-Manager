"""
Tests for the release version plumbing.

Motivation: the version was hardcoded in seven places (three build scripts,
the PyInstaller spec, the macOS plist, the NSIS installer, the .deb control
file), so cutting a `v1.0.1` tag produced `punishment-manager_1.0.0_amd64.deb`
- an installer whose filename, plist and package metadata disagreed with the
release it was attached to. The VERSION file is now the single source of
truth; these tests keep it that way.

Run with either:
    python -m pytest tests/test_version.py
    python tests/test_version.py
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

SEMVER = re.compile(r"^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$")
BUILD_SCRIPTS = (
    "build/build_linux.sh",
    "build/build_macos.sh",
    "build/build_windows.bat",
)


def read_version_file() -> str:
    return (REPO_ROOT / "VERSION").read_text(encoding="utf-8").strip()


class VersionFileTests(unittest.TestCase):
    def test_version_file_exists_and_is_semver(self) -> None:
        self.assertTrue((REPO_ROOT / "VERSION").is_file(), "missing VERSION file")
        text = (REPO_ROOT / "VERSION").read_text(encoding="utf-8")
        lines = [ln for ln in text.splitlines() if ln.strip()]
        self.assertEqual(len(lines), 1, "VERSION must hold exactly one version")
        self.assertRegex(lines[0], SEMVER)

    def test_app_version_matches_the_file(self) -> None:
        import paths

        self.assertEqual(paths.app_version(), read_version_file())


class NoDriftTests(unittest.TestCase):
    """Nothing may go back to hardcoding a version."""

    def test_build_scripts_do_not_hardcode_a_version(self) -> None:
        # 0.0.0+unknown is the deliberate "no VERSION file" sentinel, not a pin.
        pinned = re.compile(r"""VERSION\s*[=:]\s*["']?(?!0\.0\.0\+unknown)\d+\.\d+\.\d+""")
        for rel in (*BUILD_SCRIPTS, "build/pyinstaller.spec"):
            text = (REPO_ROOT / rel).read_text(encoding="utf-8")
            hit = pinned.search(text)
            self.assertIsNone(hit, f"{rel} pins a version ({hit.group(0) if hit else ''}); read VERSION instead")

    def test_build_scripts_read_the_version_file(self) -> None:
        for rel in BUILD_SCRIPTS:
            text = (REPO_ROOT / rel).read_text(encoding="utf-8")
            self.assertIn("VERSION", text, f"{rel} must resolve the version")
        spec = (REPO_ROOT / "build" / "pyinstaller.spec").read_text(encoding="utf-8")
        self.assertIn("'CFBundleShortVersionString': VERSION", spec)
        # Bundled so `--version` reports the installed build, not a checkout.
        self.assertIn("(str(PROJECT_ROOT / 'VERSION'), '.')", spec)

    def test_macos_plist_template_is_not_a_second_source(self) -> None:
        text = (REPO_ROOT / "build" / "macos" / "Info.plist").read_text(encoding="utf-8")
        self.assertIn("Reference copy only", text)

    def test_nsis_fallback_is_not_a_stale_version(self) -> None:
        text = (REPO_ROOT / "build" / "windows" / "installer.nsi").read_text(encoding="utf-8")
        m = re.search(r"!ifndef VERSION(.*?)!endif", text, re.S)
        self.assertIsNotNone(m, "NSIS must keep a /DVERSION fallback")
        self.assertNotIn('!define VERSION "1', m.group(1), "stale hardcoded version in installer.nsi")


class ResolutionTests(unittest.TestCase):
    def test_pm_version_script_reads_the_file(self) -> None:
        if os.name == "nt":
            self.skipTest("bash helper")
        script = REPO_ROOT / "scripts" / "pm_version.sh"
        cmd = f'. "{script}" && pm_version "{REPO_ROOT}"'
        out = subprocess.run(
            ["bash", "-lc", cmd], capture_output=True, text=True, cwd=str(REPO_ROOT), check=True
        )
        self.assertEqual(out.stdout.strip(), read_version_file())

    def test_env_override_wins(self) -> None:
        if os.name == "nt":
            self.skipTest("bash helper")
        script = REPO_ROOT / "scripts" / "pm_version.sh"
        env = {**os.environ, "PM_VERSION": "9.9.9-rc1"}
        out = subprocess.run(
            ["bash", "-lc", f'. "{script}" && pm_version "{REPO_ROOT}"'],
            capture_output=True, text=True, cwd=str(REPO_ROOT), env=env, check=True,
        )
        self.assertEqual(out.stdout.strip(), "9.9.9-rc1")

    def test_cli_reports_the_version(self) -> None:
        out = subprocess.run(
            [sys.executable, str(REPO_ROOT / "bot.py"), "--version"],
            capture_output=True, text=True, cwd=str(REPO_ROOT),
        )
        self.assertEqual(out.returncode, 0, msg=out.stderr or out.stdout)
        self.assertEqual(out.stdout.strip(), f"Punishment Manager {read_version_file()}")

    def test_frozen_build_reads_the_bundled_version(self) -> None:
        # The packaged copy is what a user's installed build must report, even
        # when a newer checkout happens to be on disk.
        with tempfile.TemporaryDirectory() as tmp:
            bundled = Path(tmp) / "bundle"
            bundled.mkdir()
            (bundled / "VERSION").write_text("1.2.3-bundled\n", encoding="utf-8")
            code = (
                "import sys\n"
                "sys.frozen = True\n"
                f"sys._MEIPASS = {str(bundled)!r}\n"
                f"sys.executable = {str(Path(tmp) / 'app' / 'punishment-manager')!r}\n"
                f"sys.path.insert(0, {str(REPO_ROOT)!r})\n"
                "import paths\n"
                "print(paths.app_version())\n"
            )
            out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
            self.assertEqual(out.stdout.strip(), "1.2.3-bundled")


class ReleaseScriptTests(unittest.TestCase):
    def test_make_release_refuses_a_mismatched_version(self) -> None:
        """A tag that doesn't match VERSION must fail before anything is created."""
        if os.name == "nt":
            self.skipTest("bash script")
        bogus = "9.9.8"
        res = subprocess.run(
            ["bash", str(REPO_ROOT / "scripts" / "make_release.sh"), bogus],
            capture_output=True, text=True, cwd=str(REPO_ROOT),
        )
        self.assertNotEqual(res.returncode, 0, msg="mismatched version must be rejected")
        self.assertIn("VERSION file", res.stdout + res.stderr)
        tags = subprocess.run(
            ["git", "tag", "--list", f"v{bogus}"], capture_output=True, text=True, cwd=str(REPO_ROOT)
        )
        self.assertEqual(tags.stdout.strip(), "", "make_release.sh must not create a tag on failure")


def main() -> int:
    suite = unittest.TestLoader().loadTestsFromModule(sys.modules[__name__])
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
