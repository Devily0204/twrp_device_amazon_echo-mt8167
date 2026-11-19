"""Offline checks for portable assets, device binding and rollback."""
import struct
import os
import hashlib
import sys
import unittest
import zlib
import tempfile
import subprocess
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from firmware_profile import asset, PROFILE
from firmware_formats import fdt_properties, mtd_partition
from install_bootchain import device_preloader, validate_boot_layout, apply_regions
from device_data import serialize_fdt, build_capture_bundle, PROPERTY, CAPTURE_SIZE, ATAG_PROPERTY


def update_gpt_crc(image):
    lba, count, stride = struct.unpack_from("<QII", image, 0x3048)
    start = 0x2000 + lba * 4096
    struct.pack_into("<I", image, 0x3058, zlib.crc32(image[start:start + count * stride]))
    struct.pack_into("<I", image, 0x3010, 0)
    size = struct.unpack_from("<I", image, 0x300c)[0]
    struct.pack_into("<I", image, 0x3010, zlib.crc32(image[0x3000:0x3000 + size]))


class BootchainChecks(unittest.TestCase):
    def test_fastboot_atags_preserve_inputs_and_exclude_secure_firmware(self):
        from lk_bootargs import prepare
        memory = bytearray(240)
        struct.pack_into("<4I", memory, 0, 60, 0x88610005, 1, 0xF9C0000)
        struct.pack_into("<I", memory, 0x20, 1)
        struct.pack_into("<2Q", memory, 0x28, 0x40000000, 0xF9C0000)
        struct.pack_into("<2Q", memory, 0xE0, 0x4F9C0000, 0x40000)
        raw = (struct.pack("<3I", 3, 0x88610001, 0)
               + struct.pack("<3I", 3, 0x88610002, 0)
               + struct.pack("<5I", 5, 0x88610004, 0x11005000, 921600, 1)
               + memory).ljust(1024, b"\0")
        prepared = prepare(raw)
        self.assertEqual(prepare(prepared), prepared)
        self.assertEqual(struct.unpack_from("<I", prepared, 20)[0], 99)
        self.assertEqual(struct.unpack_from("<I", prepared, 44 + 12)[0], 0xF7C0000)
        self.assertEqual(struct.unpack_from("<2Q", prepared, 44 + 0xE0), (0x4F7C0000, 0x640000))
        for invalid in (raw[:-1], bytes(1024), raw[:44] + struct.pack("<I", 2) + raw[48:]):
            with self.assertRaises(ValueError):
                prepare(invalid)

    def test_unrelated_gpt_changes_are_accepted_and_preserved(self):
        current = bytearray(asset("stock_boot0.bin"))
        entry = 0x4000 + 17 * 128  # Unused by the installer/early loader.
        current[entry + 56:entry + 128] = "custom_data".encode("utf-16le").ljust(72, b"\0")
        struct.pack_into("<Q", current, entry + 40,
                         struct.unpack_from("<Q", current, entry + 40)[0] - 64)
        struct.pack_into("<Q", current, entry + 48, 0x1234)
        current[entry + 16:entry + 32] = b"other-unit-guid!"
        update_gpt_crc(current)
        result = device_preloader(bytes(current), asset("stock_tee1.bin"))
        self.assertEqual(result[:0x8000], current[:0x8000])

    def test_needed_gpt_ranges_overlap_and_crc_are_still_checked(self):
        for mode in ("moved", "resized", "missing", "overlap", "duplicate", "crc"):
            with self.subTest(mode=mode):
                image = bytearray(asset("stock_boot0.bin"))
                boot_a = 0x4000 + 13 * 128
                if mode == "moved":
                    struct.pack_into("<Q", image, boot_a + 32, 0x1301)
                elif mode == "resized":
                    struct.pack_into("<Q", image, boot_a + 40, 0x223e)
                elif mode == "missing":
                    image[boot_a:boot_a + 16] = bytes(16)
                elif mode == "overlap":
                    struct.pack_into("<QQ", image, 0x4000 + 17 * 128 + 32, 0x1301, 0x1302)
                elif mode == "duplicate":
                    image[0x4900:0x4980] = image[boot_a:boot_a + 128]  # Previously empty entry 18.
                update_gpt_crc(image)
                if mode == "crc":
                    image[0x3038] ^= 1
                with self.assertRaises(ValueError):
                    validate_boot_layout(image)

    def test_mtd_indices_and_unused_entries_may_vary(self):
        table = 'mtd45: 00500000 00040000 "tee1"\nmtd99: 00001000 00001000 "other"\n'
        self.assertEqual(mtd_partition(table, "tee1", 0x500000), "/dev/mtd/mtd45")
        for invalid in (table.replace("00500000", "00400000"),
                        table.replace('00500000 00040000', '00500000 00020000'),
                        table + 'mtd46: 00500000 00040000 "tee1"\n'):
            with self.assertRaises(ValueError):
                mtd_partition(invalid, "tee1", 0x500000)

    def test_twrp_collection_does_not_read_nand_lk_copies(self):
        import device_data as d
        nodes = fdt_properties(asset("firmware_template.dtb"))
        nodes["/chosen"].update({PROPERTY: b"capture", "atag,devinfo": bytes(404)})
        fdt = serialize_fdt(asset("firmware_template.dtb"), nodes)
        idme = bytes(0x200000)
        responses = {("get-state",): b"recovery", ("shell", "getprop ro.product.device"): b"crumpet",
                     ("shell", "getprop ro.twrp.version"): b"3.7.0_9-0",
                     ("shell", "cat /proc/mtd"): b'mtd36: 00200000 00040000 "idme_nand"\n',
                     ("exec-out", "cat", "/sys/firmware/fdt"): fdt,
                     ("exec-out", "cat", "/dev/mtd/mtd36"): idme}
        def run(argv, **kwargs):
            return subprocess.CompletedProcess(argv, 0, stdout=responses[tuple(argv[3:])])
        result = dict(serial="SYNTHETIC", idme=idme, devinfo=bytes(404), reference_fdt=fdt)
        with tempfile.TemporaryDirectory() as folder, patch.object(d.subprocess, "run", side_effect=run), \
                patch.object(d, "validate_device_data", return_value=result):
            report = d.collect(Path(folder) / "capture", "adb", "test")
        self.assertTrue(report["raw_idme_read_twice"])

    def test_only_replaced_tee1_blocks_require_stock_and_others_are_preserved(self):
        import build_bootchain as build
        original = bytearray(asset("stock_tee1.bin"))
        original[7 * 0x40000] ^= 1
        device_preloader(asset("stock_boot0.bin"), original)
        config = dict(bridge=b"bridge", dtb=b"dtb")
        with tempfile.TemporaryDirectory() as folder, \
                patch.object(build, "build", return_value=(b"4" * 0x40000, b"L" * 0x40000, config, {})):
            build.write_candidate(Path(folder), {}, tee1=original)
            candidate = (Path(folder) / "tee1_firmware_candidate.bin").read_bytes()
        self.assertEqual(candidate[7 * 0x40000:8 * 0x40000], original[7 * 0x40000:8 * 0x40000])
        original[4 * 0x40000] ^= 1
        with self.assertRaises(ValueError):
            device_preloader(asset("stock_boot0.bin"), original)

    def test_capture_bundle_preserves_vm_ramdisk_and_relocates_fdt(self):
        import build_crumpet_fastboot as b
        path = os.environ.get("CRUMPET_RECOVERY")
        if not path:
            self.skipTest("set CRUMPET_RECOVERY for the VM recovery payload check")
        recovery = Path(path).read_bytes()
        kernel, ramdisk = b.recovery_kernel(recovery), b.extract_ramdisk(recovery)
        _, fdt = b.recovery_dtb(kernel)
        base, manifest = b.build(kernel, b.patch_memory_249m(fdt), ramdisk, "b", lk_loader=True)
        image, report = build_capture_bundle(base, manifest)
        self.assertEqual(image[0x9a0000:0x9a0000+len(ramdisk)], ramdisk)
        self.assertEqual(report["ramdisk_sha256"], hashlib.sha256(ramdisk).hexdigest())
        nodes = fdt_properties(image[0x980000:0x980000+int(report["fdt_size"], 16)])
        self.assertEqual(nodes["/chosen"][PROPERTY], b"\xa5" * CAPTURE_SIZE)
        self.assertEqual(nodes["/chosen"][ATAG_PROPERTY], b"\xa7" * 1024)
        self.assertIn(b"0x459a0000", nodes["/chosen"]["bootargs"])
        self.assertEqual(b.android_id(image).hex(), report["android_id"])
        self.assertEqual(report["persistent_operations"], [])
        with self.assertRaises(ValueError):
            build_capture_bundle(bytes(len(base)), manifest)

    def test_every_asset_is_pinned(self):
        for name in PROFILE["hashes"]:
            self.assertTrue(asset(name))

    def test_template_has_no_factory_identity(self):
        nodes = fdt_properties(asset("firmware_template.dtb"))
        self.assertFalse(any(p.startswith("/idme/") for p in nodes))
        self.assertNotIn("atag,devinfo", nodes["/chosen"])
        self.assertEqual(nodes, fdt_properties(serialize_fdt(asset("firmware_template.dtb"), nodes)))

    def test_preloader_preserves_unit_gpt(self):
        current = bytearray(asset("stock_boot0.bin"))
        current[0x3038:0x3048] = b"example-unit-GPT"
        struct.pack_into("<I", current, 0x3010, 0)
        struct.pack_into("<I", current, 0x3010, zlib.crc32(current[0x3000:0x305c]))
        result = device_preloader(bytes(current), asset("stock_tee1.bin"))
        self.assertEqual(result[:0x8000], current[:0x8000])
        end = 0x8000 + struct.unpack_from("<I", asset("preloader_donor.img"), 0x8020)[0]
        self.assertEqual(result[end:], current[end:])
        self.assertEqual(len(result), 0x40000)
        current[0x9000] ^= 1
        with self.assertRaises(ValueError):
            device_preloader(bytes(current), asset("stock_tee1.bin"))

    def test_partial_failure_rolls_back_failed_region_too(self):
        plan = [dict(name=str(n), old=b"old", new=b"new") for n in range(3)]
        state, calls = {p["name"]: p["old"] for p in plan}, []
        def write(item, kind):
            calls.append((item["name"], kind))
            state[item["name"]] = item[kind]
            if item["name"] == "1" and kind == "new":
                state["1"] = b"partial"
                raise OSError("simulated erase/write failure")
        with self.assertRaises(OSError):
            apply_regions(plan, write, lambda p: state[p["name"]], lambda *a, **k: None)
        self.assertEqual(calls, [("0", "new"), ("1", "new"), ("1", "old"), ("0", "old")])
        self.assertTrue(all(value == b"old" for value in state.values()))

    def test_full_verification_failure_rolls_back(self):
        item = dict(name="0", old=b"old", new=b"new")
        state = [b"old"]
        def fail():
            raise ValueError("full partition mismatch")
        with self.assertRaises(ValueError):
            apply_regions([item], lambda p, k: state.__setitem__(0, p[k]),
                          lambda p: state[0], lambda *a, **k: None, fail)
        self.assertEqual(state[0], b"old")


if __name__ == "__main__":
    unittest.main()
