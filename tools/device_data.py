"""Read original IDME/devinfo via Stock Fastboot -> RAM TWRP; no USB BROM.

MEID is deliberately NOT inferred here. A per-device build needs either a
qualified original MEID source or an explicit experimental dummy-ID choice.
"""
import hashlib
import json
import re
import struct
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
import build_crumpet_fastboot as fastboot
import lk_stage_loader as lk
from firmware_formats import idme_records, mtd_partition
from firmware_profile import asset

MAGIC = b"EMLKv3\0\0"
PROPERTY = "echomuse,lk-device-data"
ATAG_PROPERTY = "echomuse,preloader-atags"
FDT_CAP = 0xF000
PREFIX = 8 + 404 + 4
CAPTURE_SIZE = PREFIX + FDT_CAP
BUNDLE_SHA = "6deacccf4cac8534b97e4c3f320c8fb2c812d0d1e62efded4f4f8d22b1969a9f"
DUMMY_MEID = b"EchoMuseDummyID1"


def serialize_fdt(template, nodes):
    """Serialize parsed v17 nodes, preserving reservations and child order."""
    names, body = bytearray(), bytearray()
    offsets = {}
    def word(value):
        return struct.pack(">I", value)
    def emit(path):
        name = b"" if path == "/" else path.rsplit("/", 1)[1].encode("ascii")
        body.extend(word(1) + (name + b"\0").ljust((len(name) + 4) & ~3, b"\0"))
        for key, value in nodes[path].items():
            if key not in offsets:
                offsets[key] = len(names)
                names.extend(key.encode("ascii") + b"\0")
            body.extend(struct.pack(">III", 3, len(value), offsets[key]))
            body.extend(value + bytes((-len(value)) % 4))
        for child in nodes:
            if child != "/" and (child.rsplit("/", 1)[0] or "/") == path:
                emit(child)
        body.extend(word(2))
    emit("/")
    body.extend(word(9))
    header = bytearray(template[:fastboot.be32(template, 8)])
    struct.pack_into(">I", header, 4, len(header) + len(body) + len(names))
    struct.pack_into(">I", header, 12, len(header) + len(body))
    struct.pack_into(">II", header, 32, len(names), len(body))
    result = bytes(header + body + names)
    if fastboot.fdt_properties(result) != nodes:
        raise ValueError("FDT serialization changed properties")
    return result


def capture_code(destination, devinfo_destination):
    source = f"""push {{r0-r12,lr}}
movw r4, #{destination & 0xffff}
movt r4, #{destination >> 16}
movw r10, #{devinfo_destination & 0xffff}
movt r10, #{devinfo_destination >> 16}
mov r8,r2
ldr r0,[r8]
movw r1,#0x0dd0
movt r1,#0xedfe
cmp r0,r1
bne capture_done
ldr r6,[r8,#12]
rev r6,r6
ldr r7,[r8,#32]
rev r7,r7
adds r6,r6,r7
movw r7,#{FDT_CAP}
cmp r6,r7
bhi capture_done
cmp r6,#40
blo capture_done
mov r0,r4
addw r0,r0,#{PREFIX}
mov r1,r8
mov r2,r6
movw r3,#0x0858
movt r3,#0x41e2
blx r3
str r6,[r4,#{PREFIX - 4}]
rev r0,r6
str r0,[r4,#{PREFIX + 4}]
add r7,r4,#8
movs r0,#100
str r0,[r7],#4
str r0,[r10],#4
movs r5,#0
capture_word:
mov r0,r5
movw r3,#0x363d
movt r3,#0x41e2
blx r3
str r0,[r7],#4
str r0,[r10],#4
adds r5,#1
cmp r5,#100
blo capture_word
movw r0,#0x4d45
movt r0,#0x4b4c
str r0,[r4]
movw r0,#0x3376
str r0,[r4,#4]
capture_done:
mov r0,r4
bic r0,r0,#31
movw r1,#{CAPTURE_SIZE}
add r1,r1,r4
capture_clean:
mcr p15,0,r0,c7,c10,1
adds r0,#32
cmp r0,r1
blo capture_clean
subw r0,r10,#404
bic r0,r0,#31
capture_devinfo_clean:
mcr p15,0,r0,c7,c10,1
adds r0,#32
cmp r0,r10
blo capture_devinfo_clean
dsb sy
pop {{r0-r12,pc}}
"""
    return lk._assemble(source, lk.STAGE_ADDR + 0xC00)


