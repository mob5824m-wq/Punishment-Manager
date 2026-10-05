"""
Tests for the per-architecture build plumbing.

Motivation: Sentinel ships one installer per architecture (amd64 and arm64),
and PyInstaller cannot cross-compile, so every artifact has to be built on a
machine of the architecture it targets. The failure this suite guards against
is the quiet one: a build that is *labelled* arm64 but contains an x86_64
binary - an emulated Python on Windows on ARM, a stale interpreter on PATH, a
copy-pasted runner label - which is invisible until a user on an arm64 machine
runs the installer.

Two things are checked here:

* ``scripts/check_arch.py`` - the header reader (ELF / Mach-O / PE) the build
  scripts call to verify what they just produced, including its exit codes;
* the build plumbing itself: ``scripts/arch.sh``'s resolution rules, and that
  every build script asks for the architecture, passes it on, and verifies the
  result instead of hard-coding amd64.

Run with either:
    python -m pytest tests/test_arch.py
    python tests/test_arch.py
"""

from __future__ import annotations

import importlib.util
import os
import platform
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

SCRIPTS = REPO_ROOT / "scripts"
ARCH_SH = SCRIPTS / "arch.sh"
CHECK_ARCH = SCRIPTS / "check_arch.py"
BUILD_SCRIPTS = (
    "build/build_linux.sh",
    "build/build_macos.sh",
    "build/build_windows.bat",
)
# The shell-side helpers are POSIX; Windows runners skip those cases, like the
# release-script tests do.
SKIP_BASH = os.name == "nt"


def read(rel: str) -> str:
    return (REPO_ROOT / rel).read_text(encoding="utf-8")


