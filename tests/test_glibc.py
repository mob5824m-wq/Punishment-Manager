"""
Tests for the glibc-floor checks (``scripts/check_glibc.py``).

Motivation: the arm64 `.deb` is built on a GitHub runner, and PyInstaller bundles
that runner's CPython runtime into the artifact. A bundle built on Ubuntu 24.04
therefore carries `GLIBC_2.38`+ symbol requirements and will not start on
Raspberry Pi OS 64-bit "Bookworm" (Debian 12, glibc 2.36) - the architecture
matches, the loader still refuses. The floor has to be read from the artifact
and checked against a cap, which is what these tests cover:

* `parse_version` and the ELF `.gnu.version_r` reader, against synthetic ELF64
  and ELF32 fixtures whose expected answer is known;
* the CLI contract the CI step relies on: exit 1 above the cap, exit 2 when the
  answer cannot be determined, and a `dir` scan that skips non-ELF files;
* a real ELF (this machine's interpreter) on Linux.

Run with either:
    python -m pytest tests/test_glibc.py
    python tests/test_glibc.py
"""

from __future__ import annotations

import importlib.util
import os
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

CHECK_GLIBC = REPO_ROOT / "scripts" / "check_glibc.py"


def load_check_glibc():
    spec = importlib.util.spec_from_file_location("check_glibc", CHECK_GLIBC)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


check_glibc = load_check_glibc()

# --------------------------------------------------------------------------- #
# A minimal, well-formed ELF carrying a .gnu.version_r table, so the reader can
# be checked against a known answer instead of only against this machine.
# --------------------------------------------------------------------------- #

SHSTRTAB = b"\0.shstrtab\0.dynstr\0.gnu.version_r\0"
SH_NAME = {
    ".shstrtab": SHSTRTAB.index(b".shstrtab"),
    ".dynstr": SHSTRTAB.index(b".dynstr"),
    ".gnu.version_r": SHSTRTAB.index(b".gnu.version_r"),
}
SHT_STRTAB = 3
SHT_GNU_VERNEED = 0x6FFFFFFE


