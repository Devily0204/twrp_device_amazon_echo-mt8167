"""Rebuild the Crumpet DTB with the stock LK kernel command line.

The stock boot image carries 13 appended DTBs whose first entry is the Crumpet
device tree.  LK normally replaces /chosen/bootargs with its generated command
line; the slot loader has to do the same. Firmware interfaces are preserved
unless diagnostic_no_firmware is explicitly selected. That diagnostic mode
disables PSCI/MTEE and is not a complete FireOS boot configuration.
"""
from __future__ import annotations

import struct
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
BOOT = Path("boot.img")
TARGET_COMPATIBLE = b"amzn,crumpet-pvt"

CMDLINE = (
    "console=tty0 console=ttyMT0,921600n1 earlycon=uart8250,mmio32,0x11005000 "
    "vmalloc=496M androidboot.hardware=mt8167 androidboot.secure_cpu=1 "
    "androidboot.unlocked_kernel=false androidboot.pl_version=0x0001 "
    "androidboot.tee_version=0x0001 androidboot.lk_version=0x0001 "
    "androidboot.prod=1 androidboot.pl_build_desc=9eb982d-20231103_072325 "
    "androidboot.lk_build_desc=6f7225e-20230407_002912 "
    "androidboot.verifiedbootstate=green bootopt=64S3,32N2,32N2 "
    "lowmemorykiller.minfree=1536,2048,4096,8192,16384,16384 "
    "lowmemorykiller.adj=0,100,200,300,900,906 buildvariant=user "
    "veritykeyid=id:f3530e18f64d11fc25eb2dd762979f078de990bf "
    "androidboot.selinux=enforce androidboot.veritymode=enforcing "
    "androidboot.slot_suffix=_a rootwait ro init=/init "
    "ubi.mtd=persist,4096,4,0 ubi.mtd=userdata,4096,20,1 skip_initramfs "
    "ubi.block=1,0 root=/dev/dm-0 "
    'dm="system none ro,0 1 android-verity /dev/ubiblock1_0" '
    # Placeholder only; build_bootchain replaces it from this unit's IDME.
    "leds-is31fl3236.ledcalibparams=0"
)

TOK_BEGIN_NODE, TOK_END_NODE, TOK_PROP, TOK_NOP, TOK_END = 1, 2, 3, 4, 9


def pad4(value: int) -> int:
    return (value + 3) & ~3


def rebuild(blob: bytes, *, diagnostic_no_firmware=False) -> bytes:
    magic, total, off_struct, off_strings, off_rsv, version, last, cpid, sstr, sstruct = \
        struct.unpack_from(">10I", blob, 0)
    assert magic == 0xD00DFEED and total == len(blob)
    assert 40 <= off_rsv < off_struct <= off_strings and off_rsv % 8 == 0 and off_struct % 4 == 0
    strings = blob[off_strings:off_strings + sstr]
    rsv = blob[off_rsv:off_struct]
    out = bytearray()
    p = off_struct
    path: list[str] = []
    replaced = 0
    nonsmp = [0, 0]
    mtee = [0]
    while p < off_struct + sstruct:
        token = struct.unpack_from(">I", blob, p)[0]
        if token == TOK_BEGIN_NODE:
            end = blob.find(b"\0", p + 4)
            name = blob[p + 4:end]
            size = pad4(4 + len(name) + 1)
            out += blob[p:p + size]
            path.append(name.decode("latin1"))
            p += size
        elif token == TOK_END_NODE:
            out += blob[p:p + 4]
            path.pop()
            p += 4
        elif token == TOK_PROP:
            length, nameoff = struct.unpack_from(">II", blob, p + 4)
            end = strings.find(b"\0", nameoff)
            name = strings[nameoff:end].decode("latin1")
            value = blob[p + 12:p + 12 + length]
            body = pad4(12 + length)
            if name == "bootargs" and path and path[-1] == "chosen":
                value = CMDLINE.encode() + b"\0"
                body = pad4(12 + len(value))
                out += struct.pack(">III", TOK_PROP, len(value), nameoff)
                out += value + b"\0" * (body - 12 - len(value))
                replaced += 1
            elif (diagnostic_no_firmware and name == "enable-method" and path and path[-1].startswith("cpu@")
                  and value == b"psci\0"):
                out += blob[p:p + 12] + b"none\0" + b"\0" * (body - 17)
                nonsmp[0] += 1
            elif diagnostic_no_firmware and name == "method" and path and path[-1] == "psci" and value == b"smc\0":
                out += blob[p:p + 12] + b"xxx\0"
                nonsmp[1] += 1
            elif (diagnostic_no_firmware and name == "compatible" and path and path[-1] == "mtee"
                  and value == b"mediatek,mtee\0"):
                out += blob[p:p + 12] + b"xediatek,mtee\0" + b"\0" * (body - 26)
                mtee[0] += 1
            else:
                out += blob[p:p + body]
            p += pad4(12 + length)          # advance by the original property
        elif token == TOK_NOP:
            out += blob[p:p + 4]
            p += 4
        elif token == TOK_END:
            out += blob[p:p + 4]
            p += 4
            break
        else:
            raise AssertionError(f"bad FDT token {token:#x} at {p:#x}")
    assert replaced == 1, replaced
    assert nonsmp == ([4, 1] if diagnostic_no_firmware else [0, 0]), nonsmp
    assert mtee == ([1] if diagnostic_no_firmware else [0]), mtee
    new_strings_off = off_struct + len(out)
    new_total = new_strings_off + len(strings)
    header = bytearray(blob[:off_struct])
    struct.pack_into(">I", header, 4, new_total)
    struct.pack_into(">I", header, 0x0C, new_strings_off)
    struct.pack_into(">I", header, 0x24, len(out))
    return bytes(header) + bytes(out) + strings


