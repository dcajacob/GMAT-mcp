"""Standard-library-only native compatibility checks (also used by the worker)."""

from __future__ import annotations

import platform
import struct
from pathlib import Path

CPU_TYPES = {0x01000007: "x86_64", 0x0100000C: "arm64", 7: "x86", 12: "arm"}
MACH_MAGICS = {
    b"\xce\xfa\xed\xfe": "<",
    b"\xcf\xfa\xed\xfe": "<",
    b"\xfe\xed\xfa\xce": ">",
    b"\xfe\xed\xfa\xcf": ">",
}
FAT_MAGICS = {
    b"\xca\xfe\xba\xbe": (">", 20),
    b"\xbe\xba\xfe\xca": ("<", 20),
    b"\xca\xfe\xba\xbf": (">", 32),
    b"\xbf\xba\xfe\xca": ("<", 32),
}


def binary_architectures(path: Path) -> set[str] | None:
    try:
        with path.open("rb") as stream:
            header = stream.read(64)
            if header[:2] == b"MZ" and len(header) >= 64:
                stream.seek(struct.unpack_from("<I", header, 60)[0])
                pe = stream.read(6)
                if pe[:4] == b"PE\0\0":
                    arch = {0x8664: "x86_64", 0x14C: "x86", 0xAA64: "arm64"}.get(
                        struct.unpack_from("<H", pe, 4)[0]
                    )
                    return {arch} if arch else None
            if header[:4] == b"\x7fELF" and len(header) >= 20:
                endian = "<" if header[5] == 1 else ">"
                arch = {62: "x86_64", 3: "x86", 183: "arm64"}.get(
                    struct.unpack_from(endian + "H", header, 18)[0]
                )
                return {arch} if arch else None
            if header[:4] in MACH_MAGICS and len(header) >= 28:
                arch = CPU_TYPES.get(
                    struct.unpack_from(MACH_MAGICS[header[:4]] + "I", header, 4)[0]
                )
                return {arch} if arch else None
            if header[:4] in FAT_MAGICS and len(header) >= 8:
                endian, record_size = FAT_MAGICS[header[:4]]
                count = struct.unpack_from(endian + "I", header, 4)[0]
                if not 1 <= count <= 32:
                    return None
                stream.seek(0, 2)
                length = stream.tell()
                if 8 + count * record_size > length:
                    return None
                result = set()
                for index in range(count):
                    stream.seek(8 + index * record_size)
                    record = stream.read(record_size)
                    cpu = struct.unpack_from(endian + "I", record)[0]
                    offset, size = struct.unpack_from(
                        endian + ("QQ" if record_size == 32 else "II"), record, 8
                    )
                    if offset < 8 + count * record_size or size < 28 or offset + size > length:
                        return None
                    stream.seek(offset)
                    thin = stream.read(8)
                    if (
                        thin[:4] not in MACH_MAGICS
                        or struct.unpack_from(MACH_MAGICS[thin[:4]] + "I", thin, 4)[0] != cpu
                    ):
                        return None
                    if cpu in CPU_TYPES:
                        result.add(CPU_TYPES[cpu])
                return result or None
    except (OSError, struct.error):
        pass
    return None


def architecture_label(architectures):
    if architectures is None:
        return None
    if not architectures:
        return "incompatible"
    if architectures == {"x86_64", "arm64"}:
        return "universal2"
    return ",".join(sorted(architectures))


def binary_architecture(path: Path) -> str | None:
    return architecture_label(binary_architectures(path))


def current_architecture() -> str:
    if struct.calcsize("P") == 4:
        return "x86"
    machine = platform.machine().lower()
    return {"amd64": "x86_64", "aarch64": "arm64"}.get(machine, machine)


def binding_architectures(directory: Path) -> set[str] | None:
    compatible = None
    for pattern in ("*.pyd", "*.so", "*.dylib"):
        for path in sorted(directory.glob(pattern)):
            arches = binary_architectures(path)
            if arches is not None:
                compatible = arches if compatible is None else compatible & arches
    return compatible


def binding_architecture(directory: Path) -> str | None:
    return architecture_label(binding_architectures(directory))