def capture_preloader_args(destination):
    # Read the incoming argument pointer saved by original LK's entry code,
    # before the recovery loader can reuse that memory. No SRAM is modified.
    return lk._assemble(f"""
push {{r0-r12,lr}}
ldr r1,=0x41e00020
ldr r1,[r1]
ldr r0,=0x40000000
cmp r1,r0
blo done
ldr r0,=0x4f7bfc00
cmp r1,r0
bhi done
ldr r0,={destination:#x}
movw r2,#1024
ldr r3,=0x41e20858
blx r3
ldr r0,={destination & ~31:#x}
ldr r1,={destination + 1024:#x}
clean:
mcr p15,0,r0,c7,c10,1
adds r0,#32
cmp r0,r1
blo clean
dsb sy
done:
pop {{r0-r12,lr}}
movw r4,#0x5ea4
b.w {lk.STAGE_ADDR + 6:#x}
""", lk.STAGE_ADDR + 0xD00)


def build_capture_bundle(baseline=None, manifest=None):
    if baseline is None or manifest is None:
        raise ValueError("a freshly built Fastboot bundle and manifest are required")
    if (len(baseline) != manifest["bundle_size"]
            or hashlib.sha256(baseline).hexdigest() != manifest["bundle_sha256"]
            or manifest["ramdisk_offset"] != "0x990000"):
        raise ValueError("changed or already relocated baseline bundle")
    manifest = dict(manifest)
    if manifest["kernel_sha256"] != "0ea78c5250d4c431b3856a8079d47cf0de0ff2e267b4d53536889715f26a540d":
        raise ValueError("unsupported recovery kernel geometry")
    start, length = fastboot.FDT_OFF, int(manifest["fdt_size"], 16)
    template = baseline[start:start + length]
    nodes = fastboot.fdt_properties(template)
    nodes["/chosen"][PROPERTY] = b"\xa5" * CAPTURE_SIZE
    nodes["/chosen"][ATAG_PROPERTY] = b"\xa7" * 1024
    nodes["/chosen"]["atag,devinfo"] = b"\xa6" * 404
    ramdisk_size = int(manifest["ramdisk_size"], 16)
    nodes["/chosen"]["bootargs"] = fastboot.boot_cmdline("b", ramdisk_size).replace(b"0x45990000", b"0x459a0000")
    fdt = serialize_fdt(template, nodes)
    if len(fdt) > 0x20000:
        raise ValueError("device-data FDT overlaps relocated recovery ramdisk")
    destination = 0x45980000 + fdt.index(b"\xa5" * CAPTURE_SIZE)
    args_destination = 0x45980000 + fdt.index(b"\xa7" * 1024)
    devinfo_destination = 0x45980000 + fdt.index(b"\xa6" * 404)
    sections = lk._split_sections(Path(lk.__file__).with_suffix(".S").read_text())
    sections["kernel_hook"] = sections["kernel_hook"].replace(
        "kernel_entry_hook:", "kernel_entry_hook:\n    push {r3,lr}\n    movw r3,#0x1c01\n    movt r3,#0x4597\n    blx r3\n    pop {r3,lr}")
    stage = bytearray(lk.assemble_loader_stage())
    hook = lk._assemble(sections["kernel_hook"], 0x41E45200)
    if len(hook) > 0x1E0:
        raise ValueError("device-data hook overlaps LK workspace")
    stage[0x720:0x900] = hook.ljust(0x1E0, b"\0")
    code = capture_code(destination, devinfo_destination)
    if len(code) > 0x100:
        raise ValueError("device-data helper exceeds stage")
    stage[0xC00:0xC00 + len(code)] = code
    args_code = capture_preloader_args(args_destination)
    if len(args_code) > 0x300 or stage[2:6] != bytes.fromhex("45f6a464"):
        raise ValueError("preloader argument hook exceeds qualified stage layout")
    stage[0xD00:0xD00 + len(args_code)] = args_code
    stage[2:6] = lk._assemble(f"b.w {lk.STAGE_ADDR + 0xD00:#x}", lk.STAGE_ADDR + 2)
    ramdisk = baseline[0x990000:0x990000 + ramdisk_size]
    result = bytearray((0x9A0000 + ramdisk_size + 0xFFF) & ~0xFFF)
    result[:0x980000] = baseline[:0x980000]
    result[0x971000:0x972000] = stage
    result[0x980000:0x980000 + len(fdt)] = fdt
    result[0x9A0000:0x9A0000 + len(ramdisk)] = ramdisk
    cmdline = nodes["/chosen"]["bootargs"]
    result[64:576] = cmdline.ljust(512, b"\0")
    result[576:608] = fastboot.android_id(result) + bytes(12)
    manifest.update(capture_address=hex(destination), helper_bytes=len(code),
                    preloader_atags_address=hex(args_destination), preloader_atags_bytes=1024,
                    fdt_size=hex(len(fdt)), fdt_sha256=hashlib.sha256(fdt).hexdigest(),
                    ramdisk_offset="0x9a0000", ramdisk_address="0x459a0000",
                    initrd_address="0x459a0000", initrd_end=hex(0x459A0000 + len(ramdisk)),
                    cmdline=cmdline.rstrip(b"\0").decode(), stage_source="tools/device_data.py + lk_stage_loader.S",
                    stage_sha256=hashlib.sha256(stage).hexdigest(), bundle_size=len(result),
                    android_id=result[576:596].hex(), hardware_tested=False,
                    baseline_sha256=hashlib.sha256(baseline).hexdigest(), efuse_writes=0, nand_writes=0,
                    crypto_mmio_writes=0, original_meid_capture=False)
    return bytes(result), manifest