def describe(blob: bytes) -> bytes:
    magic, total, off_struct, off_strings, off_rsv, version, last, cpid, sstr, sstruct = \
        struct.unpack_from(">10I", blob, 0)
    assert magic == 0xD00DFEED and total == len(blob)
    assert off_struct + sstruct == off_strings
    assert off_strings + sstr == total
    strings = blob[off_strings:off_strings + sstr]
    p, path, bootargs = off_struct, [], None
    while p < off_strings:
        token = struct.unpack_from(">I", blob, p)[0]
        if token == TOK_BEGIN_NODE:
            end = blob.find(b"\0", p + 4)
            path.append(blob[p + 4:end].decode("latin1"))
            p += pad4(4 + end - (p + 4) + 1)
        elif token == TOK_END_NODE:
            path.pop()
            p += 4
        elif token == TOK_PROP:
            length, nameoff = struct.unpack_from(">II", blob, p + 4)
            end = strings.find(b"\0", nameoff)
            if strings[nameoff:end] == b"bootargs" and path[-1] == "chosen":
                bootargs = blob[p + 12:p + 12 + length].rstrip(b"\0")
            p += pad4(12 + length)
        elif token == TOK_NOP:
            p += 4
        elif token == TOK_END:
            break
        else:
            raise AssertionError("bad token")
    assert bootargs is not None
    return bootargs


def root_compatible(blob: bytes) -> list[bytes]:
    magic, total, off_struct, off_strings, off_rsv, version, last, cpid, sstr, sstruct = \
        struct.unpack_from(">10I", blob, 0)
    assert magic == 0xD00DFEED and total == len(blob)
    strings = blob[off_strings:off_strings + sstr]
    p, depth = off_struct, 0
    while p < total:
        token = struct.unpack_from(">I", blob, p)[0]
        if token == TOK_BEGIN_NODE:
            end = blob.find(b"\0", p + 4)
            p = (end + 1 + 3) & ~3
            depth += 1
        elif token == TOK_END_NODE:
            depth -= 1
            p += 4
        elif token == TOK_PROP:
            length, nameoff = struct.unpack_from(">II", blob, p + 4)
            end = strings.find(b"\0", nameoff)
            name = strings[nameoff:end]
            value = blob[p + 12:p + 12 + length]
            p = (p + 12 + length + 3) & ~3
            if depth == 1 and name == b"compatible":
                return value.rstrip(b"\0").split(b"\0")
        elif token == TOK_NOP:
            p += 4
        elif token == TOK_END:
            break
        else:
            raise AssertionError("bad token")
    return []


def select_dtb(boot: bytes) -> bytes:
    for off in [i for i in range(len(boot)) if boot.startswith(b"\xd0\x0d\xfe\xed", i)]:
        total = struct.unpack_from(">I", boot, off + 4)[0]
        blob = boot[off:off + total]
        if TARGET_COMPATIBLE in root_compatible(blob):
            return blob
    raise AssertionError("no crumpet-pvt DTB in boot image")


def main() -> int:
    boot = BOOT.read_bytes()
    blob = select_dtb(boot)
    # This file is consumed by the existing direct diagnostic slot loader.
    # Firmware integration must use rebuild(blob) and its own reserved RAM map.
    out = rebuild(blob, diagnostic_no_firmware=True)
    assert describe(out) == CMDLINE.encode()
    assert len(out) <= 0xDC00, hex(len(out))
    Path(HERE / "crumpet-cmdline.dtb").write_bytes(out)
    print(f"DTB_OK bytes={len(out)} bootargs={len(CMDLINE)}", flush=True)
    for pattern, byte in ((b"androidboot.slot_suffix=_a", b"a"),
                          (b"ubi.block=1,0", b"0"),
                          (b"ubiblock1_0", b"0")):
        index = out.find(pattern)
        assert index >= 0 and out.find(pattern, index + 1) < 0
        print(f"PATCH {pattern.decode()} offset={index + len(pattern) - 1:#x} old={byte.decode()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
