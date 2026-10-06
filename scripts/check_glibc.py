#!/usr/bin/env python3
"""Report - and cap - the oldest glibc a Linux build can run against.

Why this exists: PyInstaller bundles the CPython runtime and every extension
module into the artifact, and a shared library built on a newer distribution
carries versioned symbol requirements (`GLIBC_2.35`, `GLIBC_2.38`, ...) from
that distribution. The *highest* such requirement across the bundle is the
oldest glibc the artifact can run on, no matter what the `.deb` says in its
`Depends:` field. Build on Ubuntu 24.04 and the arm64 `.deb` will not start on
Raspberry Pi OS 64-bit "Bookworm" (Debian 12, glibc 2.36), even though the
architecture is right - the loader stops with

    /opt/sentinel/sentinel: /lib/aarch64-linux-gnu/libc.so.6:
        version `GLIBC_2.38' not found

which is exactly the report this module answers before shipping. The version
is read out of each ELF file's own `.gnu.version_r` table, so it is the
artifact's real floor rather than the build host's version.

Reference floors (glibc):

    2.31  Debian 11 "bullseye"      Raspberry Pi OS 64-bit Bullseye (legacy)
    2.35  Ubuntu 22.04 "jammy"      the CI build host for the Linux artifacts
    2.36  Debian 12 "bookworm"      Raspberry Pi OS 64-bit Bookworm
    2.39  Ubuntu 24.04 "noble"
    2.41  Debian 13 "trixie"        Raspberry Pi OS 64-bit Trixie

Usage:

    scripts/check_glibc.py file dist/sentinel/sentinel              # one binary
    scripts/check_glibc.py dir  dist/sentinel                       # whole bundle
    scripts/check_glibc.py dir  dist/sentinel --max 2.36            # cap it

`--max` fails the command when the bundle needs a newer glibc, naming the
files that do. Exit codes: 0 = within the cap, 1 = above it, 2 = could not
tell (missing path, unreadable ELF).

Only ELF files are inspected, so a bundle may be pointed at directly; other
files (Python modules, the dashboard HTML, the bundled VERSION) are skipped.
"""

from __future__ import annotations

import argparse
import struct
import sys
from pathlib import Path

# Section whose entries list the versioned symbols a file needs.
VERNEED_SECTION = ".gnu.version_r"
STRTAB_SECTION = ".dynstr"

ELF_MAGIC = b"\x7fELF"
SHT_STRTAB = 3


class UnknownFloor(Exception):
    """The file could not be read, or is not an ELF this parser understands."""


def parse_version(text: str) -> tuple[int, ...]:
    """'2.36' or 'GLIBC_2.36' -> (2, 36); anything else is an error."""
    value = text.strip()
    if value.upper().startswith("GLIBC_"):
        value = value[len("GLIBC_"):]
    parts = value.split(".")
    if not parts or not all(part.isdigit() for part in parts):
        raise ValueError(f"not a glibc version: {text!r}")
    return tuple(int(part) for part in parts)


def format_version(version: tuple[int, ...]) -> str:
    return ".".join(str(part) for part in version)


def is_elf(data: bytes) -> bool:
    return data[:4] == ELF_MAGIC and len(data) >= 20