def load_check_arch():
    """Import scripts/check_arch.py without installing it as a package."""
    spec = importlib.util.spec_from_file_location("check_arch", CHECK_ARCH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


check_arch = load_check_arch()


def host_arch_for_this_machine() -> str:
    """The canonical architecture this test process runs as."""
    machine = platform.machine()
    if sys.platform.startswith("win"):
        machine = os.environ.get("PROCESSOR_ARCHITECTURE") or machine
    return check_arch.canonical_arch(machine)


def other_arch(arch: str) -> str:
    return "arm64" if arch == "amd64" else "amd64"


# --------------------------------------------------------------------------- #
# Synthetic binaries: one small fixture per format, so detection is checked
# against known-good bytes rather than only against whatever this machine is.
# --------------------------------------------------------------------------- #

def elf_bytes(machine: int) -> bytes:
    header = bytearray(b"\x7fELF" + bytes([2, 1, 1, 0]) + b"\x00" * 8)
    header += struct.pack("<H", 2)          # e_type
    header += struct.pack("<H", machine)    # e_machine
    return bytes(header) + b"\x00" * 64


def macho_bytes(cputype: int) -> bytes:
    # 64-bit little-endian Mach-O: the layout of every shipping macOS binary.
    return b"\xcf\xfa\xed\xfe" + struct.pack("<I", cputype) + b"\x00" * 64


def universal_bytes(*cputypes: int) -> bytes:
    table = b"".join(
        struct.pack(">i", cpu) + struct.pack(">i", 0) + struct.pack(">I", 0) * 3
        for cpu in cputypes
    )
    return b"\xca\xfe\xba\xbe" + struct.pack(">I", len(cputypes)) + table + b"\x00" * 64


def pe_bytes(machine: int) -> bytes:
    data = bytearray(b"\x00" * 0x90)
    data[0:2] = b"MZ"
    data[0x3C:0x40] = struct.pack("<I", 0x80)
    data[0x80:0x84] = b"PE\0\0"
    data[0x84:0x86] = struct.pack("<H", machine)
    return bytes(data)


class CanonicalNameTests(unittest.TestCase):
    """One spelling per architecture, so labels and filenames can't drift."""

    def test_accepted_spellings(self) -> None:
        for value in ("amd64", "AMD64", "x86_64", "x64", "intel"):
            self.assertEqual(check_arch.canonical_arch(value), "amd64", value)
        for value in ("arm64", "ARM64", "aarch64", "armv8l"):
            self.assertEqual(check_arch.canonical_arch(value), "arm64", value)

    def test_unknown_spellings_are_rejected(self) -> None:
        for value in ("", "sparc", "mips", "armv7l"):
            with self.subTest(value=value):
                with self.assertRaises(check_arch.UnknownArchitecture):
                    check_arch.canonical_arch(value)

    def test_universal_binaries_match_any_of_their_slices(self) -> None:
        self.assertTrue(check_arch.arch_matches("amd64+arm64", "arm64"))
        self.assertTrue(check_arch.arch_matches("amd64+arm64", "x86_64"))
        self.assertFalse(check_arch.arch_matches("amd64", "arm64"))


class DetectArchTests(unittest.TestCase):
    """The header reader the build scripts run on their own output."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="sentinel-arch-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def write(self, name: str, data: bytes) -> Path:
        path = self.tmp / name
        path.write_bytes(data)
        return path

    def test_elf(self) -> None:
        self.assertEqual(check_arch.detect_arch(self.write("arm", elf_bytes(0xB7))), "arm64")
        self.assertEqual(check_arch.detect_arch(self.write("x86", elf_bytes(0x3E))), "amd64")

    def test_macho(self) -> None:
        self.assertEqual(check_arch.detect_arch(self.write("arm", macho_bytes(0x0100000C))), "arm64")
        self.assertEqual(check_arch.detect_arch(self.write("intel", macho_bytes(0x01000007))), "amd64")

    def test_universal_macho(self) -> None:
        path = self.write("fat", universal_bytes(0x01000007, 0x0100000C))
        self.assertEqual(check_arch.detect_arch(path), "amd64+arm64")

    def test_pe(self) -> None:
        self.assertEqual(check_arch.detect_arch(self.write("arm.exe", pe_bytes(0xAA64))), "arm64")
        self.assertEqual(check_arch.detect_arch(self.write("x64.exe", pe_bytes(0x8664))), "amd64")

    def test_the_running_interpreter_is_a_known_architecture(self) -> None:
        # The build scripts check this before they build anything.
        self.assertEqual(check_arch.detect_arch(sys.executable), host_arch_for_this_machine())

    def test_junk_is_not_an_architecture(self) -> None:
        for name, data in (("text", b"#!/bin/sh\necho hi\n"), ("empty", b""), ("short", b"MZ")):
            with self.subTest(name=name):
                with self.assertRaises(check_arch.UnknownArchitecture):
                    check_arch.detect_arch(self.write(name, data))

    def test_a_missing_file_is_reported_not_guessed(self) -> None:
        with self.assertRaises(check_arch.UnknownArchitecture):
            check_arch.detect_arch(self.tmp / "does-not-exist")


class HostArchTests(unittest.TestCase):
    def test_matches_the_interpreter(self) -> None:
        self.assertEqual(check_arch.host_arch(), host_arch_for_this_machine())


class CommandLineTests(unittest.TestCase):
    """Exit codes the build scripts rely on: 0 ok, 1 wrong arch, 2 unknown."""

    def run_cli(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(CHECK_ARCH), *args],
            capture_output=True, text=True, cwd=str(REPO_ROOT),
        )

    def test_file_reports_the_architecture(self) -> None:
        result = self.run_cli("file", sys.executable)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(host_arch_for_this_machine(), result.stdout)

    def test_expectation_holds(self) -> None:
        for spelling in (host_arch_for_this_machine(),):
            result = self.run_cli("file", sys.executable, "--expect", spelling)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_a_mismatch_fails_the_build(self) -> None:
        result = self.run_cli("file", sys.executable, "--expect", other_arch(host_arch_for_this_machine()))
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("requested", result.stderr)

    def test_host_subcommand(self) -> None:
        self.assertEqual(self.run_cli("host").returncode, 0)
        self.assertEqual(self.run_cli("host", "--expect", host_arch_for_this_machine()).returncode, 0)
        self.assertEqual(
            self.run_cli("host", "--expect", other_arch(host_arch_for_this_machine())).returncode, 1
        )

    def test_an_unreadable_file_is_exit_2(self) -> None:
        result = self.run_cli("file", str(REPO_ROOT / "VERSION"), "--expect", "amd64")
        self.assertEqual(result.returncode, 2, result.stdout)


class ArchShellHelperTests(unittest.TestCase):
    """scripts/arch.sh, the shell side of the same rules."""

    def setUp(self) -> None:
        if SKIP_BASH:
            self.skipTest("bash helper")

    def shell(self, body: str, env: dict | None = None) -> subprocess.CompletedProcess:
        environment = {**os.environ}
        environment.pop("SENTINEL_TARGET_ARCH", None)
        environment.update(env or {})
        return subprocess.run(
            ["bash", "-c", f'. "{ARCH_SH}"\n{body}'],
            capture_output=True, text=True, cwd=str(REPO_ROOT), env=environment,
        )

    def test_defaults_to_the_host(self) -> None:
        result = self.shell('resolve_arch')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), check_arch.host_arch())

    def test_override_is_normalized(self) -> None:
        for value, expected in (("arm64", "arm64"), ("AArch64", "arm64"), ("x86_64", "amd64")):
            with self.subTest(value=value):
                result = self.shell('resolve_arch', env={"SENTINEL_TARGET_ARCH": value})
                self.assertEqual(result.stdout.strip(), expected, result.stderr)

    def test_an_unknown_override_fails_loudly(self) -> None:
        result = self.shell('resolve_arch', env={"SENTINEL_TARGET_ARCH": "sparc"})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("SENTINEL_TARGET_ARCH", result.stderr)

    def test_macos_labels(self) -> None:
        result = self.shell('macos_arch_label amd64; macos_arch_label arm64')
        self.assertEqual(result.stdout.split(), ["x86_64", "arm64"])

    def test_verify_binary_arch_accepts_the_host_and_rejects_the_other(self) -> None:
        host = check_arch.host_arch()
        good = self.shell(f'verify_binary_arch "{sys.executable}" {host}')
        self.assertEqual(good.returncode, 0, good.stderr)
        bad = self.shell(f'verify_binary_arch "{sys.executable}" {other_arch(host)}')
        self.assertNotEqual(bad.returncode, 0)
        self.assertIn("refusing to package", bad.stderr)

    def test_the_helper_is_sourceable_and_side_effect_free(self) -> None:
        # Sourcing it must not print anything by itself (build scripts source
        # it before their first line of output).
        result = self.shell(":")
        self.assertEqual(result.stdout, "")


class BuildScriptWiringTests(unittest.TestCase):
    """Every build script must resolve an architecture and verify its output."""

    def test_build_scripts_read_the_arch_helper(self) -> None:
        for rel in BUILD_SCRIPTS:
            with self.subTest(script=rel):
                self.assertIn("arch.sh", read(rel), f"{rel} must use scripts/arch.sh")

    def test_shell_build_scripts_verify_the_binary_they_built(self) -> None:
        for rel in ("build/build_linux.sh", "build/build_macos.sh"):
            with self.subTest(script=rel):
                self.assertIn("verify_binary_arch", read(rel))

    def test_no_build_script_hardcodes_amd64(self) -> None:
        # `ARCH="amd64"` is what made the .deb amd64-only; the arch now comes
        # from the host (or SENTINEL_TARGET_ARCH).
        for rel in BUILD_SCRIPTS:
            text = read(rel)
            with self.subTest(script=rel):
                self.assertNotIn('ARCH="amd64"', text)
                self.assertIn("SENTINEL_TARGET_ARCH", text)

    def test_artifacts_are_named_per_architecture(self) -> None:
        linux = read("build/build_linux.sh")
        self.assertIn("_${DEB_ARCH}.deb", linux)
        macos = read("build/build_macos.sh")
        self.assertIn("${MACOS_ARCH}.dmg", macos)
        windows = read("build/build_windows.bat")
        self.assertIn("Sentinel-Setup-%VERSION%-arm64.exe", windows)

    def test_the_pyinstaller_spec_refuses_to_cross_compile(self) -> None:
        spec = read("build/pyinstaller.spec")
        self.assertIn("SENTINEL_TARGET_ARCH", spec)
        self.assertIn("does not cross-compile", spec)
        # macOS is the one platform where the architecture is passed through.
        self.assertIn("target_arch=", spec)

    def test_the_nsis_script_labels_the_architecture(self) -> None:
        nsi = read("build/windows/installer.nsi")
        self.assertIn("!ifndef ARCH", nsi)
        self.assertIn("${ARCHLABEL}", nsi)
        self.assertIn('"Architecture" "${ARCH}"', nsi)

    def test_the_windows_deps_helper_installs_the_right_architecture(self) -> None:
        ps1 = read(".github/scripts/install-windows-deps.ps1")
        self.assertIn("param(", ps1)
        self.assertIn("arm64", ps1)
        self.assertIn("python-$PythonVersion-$installerArch.exe", ps1)
        # An emulated x64 interpreter must not be mistaken for a native one.
        self.assertIn("Get-PythonArch", ps1)

    def test_the_linux_deps_helper_derives_the_library_version(self) -> None:
        # Hard-coding libpython3.11 is fine on the amd64 image but is one more
        # thing to drift; the version now comes from the interpreter.
        sh = read(".github/scripts/install-linux-deps.sh")
        self.assertIn("version_info", sh)


def main() -> int:
    suite = unittest.TestLoader().loadTestsFromModule(sys.modules[__name__])
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