def parse_capture(data):
    if len(data) != CAPTURE_SIZE or data[:8] != MAGIC:
        raise ValueError("missing original-LK device capture; use the device-data TWRP bundle")
    devinfo = data[8:412]
    size = struct.unpack_from("<I", data, 412)[0]
    if not 40 <= size <= FDT_CAP or struct.unpack_from("<I", devinfo)[0] != 100:
        raise ValueError("invalid devinfo/FDT capture")
    fdt = data[PREFIX:PREFIX + size]
    if fastboot.fdt_properties(fdt).get("/chosen", {}).get("atag,devinfo") != devinfo:
        raise ValueError("LK FDT devinfo differs from original hardware reader")
    return devinfo, fdt


def validate_device_data(capture, idme):
    devinfo, fdt = parse_capture(capture)
    if len(idme) != 0x200000:
        raise ValueError("unexpected IDME partition length")
    records = idme_records(idme[0x180000:0x1C0000])
    serial = records["serial"]["value"].rstrip(b"\0").decode("ascii")
    if len(serial) != 16 or not serial.isalnum():
        raise ValueError("invalid factory serial")
    # The Stock Fastboot DT fixups supply identity, not our firmware DTB.
    # Board compatibility is checked against the original LK selector table.
    lk_image = asset("stock_lk.img")
    selectors = dict(re.findall(rb"([0-9a-f]{16})\0(amzn,crumpet[^\0]*)\0", lk_image))
    if selectors.get(records["board_id"]["value"]) != b"amzn,crumpet-pvt":
        raise ValueError("unsupported factory board selector")
    words = struct.unpack("<101I", devinfo)
    if (words[7], words[54], words[55]) != (0x46, 1, 0):
        raise ValueError("unsupported security/rollback profile; no writes allowed")
    matched = 0
    for path, props in fastboot.fdt_properties(fdt).items():
        if path.startswith("/idme/") and "value" in props:
            record = records[path.split("/")[2]]
            if (props["value"].rstrip(b"\0") != record["value"].rstrip(b"\0")
                    or props["permission"] != struct.pack(">I", record["permissions"])):
                raise ValueError("Stock-LK/MTD IDME disagreement")
            matched += 1
    if matched != sum(bool(r["exported"]) for r in records.values()):
        raise ValueError("incomplete original LK IDME export")
    return dict(meid=None, devinfo=devinfo, reference_fdt=fdt, idme=idme, serial=serial)


