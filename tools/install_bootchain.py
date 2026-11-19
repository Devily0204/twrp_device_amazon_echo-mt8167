"""Separate host installer for the qualified Crumpet PVT bootchain.

Run after the supplied Fastboot bundle has started RAM TWRP. Default: backup,
validate and build a reviewable plan only. --execute --yes enables NAND writes.
The original MEID read path is unqualified; --dummy-meid is an explicit test.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shlex
import struct
import subprocess
import zlib
from pathlib import Path

BLOCK = 0x40000
ROOT = Path(__file__).resolve().parents[1]
BACKEND = "/sbin/crumpet-bootchain"


def sha(data):
    return hashlib.sha256(data).hexdigest()


def validate_boot_layout(image, extra=None):
    """Validate the GPT's integrity and only the ranges actually used."""
    from firmware_profile import REQUIRED_PARTITIONS
    if len(image) != BLOCK:
        raise ValueError("wrong Boot0 size")
    header_size = struct.unpack_from("<I", image, 0x300c)[0]
    if not 92 <= header_size <= 4096:
        raise ValueError("invalid GPT header size")
    gpt = bytearray(image[0x3000:0x3000 + header_size])
    crc = struct.unpack_from("<I", gpt, 16)[0]
    struct.pack_into("<I", gpt, 16, 0)
    entries_lba, count, stride, entries_crc = struct.unpack_from("<QIII", gpt, 72)
    start = 0x2000 + entries_lba * 4096
    end = start + count * stride
    if (gpt[:8] != b"EFI PART" or struct.unpack_from("<I", gpt, 8)[0] != 0x10000
            or zlib.crc32(gpt) != crc or stride != 128 or count == 0
            or not 0x4000 <= start < end <= 0x8000):
        raise ValueError("invalid original GPT structure/CRC")
    entries = image[start:end]
    if zlib.crc32(entries) != entries_crc:
        raise ValueError("invalid original GPT CRC")
    ranges = []
    for offset in range(0, len(entries), stride):
        record = entries[offset:offset + stride]
        if record[:16] == bytes(16):
            continue
        first, last = struct.unpack_from("<QQ", record, 32)
        if first > last:
            raise ValueError("invalid GPT partition bounds")
        name = record[56:128].decode("utf-16le", errors="replace").split("\0", 1)[0]
        ranges.append((name, first * 4096, (last + 1) * 4096))
    for name, (address, size) in (REQUIRED_PARTITIONS | (extra or {})).items():
        if [(first, last) for label, first, last in ranges if label == name] != [(address, address + size)]:
            raise ValueError("unexpected GPT range: " + name)
        if any(label != name and first < address + size and last > address
               for label, first, last in ranges):
            raise ValueError("GPT partition overlaps required range: " + name)


def device_preloader(current, tee1):
    from firmware_profile import asset, TEE1_BLOCKS
    stock, donor = asset("stock_boot0.bin"), asset("preloader_donor.img")
    validate_boot_layout(current)
    stock_tee1 = asset("stock_tee1.bin")
    if (current[:0x2000] != stock[:0x2000] or current[0x8000:] != stock[0x8000:]
            or len(tee1) != len(stock_tee1)
            or any(tee1[n * BLOCK:(n + 1) * BLOCK] != stock_tee1[n * BLOCK:(n + 1) * BLOCK]
                   for n in TEE1_BLOCKS)):
        raise ValueError("installation requires the qualified stock preloader and TEE1")
    # Retain this unit's BRLYT/GPT and all bytes outside the signed GFH region.
    end = 0x8000 + struct.unpack_from("<I", donor, 0x8020)[0]
    if not 0x8300 < end <= len(donor) <= BLOCK:
        raise ValueError("invalid donor GFH length")
    return current[:0x8000] + donor[0x8000:end] + current[end:]


