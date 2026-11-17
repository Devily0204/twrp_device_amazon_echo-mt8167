"""Build active-slot firmware from pinned generic assets and this unit's data.

The supplied OEM bridge retains its authenticated firmware handoff. Device
identity is supplied at installation, never taken from another unit's backup.
"""
from __future__ import annotations

import hashlib
import json
import re
import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = Path(__file__).resolve().parent
from firmware_formats import fdt_properties, idme_records
from firmware_profile import asset, PROFILE, ASSETS
import firmware_dtb as build_dtb
import slot_loader
from types import SimpleNamespace
build_slot_block4 = SimpleNamespace(BLOCK=ASSETS / "block4_bootstrap.bin", BASE_SHA256=PROFILE["hashes"]["block4_bootstrap.bin"], BOOTPATCH=ASSETS)
OUTPUT = Path("bootchain-output")


ENTRY = 0x46C00000


def sha(data):
    return hashlib.sha256(data).hexdigest()


def pinned(path, digest):
    data = path.read_bytes()
    if sha(data) != digest:
        raise ValueError(f"changed input: {path}")
    return data


def idme_text(record):
    # Factory slots have fixed storage capacities. The Linux/PAL interface
    # needs a C string, not all of that padding: PAL removes only ONE NUL.
    value = record["value"].rstrip(b"\0")
    if any(byte < 32 or byte >= 127 for byte in value):
        raise ValueError("this pinned IDME export requires ASCII text")
    return value + b"\0"


