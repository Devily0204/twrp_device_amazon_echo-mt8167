"""Offline storage/profile checks. No device access or third-party packages."""

import argparse
import hashlib
import os
from pathlib import Path
import re
import shlex
import shutil
import struct
import subprocess
import tempfile
import unittest
import zlib
import sys
import io

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import prepare_crumpet_ota as ota
import inspect_crumpet_bcb as bcb
import inspect_recovery

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "crumpet/recovery/root/sbin/crumpet-storage.sh"
MTD = (REPO / "tests/data/crumpet-proc-mtd.txt").read_text()
BACKUP_ROOT = None
ZIG = None
BASH = shutil.which("bash") or (
    "C:/Program Files/Git/bin/bash.exe" if os.name == "nt" else None
)


def mtd_rows(text):
    return re.findall(r'^mtd(\d+): ([0-9a-f]+) ([0-9a-f]+) "([^"]+)"$', text, re.M)


class StorageFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        for name in ("proc", "sbin", "dev/block", "sys/class/ubi"):
            (self.root / name).mkdir(parents=True, exist_ok=True)
        (self.root / "proc/mtd").write_text(MTD, newline="\n")
        (self.root / "sbin/getprop").write_text('#!/bin/sh\nprintf "%s\\n" "$TEST_SLOT"\n', newline="\n")
        (self.root / "sbin/getprop").chmod(0o755)
        (self.root / "sbin/crumpet-ubi").write_text('''#!/bin/sh
echo "$*" >> "$CRUMPET_STORAGE_ROOT/tmp/ubi-commands"
if [ "$1" = attach ] && [ "${TEST_FAIL_ATTACH:-0}" = 1 ]; then exit 1; fi
''', newline="\n")
        (self.root / "sbin/crumpet-ubi").chmod(0o755)

    def volume(self, ubi, number, name, parent=17, ro="1"):
        base = self.root / "sys/class/ubi"
        (base / f"ubi{ubi}").mkdir(exist_ok=True)
        (base / f"ubi{ubi}/mtd_num").write_text(str(parent))
        volume = base / f"ubi{ubi}_{number}"
        volume.mkdir()
        (volume / "name").write_text(name)
        (volume / "type").write_text("dynamic")
        (self.root / f"dev/ubi{ubi}_{number}").write_text(name)
        block = self.root / f"sys/class/block/ubiblock{ubi}_{number}"
        block.mkdir(parents=True)
        (block / "ro").write_text(ro)
        # Regular files stand in for kernel block devices only in these tests.
        (self.root / f"dev/block/ubiblock{ubi}_{number}").write_text(name)

    def run_helper(self, slot="_b", mock_blocks=True, initialize=False):
        root = self.root.as_posix()
        if os.name == "nt":
            root = "/" + root[0].lower() + root[2:]
        prefix = ""
        if mock_blocks:
            prefix = '''function [() {
                if [[ $# == 3 && ( ( $1 == -b && $2 == "$CRUMPET_STORAGE_ROOT"/dev/block/* ) || ( $1 == -c && $2 == "$CRUMPET_STORAGE_ROOT"/dev/* ) ) ]]; then
                    builtin test -f "$2"
                else
                    builtin [ "$@"
                fi
            }
            '''
        result = subprocess.run(
            [BASH, "-c", prefix + 'script=$1; shift; . "$script"', "check", SCRIPT.as_posix(), *(["--init"] if initialize else [])],
            env={**os.environ, "CRUMPET_STORAGE_ROOT": root, "TEST_SLOT": slot},
            capture_output=True, text=True,
        )
        return result, (self.root / "tmp/crumpet-storage.log").read_text()

    def selected_system(self):
        # Using the same shell also handles MSYS-emulated symlinks on Windows.
        return subprocess.run(
            [BASH, "-c", 'cat "$1"', "check", (self.root / "dev/block/current-system").as_posix()],
            capture_output=True, text=True,
        )

