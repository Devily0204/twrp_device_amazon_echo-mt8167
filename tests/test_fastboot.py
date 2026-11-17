"""Host-only tests for the independent Crumpet Fastboot builder and runner.

No LK baseline, vendor rescue payload or device access is used. A recovery
image is optional: if one is found (CRUMPET_RECOVERY or the usual build
locations) the payload tests run too; without one they are skipped. The
fsync patch test uses a small synthetic ramdisk, not a full image.
"""
import gzip
import hashlib
import json
import os
from pathlib import Path
from unittest.mock import patch
import struct
import sys
import tempfile
import types
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import build_crumpet_fastboot as bundle
import lk_fastboot as lk
import run_crumpet_fastboot as runner

REPO = Path(__file__).resolve().parents[1]


def _recovery_image():
    candidates = []
    override = os.environ.get("CRUMPET_RECOVERY")
    if override:
        candidates.append(Path(override))
    candidates += [
        REPO / "crumpet-nand-build/proxmox-vm/recovery.img",
        REPO / "crumpet-nand-build/recovery.img",
        REPO / "crumpet-nand-build/proxmox-vm/uncommitted-main/recovery.img",
        REPO / "../../../out/target/product/crumpet/recovery.img",
    ]
    for path in candidates:
        if path.is_file():
            return path.read_bytes()
    return None


def _old_mtd_ramdisk():
    """A tiny old crumpet-mtd ramdisk with the original fsync stub bytes."""
    body = bytearray(0x46550)
    body[0x2F88:0x2F90] = bytes.fromhex("07c0a0e17670a0e3")
    archive = bundle.newc_record("sbin/crumpet-mtd", bytes(body), 0o100755)
    archive += bundle.newc_record("TRAILER!!!", b"", 0)
    return gzip.compress(archive, 9, mtime=0)