def firmware_dtb(boot, device_data=None):
    if device_data is None:
        raise ValueError("original device data is required")
    reference = asset("firmware_template.dtb")
    assert build_dtb.root_compatible(reference) == build_dtb.root_compatible(boot)
    idme = device_data["idme"]
    records = idme_records(idme[0x180000:0x1C0000])
    if device_data is not None:
        from device_data import serialize_fdt
        # Reuse the proven structural/secure-memory fixes, replacing EVERY
        # per-device node. No old serial, MAC, calibration or devinfo survives.
        nodes = {path: props for path, props in fdt_properties(reference).items()
                 if not path.startswith("/idme/")}
        nodes["/chosen"]["atag,devinfo"] = device_data["devinfo"]
        for name, record in records.items():
            if record["exported"]:
                nodes["/idme/" + name] = {"value": record["value"],
                    "permission": struct.pack(">I", record["permissions"])}
        reference = serialize_fdt(reference, nodes)
    nodes = fdt_properties(reference)
    original_cmdline = build_dtb.CMDLINE
    try:
        if device_data is not None:
            led = records["ledparams"]["value"].rstrip(b"\0").decode("ascii")
            if not re.fullmatch(r"ledcalibparams=[0-9A-Fa-fXx,]+", led):
                raise ValueError("unsupported factory LED calibration format")
            build_dtb.CMDLINE = re.sub(r"leds-is31fl3236\.ledcalibparams=\S+",
                                       "leds-is31fl3236." + led, build_dtb.CMDLINE)
        build_dtb.CMDLINE += " androidboot.serialno=" + records["serial"]["value"].decode("ascii")
        dtb = bytearray(build_dtb.rebuild(reference))
    finally:
        build_dtb.CMDLINE = original_cmdline
    _, total, off_struct, off_strings, off_rsv, *_ = struct.unpack_from(">10I", dtb)
    strings = dtb[off_strings:]
    p, path, changed, initrd_end, idme_end = off_struct, [], 0, None, None
    text_edits = []
    while p < off_strings:
        token = struct.unpack_from(">I", dtb, p)[0]
        if token == 1:
            end = dtb.index(0, p + 4)
            path.append(dtb[p + 4:end].decode())
            p = build_dtb.pad4(end + 1)
        elif token == 2:
            if path[-1] == "idme":
                idme_end = p
            path.pop()
            p += 4
        elif token == 3:
            length, nameoff = struct.unpack_from(">II", dtb, p + 4)
            name = strings[nameoff:strings.index(0, nameoff)]
            if len(path) >= 2 and path[-2] == "idme" and name == b"value":
                record = records[path[-1]]
                assert dtb[p + 12:p + 12 + length] == record["value"]
                value = idme_text(record)
                replacement = struct.pack(">III", 3, len(value), nameoff) + value.ljust(build_dtb.pad4(len(value)), b"\0")
                text_edits.append((p, p + build_dtb.pad4(12 + length), replacement))
            if path[-1].startswith("memory@") and name == b"reg":
                assert length == 32
                assert struct.unpack_from(">4Q", dtb, p + 12) == (0x40000000, 0xF7C0000, 0x4F800000, 0x800000)
                changed += 1
            if path[-1] == "chosen" and name in (b"linux,initrd-start", b"linux,initrd-end"):
                assert length == 4
                struct.pack_into(">I", dtb, p + 12, 0x44000000)
                if name == b"linux,initrd-end":
                    initrd_end = p + 12
            p += build_dtb.pad4(12 + length)
        elif token == 4:
            p += 4
        elif token == 9:
            break
        else:
            raise ValueError(f"invalid FDT token {token}")
    assert changed == 1 and initrd_end is not None and idme_end is not None
    initrd_end += sum(len(value) - (end - start) for start, end, value in text_edits if start < initrd_end)
    idme_end += sum(len(value) - (end - start) for start, end, value in text_edits if start < idme_end)
    text_delta = sum(len(value) - (end - start) for start, end, value in text_edits)
    for start, end, value in reversed(text_edits):
        dtb[start:end] = value
    total += text_delta
    off_strings += text_delta
    struct.pack_into(">I", dtb, 36, struct.unpack_from(">I", dtb, 36)[0] + text_delta)
    # Follow the factory export flag and permissions, but serialize text
    # rather than fixed-capacity raw NAND records, including the old 19 nodes.
    additions = bytearray()
    permission_name, value_name = strings.index(b"permission\0"), strings.index(b"value\0")
    for name, record in records.items():
        if not record["exported"]:
            continue
        existing_node = nodes.get("/idme/" + name)
        if existing_node is not None:
            assert existing_node["value"] == record["value"]
            assert existing_node["permission"] == struct.pack(">I", record["permissions"])
            continue
        name_bytes = name.encode("ascii") + b"\0"
        additions += struct.pack(">I", 1) + name_bytes.ljust(build_dtb.pad4(len(name_bytes)), b"\0")
        additions += struct.pack(">IIII", 3, 4, permission_name, record["permissions"])
        value = idme_text(record)
        additions += struct.pack(">III", 3, len(value), value_name)
        additions += value.ljust(build_dtb.pad4(len(value)), b"\0") + struct.pack(">I", 2)
    dtb[idme_end:idme_end] = additions
    if initrd_end >= idme_end:
        initrd_end += len(additions)
    total += len(additions)
    off_strings += len(additions)
    struct.pack_into(">I", dtb, 4, total)
    struct.pack_into(">I", dtb, 12, off_strings)
    struct.pack_into(">I", dtb, 36, struct.unpack_from(">I", dtb, 36)[0] + len(additions))
    expected_reservations = [(0x40000000, 0x8000), (0x40C0F000, 0x1000),
                             (0x42000000, 0x200000), (0x4F7C0000, 0x640000)]
    existing = b"".join(struct.pack(">QQ", *item) for item in expected_reservations) + bytes(16)
    assert dtb[off_rsv:off_struct] == existing
    # Same OEM context and secure reservations as the proven GPTLive bridge,
    # plus the relocated loader and external FDT. Fixed stock reserved nodes
    # (ATF, ram_console, pstore, minirdump) remain intact in the structure.
    reservations = [(ENTRY, 0x40000), (0x4E000000, 0x20000),
                    (0x4FE00000, 0x200000)]
    extra = b"".join(struct.pack(">QQ", *item) for item in reservations)
    struct.pack_into(">I", dtb, 4, total + len(extra))
    struct.pack_into(">I", dtb, 8, off_struct + len(extra))
    struct.pack_into(">I", dtb, 12, off_strings + len(extra))
    dtb[off_rsv:off_rsv] = extra
    assert len(dtb) == total + len(extra)
    assert b"mediatek,mtee\0" in dtb and b"psci\0" in dtb
    return bytes(dtb), initrd_end + len(extra)


def inputs(device_data=None):
    if device_data is None or device_data.get("meid") is None:
        raise ValueError("original MEID source remains unqualified; an experimental dummy requires explicit opt-in")
    bridge, oem = asset("firmware_bridge.bin"), asset("oem_code.bin")
    meid = device_data["meid"]
    dtb, initrd_end = firmware_dtb(asset("stock_pvt.dtb"), device_data)
    return dict(dtb=dtb, initrd_end_offset=initrd_end, bridge=bytes(bridge), oem=oem, meid=meid)