class StorageChecks(StorageFixture):
    def test_slot_b_and_nonstandard_ubi_device_number(self):
        self.volume(7, 2, "system_a")
        self.volume(7, 5, "system_b")
        result, log = self.run_helper()
        self.assertEqual(result.returncode, 0, log + result.stderr)
        self.assertEqual(self.selected_system().stdout, "system_b")

    def test_unknown_slot_does_not_default_to_a(self):
        self.volume(7, 2, "system_a")
        _, log = self.run_helper("")
        self.assertIn("refusing to default", log)
        self.assertNotEqual(self.selected_system().returncode, 0)

    def test_matching_volume_on_wrong_mtd_is_rejected(self):
        self.volume(7, 2, "system_b", parent=16)
        self.run_helper()
        self.assertNotEqual(self.selected_system().returncode, 0)

    def test_legacy_ubiblock_ro_flag_is_not_the_filesystem_type(self):
        self.volume(7, 2, "system_b", ro="0")
        self.run_helper()
        self.assertEqual(self.selected_system().stdout, "system_b")

    def test_userdata_volume_is_not_exposed_as_a_system(self):
        self.volume(7, 2, "userdata")
        self.run_helper()
        self.assertNotEqual(self.selected_system().returncode, 0)

    def test_regular_file_is_not_a_real_block_device(self):
        self.volume(7, 2, "system_b")
        self.run_helper(mock_blocks=False)
        self.assertNotEqual(self.selected_system().returncode, 0)

    def test_rerun_clears_stale_slot_and_ambiguous_mtd_fails(self):
        self.volume(7, 2, "system_b")
        self.run_helper()
        (self.root / "proc/mtd").write_text(MTD + 'mtd18: 10000000 00040000 "userdata"\n', newline="\n")
        result, log = self.run_helper()
        self.assertEqual(result.returncode, 1)
        self.assertIn("Duplicate userdata", log)
        self.assertNotEqual(self.selected_system().returncode, 0)

    def test_no_ubi_is_a_diagnostic_result_not_an_attach(self):
        result, log = self.run_helper()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("not exposed", log)
        self.assertNotEqual(self.selected_system().returncode, 0)

    def test_initialization_uses_attach_and_block_ioctls_without_formatting(self):
        self.volume(7, 0, "system_a")
        self.volume(7, 1, "system_b")
        self.volume(7, 2, "cache")
        self.volume(7, 3, "userdata")
        self.volume(9, 0, "persist", parent=16)
        result, log = self.run_helper(initialize=True)
        self.assertEqual(result.returncode, 0, log + result.stderr)
        commands = (self.root / "tmp/ubi-commands").read_text().splitlines()
        self.assertEqual(commands[:2], ["attach persist", "attach userdata"])
        self.assertIn("block /dev/ubi7_0", commands)
        self.assertIn("block /dev/ubi7_1", commands)
        self.assertFalse(any("format" in c or "erase" in c or "update" in c for c in commands))
        self.assertTrue((self.root / "dev/crumpet/ubi/userdata").exists())

    def test_attach_failure_does_not_continue(self):
        stub = self.root / "sbin/crumpet-ubi"
        stub.write_text('#!/bin/sh\nexit 1\n', newline="\n")
        result, _ = self.run_helper(initialize=True)
        self.assertEqual(result.returncode, 1)
        self.assertNotEqual(self.selected_system().returncode, 0)

    def test_unusable_inactive_system_does_not_hide_good_active_system(self):
        self.volume(7, 0, "system_a")
        self.volume(7, 1, "system_b")
        stub = self.root / "sbin/crumpet-ubi"
        stub.write_text('#!/bin/sh\nif [ "$1:$2" = "block:/dev/ubi7_0" ]; then exit 1; fi\n', newline="\n")
        result, log = self.run_helper(initialize=True)
        self.assertEqual(result.returncode, 0, log + result.stderr)
        self.assertEqual(self.selected_system().stdout, "system_b")