def build_elf(versions: list[str], bits: int = 64, little: bool = True) -> bytes:
    """An ELF (no symbols, no program headers) requiring the given versions.

    The result is the shell of a real binary as far as this parser is
    concerned: an ELF header, the three string/version sections, and a section
    header table that names them. ``versions=[]`` omits the version table
    entirely, which is the "nothing to read" case.
    """
    endian = "<" if little else ">"
    with_versions = bool(versions)

    dynstr = bytearray(b"\0")
    libc_offset = len(dynstr)
    dynstr += b"libc.so.6\0"
    name_offsets = []
    for version in versions:
        name_offsets.append(len(dynstr))
        dynstr += f"GLIBC_{version}".encode() + b"\0"

    # One Verneed for libc.so.6, as a real file has, with one Vernaux per
    # versioned symbol - chained through vna_next.
    verneed = bytearray()
    if with_versions:
        verneed += struct.pack(endian + "HHIII", 1, len(name_offsets), libc_offset, 16, 0)
        for index, name_offset in enumerate(name_offsets):
            more = 16 if index + 1 < len(name_offsets) else 0
            verneed += struct.pack(endian + "IHHII", 0x0D696910, 0, index + 2, name_offset, more)

    # [ELF header][.shstrtab][.dynstr][.gnu.version_r][section headers]
    header_size = 64 if bits == 64 else 52
    shdr_size = 64 if bits == 64 else 40
    shstrtab_offset = header_size
    dynstr_offset = shstrtab_offset + len(SHSTRTAB)
    verneed_offset = dynstr_offset + len(dynstr)
    sections = [None,  # the mandatory null section
                ("shstrtab", SHSTRTAB, SHT_STRTAB),
                ("dynstr", bytes(dynstr), SHT_STRTAB)]
    if with_versions:
        sections.append(("gnu.version_r", bytes(verneed), SHT_GNU_VERNEED))
    shoff = verneed_offset + (len(verneed) if with_versions else 0)

    out = bytearray(b"\0" * shoff)
    # e_ident
    out[0:4] = b"\x7fELF"
    out[4] = 2 if bits == 64 else 1          # EI_CLASS
    out[5] = 1 if little else 2              # EI_DATA
    out[6] = 1                               # EI_VERSION
    if bits == 64:
        struct.pack_into(endian + "HHI", out, 16, 2, 0x3E, 1)      # type, machine, version
        struct.pack_into(endian + "Q", out, 0x28, shoff)            # e_shoff
        struct.pack_into(endian + "HHH", out, 0x3A, shdr_size, len(sections), 1)
    else:
        struct.pack_into(endian + "HHI", out, 16, 2, 0x28, 1)
        struct.pack_into(endian + "I", out, 0x20, shoff)
        struct.pack_into(endian + "HHH", out, 0x2E, shdr_size, len(sections), 1)

    out[shstrtab_offset:shstrtab_offset + len(SHSTRTAB)] = SHSTRTAB
    out[dynstr_offset:dynstr_offset + len(dynstr)] = bytes(dynstr)
    if with_versions:
        out[verneed_offset:verneed_offset + len(verneed)] = bytes(verneed)

    def pack_section(sh_name: int, sh_type: int, offset: int, size: int) -> bytes:
        if bits == 64:
            return struct.pack(endian + "IIQQQQIIQQ", sh_name, sh_type, 0, 0, offset, size, 0, 0, 1, 0)
        return struct.pack(endian + "IIIIIIIIII", sh_name, sh_type, 0, 0, offset, size, 0, 0, 1, 0)

    body = bytearray()
    for section in sections:
        if section is None:
            body += pack_section(0, 0, 0, 0)
            continue
        name, data, section_type = section
        key = "." + name
        if section_type == SHT_GNU_VERNEED:
            body += pack_section(SH_NAME[key], section_type, verneed_offset, len(data))
        elif name == "dynstr":
            body += pack_section(SH_NAME[key], section_type, dynstr_offset, len(data))
        else:
            body += pack_section(SH_NAME[key], section_type, shstrtab_offset, len(data))

    return bytes(out + body)