def _section_headers(data: bytes) -> list[dict]:
    """Every section header, as dicts of name/type/offset/size."""
    elf_class = data[4]
    little = data[5] == 1
    endian = "<" if little else ">"
    if elf_class == 2:  # ELF64
        shoff_format = endian + "Q"
        shoff_offset, shentsize_offset, shnum_offset, shstrndx_offset = 0x28, 0x3A, 0x3C, 0x3E
        header_format, header_size = endian + "IIQQQQIIQQ", 64
        name_at, type_at, offset_at, size_at = 0, 1, 4, 5
    elif elf_class == 1:  # ELF32
        shoff_format = endian + "I"
        shoff_offset, shentsize_offset, shnum_offset, shstrndx_offset = 0x20, 0x2E, 0x30, 0x32
        header_format, header_size = endian + "IIIIIIIIII", 40
        name_at, type_at, offset_at, size_at = 0, 1, 4, 5
    else:
        raise UnknownFloor("unknown ELF class")

    (shoff,) = struct.unpack_from(shoff_format, data, shoff_offset)
    (shentsize,) = struct.unpack_from(endian + "H", data, shentsize_offset)
    (shnum,) = struct.unpack_from(endian + "H", data, shnum_offset)
    (shstrndx,) = struct.unpack_from(endian + "H", data, shstrndx_offset)

    if shoff == 0 or shnum == 0:
        raise UnknownFloor("no section headers (stripped?)")
    if shentsize < header_size:
        raise UnknownFloor("unexpected section header size")

    sections = []
    for index in range(shnum):
        offset = shoff + index * shentsize
        if offset + header_size > len(data):
            raise UnknownFloor("section headers run past the end of the file")
        fields = struct.unpack_from(header_format, data, offset)
        sections.append({
            "name": fields[name_at],
            "type": fields[type_at],
            "offset": fields[offset_at],
            "size": fields[size_at],
        })

    # Resolve names through the section header string table.
    if shstrndx >= len(sections):
        raise UnknownFloor("bad section header string table index")
    strings = sections[shstrndx]
    blob = data[strings["offset"]:strings["offset"] + strings["size"]]
    for section in sections:
        section["name"] = _cstring(blob, section["name"])
    return sections


def _cstring(blob: bytes, offset: int) -> str:
    if offset >= len(blob):
        return ""
    end = blob.find(b"\0", offset)
    return blob[offset:end if end != -1 else len(blob)].decode("utf-8", "replace")


def required_glibc(path: str | Path) -> tuple[int, ...]:
    """The highest ``GLIBC_x.y`` symbol version the ELF file at ``path`` needs.

    Raises ``UnknownFloor`` when the file is not an ELF or carries no
    ``.gnu.version_r`` table (a static binary, for instance).
    """
    try:
        data = Path(path).read_bytes()
    except OSError as error:
        raise UnknownFloor(f"cannot read {path}: {error}") from error
    if not is_elf(data):
        raise UnknownFloor(f"{path} is not an ELF file")

    sections = _section_headers(data)
    by_name = {section["name"]: section for section in sections}
    verneed = by_name.get(VERNEED_SECTION)
    dynstr = by_name.get(STRTAB_SECTION)
    if verneed is None:
        raise UnknownFloor(f"{path} has no {VERNEED_SECTION} section")
    if dynstr is None:
        raise UnknownFloor(f"{path} has no {STRTAB_SECTION} section")

    strings = data[dynstr["offset"]:dynstr["offset"] + dynstr["size"]]
    blob = data[verneed["offset"]:verneed["offset"] + verneed["size"]]
    # The version table follows the file's byte order (EI_DATA).
    endian = "<" if data[5] == 1 else ">"

    highest: tuple[int, ...] = ()
    offset = 0
    while offset + 16 <= len(blob):
        # Elf{32,64}_Verneed and Elf{32,64}_Vernaux are both 16 bytes and
        # share a layout between the two ELF classes.
        _version, count, _file, aux, next_need = struct.unpack_from(endian + "HHIII", blob, offset)
        aux_offset = offset + aux
        for _ in range(count):
            if aux_offset + 16 > len(blob):
                break
            _hash, _flags, _other, name_offset, next_aux = struct.unpack_from(endian + "IHHII", blob, aux_offset)
            name = _cstring(strings, name_offset)
            if name.startswith("GLIBC_"):
                try:
                    candidate = parse_version(name)
                except ValueError:
                    candidate = ()
                if candidate > highest:
                    highest = candidate
            if not next_aux:
                break
            aux_offset += next_aux
        if not next_need:
            break
        offset += next_need

    if not highest:
        raise UnknownFloor(f"{path} requires no versioned glibc symbols")
    return highest


def elf_files(root: Path) -> list[Path]:
    """Every ELF file under ``root`` (recursively), in a stable order."""
    found: list[Path] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        try:
            with open(path, "rb") as handle:
                header = handle.read(4)
        except OSError:
            continue
        if header == ELF_MAGIC:
            found.append(path)
    return found