def build(device_data=None, fastboot_args=None):
    config = inputs(device_data)
    if fastboot_args is not None:
        config["fastboot_args"] = fastboot_args
    stage = slot_loader.build(firmware=config, uart=True)
    assert len(stage) == 0x40000
    package_end = slot_loader.FIRMWARE_OEM_OFFSET + len(config["oem"])
    if fastboot_args is not None:
        package_end = slot_loader.FASTBOOT_STUB_OFFSET + 20
    used = (package_end + 0xFFF) & ~0xFFF
    assert stage[package_end:] == b"\xff" * (0x40000 - package_end)
    block = bytearray(pinned(build_slot_block4.BLOCK, build_slot_block4.BASE_SHA256))
    changes = {0x10C38: (0x18000000, 0x10C0000),
               0x10C40: (0x43000000, ENTRY), 0x10C50: (0x43000000, ENTRY),
               0x10C98: (0x43000000, ENTRY)}
    for offset, (old, new) in changes.items():
        assert struct.unpack_from("<I", block, offset)[0] == old
        struct.pack_into("<I", block, offset, new)
    for offset in (0x10C3C, 0x10C54):
        assert struct.unpack_from("<I", block, offset)[0] == len(stage)
    block[0x11000:0x11020] = hashlib.sha256(stage).digest()
    # Phase markers in otherwise unused bootstrap space. Preserve entry r0
    # in r8 as before; r0/r1 are dead before its first zero/context operation.
    from keystone import Ks, KS_ARCH_ARM, KS_MODE_ARM
    assembler = Ks(KS_ARCH_ARM, KS_MODE_ARM)
    def arm(source, address):
        return bytes(assembler.asm(source, addr=address)[0])
    assert block[0x11100:0x11200] == bytes(0x100)
    assert struct.unpack_from("<I", block, 0x10808)[0] & 0xFFFFF000 == 0xE59F9000
    block[0x10808:0x1080C] = arm("b 0x200f00", 0x200608)
    stub = arm("ldr r0,=0x11005000; mov r1,#0x43; str r1,[r0]; "
               "ldr r9,=0x200d00; b 0x20060c", 0x200F00)
    assert len(stub) <= 0x40
    block[0x11100:0x11100 + len(stub)] = stub
    assert struct.unpack_from("<I", block, 0x10B48)[0] == 0xE59FF148
    block[0x10B48:0x10B4C] = arm("b 0x200f40", 0x200948)
    stub = arm(f"ldr r0,=0x11005000; mov r1,#0x56; str r1,[r0]; ldr pc,={ENTRY:#x}", 0x200F40)
    assert len(stub) <= 0x40
    block[0x11140:0x11140 + len(stub)] = stub
    read_literal = block.index(struct.pack("<I", slot_loader.reader.READ), 0x10800, 0x10D00)
    assert block.count(struct.pack("<I", slot_loader.reader.READ), 0x10800, 0x10D00) == 1
    struct.pack_into("<I", block, read_literal, 0x200F80)
    stub = arm("push {r4,lr}; push {r0,r1}; ldr r0,=0x11005000; mov r1,#0x52; str r1,[r0]; "
               f"pop {{r0,r1}}; ldr r12,={slot_loader.reader.READ:#x}; blx r12; "
               "push {r0,r1}; ldr r0,=0x11005000; mov r1,#0x44; str r1,[r0]; "
               "pop {r0,r1}; pop {r4,pc}", 0x200F80)
    assert len(stub) <= 0x80
    block[0x11180:0x11180 + len(stub)] = stub
    # No payload is placed in Block 4's old empty tail; Block 11 owns it all.
    assert block[0x20000:] == b"\xff" * 0x20000
    manifest = dict(format=1, hardware_tested=False, fireos_boot_verified=False,
                    firmware_initialization_requested=True, diagnostic_only=False,
                    slot_policy="positive priority after donor preloader consumes retry; preserve last zero-tries attempt",
                    tee2_physical="0x2240000", tee2_bytes=0x500000,
                    block4_sha256=sha(block), block11_sha256=sha(stage),
                    payload_destination=hex(ENTRY), payload_bytes=len(stage),
                    used_package_bytes=used, remaining_package_bytes=0x40000 - used,
                    dtb_bytes=len(config["dtb"]), dtb_package_end=hex(0x2400 + len(config["dtb"])),
                    bridge_sha256=sha(config["bridge"]), oem_code_sha256=sha(config["oem"]),
                    dtb_sha256=sha(config["dtb"]), dtb_destination="0x4e000000",
                    firmware_entry="0x40c005e0", firmware_failure="halt; UART F; no kernel fallback",
                    uart_markers=True, bootstrap_markers="C entered; R read; D read returned; V hash verified",
                    meid_source=device_data.get("meid_source", "explicit supplied ID"),
                    meid_sha256=sha(config["meid"]), required_device_serial=device_data["serial"],
                    per_device_identity_required=True, watchdog_disabled_before_boot_reads=True,
                    bounded_uart_tx_ready_wait=True,
                    runtime_boot_partition_hash_check=False, runtime_tee2_full_hash_check=False,
                    runtime_read_chunk_hashes=False, runtime_block11_hash_check=True,
                    installation_boot_and_tee2_hash_checks=False,
                    native_dtb_fixups="identity-free structure; original per-device IDME/devinfo",
                    kernel_modified=False, psci_preserved=True, mtee_preserved=True,
                    loader_nand_write_operations=0, ubi_writes=0, misc_writes=0,
                    retry_owner="signed donor preloader 0x201f20, called with flags 0x0f",
                    donor_preloader_sha256=slot_loader.reader.DONOR_SHA,
                    donor_slot_selector="0x201f20; native read/write and bitfield policy before Block 4",
                    post_preloader_last_attempt_supported=True,
                    preloader_misc_writes_for_unconfirmed_slots=True,
                    successful_slots_consume_retries=False, maximum_tries=7,
                    required_install_writes=["mtd0", "TEE1 blocks 0,1,2,3,4,11"],
                    source="tools/slot_loader.py")
    manifest["button_fastboot_requested"] = fastboot_args is not None
    manifest["source_sha256"] = {path.name: sha(path.read_bytes()) for path in
                                 (Path(__file__), SOURCE / "slot_loader.py", SOURCE / "firmware_dtb.py", SOURCE / "lk_bootargs.py")}
    manifest.update(dummy_meid_test=config["meid"] == b"EchoMuseDummyID1",
                    device_idme_sha256=sha(device_data["idme"]),
                    device_devinfo_sha256=sha(device_data["devinfo"]))
    return bytes(block), stage, config, manifest