def apply_regions(plan, write, read, event, verify_final=lambda: None):
    """Restore every attempted region, including a partially failed write."""
    attempted = []
    try:
        for item in plan:
            attempted.append(item)
            event("write", region=item["name"])
            write(item, "new")
            if read(item) != item["new"]:
                raise RuntimeError("readback mismatch: " + item["name"])
            event("verified", region=item["name"])
        verify_final()
    except BaseException:
        errors = []
        for item in reversed(attempted):
            try:
                write(item, "old")
                if read(item) != item["old"]:
                    raise RuntimeError("rollback readback mismatch")
                event("rollback-verified", region=item["name"])
            except BaseException as error:
                errors.append(item["name"] + ": " + str(error))
        if errors:
            raise RuntimeError("ROLLBACK FAILED; keep TWRP powered: " + "; ".join(errors))
        raise


def install(args):
    from device_data import collect, load_device_data, DUMMY_MEID
    from firmware_profile import REQUIRED_PARTITIONS
    from firmware_formats import mtd_partition
    from build_bootchain import write_candidate

    work = args.work.resolve()
    work.mkdir(parents=True, exist_ok=False)
    log = work / "events.jsonl"

    def event(kind, **values):
        line = json.dumps(dict(event=kind, **values))
        with log.open("a", encoding="utf-8") as stream:
            stream.write(line + "\n")
        print(line, flush=True)

    def adb(*parts):
        return subprocess.run([args.adb, "-s", args.serial, *map(str, parts)],
                              check=True, capture_output=True, timeout=180).stdout

    def shell(*parts):
        return adb("shell", shlex.join(map(str, parts)))

    metadata = json.loads((ROOT / "recovery-tools.json").read_text())
    if sha(adb("exec-out", "cat", BACKEND)) != metadata["crumpet-bootchain_sha256"]:
        raise ValueError("RAM TWRP bootchain helper differs from this VM bundle")
    collect(work / "device-data", args.adb, args.serial)
    data = load_device_data(work / "device-data")
    data.update(meid=DUMMY_MEID, meid_source="explicit experimental dummy EchoMuseDummyID1")
    table = adb("shell", "cat /proc/mtd").decode()
    devices = {name: mtd_partition(table, name, size)
               for name, (_, size) in REQUIRED_PARTITIONS.items()}
    backups = {}
    for name in ("brhgptpl_0", "tee1"):
        device, size = devices[name], REQUIRED_PARTITIONS[name][1]
        info = shell(BACKEND, "info", device).decode()
        if not all(re.search(r"\b" + key + "=" + str(value) + r"\b", info)
                   for key, value in (("size", size), ("erasesize", BLOCK), ("writesize", 4096))):
            raise ValueError("unexpected NAND geometry")
        image = adb("exec-out", "cat", device)
        if len(image) != size:
            raise ValueError("short NAND backup")
        (work / f"original-{name}.bin").write_bytes(image)
        backups[name] = image
    preloader = device_preloader(backups["brhgptpl_0"], backups["tee1"])
    if getattr(args, "button_fastboot", False):
        from lk_bootargs import prepare
        from firmware_profile import asset
        if "preloader_atags" not in data:
            raise ValueError("original ATAG capture missing; start TWRP with the updated Fastboot bundle")
        fastboot_args = prepare(data["preloader_atags"])
        validate_boot_layout(backups["brhgptpl_0"], {"lk_a": (0x100000, 0x180000)})
        lk = adb("exec-out", "cat", mtd_partition(table, "lk_a", 0x180000))
        reference = asset("stock_lk.img")
        if (len(lk) != 0x180000 or struct.unpack_from("<2I", lk) != (0x58881688, 233532)
                or sha(lk[512:512 + 233532]) != sha(reference[512:512 + 233532])):
            raise ValueError("button Fastboot requires the qualified original LK")
        (work / "original-lk_a.bin").write_bytes(lk)
        manifest = write_candidate(work / "firmware", data, tee1=backups["tee1"], fastboot_args=fastboot_args)
    else:
        manifest = write_candidate(work / "firmware", data, tee1=backups["tee1"])
    new_tee1 = (work / "firmware/tee1_firmware_candidate.bin").read_bytes()
    plan = []
    for name, block in [("tee1", n) for n in (11, 4, 3, 2, 1, 0)] + [("brhgptpl_0", 0)]:
        offset = block * BLOCK
        new = preloader if name == "brhgptpl_0" else new_tee1[offset:offset + BLOCK]
        item = dict(name=f"{name}-block{block}", device=devices[name],
                    offset=offset, new=new, old=backups[name][offset:offset + BLOCK])
        if len(new) != BLOCK:
            raise ValueError("wrong candidate block length")
        # Check every future erase block before the first write, not mid-install.
        shell(BACKEND, "check", item["device"], offset, BLOCK)
        for kind in ("new", "old"):
            (work / f'{item["name"]}-{kind}.bin').write_bytes(item[kind])
        plan.append(item)
    report = dict(device_serial=data["serial"], idme_sha256=sha(data["idme"]),
                  experimental_dummy_meid=True, firmware=manifest,
                  regions=[{k: v for k, v in item.items() if k not in ("new", "old")} |
                           dict(new_sha256=sha(item["new"]), old_sha256=sha(item["old"])) for item in plan],
                  ubi_writes=0, efuse_writes=0, boot_image_writes=0)
    (work / "install-plan.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    event("plan-ready", path=str(work / "install-plan.json"), execute=args.execute)
    if not args.execute:
        return
    # Bind the freshly generated plan to the same physical unit and exact source
    # state immediately before erasing; reconnecting another unit must fail.
    if adb("exec-out", "cat", devices["idme_nand"]) != data["idme"]:
        raise ValueError("device identity changed")
    for name, original in backups.items():
        if adb("exec-out", "cat", devices[name]) != original:
            raise ValueError("source NAND changed while preparing plan")

    def write(item, kind):
        if adb("exec-out", "cat", devices["idme_nand"]) != data["idme"]:
            raise ValueError("physical device changed; refusing write/rollback on another unit")
        path = work / f'{item["name"]}-{kind}.bin'
        if path.read_bytes() != item[kind]:
            raise ValueError("local candidate/backup changed")
        remote = "/tmp/crumpet-bootchain-install.bin"
        adb("push", path, remote)
        if sha(adb("exec-out", "cat", remote)) != sha(item[kind]):
            raise ValueError("ADB upload changed")
        shell(BACKEND, "write", item["device"], item["offset"], remote)

    def read(item):
        remote = "/tmp/crumpet-bootchain-readback.bin"
        shell(BACKEND, "read", item["device"], item["offset"], BLOCK, remote)
        return adb("exec-out", "cat", remote)

    expected = bytearray(backups["tee1"])
    for item in plan:
        if item["device"] == devices["tee1"]:
            expected[item["offset"]:item["offset"] + BLOCK] = item["new"]
    def verify_final():
        if (adb("exec-out", "cat", devices["brhgptpl_0"]) != preloader
                or adb("exec-out", "cat", devices["tee1"]) != expected):
            raise RuntimeError("full partition readback mismatch")
    apply_regions(plan, write, read, event, verify_final)
    event("install-complete", reboot=False)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--adb", default="adb")
    parser.add_argument("--serial", default="0123456789ABCDEF", help="ADB transport serial")
    parser.add_argument("--work", required=True, type=Path, help="new private backup/output directory")
    parser.add_argument("--dummy-meid", action="store_true", help="explicit experimental software dummy ID")
    parser.add_argument("--no-button-fastboot", dest="button_fastboot", action="store_false", default=True,
                        help="omit the action-button LK path (compatibility with old TWRP captures)")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--yes", action="store_true")
    args = parser.parse_args()
    if not args.dummy_meid:
        parser.error("original MEID capture is unqualified; this experimental installer requires --dummy-meid")
    if args.execute and not args.yes:
        parser.error("--execute requires --yes")
    install(args)


if __name__ == "__main__":
    main()