def scan(root: Path) -> tuple[tuple[int, ...], dict[tuple[int, ...], list[Path]], int, int]:
    """Return (highest requirement, versions -> files, ELF count, skipped count).

    Files whose version table is unreadable (static binaries, the loader) are
    counted as skipped: they cannot lower the floor, and they are not a
    reason to fail a build.
    """
    highest: tuple[int, ...] = ()
    versions: dict[tuple[int, ...], list[Path]] = {}
    elfs = elf_files(root)
    skipped = 0
    for path in elfs:
        try:
            requirement = required_glibc(path)
        except UnknownFloor:
            skipped += 1
            continue
        versions.setdefault(requirement, []).append(path)
        if requirement > highest:
            highest = requirement
    return highest, versions, len(elfs), skipped


def report(root: Path, highest: tuple[int, ...], versions, elfs: int, skipped: int) -> None:
    if not elfs:
        print(f"{root}: no ELF files found")
        return
    print(f"{root}: {elfs} ELF file(s), highest GLIBC requirement: {format_version(highest) or 'none'}")
    at_max = versions.get(highest, [])
    for path in at_max[:5]:
        print(f"    {path.relative_to(root) if path.is_relative_to(root) else path}")
    if len(at_max) > 5:
        print(f"    ... and {len(at_max) - 5} more at {format_version(highest)}")
    if skipped:
        print(f"    ({skipped} file(s) without a version table, e.g. static binaries - ignored)")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Report or cap the glibc version a Linux build requires.",
        epilog=(
            "reference floors: 2.31 Debian 11, 2.35 Ubuntu 22.04, 2.36 Debian 12 "
            "(Raspberry Pi OS 64-bit Bookworm), 2.39 Ubuntu 24.04, 2.41 Debian 13"
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("file", "dir"):
        command = sub.add_parser(name, help=f"check a {'single binary' if name == 'file' else 'directory of binaries'}")
        command.add_argument("path")
        command.add_argument("--max", metavar="GLIBC", default=None, help="e.g. 2.36; fail if the build needs newer")
        command.add_argument("--quiet", action="store_true", help="print only the version (e.g. 2.35), for scripts")

    args = parser.parse_args(argv)

    try:
        cap = parse_version(args.max) if args.max else None
    except ValueError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 2

    path = Path(args.path)

    if args.command == "file":
        try:
            highest = required_glibc(path)
        except UnknownFloor as error:
            print(f"ERROR: {error}", file=sys.stderr)
            return 2
        if args.quiet:
            print(format_version(highest))
        else:
            print(f"{path}: requires GLIBC {format_version(highest)}")
        if cap and highest > cap:
            print(
                f"ERROR: {path} needs GLIBC {format_version(highest)} but the cap is "
                f"{format_version(cap)}.",
                file=sys.stderr,
            )
            return 1
        return 0

    if not path.is_dir():
        print(f"ERROR: {path} is not a directory", file=sys.stderr)
        return 2

    highest, versions, elfs, skipped = scan(path)
    if not args.quiet:
        report(path, highest, versions, elfs, skipped)
    if cap is None:
        if args.quiet:
            if not elfs:
                print(f"ERROR: no ELF files in {path}", file=sys.stderr)
                return 2
            print(format_version(highest))
        return 0
    if not elfs:
        print(f"ERROR: nothing to check in {path}", file=sys.stderr)
        return 2
    if highest > cap:
        offenders = versions.get(highest, [])[:5]
        if args.quiet:
            print(format_version(highest))
        print(
            f"ERROR: this build needs GLIBC {format_version(highest)}, above the "
            f"{format_version(cap)} cap.",
            file=sys.stderr,
        )
        for offender in offenders:
            print(f"       {offender}", file=sys.stderr)
        print(
            "       Build on an older distribution (or with an older-baseline "
            "Python) to lower the floor.",
            file=sys.stderr,
        )
        return 1
    if args.quiet:
        print(format_version(highest))
    else:
        print(f"    within the {format_version(cap)} cap")
    return 0


if __name__ == "__main__":
    sys.exit(main())
