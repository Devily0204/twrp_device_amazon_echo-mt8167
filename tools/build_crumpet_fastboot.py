#!/usr/bin/env python3
"""Build a self-contained Crumpet Fastboot bundle from a TWRP recovery image.

Kernel, PVT FDT and the real TWRP initramfs are extracted from the recovery
image. The Android/MTK header, a tiny valid initramfs for the LK-facing boot
header, and a minimal Thumb handoff are generated from source here. No LK
baseline, vendor binary, rescue ramdisk or binary stage is used. The default
callback enters our minimal stage at 0x45971001; `--stage direct` instead
invokes LK's own loader at 0x41E1D585 for comparison.

Example:
    python tools/build_crumpet_fastboot.py --recovery out/recovery.img \
      --slot b --output out/crumpet-twrp-b.bundle.bin

``--fsync-fix`` is only for old crumpet-mtd builds; current sources contain it.
No third-party Python packages or device access are needed to build a bundle.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
from pathlib import Path
import struct
import sys

REPO = Path(__file__).resolve().parents[1]
PAGE, KERNEL_OFF, KERNEL_ACTUAL = 0x800, 0xA00, 0x5E5F3F
KERNEL_SIZE, KERNEL_ADDR = 0x5E613F, 0x40008000
DUMMY_OFF, DUMMY_ADDR = 0x5E7000, 0x44000000
SECOND_ADDR, TAG_ADDR, OS_VERSION = 0x40F00000, 0x4E000000, 0x0E0411A3
FDT_OFF, RAMDISK_OFF = 0x980000, 0x990000
RAMDISK_ADDR, MAX_DOWNLOAD = 0x45990000, 0x8000000
LOADER_ENTRY_THUMB = 0x41E1D585
STAGE_ENTRY_THUMB = 0x45971001
STAGE_OFF = 0x971000
STOCK_LOADER = {
    "entry_thumb": hex(LOADER_ENTRY_THUMB),
    "selected_boot_mode": 4,
    "boot_image_initializer_thumb": "0x41e11a79",
    "core_boot_thumb": "0x41e1c241",
}


def require(ok, message):
    if not ok:
        raise ValueError(message)


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def be32(data, offset):
    return struct.unpack_from(">I", data, offset)[0]


def boot_geometry(image):
    require(image[:8] == b"ANDROID!", "Missing ANDROID! header")
    ks, rs, ss = struct.unpack_from("<I", image, 8)[0], struct.unpack_from("<I", image, 16)[0], struct.unpack_from("<I", image, 24)[0]
    page = struct.unpack_from("<I", image, 36)[0]
    require(page == PAGE and not ss and ks == KERNEL_ACTUAL, "Unexpected recovery image geometry")
    return page, rs


def extract_ramdisk(image):
    page, rs = boot_geometry(image)
    ks = struct.unpack_from("<I", image, 8)[0]
    offset = page + ((ks + page - 1) // page * page)
    require(offset + rs <= len(image), "Ramdisk exceeds recovery image")
    ramdisk = image[offset:offset + rs]
    require(ramdisk[:3] == b"\x1f\x8b\x08", "Recovery ramdisk is not gzip")
    require(gzip.decompress(ramdisk)[:6] in (b"070701", b"070702"), "Ramdisk is not newc")
    return ramdisk


def appended_dtbs(kernel):
    """Return (byte-offset, FDT) pairs from the recovery zImage-dtb."""
    result = []
    offset = 0
    while True:
        offset = kernel.find(b"\xd0\x0d\xfe\xed", offset)
        if offset < 0:
            break
        size = be32(kernel, offset + 4)
        require(0 < size <= len(kernel) - offset, f"Invalid appended FDT at {offset:#x}")
        result.append((offset, kernel[offset:offset + size]))
        offset += size
    require(result, "No FDTs in the recovery kernel")
    return result


def recovery_kernel(image):
    boot_geometry(image)
    # recovery.img has no MTK header; add the baseline header at KERNEL_OFF-0x200.
    kernel = image[PAGE:PAGE + KERNEL_ACTUAL]
    require(kernel[0x24:0x28] == bytes.fromhex("18286f01"), "Not an ARM zImage")
    return kernel


def recovery_dtb(kernel):
    candidates = []
    for offset, fdt in appended_dtbs(kernel):
        root = fdt_properties(fdt).get("/", {})
        compatible = root.get("compatible", b"")
        if b"amzn,crumpet-pvt\0" in compatible:
            require(root.get("model") == b"Amazon Crumpet DVT\0", "Unexpected PVT model")
            candidates.append((offset, fdt))
    require(len(candidates) == 1, "Expected exactly one PVT FDT in recovery zImage")
    return candidates[0]


def patch_memory_249m(fdt):
    nodes = fdt_properties(fdt)
    path = "/memory@00000000" if "/memory@00000000" in nodes else "/memory"
    require(path in nodes and len(nodes[path].get("reg", b"")) == 16, "Missing /memory reg")
    before = nodes[path]["reg"]
    expected = struct.pack(">QQ", 0x40000000, 0x20000000)
    patched = struct.pack(">QQ", 0x40000000, 0x0F9C0000)
    require(before in (expected, patched), f"Unexpected physical memory map: {before.hex()}")
    out = bytearray(fdt)
    start = out.find(before)
    require(start >= 0 and out[start:start + 16] == before, "Cannot locate /memory reg")
    out[start:start + 16] = patched
    return bytes(out)


def cpio_entries(data):
    offset = 0
    while offset + 110 <= len(data):
        require(data[offset:offset + 6] in (b"070701", b"070702"), "Invalid newc ramdisk")
        fields = [int(data[offset + 6 + i * 8:offset + 14 + i * 8], 16) for i in range(13)]
        size, namesize = fields[6], fields[11]
        start = offset + 110
        name = data[start:start + namesize - 1].decode("utf-8")
        start = (start + namesize + 3) & ~3
        if name == "TRAILER!!!":
            return
        yield name.removeprefix("./"), start, size
        offset = (start + size + 3) & ~3
    raise ValueError("Unterminated newc ramdisk")


def patch_fsync(ramdisk):
    raw = bytearray(gzip.decompress(ramdisk))
    targets = [(offset, size) for name, offset, size in cpio_entries(raw) if name == "sbin/crumpet-mtd"]
    require(len(targets) == 1, "sbin/crumpet-mtd missing")
    offset, size = targets[0]
    require(size == 0x46550, "Different crumpet-mtd build; use its source fix")
    stub = offset + 0x2F88
    original, patched = bytes.fromhex("07c0a0e17670a0e3"), bytes.fromhex("0000a0e31eff2fe1")
    require(raw[stub:stub + 8] in (original, patched), "Unexpected fsync site; no blind patch")
    raw[stub:stub + 8] = patched
    return gzip.compress(bytes(raw), 9, mtime=0)


def fdt_properties(data):
    require(data[:4] == b"\xd0\x0d\xfe\xed" and be32(data, 4) == len(data), "Invalid FDT size")
    offset, path, nodes = be32(data, 8), [], {}
    strings = be32(data, 12)
    while True:
        require(offset + 4 <= len(data), "Truncated FDT")
        token = be32(data, offset)
        offset += 4
        if token == 1:
            end = data.index(b"\0", offset)
            name = data[offset:end].decode("utf-8")
            offset = (end + 4) & ~3
            if name:
                path.append(name)
            nodes["/" + "/".join(path)] = {}
        elif token == 2:
            if path:
                path.pop()
        elif token == 3:
            length, nameoff = be32(data, offset), be32(data, offset + 4)
            offset += 8
            end = data.index(b"\0", strings + nameoff)
            key = data[strings + nameoff:end].decode("utf-8")
            node = "/" + "/".join(path)
            require(node in nodes and offset + length <= len(data), "Invalid FDT property")
            nodes[node][key] = data[offset:offset + length]
            offset = (offset + length + 3) & ~3
        elif token == 4:
            pass
        elif token == 9:
            return nodes
        else:
            raise ValueError(f"Unknown FDT token {token:#x}")


def patch_bootargs(fdt, cmdline):
    nodes = fdt_properties(fdt)
    require("bootargs" in nodes.get("/chosen", {}), "No /chosen/bootargs")
    strings, strings_size = be32(fdt, 12), be32(fdt, 32)
    found = None
    for offset in range(0, len(fdt) - 12, 4):
        if be32(fdt, offset) != 3:
            continue
        length, nameoff = be32(fdt, offset + 4), be32(fdt, offset + 8)
        if not (0 < length <= 0x2000 and nameoff < strings_size):
            continue
        end = fdt.find(b"\0", strings + nameoff)
        if end >= 0 and fdt[strings + nameoff:end] == b"bootargs" and fdt[offset + 12:offset + 12 + length] == nodes["/chosen"]["bootargs"]:
            found = offset
            break
    require(found is not None, "Cannot locate /chosen/bootargs")
    padded = (len(cmdline) + 3) & ~3
    old_padded = (be32(fdt, found + 4) + 3) & ~3
    out = bytearray(fdt[:found + 4])
    out += struct.pack(">II", len(cmdline), be32(fdt, found + 8))
    out += cmdline + bytes(padded - len(cmdline))
    out += fdt[found + 12 + old_padded:]
    shift = padded - old_padded
    struct.pack_into(">I", out, 4, len(out))
    struct.pack_into(">I", out, 12, be32(fdt, 12) + shift)
    struct.pack_into(">I", out, 36, be32(fdt, 36) + shift)
    out = bytes(out)
    require(fdt_properties(out)["/chosen"]["bootargs"] == cmdline, "Bootargs verification failed")
    return out


def boot_cmdline(slot, ramdisk_size):
    return (
        "console=ttyMT0,921600n1 earlycon=uart8250,mmio32,0x11005000 "
        f"initrd={RAMDISK_ADDR:#x},{ramdisk_size:#x} "
        "androidboot.hardware=mt8167 androidboot.selinux=permissive "
        f"androidboot.slot_suffix=_{slot} androidboot.mode=recovery "
        "initcall_blacklist=mtk_wdt_driver_init "
        "initcall_blacklist=virtual_sensor_thermal_init "
        "initcall_blacklist=mt8167_cpufreq_driver_init "
        "initcall_blacklist=mtk_cpufreq_init loglevel=8 root=/dev/ram rdinit=/init"
    ).encode("ascii") + b"\0"


def android_id(image):
    ks, rs = struct.unpack_from("<I", image, 8)[0], struct.unpack_from("<I", image, 16)[0]
    ss, page, ds = struct.unpack_from("<I", image, 24)[0], struct.unpack_from("<I", image, 36)[0], struct.unpack_from("<I", image, 40)[0]
    offsets = (page, page + ((ks + page - 1) // page * page))
    offsets += (offsets[-1] + ((rs + page - 1) // page * page),)
    offsets += (offsets[-1] + ((ss + page - 1) // page * page),)
    digest = hashlib.sha1()
    for offset, size in zip(offsets, (ks, rs, ss, ds)):
        digest.update(image[offset:offset + size])
        digest.update(struct.pack("<I", size))
    return digest.digest()


def newc_record(name, data, mode):
    """Create one deterministic newc entry without using any existing ramdisk."""
    encoded = name.encode("utf-8") + b"\0"
    fields = [
        "00000000", f"{mode:08x}", "00000000", "00000000", "00000001",
        "00000000", f"{len(data):08x}", "00000000", "00000000",
        "00000000", "00000000", f"{len(encoded):08x}", "00000000",
    ]
    header = b"070701" + "".join(fields).encode("ascii")
    require(len(header) == 110, "Invalid newc header")
    # newc aligns header length plus name length, not the name alone.
    return header + encoded + bytes((-(110 + len(encoded))) % 4) + data + bytes((-len(data)) % 4)


def minimal_ramdisk():
    """A tiny valid gzip/newc archive for the LK-facing header, not for TWRP.

    Linux is explicitly directed to the actual TWRP ramdisk at 0x45990000.
    This small archive is only present because the LK boot header describes a
    ramdisk. It contains no Amazon/AOSP executable and no rescue environment.
    """
    archive = newc_record("default.prop", b"ro.secure=1\n", 0o100644)
    archive += newc_record("init", b"#!/sbin/sh\n# Not used by the Fastboot handoff.\n", 0o100755)
    archive += newc_record("TRAILER!!!", b"", 0)
    return gzip.compress(archive, compresslevel=9, mtime=0)


def mtk_header(kernel):
    require(len(kernel) == KERNEL_ACTUAL, "Wrong recovery kernel size")
    header = bytearray(0x200)
    struct.pack_into("<II", header, 0, 0x58881688, len(kernel))
    header[8:14] = b"KERNEL\0"
    header[0x20:] = b"\xff" * (0x200 - 0x20)
    return bytes(header)


def build(kernel, fdt, ramdisk, slot, stage="minimal", canary=None, mmu_off=False, lk_loader=False):
    require(len(kernel) == KERNEL_ACTUAL and kernel[0x24:0x28] == bytes.fromhex("18286f01"), "Not an ARM zImage")
    require(fdt[:4] == b"\xd0\x0d\xfe\xed" and be32(fdt, 4) == len(fdt), "Invalid recovery FDT")
    require(ramdisk[:3] == b"\x1f\x8b\x08", "Invalid TWRP ramdisk")
    dummy = minimal_ramdisk()
    require(RAMDISK_OFF + len(ramdisk) <= MAX_DOWNLOAD, "Bundle exceeds LK download limit")
    cmdline = boot_cmdline(slot, len(ramdisk))
    require(len(cmdline) <= 512, "Boot command line too long")
    fdt = patch_bootargs(fdt, cmdline)
    size = (max(FDT_OFF + len(fdt), DUMMY_OFF + len(dummy), RAMDISK_OFF + len(ramdisk)) + 0xFFF) & ~0xFFF
    out = bytearray(size)
    out[:8] = b"ANDROID!"
    struct.pack_into("<I", out, 8, KERNEL_SIZE)
    struct.pack_into("<I", out, 12, KERNEL_ADDR)
    struct.pack_into("<I", out, 16, len(dummy))
    struct.pack_into("<I", out, 20, DUMMY_ADDR)
    struct.pack_into("<I", out, 28, SECOND_ADDR)
    struct.pack_into("<I", out, 32, TAG_ADDR)
    struct.pack_into("<I", out, 36, PAGE)
    struct.pack_into("<I", out, 44, OS_VERSION)
    out[48:64] = b"crumpet-twrp".ljust(16, b"\0")
    out[64:64 + len(cmdline)] = cmdline
    out[PAGE:PAGE + 0x200] = mtk_header(kernel)
    out[KERNEL_OFF:KERNEL_OFF + len(kernel)] = kernel
    out[DUMMY_OFF:DUMMY_OFF + len(dummy)] = dummy
    out[FDT_OFF:FDT_OFF + len(fdt)] = fdt
    out[RAMDISK_OFF:RAMDISK_OFF + len(ramdisk)] = ramdisk
    if lk_loader and stage == "direct":
        raise ValueError("The full LK loader stage cannot be combined with --stage direct")
    if stage in ("minimal", "dpc"):
        from lk_stage_minimal import assemble_stage
        stage_bytes = assemble_stage(canary, dpc=stage == "dpc", mmu_off=mmu_off, lk_loader=lk_loader, initrd_end=RAMDISK_ADDR + len(ramdisk))
        entry_thumb = STAGE_ENTRY_THUMB
        out[STAGE_OFF:STAGE_OFF + len(stage_bytes)] = stage_bytes
        stage_hash = sha256(stage_bytes)
    elif stage == "direct":
        if canary is not None:
            raise ValueError("Reset canaries require --stage minimal")
        entry_thumb = LOADER_ENTRY_THUMB
        stage_hash = None
    else:
        raise ValueError(f"Unknown stage profile: {stage}")
    out[576:608] = android_id(out) + bytes(12)
    manifest = {
        "variant": f"Crumpet TWRP Fastboot: recovery payload, generated LK header and minimal initramfs, slot _{slot}",
        "kernel_off": hex(PAGE), "mtk_header_size": "0x200",
        "mtk_kernel_size": hex(KERNEL_ACTUAL), "kernel_size": hex(KERNEL_SIZE),
        "kernel_actual": hex(KERNEL_ACTUAL), "kernel_sha256": sha256(kernel),
        "kernel_source": "recovery", "fdt_source": "recovery-appended-pvt",
        "kernel_address": hex(KERNEL_ADDR),
        "dummy_ramdisk_offset": hex(DUMMY_OFF), "dummy_ramdisk_size": hex(len(dummy)),
        "dummy_ramdisk_address": hex(DUMMY_ADDR), "dummy_source": "source-generated minimal newc archive",
        "ramdisk_offset": hex(RAMDISK_OFF), "ramdisk_address": hex(RAMDISK_ADDR),
        "ramdisk_size": hex(len(ramdisk)), "ramdisk_sha256": sha256(ramdisk),
        "initrd_address": hex(RAMDISK_ADDR), "initrd_end": hex(RAMDISK_ADDR + len(ramdisk)),
        "slot_suffix": "_" + slot, "fdt_offset": hex(FDT_OFF), "fdt_address": "0x45980000",
        "fdt_size": hex(len(fdt)), "fdt_sha256": sha256(fdt), "r2": "0x45980000",
        "cmdline": cmdline.rstrip(b"\0").decode(), "android_id": bytes(out[576:596]).hex(),
        "bundle_size": len(out), "bundle_sha256": sha256(out),
        "entry_thumb": hex(entry_thumb), "stock_loader": STOCK_LOADER,
        "telemetry": [], "persistent_operations": [],
        "custom_stage": "lk-loader" if lk_loader else stage,
        "stage_canary": canary, "mmu_off": mmu_off, "lk_loader": lk_loader,
        "tags_address": hex(TAG_ADDR),
    }
    if stage_hash:
        manifest["stage_offset"] = hex(STAGE_OFF)
        manifest["stage_sha256"] = stage_hash
        manifest["stage_source"] = (
            "tools/lk_stage_loader.S" if lk_loader else "tools/lk_stage_minimal.S"
        )
    return bytes(out), manifest


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--recovery", required=True, type=Path, help="TWRP recovery.img built by this project")
    parser.add_argument("--recovery-kernel", type=Path, help="Alternative zImage-dtb for the kernel and FDT")
    parser.add_argument("--ramdisk", type=Path, help="Alternative gzip/newc TWRP ramdisk")
    parser.add_argument("--slot", choices=("a", "b"), required=True)
    parser.add_argument("--output", required=True, type=Path, help="Output .bundle.bin")
    parser.add_argument("--stage", choices=("minimal", "dpc", "direct"), default="minimal", help="Project-owned direct stage (default), deferred-DPC stage, or LK loader experiment")
    parser.add_argument("--canary", choices=("entry", "copy", "cache", "mmu", "verify", "cleanup"), help="Diagnostic only: reset at the selected stage milestone instead of entering Linux")
    parser.add_argument("--mmu-off", action="store_true", help="Use the proven LK cache/MMU shutdown before entering zImage")
    parser.add_argument("--lk-loader", action="store_true", help="Use the project-owned full LK loader stage (DPC worker, loader prolog/initializer/DTB hooks) instead of a direct zImage jump")
    parser.add_argument("--parts-dir", type=Path, help="Export final ramdisk.bin, fdt.dtb and dummy-initramfs.gz")
    parser.add_argument("--fsync-fix", action="store_true", help="Patch an older crumpet-mtd build")
    args = parser.parse_args(argv)
    try:
        if args.recovery_kernel:
            kernel = args.recovery_kernel.read_bytes()
        else:
            kernel = recovery_kernel(args.recovery.read_bytes())
        fdt_offset, fdt = recovery_dtb(kernel)
        fdt = patch_memory_249m(fdt)
        ramdisk = args.ramdisk.read_bytes() if args.ramdisk else extract_ramdisk(args.recovery.read_bytes())
        if args.fsync_fix:
            ramdisk = patch_fsync(ramdisk)
        result, manifest = build(kernel, fdt, ramdisk, args.slot, stage=args.stage, canary=args.canary, mmu_off=args.mmu_off, lk_loader=args.lk_loader)
        manifest["fdt_source"] = f"recovery-zImage appended PVT at {fdt_offset:#x}"
        manifest["sources"] = {
            "recovery": str(args.recovery.resolve()),
            "ramdisk": str(args.ramdisk.resolve()) if args.ramdisk else None,
            "recovery_kernel": str(args.recovery_kernel.resolve()) if args.recovery_kernel else None,
            "fsync_fix": args.fsync_fix,
        }
        require(args.output.name.endswith(".bundle.bin"), "--output must end in .bundle.bin")
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_bytes(result)
        args.output.with_suffix(".json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        if args.parts_dir:
            args.parts_dir.mkdir(parents=True, exist_ok=True)
            (args.parts_dir / "ramdisk.bin").write_bytes(ramdisk)
            (args.parts_dir / "fdt.dtb").write_bytes(result[FDT_OFF:FDT_OFF + int(manifest["fdt_size"], 16)])
            (args.parts_dir / "dummy-initramfs.gz").write_bytes(minimal_ramdisk())
        print(f"BUNDLE {args.output.resolve()}\nSIZE {len(result)}\nSHA256 {manifest['bundle_sha256']}\nRAMDISK {manifest['ramdisk_size']} {manifest['ramdisk_sha256']}\nDUMMY {manifest['dummy_ramdisk_size']}\nINITRD_END {manifest['initrd_end']}")
        return 0
    except (OSError, ValueError, KeyError, AssertionError, EOFError) as exc:
        print(f"ERROR {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
