#!/usr/bin/env python3
"""Report - and verify - the CPU architecture a binary was built for.

Why this exists: Sentinel ships one installer per architecture (amd64 and
arm64), and each is built on a machine of that architecture because
PyInstaller cannot cross-compile. That makes "the build produced the wrong
architecture" a realistic failure - a CI runner with an emulated Python, an
arm64 label on an x86_64 runner, a stale Python on PATH - and it is invisible
until a user on an arm64 machine tries to run the installer. So every build
script checks the artifact it just produced, by reading the file's own header,
and refuses to package the wrong thing.

Recognized formats (enough to cover the three platforms we ship):

  * ELF      - Linux, the `sentinel` binary and extension modules
  * Mach-O   - macOS, including universal (fat) binaries
  * PE/COFF  - Windows, the `sentinel.exe`

Usage:

    scripts/check_arch.py file dist/sentinel/sentinel            # what is it?
    scripts/check_arch.py file dist/sentinel/sentinel --expect arm64
    scripts/check_arch.py host --expect arm64

Exit codes: 0 = as expected, 1 = built for a different architecture,
2 = could not tell (missing file, unrecognized format, unknown arch name).

Run standalone (no third-party dependencies), or import it:

    from check_arch import detect_arch, host_arch, canonical_arch
"""

from __future__ import annotations

import argparse
import platform
import struct
import sys
from pathlib import Path

# Canonical names used throughout the build: `amd64` (the Debian/Ubuntu name
# for x86-64, also what Windows and Linux users expect) and `arm64`.
CANONICAL_ALIASES = {
    "amd64": "amd64",
    "x86_64": "amd64",
    "x64": "amd64",
    "intel": "amd64",
    "arm64": "arm64",
    "aarch64": "arm64",
    "armv8": "arm64",
    "armv8l": "arm64",
}

# ELF e_machine -> canonical name (only the values we are likely to meet).
ELF_MACHINES = {
    0x03: "x86",
    0x28: "arm",
    0x3E: "amd64",
    0xB7: "arm64",
    0xF3: "riscv",
}

# Mach-O cputype -> canonical name.
MACHO_CPUS = {
    7: "x86",
    0x01000007: "amd64",
    12: "arm",
    0x0100000C: "arm64",
}

# PE COFF machine -> canonical name.
PE_MACHINES = {
    0x014C: "x86",
    0x01C0: "arm",
    0x01C4: "arm",
    0x8664: "amd64",
    0xAA64: "arm64",
    0x0200: "ia64",
}

MH_MAGIC = 0xFEEDFACE
MH_MAGIC_64 = 0xFEEDFACF
MH_CIGAM = 0xCEFAEDFE
MH_CIGAM_64 = 0xCFFAEDFE
FAT_MAGIC = 0xCAFEBABE
FAT_MAGIC_64 = 0xCAFEBABF


class UnknownArchitecture(Exception):
    """The file could not be read, or its format carries no CPU type."""


def canonical_arch(value: str) -> str:
    """Normalize a spelling of an architecture to ``amd64`` or ``arm64``.

    Raises ``UnknownArchitecture`` for anything else, including a string that
    is already canonical for a different CPU (e.g. ``x86``).
    """
    key = (value or "").strip().lower()
    if key in CANONICAL_ALIASES:
        return CANONICAL_ALIASES[key]
    raise UnknownArchitecture(f"unknown architecture: {value!r}")


def host_arch() -> str:
    """The architecture of the *interpreter's* machine, canonicalized.

    On Windows, ``platform.machine()`` reports the OS architecture even when
    the Python interpreter is an emulated x86_64 one on an arm64 host, which
    is exactly the case worth catching; ``PROCESSOR_ARCHITECTURE`` describes
    the interpreter. PyInstaller makes the same distinction when it picks its
    bootloader (- see ``PyInstaller.compat``).
    """
    import os

    if sys.platform.startswith("win"):
        machine = os.environ.get("PROCESSOR_ARCHITECTURE") or platform.machine()
    else:
        machine = platform.machine()
    try:
        return canonical_arch(machine)
    except UnknownArchitecture:
        # Fall back to letting the caller compare the raw name.
        return machine


def _elf_arch(data: bytes) -> str:
    if len(data) < 20:
        raise UnknownArchitecture("truncated ELF header")
    little = data[5] == 1  # EI_DATA: 1 = little-endian, 2 = big-endian
    endian = "<" if little else ">"
    (machine,) = struct.unpack_from(endian + "H", data, 18)
    return ELF_MACHINES.get(machine, f"elf-0x{machine:04x}")