class VersionParsingTests(unittest.TestCase):
    def test_accepted_spellings(self) -> None:
        self.assertEqual(check_glibc.parse_version("2.36"), (2, 36))
        self.assertEqual(check_glibc.parse_version("GLIBC_2.36"), (2, 36))
        self.assertEqual(check_glibc.parse_version(" 2.2.5 "), (2, 2, 5))

    def test_ordering_is_numeric_not_lexicographic(self) -> None:
        # Lexicographically "2.9" sorts after "2.17", which would get the cap
        # comparison backwards: 2.9 is the older glibc. Compare ints.
        self.assertGreater("2.9", "2.17")  # the trap
        self.assertLess(check_glibc.parse_version("2.9"), check_glibc.parse_version("2.17"))
        self.assertGreater(check_glibc.parse_version("2.36"), check_glibc.parse_version("2.9"))

    def test_junk_is_rejected(self) -> None:
        for value in ("", "bookworm", "GLIBCXX_3.4", "2.x"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    check_glibc.parse_version(value)

    def test_formatting_round_trips(self) -> None:
        self.assertEqual(check_glibc.format_version(check_glibc.parse_version("2.36")), "2.36")


class VerneedReadingTests(unittest.TestCase):
    """The ELF reader, against fixtures whose answer is known."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="sentinel-glibc-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def write(self, name: str, data: bytes) -> Path:
        path = self.tmp / name
        path.write_bytes(data)
        return path

    def test_elf64(self) -> None:
        path = self.write("elf64", build_elf(["2.17", "2.34", "2.38"]))
        self.assertEqual(check_glibc.required_glibc(path), (2, 38))

    def test_elf32(self) -> None:
        # 32-bit ELF has a different section header layout - the case a
        # Raspberry Pi (armhf) artifact would exercise.
        path = self.write("elf32", build_elf(["2.17", "2.28"], bits=32))
        self.assertEqual(check_glibc.required_glibc(path), (2, 28))

    def test_big_endian(self) -> None:
        path = self.write("be", build_elf(["2.19"], little=False))
        self.assertEqual(check_glibc.required_glibc(path), (2, 19))

    def test_no_version_table(self) -> None:
        # A static binary needs no versioned symbols; that is not an error,
        # it just cannot lower the floor and must not be treated as "0".
        path = self.write("static", build_elf([]))
        with self.assertRaises(check_glibc.UnknownFloor):
            check_glibc.required_glibc(path)

    def test_not_an_elf(self) -> None:
        path = self.write("text", b"#!/bin/sh\necho hi\n")
        with self.assertRaises(check_glibc.UnknownFloor):
            check_glibc.required_glibc(path)

    def test_truncated_elf(self) -> None:
        with self.assertRaises(check_glibc.UnknownFloor):
            check_glibc.required_glibc(self.write("truncated", b"\x7fELF" + b"\x00" * 20))

    def test_missing_file(self) -> None:
        with self.assertRaises(check_glibc.UnknownFloor):
            check_glibc.required_glibc(self.tmp / "nope")

    @unittest.skipUnless(sys.platform.startswith("linux"), "ELF is a Linux format")
    def test_a_real_binary(self) -> None:
        requirement = check_glibc.required_glibc(sys.executable)
        # Every Linux binary needs at least the ancient baseline symbols.
        self.assertGreaterEqual(requirement, (2, 2, 5))
        self.assertLess(requirement, (3, 0))

    @unittest.skipUnless(sys.platform.startswith("linux"), "ELF is a Linux format")
    def test_the_scan_walks_a_tree_and_skips_non_elf(self) -> None:
        (self.tmp / "sub").mkdir()
        self.write("a.bin", build_elf(["2.17"]))
        (self.tmp / "sub" / "b.bin").write_bytes(build_elf(["2.36"]))
        (self.tmp / "notes.txt").write_text("not a binary\n", encoding="utf-8")
        highest, versions, elfs, skipped = check_glibc.scan(self.tmp)
        self.assertEqual(highest, (2, 36))
        self.assertEqual(elfs, 2)
        self.assertEqual(skipped, 0)
        self.assertEqual(sorted(p.name for p in versions[(2, 36)]), ["b.bin"])


class CommandLineTests(unittest.TestCase):
    """Exit codes the CI gate depends on: 0 ok, 1 over the cap, 2 unknown."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="sentinel-glibc-cli-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def run_cli(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(CHECK_GLIBC), *args],
            capture_output=True, text=True, cwd=str(REPO_ROOT),
        )

    def fixture(self, versions: list[str], name: str = "binary") -> Path:
        path = self.tmp / name
        path.write_bytes(build_elf(versions))
        return path

    def test_reports_without_a_cap(self) -> None:
        result = self.run_cli("file", str(self.fixture(["2.38"])))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("2.38", result.stdout)

    def test_within_the_cap(self) -> None:
        result = self.run_cli("file", str(self.fixture(["2.35"])), "--max", "2.36")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_above_the_cap_fails_and_names_the_file(self) -> None:
        path = self.fixture(["2.38"])
        result = self.run_cli("file", str(path), "--max", "2.36")
        self.assertEqual(result.returncode, 1)
        self.assertIn("2.38", result.stderr)
        self.assertIn(str(path), result.stderr)

    def test_a_directory_scan_caps_the_whole_bundle(self) -> None:
        # The Raspberry Pi OS 64-bit "Bookworm" promise: nothing in the bundle
        # may need more than glibc 2.36.
        self.fixture(["2.17"], "ok")
        self.fixture(["2.36"], "bookworm")
        good = self.run_cli("dir", str(self.tmp), "--max", "2.36")
        self.assertEqual(good.returncode, 0, good.stdout + good.stderr)
        self.assertIn("2.36", good.stdout)

        self.fixture(["2.39"], "ubuntu2404")
        bad = self.run_cli("dir", str(self.tmp), "--max", "2.36")
        self.assertEqual(bad.returncode, 1)
        self.assertIn("ubuntu2404", bad.stderr)
        self.assertIn("older distribution", bad.stderr)

    def test_a_missing_path_is_exit_2(self) -> None:
        self.assertEqual(self.run_cli("file", str(self.tmp / "nope")).returncode, 2)
        self.assertEqual(self.run_cli("dir", str(self.tmp / "nope")).returncode, 2)

    def test_a_non_elf_file_is_exit_2(self) -> None:
        path = self.tmp / "text"
        path.write_text("hello\n", encoding="utf-8")
        self.assertEqual(self.run_cli("file", str(path), "--max", "2.36").returncode, 2)

    def test_a_bad_cap_is_exit_2(self) -> None:
        result = self.run_cli("file", str(self.fixture(["2.36"])), "--max", "bookworm")
        self.assertEqual(result.returncode, 2)
        self.assertIn("not a glibc version", result.stderr)

    def test_quiet_prints_only_the_version(self) -> None:
        # build_linux.sh reads the floor this way for the .deb's Depends line.
        self.fixture(["2.35"], "lib")
        result = self.run_cli("dir", str(self.tmp), "--quiet")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "2.35")

    def test_quiet_reports_the_floor_it_rejected(self) -> None:
        self.fixture(["2.38"], "lib")
        result = self.run_cli("dir", str(self.tmp), "--quiet", "--max", "2.36")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout.strip(), "2.38")

    def test_an_empty_directory_is_exit_2_when_capped(self) -> None:
        empty = self.tmp / "empty"
        empty.mkdir()
        self.assertEqual(self.run_cli("dir", str(empty), "--max", "2.36").returncode, 2)