class FlashChecks(StorageFixture):
    def run_script(self, name, arguments, slot="_b"):
        root = self.root.as_posix()
        if os.name == "nt":
            root = "/" + root[0].lower() + root[2:]
        return subprocess.run([BASH, str(SCRIPT.parent / name), *map(str, arguments)],
                              env={**os.environ, "CRUMPET_STORAGE_ROOT": root, "TEST_SLOT": slot},
                              capture_output=True, text=True)

    def test_hash_and_active_slot_rejected_before_native_writer(self):
        image = self.root / "boot.img"
        image.write_bytes(b"ANDROID!" + bytes(2040))
        for target, checksum in (("boot_b", hashlib.sha256(image.read_bytes()).hexdigest()), ("boot_a", "0" * 64)):
            result = self.run_script("crumpet-flash.sh", [target, image, checksum])
            self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.root / "tmp/ubi-commands").exists())

    def test_plan_validates_both_images_and_writes_system_before_boot(self):
        plan = self.root / "plan"
        plan.mkdir()
        for name in ("system", "boot"):
            (plan / (name + ".img")).write_bytes(name.encode())
        (plan / "target-slot").write_text("a\n", newline="\n")
        (plan / "SHA256SUMS").write_text("".join(f'{hashlib.sha256((plan / (name + ".img")).read_bytes()).hexdigest()}  {name}.img\n' for name in ("system", "boot")), newline="\n")
        stub = self.root / "sbin/crumpet-flash.sh"
        stub.write_text('#!/bin/sh\necho "$1" >> "$CRUMPET_STORAGE_ROOT/actions"\n', newline="\n")
        result = self.run_script("crumpet-apply-plan.sh", [plan])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.root / "actions").read_text().splitlines(), ["system_a", "boot_a"])
        (self.root / "actions").unlink()
        (plan / "boot.img").write_bytes(b"changed")
        self.assertNotEqual(self.run_script("crumpet-apply-plan.sh", [plan]).returncode, 0)
        self.assertFalse((self.root / "actions").exists())

    def test_plan_activates_only_after_both_writes_succeed(self):
        plan = self.root / "plan"
        plan.mkdir()
        for name in ("system", "boot"):
            (plan / (name + ".img")).write_bytes(name.encode())
        (plan / "target-slot").write_text("a\n", newline="\n")
        (plan / "SHA256SUMS").write_text("".join(f'{hashlib.sha256((plan / (name + ".img")).read_bytes()).hexdigest()}  {name}.img\n' for name in ("system", "boot")), newline="\n")
        flash = self.root / "sbin/crumpet-flash.sh"
        flash.write_text('#!/bin/sh\necho "$1" >> "$CRUMPET_STORAGE_ROOT/actions"\n[ "$1" != "boot_a" ]\n', newline="\n")
        (self.root / "sbin/crumpet-bcb.sh").write_text('#!/bin/sh\necho activated >> "$CRUMPET_STORAGE_ROOT/actions"\n', newline="\n")
        result = self.run_script("crumpet-apply-plan.sh", [plan, "--activate"])
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((self.root / "actions").read_text().splitlines(), ["system_a", "boot_a"])
        (self.root / "actions").unlink()
        flash.write_text('#!/bin/sh\necho "$1" >> "$CRUMPET_STORAGE_ROOT/actions"\n', newline="\n")
        result = self.run_script("crumpet-apply-plan.sh", [plan, "--activate"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.root / "actions").read_text().splitlines(), ["system_a", "boot_a", "activated"])


class OtaRejectChecks(unittest.TestCase):
    def test_malformed_protobuf_and_incremental_payload_rejected(self):
        for data in (b"\x80", b"\0", b"\x0a\xff\xff\xff\x7f"):
            with self.assertRaises(ValueError):
                ota.fields(data)
        manifest = b"\x60\x01" # minor_version=1, an incremental payload.
        with self.assertRaisesRegex(ValueError, "Incremental"):
            ota.payload_manifest(io.BytesIO(b"CrAU" + struct.pack(">QQI", 2, len(manifest), 0) + manifest))

    def test_source_delta_operation_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaisesRegex(ValueError, "Unsupported delta"):
                ota.extract_partition(io.BytesIO(), {8: [b"\x08\x02"]}, 4096, bytes(32), 0, 4096, Path(folder) / "boot.img")