class IndependentPayloadChecks(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.recovery = _recovery_image()
        if cls.recovery is not None:
            cls.kernel = bundle.recovery_kernel(cls.recovery)
            cls.ramdisk = bundle.extract_ramdisk(cls.recovery)
            _, cls.fdt = bundle.recovery_dtb(cls.kernel)
            cls.fdt = bundle.patch_memory_249m(cls.fdt)

    def _need_recovery(self):
        if self.recovery is None:
            self.skipTest("no recovery image available")

    def test_recovery_kernel_and_pvt_fdt(self):
        self._need_recovery()
        self.assertEqual(bundle.sha256(self.kernel), "0ea78c5250d4c431b3856a8079d47cf0de0ff2e267b4d53536889715f26a540d")
        offset, _ = bundle.recovery_dtb(self.kernel)
        self.assertEqual(offset, 0x55701A)
        self.assertEqual(bundle.fdt_properties(self.fdt)["/memory@00000000"]["reg"], struct.pack(">QQ", 0x40000000, 0x0F7C0000))

    def test_generated_mtk_header_is_deterministic(self):
        self._need_recovery()
        header = bundle.mtk_header(self.kernel)
        self.assertEqual(len(header), 512)
        self.assertEqual(struct.unpack_from("<II", header, 0), (0x58881688, len(self.kernel)))
        self.assertEqual(header[8:14], b"KERNEL")
        self.assertEqual(header, bundle.mtk_header(self.kernel))

    def test_dummy_is_a_valid_tiny_newc_archive(self):
        dummy = bundle.minimal_ramdisk()
        raw = __import__("gzip").decompress(dummy)
        entries = list(bundle.cpio_entries(raw))
        self.assertEqual([name for name, _, _ in entries], ["default.prop", "init"])
        self.assertIn(b"TRAILER!!!", raw)
        self.assertLess(len(dummy), 200)
        self.assertIn(b"ro.secure=1", raw)
        self.assertNotIn(b"service.adb.root", raw)

    def test_independent_bundle_layout_and_metadata(self):
        self._need_recovery()
        output, manifest = bundle.build(self.kernel, self.fdt, self.ramdisk, "b")
        self.assertEqual(output[:8], b"ANDROID!")
        self.assertEqual(output[0x800:0x804], bytes.fromhex("88168858"))
        self.assertEqual(output[0xA00:0xA00 + len(self.kernel)], self.kernel)
        self.assertEqual(output[0x5E7000:0x5E7003], b"\x1f\x8b\x08")
        self.assertEqual(manifest["entry_thumb"], "0x45971001")
        self.assertEqual(manifest["stage_offset"], "0x971000")
        self.assertEqual(manifest["stage_sha256"], hashlib.sha256(output[0x971000:0x972000]).hexdigest())
        self.assertEqual(manifest["dummy_ramdisk_size"], hex(len(bundle.minimal_ramdisk())))
        self.assertEqual(manifest["tags_address"], "0x4e000000")
        self.assertEqual(output[0x20:0x24], (0x4E000000).to_bytes(4, "little"))
        self.assertEqual(output[0x1C:0x20], (0x40F00000).to_bytes(4, "little"))
        self.assertEqual(manifest["kernel_source"], "recovery")
        self.assertEqual(manifest["persistent_operations"], [])
        self.assertEqual(manifest["telemetry"], [])
        self.assertEqual(manifest["ramdisk_sha256"], hashlib.sha256(self.ramdisk).hexdigest())
        self.assertEqual(int(manifest["initrd_end"], 16), 0x45990000 + len(self.ramdisk))
        self.assertEqual(bundle.android_id(output).hex(), manifest["android_id"])

    def test_project_owned_stage_assembles(self):
        import lk_stage_minimal
        code = lk_stage_minimal.assemble_stage()
        self.assertEqual(len(code), 4096)
        self.assertEqual(code[:4], bytes.fromhex("73b6eff3"))
        code_length = len(code.rstrip(b"\0"))
        self.assertLess(code_length, 0x200)
        self.assertEqual(code[code_length - 2:code_length], bytes.fromhex("7047"))
        self.assertIn(bytes.fromhex("2847"), code[:code_length])
        self.assertIn(bytes.fromhex("41f20004c4f2975442f20005c4f29755"), code[:0x80])
        self.assertEqual(code.count(bytes.fromhex("46f23f17c0f25e07")), 3)
        self.assertTrue(all(byte == 0 for byte in code[code_length:]))
        self.assertEqual(lk_stage_minimal.STAGE_ENTRY_THUMB, 0x45971001)

    def test_deferred_dpc_stage_assembles(self):
        import lk_stage_minimal
        direct = lk_stage_minimal.assemble_stage()
        deferred = lk_stage_minimal.assemble_stage(dpc=True)
        self.assertEqual(len(deferred), 4096)
        self.assertNotEqual(direct, deferred)
        self.assertLess(len(deferred.rstrip(b"\0")), 0x400)
        self.assertIn(bytes.fromhex("4bf23513"), deferred[:0x200])
        mmu_off = lk_stage_minimal.assemble_stage(dpc=True, mmu_off=True)
        self.assertNotEqual(deferred, mmu_off)
        self.assertLess(len(mmu_off.rstrip(b"\0")), 0x400)
        lk_loader = lk_stage_minimal.assemble_stage(dpc=True, lk_loader=True)
        self.assertNotEqual(deferred, lk_loader)
        self.assertLess(len(lk_loader.rstrip(b"\0")), 0xB00)
        self.assertEqual(lk_loader[:4], bytes.fromhex("f0b545f6"))
        self.assertEqual(lk_loader[0x6C0:0x6CA], bytes.fromhex("41f20153c4f297531847"))
        self.assertEqual(lk_loader[0x6E0:0x6EA], bytes.fromhex("41f26153c4f297531847"))
        self.assertEqual(lk_loader[0x5EA:0x5EC], bytes.fromhex("1fb5"))
        self.assertEqual(lk_loader[0x608:0x60A], bytes.fromhex("1fb5"))
        self.assertEqual(lk_loader[0x720:0x722], bytes.fromhex("1fb5"))
        self.assertEqual(lk_loader[0xB00:0xC00], bytes(0x100))
        self.assertEqual(lk_loader[0xD00:0xE00], bytes(0x100))
        for immediate in (
            bytes.fromhex("4df28454c4f2e114"),  # 0x41e1d584
            bytes.fromhex("4ff22705cff66c55"),  # bl 0x41e45060
            bytes.fromhex("4df29a64c4f2e114"),  # 0x41e1d69a
            bytes.fromhex("4cf2d234c4f2e114"),  # 0x41e1c3d2
            bytes.fromhex("4cf27834c4f2e114"),  # 0x41e1c378
            bytes.fromhex("4df2f644c4f2e114"),  # 0x41e1d4f6
            bytes.fromhex("4df27824c4f2e114"),  # 0x41e1d278
        ):
            self.assertIn(immediate, lk_loader)
        for patch in ("4ff22705", "4ff6f145", "4ff22805", "4ff66565", "cff6b665", "4ff6e855", "cff6c275"):
            self.assertIn(bytes.fromhex(patch), lk_loader)

    def test_reset_canaries_assemble_at_each_milestone(self):
        import lk_stage_minimal
        stages = {name: lk_stage_minimal.assemble_stage(name) for name in lk_stage_minimal.CANARIES}
        self.assertEqual(set(stages), {"entry", "copy", "cache", "mmu", "verify", "cleanup"})
        self.assertEqual(len({hashlib.sha256(code).digest() for code in stages.values()}), 6)
        for code in stages.values():
            self.assertEqual(len(code), 4096)
            self.assertIn(bytes.fromhex("41f20921"), code[:0x300])
        with self.assertRaisesRegex(ValueError, "Unknown stage canary"):
            lk_stage_minimal.assemble_stage("invalid")

    def test_fsync_patch_updates_the_ramdisk_only(self):
        patched = bundle.patch_fsync(_old_mtd_ramdisk())
        raw = bytearray(gzip.decompress(patched))
        (offset, size), = [(o, s) for name, o, s in bundle.cpio_entries(raw) if name == "sbin/crumpet-mtd"]
        self.assertEqual(size, 0x46550)
        self.assertEqual(raw[offset + 0x2F88:offset + 0x2F90], bytes.fromhex("0000a0e31eff2fe1"))
        # The rest of the old crumpet-mtd body must be untouched.
        body = bytearray(0x46550)
        body[0x2F88:0x2F90] = bytes.fromhex("07c0a0e17670a0e3")
        expected = bytearray(body)
        expected[0x2F88:0x2F90] = bytes.fromhex("0000a0e31eff2fe1")
        self.assertEqual(raw[offset:offset + size], expected)


class ProtocolChecks(unittest.TestCase):
    def test_lk_version_gate_remains_before_ram_handoff(self):
        responses = {b"getvar:product": "OKAYCRUMPET", b"getvar:unlock_status": "OKAYfalse",
                     b"getvar:lk_build_desc": "OKAY" + lk.EXPECTED_LK_BUILD,
                     b"getvar:max-download-size": "OKAY0x8000000"}
        with patch.object(lk, "read_only_exchange", side_effect=lambda _d, command: responses[command]):
            lk.gates(None, 0x1400000)
            responses[b"getvar:lk_build_desc"] = "OKAYunsupported"
            with self.assertRaisesRegex(RuntimeError, "Unexpected LK build"):
                lk.gates(None, 0x1400000)

    def test_direct_loader_packets(self):
        packets = lk.loader_packets()
        self.assertEqual(len(packets), 12)
        self.assertTrue(all(len(packet) == 512 for packet in packets))
        self.assertEqual(struct.unpack_from("<I", packets[0], lk.FAKE_REQUEST - lk.COMMAND_BUFFER + 8)[0], lk.LOADER_ENTRY_THUMB)
        transfers = lk.exploit_transfers()
        self.assertEqual([len(packet) for packet in transfers], [512] * 8 + [2048])
        self.assertEqual(transfers[:8], packets[:8])
        self.assertEqual(transfers[8], b"".join(packets[8:]))

    def test_bundle_manifest_gate(self):
        payload = bytes(0x2000)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "test.bundle.bin"
            path.write_bytes(payload)
            metadata = {
                "bundle_size": len(payload), "bundle_sha256": hashlib.sha256(payload).hexdigest(),
                "entry_thumb": "0x41e1d585", "persistent_operations": [],
            }
            path.with_suffix(".json").write_text(json.dumps(metadata))
            loaded, selected = runner.load_bundle(path)
            self.assertEqual(loaded, payload)
            self.assertEqual(selected["entry_thumb"], "0x41e1d585")
            metadata["persistent_operations"] = ["flash"]
            path.with_suffix(".json").write_text(json.dumps(metadata))
            with self.assertRaisesRegex(ValueError, "persistent"):
                runner.load_bundle(path)

    def test_twrp_identity_and_version(self):
        row = {"serial": "0123456789ABCDEF", "state": "recovery", "device": "crumpet", "product": "omni_crumpet"}
        devices, read = runner.adb_devices, runner.adb_read
        runner.adb_devices = lambda adb: [row]
        runner.adb_read = lambda *args: {"returncode": 0, "stdout": "3.7.0_9-0", "stderr": ""}
        try:
            self.assertTrue(runner.wait_for_twrp("adb", "0123456789ABCDEF", "3.7.0_9-0", 1)["success"])
        finally:
            runner.adb_devices, runner.adb_read = devices, read

    def test_rescue_identity_is_not_accepted_as_twrp(self):
        row = {"serial": "0123456789ABCDEF", "state": "recovery", "device": "crumpet", "product": "crumpet"}
        devices, read = runner.adb_devices, runner.adb_read
        runner.adb_devices = lambda adb: [row]
        runner.adb_read = lambda *args: {"returncode": 1, "stdout": "", "stderr": "not found"}
        try:
            result = runner.wait_for_twrp("adb", "0123456789ABCDEF", "3.7.0_9-0", 1)
            self.assertFalse(result["success"])
            self.assertIn("ro.twrp.version", result["error"])
        finally:
            runner.adb_devices, runner.adb_read = devices, read


if __name__ == "__main__":
    unittest.main()