WORKFLOW = REPO_ROOT / ".github" / "workflows" / "build-installers.yml"


class HelperWiringTests(unittest.TestCase):
    def test_the_helper_is_executable(self) -> None:
        self.assertTrue(os.access(CHECK_GLIBC, os.X_OK), "scripts/check_glibc.py is not executable")

    def test_the_cap_and_its_reason_are_documented(self) -> None:
        text = CHECK_GLIBC.read_text(encoding="utf-8")
        self.assertIn("2.36", text)
        self.assertIn("Raspberry Pi OS", text)

    def test_ci_enforces_the_cap_on_the_linux_artifacts(self) -> None:
        text = WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("check_glibc.py dir dist/sentinel --max", text)
        self.assertIn("glibc_max: '2.36'", text)

    def test_the_deb_depends_on_the_floor_it_was_measured_at(self) -> None:
        # Not a hard-coded libc6 version: the control file has to state what the
        # bundle actually needs, so apt can refuse a too-old distribution.
        text = (REPO_ROOT / "build" / "build_linux.sh").read_text(encoding="utf-8")
        self.assertIn("check_glibc.py dir dist/sentinel --quiet", text)
        self.assertIn("Depends: libc6 (>= ${GLIBC_FLOOR})", text)
        self.assertNotIn("libc6 (>= 2.31)", text)

    def test_the_linux_artifacts_are_built_on_the_pi_compatible_baseline(self) -> None:
        # Ubuntu 24.04's glibc (2.39) exceeds the cap enforced above, so the
        # Linux legs have to stay on jammy (2.35). If someone bumps the runner
        # without lowering the floor another way, this fails with the reason
        # instead of shipping a .deb that dies on Raspberry Pi OS.
        text = WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("runner: ubuntu-22.04", text)
        self.assertIn("runner: ubuntu-22.04-arm", text)
        self.assertNotIn("runner: ubuntu-24.04", text)


def main() -> int:
    suite = unittest.TestLoader().loadTestsFromModule(sys.modules[__name__])
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