def _macho_slice_arch(cputype: int) -> str:
    return MACHO_CPUS.get(cputype, f"macho-0x{cputype:08x}")


def _macho_arch(data: bytes) -> str:
    if len(data) < 8:
        raise UnknownArchitecture("truncated Mach-O header")
    # Read the magic big-endian: a big-endian answer means the file itself is
    # big-endian, and a swapped one means it is little-endian - which is what
    # every Intel/Apple Silicon Mach-O actually is.
    (magic,) = struct.unpack_from(">I", data, 0)
    if magic in (MH_MAGIC, MH_MAGIC_64):
        endian = ">"
    elif magic in (MH_CIGAM, MH_CIGAM_64):
        endian = "<"
    else:
        endian = None
    if endian is not None:
        (cputype,) = struct.unpack_from(endian + "I", data, 4)
        return _macho_slice_arch(cputype)
    if magic in (FAT_MAGIC, FAT_MAGIC_64):
        # Universal binary: a table of slices.
        (count,) = struct.unpack_from(">I", data, 4)
        entry = 32 if magic == FAT_MAGIC_64 else 20
        offset = 8
        arches = []
        for _ in range(count):
            if len(data) < offset + 4:
                break
            (cputype,) = struct.unpack_from(">i", data, offset)
            arches.append(_macho_slice_arch(cputype & 0xFFFFFFFF))
            offset += entry
        if not arches:
            raise UnknownArchitecture("universal Mach-O with no slices")
        return "+".join(arches)
    raise UnknownArchitecture("not a Mach-O file")


def _pe_arch(data: bytes) -> str:
    if len(data) < 0x40:
        raise UnknownArchitecture("truncated DOS header")
    (lfanew,) = struct.unpack_from("<I", data, 0x3C)
    if len(data) < lfanew + 6 or data[lfanew:lfanew + 4] != b"PE\0\0":
        raise UnknownArchitecture("no PE header")
    (machine,) = struct.unpack_from("<H", data, lfanew + 4)
    return PE_MACHINES.get(machine, f"pe-0x{machine:04x}")


def detect_arch(path: str | Path, read_size: int = 4096) -> str:
    """Return the canonical architecture of the binary at ``path``.

    A universal Mach-O reports every slice it contains, joined with ``+``
    (e.g. ``amd64+arm64``). Raises ``UnknownArchitecture`` when the file is
    missing, empty or not a recognized executable format.
    """
    try:
        with open(path, "rb") as handle:
            data = handle.read(read_size)
    except OSError as error:
        raise UnknownArchitecture(f"cannot read {path}: {error}") from error
    if not data:
        raise UnknownArchitecture(f"{path} is empty")

    if data[:4] == b"\x7fELF":
        return _elf_arch(data)
    if data[:2] == b"MZ":
        return _pe_arch(data)
    try:
        return _macho_arch(data)
    except UnknownArchitecture as error:
        raise UnknownArchitecture(f"{path}: unrecognized executable format") from error


def arch_matches(found: str, expected: str) -> bool:
    """True when ``found`` (possibly ``a+b``) contains the expected arch."""
    expected = canonical_arch(expected)
    return expected in found.split("+")


def _report(label: str, found: str, expected: str | None) -> int:
    if expected is None:
        print(f"{label}: {found}")
        return 0
    if arch_matches(found, expected):
        print(f"{label}: {found} (matches {canonical_arch(expected)})")
        return 0
    print(
        f"ERROR: {label} is {found}, but {canonical_arch(expected)} was requested "
        "- refusing to package the wrong architecture.",
        file=sys.stderr,
    )
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Report or verify the CPU architecture of a binary.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    for name, help_text in (
        ("file", "check an executable file (ELF, Mach-O or PE)"),
        ("host", "check the running Python interpreter's architecture"),
    ):
        command = sub.add_parser(name, help=help_text)
        if name == "file":
            command.add_argument("path", help="the binary to inspect")
        command.add_argument(
            "--expect",
            metavar="ARCH",
            default=None,
            help="amd64 or arm64; exits 1 if the binary is anything else",
        )

    args = parser.parse_args(argv)

    try:
        if args.command == "host":
            found = host_arch()
            label = "the running interpreter"
        else:
            found = detect_arch(args.path)
            label = args.path
        return _report(label, found, args.expect)
    except UnknownArchitecture as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