def load_device_data(folder):
    folder = Path(folder)
    data = validate_device_data((folder / "lk-capture.bin").read_bytes(),
                                (folder / "idme.bin").read_bytes())
    path = folder / "preloader-atags.bin"
    if path.exists():
        data["preloader_atags"] = path.read_bytes()
    return data


def collect(folder, adb, serial):
    def read(*args):
        return subprocess.run([adb, "-s", serial, *args], check=True,
                              capture_output=True, timeout=60).stdout
    if (read("get-state").strip() != b"recovery"
            or read("shell", "getprop ro.product.device").strip() != b"crumpet"
            or read("shell", "getprop ro.twrp.version").strip() != b"3.7.0_9-0"):
        raise ValueError("expected qualified RAM TWRP")
    table = read("shell", "cat /proc/mtd").decode()
    # LK compatibility is gated before running its RAM capture hook. Once
    # TWRP runs, validate the captured data rather than either NAND LK copy.
    idme_device = mtd_partition(table, "idme_nand", 0x200000)
    nodes = fastboot.fdt_properties(read("exec-out", "cat", "/sys/firmware/fdt"))
    if b"amzn,crumpet-pvt\0" not in nodes["/"]["compatible"]:
        raise ValueError("wrong recovery board tree")
    capture = nodes["/chosen"][PROPERTY]
    idme = read("exec-out", "cat", idme_device)
    if idme != read("exec-out", "cat", idme_device):
        raise ValueError("unstable IDME reads")
    result = validate_device_data(capture, idme)
    if nodes["/chosen"]["atag,devinfo"] != result["devinfo"]:
        raise ValueError("recovery devinfo export disagrees with capture")
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=False)
    for name, value in (("lk-capture.bin", capture), ("idme.bin", idme),
                        ("devinfo.bin", result["devinfo"]), ("stock-lk.dtb", result["reference_fdt"])):
        (folder / name).write_bytes(value)
    if ATAG_PROPERTY in nodes["/chosen"]:
        from lk_bootargs import prepare
        atags = nodes["/chosen"][ATAG_PROPERTY]
        prepare(atags)                   # validate capture before saving it
        (folder / "preloader-atags.bin").write_bytes(atags)
    report = dict(serial=result["serial"], source="original LK immediately before RAM TWRP",
                  raw_idme_read_twice=True, all_original_idme_values_cross_checked=True,
                  devinfo_hardware_reader_cross_checked=True, efuse_writes=0, nand_writes=0,
                  meid_source_qualified=False, files={p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                         for p in folder.iterdir() if p.is_file()})
    (folder / "device.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return report


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=Path)
    parser.add_argument("--collect", type=Path)
    parser.add_argument("--adb", default="adb")
    parser.add_argument("--serial", default="0123456789ABCDEF")
    args = parser.parse_args()
    if args.collect:
        print(json.dumps(collect(args.collect, args.adb, args.serial), indent=2))
    elif args.bundle:
        path = args.bundle
        image, report = build_capture_bundle(path.read_bytes(), json.loads(path.with_suffix(".json").read_text()))
        report["bundle_sha256"] = hashlib.sha256(image).hexdigest()
        report["sources"] = {"recovery": "VM-built recovery.img"}
        path.write_bytes(image)
        path.with_suffix(".json").write_text(json.dumps(report, indent=2) + "\n")
    else:
        parser.error("choose --bundle or --collect")