class ProfileChecks(unittest.TestCase):
    def test_init_import_configs_use_regular_product_copy_files(self):
        board = (REPO / "crumpet/BoardConfig.mk").read_text()
        for name in ("init.recovery.logd.rc", "init.recovery.nano.rc", "init.recovery.vold_decrypt.rc"):
            mapping = f"$(DEVICE_PATH)/recovery/root/{name}:recovery/root/{name}"
            self.assertIn(mapping, board)
            self.assertTrue((REPO / "crumpet/recovery/root" / name).stat().st_size)

    def test_init_import_closure_and_required_services(self):
        files = {
            'init.rc': b'import /init.recovery.logd.rc\nimport /init.recovery.vold_decrypt.rc\nimport /init.recovery.${ro.hardware}.rc\nservice ueventd /sbin/ueventd\nservice adbd /sbin/adbd\nservice recovery /sbin/recovery\n',
            'init.recovery.mt8167.rc': b'import /init.recovery.led-service.rc\n',
            'init.recovery.led-service.rc': b'# present\n',
            'sbin/ueventd': b'../init', 'sbin/adbd': b'adbd', 'sbin/recovery': b'recovery',
        }
        for name in ('init.recovery.logd.rc', 'init.recovery.vold_decrypt.rc'):
            with self.assertRaisesRegex(AssertionError, 'Missing init import: /' + name):
                inspect_recovery.check_init_imports(files)
            files[name] = (REPO / 'crumpet/recovery/root' / name).read_bytes()
        configs, services = inspect_recovery.check_init_imports(files)
        self.assertEqual(len(configs), 5)
        self.assertEqual(set(services), {'ueventd', 'adbd', 'recovery'})
        del files['init.recovery.led-service.rc']
        with self.assertRaisesRegex(AssertionError, 'Missing init import: /init.recovery.led-service.rc'):
            inspect_recovery.check_init_imports(files)
        files['init.recovery.led-service.rc'] = b'# present\n'
        del files['sbin/adbd']
        with self.assertRaisesRegex(AssertionError, 'Missing service executable: /sbin/adbd'):
            inspect_recovery.check_init_imports(files)

    def test_android_ramdisk_overlay_excludes_emmc_scripts(self):
        board = (REPO / "crumpet/BoardConfig.mk").read_text()
        selected = re.search(r'^TARGET_RECOVERY_DEVICE_DIRS := (.+)$', board, re.M).group(1)
        selected = selected.replace("$(COMMON_PATH)", ".").replace("$(DEVICE_PATH)", "crumpet")
        packed = {}
        for directory in selected.split():
            root = REPO / directory / "recovery/root"
            for path in root.rglob("*"):
                if path.is_file():
                    packed[path.relative_to(root).as_posix()] = path
        for name in ("bcbfix", "bcbtool", "e2fsck-data.sh", "slot-symlinks.sh", "resetprop"):
            self.assertNotIn("sbin/" + name, packed)
        for name in ("led-anim.sh", "led-common.sh", "led-status.sh", "crumpet-storage.sh"):
            self.assertIn("sbin/" + name, packed)
        self.assertNotIn("fstab.device", packed)
        init = packed["init.recovery.mt8167.rc"].read_text()
        self.assertNotRegex(init, r"mmcblk|e2fsck|bcbfix|slot-symlinks|\bwait\b")
        fstab = packed["etc/recovery.fstab"].read_text()
        entries = [shlex.split(line) for line in fstab.splitlines() if line and not line.startswith("#")]
        self.assertEqual([e[0] for e in entries], ["/system_root", "/system_a", "/system_b", "/system_a_image", "/system_b_image", "/boot_a", "/boot_b", "/data", "/cache", "/persist", "/usb-otg"])
        for entry in entries[:3]:
            self.assertEqual(entry[1], "squashfs")
            self.assertIn("canbewiped=0", entry[3])
            self.assertIn("fsflags=ro", entry[3])
        self.assertIn("AB_OTA_UPDATER := false", board)
        self.assertIn("TW_INCLUDE_BCBTOOL := false", board)
        self.assertRegex(board, r"(?m)^RECOVERY_SDCARD_ON_DATA :=\s*$")
        self.assertIn("filter cupcake donut,", (REPO / "Android.mk").read_text())
        filesystems = {entry[0]: entry[1] for entry in entries}
        for mount in ("/data", "/cache", "/persist"):
            self.assertEqual(filesystems[mount], "ubifs")
        self.assertNotIn("/misc", filesystems)
        self.assertIn("TW_CRUMPET_NAND := true", board)
        self.assertIn("crumpet-nand-support.version", board)

    def test_stock_header_addresses_and_kernel(self):
        board = (REPO / "crumpet/BoardConfig.mk").read_text()
        base = int(re.search(r"BOARD_KERNEL_BASE := (\S+)", board).group(1), 0)
        args = re.search(r"BOARD_MKBOOTIMG_ARGS := (.+)", board).group(1)
        args = shlex.split(args.replace("$(BOARD_KERNEL_BASE)", str(base)))
        values = dict(zip(args[::2], args[1::2]))
        for name, address in (("kernel", 0x40008000), ("ramdisk", 0x44000000),
                              ("second", 0x40F00000), ("tags", 0x4E000000)):
            self.assertEqual(base + int(values[f"--{name}_offset"], 0), address)
        self.assertIn("bootopt=64S3,32N2,32N2", board)
        self.assertIn("BOARD_FLASH_BLOCK_SIZE := 262144", board)
        for image in ("BOOT", "RECOVERY"):
            self.assertIn(f"BOARD_{image}IMAGE_PARTITION_SIZE := 15990784", board)
        kernel = REPO / "crumpet/prebuilts/zImage-dtb"
        data = kernel.read_bytes()
        self.assertEqual(hashlib.sha256(data).hexdigest(),
                         "0ea78c5250d4c431b3856a8079d47cf0de0ff2e267b4d53536889715f26a540d")
        image = zlib.decompress(data[data.index(b"\x1f\x8b\x08"):], 16 + zlib.MAX_WBITS)
        for marker in (b"ubi_attach_mtd_dev", b"ubiblock%d_%d", b"UBIFS", b"squashfs"):
            self.assertIn(marker, image)

    def test_shell_syntax(self):
        for root in (REPO / "recovery/root/sbin", REPO / "leds/recovery/root/sbin", REPO / "crumpet/recovery/root/sbin"):
            for path in root.iterdir():
                if path.read_bytes().startswith(b"#!/sbin/sh"):
                    result = subprocess.run([BASH, "-n", str(path)], capture_output=True, text=True)
                    self.assertEqual(result.returncode, 0, str(path) + result.stderr)

    def test_mtd_rmw_with_fault_injection(self):
        cc = [str(ZIG), "cc"] if ZIG else [shutil.which("cc") or shutil.which("clang") or ""]
        if not cc[0]:
            self.skipTest("C compiler unavailable; pass --zig PATH")
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / ("model.exe" if os.name == "nt" else "model")
            subprocess.run([*cc, "-O1", "-Wall", "-Wextra", "-Werror", "-o", str(output), str(REPO / "tests/test_mtd.c")], check=True, capture_output=True)
            result = subprocess.run([str(output), str(Path(folder) / "image.bin")], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("failure cases passed", result.stdout)

    def test_bcb_metadata_is_in_a_rolling_record_not_the_partition_prefix(self):
        if not BACKUP_ROOT:
            self.skipTest("pass --backup-root to verify original evidence")
        for directory in BACKUP_ROOT.glob("CleanDot*"):
            path = directory / "partitions/mtd9_misc.bin"
            if path.exists():
                with path.open("rb") as stream:
                    stream.seek(0x360)
                    self.assertEqual(stream.read(7), b"\xff" * 7)
                found = bcb.records(path)
                self.assertEqual(len(found), 1)
                self.assertGreater(found[0]["block"], 0)
                self.assertEqual(found[0]["metadata"], "00414242018f8e00")

    def test_original_full_ota_images_match_existing_stock_extraction(self):
        if not BACKUP_ROOT:
            self.skipTest("pass --backup-root to verify original OTA")
        archive = BACKUP_ROOT / "update-kindle-crumpet-NS65741_user_8149_0013222532484.zip"
        with archive.open("rb") as stream:
            checksum = ota.digest(stream).hex()
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "plan"
            plan = ota.prepare(archive, checksum, output, "a")
            for name in ("boot", "system"):
                original = BACKUP_ROOT / "EverythingCollected/Firmware/Original/NS65741-8149" / (name + ".img")
                with original.open("rb") as stream:
                    self.assertEqual(ota.digest(stream).hex(), plan["images"][name]["sha256"])
            with self.assertRaisesRegex(ValueError, "SHA256 mismatch"):
                ota.prepare(archive, "0" * 64, Path(folder) / "bad", "a")

    def test_backup_metadata_and_redundant_ubi_tables(self):
        if not BACKUP_ROOT:
            self.skipTest("pass --backup-root to verify original evidence")
        directories = [d for d in BACKUP_ROOT.glob("CleanDot*") if (d / "meta/proc_mtd.txt").exists()]
        self.assertGreaterEqual(len(directories), 2)
        expected = {"persist": [("persist", 20, 1)], "userdata": [
            ("system_a", 538, 1), ("system_b", 538, 1), ("cache", 32, 1), ("userdata", 638, 1)]}
        for directory in directories:
            self.assertEqual(mtd_rows((directory / "meta/proc_mtd.txt").read_text()), mtd_rows(MTD))
            for part in ("mtd13_boot_a.bin", "mtd15_boot_b.bin"):
                with (directory / "partitions" / part).open("rb") as stream:
                    header = stream.read(2048)
                self.assertEqual(header[:8], b"ANDROID!")
                values = struct.unpack_from("<10I", header, 8)
                self.assertEqual(tuple(values[i] for i in (1, 3, 5, 6, 7)),
                                 (0x40008000, 0x44000000, 0x40F00000, 0x4E000000, 2048))
                self.assertIn(b"bootopt=64S3,32N2,32N2", header[64:576])
            for part, name in (("mtd16_persist.bin", "persist"), ("mtd17_userdata.bin", "userdata")):
                path = directory / "partitions" / part
                tables = []
                with path.open("rb") as stream:
                    for offset in range(0, path.stat().st_size, 262144):
                        stream.seek(offset)
                        ec = stream.read(64)
                        if ec[:4] != b"UBI#":
                            continue
                        vid_offset, data_offset = struct.unpack_from(">II", ec, 16)
                        stream.seek(offset + vid_offset)
                        vid = stream.read(64)
                        if vid[:4] != b"UBI!" or struct.unpack_from(">I", vid, 8)[0] != 0x7FFFEFFF:
                            continue
                        for record in (ec, vid):
                            self.assertEqual(zlib.crc32(record[:60]) ^ 0xFFFFFFFF,
                                             struct.unpack_from(">I", record, 60)[0])
                        stream.seek(offset + data_offset)
                        records = stream.read(128 * 172)
                        volumes = []
                        for i in range(128):
                            record = records[i * 172:(i + 1) * 172]
                            pebs = struct.unpack_from(">I", record)[0]
                            if not pebs:
                                continue
                            self.assertEqual(zlib.crc32(record[:168]) ^ 0xFFFFFFFF,
                                             struct.unpack_from(">I", record, 168)[0])
                            length = struct.unpack_from(">H", record, 14)[0]
                            volumes.append((record[16:16 + length].decode(), pebs, record[12]))
                        tables.append(volumes)
                self.assertEqual(tables, [expected[name], expected[name]])


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backup-root", type=Path)
    parser.add_argument("--zig", type=Path, help="Optional Zig executable for the native NAND model test")
    args, rest = parser.parse_known_args()
    BACKUP_ROOT = args.backup_root
    ZIG = args.zig
    unittest.main(argv=[__file__, *rest])