def write_candidate(output, device_data=None, tee1=None, fastboot_args=None):
    block, stage, config, manifest = build(device_data, fastboot_args)
    output.mkdir(parents=True, exist_ok=True)
    for name, data in (("tee1_block4_firmware.bin", block), ("tee1_block11_firmware.bin", stage),
                       ("firmware_zimage_bridge.bin", config["bridge"]), ("slot_firmware.dtb", config["dtb"])):
        (output / name).write_bytes(data)
    # Preserve this unit's untouched TEE1 blocks, even if they differ from the
    # stock reference. Only the six explicitly selected blocks are replaced.
    tee1 = bytearray(asset("stock_tee1.bin") if tee1 is None else tee1)
    if len(tee1) != 0x500000:
        raise ValueError("wrong TEE1 candidate length")
    for index, digest in enumerate(("a2d51efe724fd297a65f21812919902f9de5d3e56afb908009e845f91ec9e504",
                                    *("8a39d2abd3999ab73c34db2476849cddf303ce389b35826850f9a700589b4a90",) * 3)):
        tee1[index * 0x40000:(index + 1) * 0x40000] = pinned(build_slot_block4.BOOTPATCH / f"tee1_block{index}.bin", digest)
    tee1[0x100000:0x140000] = block
    tee1[0x2C0000:0x300000] = stage
    (output / "tee1_firmware_candidate.bin").write_bytes(tee1)
    manifest["full_tee1_sha256"] = sha(tee1)
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return manifest


def main():
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--device-data", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--dummy-meid", action="store_true", help="explicit experimental EchoMuseDummyID1; requires --device-data")
    parser.add_argument("--fastboot-args", type=Path, help="experimental native LK arguments, mode 99; test unit only")
    args = parser.parse_args()
    device_data = None
    if args.dummy_meid and not args.device_data:
        parser.error("--dummy-meid requires --device-data")
    if args.device_data:
        if args.output is None or args.output.resolve() == OUTPUT.resolve():
            parser.error("per-device builds require a separate --output directory")
        from device_data import load_device_data, DUMMY_MEID
        device_data = load_device_data(args.device_data)
        if args.dummy_meid:
            device_data.update(meid=DUMMY_MEID, meid_source="explicit experimental dummy EchoMuseDummyID1")
    fastboot_args = args.fastboot_args.read_bytes() if args.fastboot_args else None
    if fastboot_args is None and device_data and "preloader_atags" in device_data:
        from lk_bootargs import prepare
        fastboot_args = prepare(device_data["preloader_atags"])
    manifest = write_candidate(args.output or OUTPUT, device_data, fastboot_args=fastboot_args)
    print(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    main()
