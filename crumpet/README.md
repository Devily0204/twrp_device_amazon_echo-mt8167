# Crumpet NAND recovery status

This target is for Echo Dot 3 Refresh (`crumpet`, 32-bit ARM). It includes NAND
and UBI helpers, a recovery-core patch for UBIFS and a full OTA staging workflow.
The separate host workflow in [FASTBOOT.md](FASTBOOT.md) builds and starts TWRP
from the locked LK Fastboot in RAM. The sections below retain the original
NAND-first implementation notes and earlier hardware-test caveats.

## Evidence

Offline checks used the two local backup sets dated 2026-11-09 and 2026-11-10:
`meta/proc_mtd.txt`, the `boot_a`/`boot_b` images, and both CRC-valid UBI volume
table copies in the `persist` and `userdata` partition images. The original
Amazon NS65741/8149 `boot.img` and ramdisk fstab were cross-checked as well.
Only the non-private MTD table is copied into `tests/data/crumpet-proc-mtd.txt`.
No device identity, account, certificate or filesystem contents are included.

Both units expose the same 18 MTD partitions:

| MTD | Name | Size (bytes) |
| --- | --- | ---: |
| 0 | brhgptpl_0 | 262144 |
| 1 | reserve0 | 786432 |
| 2 | lk_a | 1572864 |
| 3 | lk_b | 1572864 |
| 4 | brhgptpl_1 | 262144 |
| 5 | reserve1 | 1835008 |
| 6 | idme_nand | 2097152 |
| 7 | brhgptpl_2 | 262144 |
| 8 | reserve2 | 1835008 |
| 9 | misc | 2097152 |
| 10 | brhgptpl_3 | 262144 |
| 11 | reserve3 | 1835008 |
| 12 | tee1 | 5242880 |
| 13 | boot_a | 15990784 |
| 14 | tee2 | 5242880 |
| 15 | boot_b | 15990784 |
| 16 | persist | 8388608 |
| 17 | userdata | 469237760 |

MTD eraseblocks are 262144 bytes, NAND pages 4096 bytes, and OOB 256 bytes.
These are distinct from the Android boot-image header's 2048-byte page size.
`userdata` is an entire UBI container, not an ext4 filesystem for `/data`.

| MTD parent | UBI volume ID | Name | Reserved eraseblocks | Filesystem |
| --- | ---: | --- | ---: | --- |
| persist | 0 | persist | 20 | UBIFS |
| userdata | 0 | system_a | 538 | SquashFS |
| userdata | 1 | system_b | 538 | SquashFS |
| userdata | 2 | cache | 32 | UBIFS |
| userdata | 3 | userdata | 638 | UBIFS |

All these volume-table records have UBI type **dynamic (1)** in both backups;
both redundant tables agree and their record CRCs validate. Some earlier notes
called the system volumes static. The read-only SquashFS filesystem must not be
confused with the containing UBI volume's type. UBI device numbers (`ubi0`,
`ubi1`, etc.) depend on attach order and must not be hardcoded.

The stock boot headers agree on kernel `0x40008000`, ramdisk `0x44000000`,
second-stage field `0x40f00000`, tags `0x4e000000`, and
`bootopt=64S3,32N2,32N2`. The second-stage payload length is zero. The profile
now reproduces those address fields. A 15990784-byte recovery artifact ceiling
matches the boot partitions; it does not invent a recovery partition or choose
an installation destination. There is no `recovery`, `swdl` or `mmcblk0boot0` in
the observed NAND layout.

The upstream prebuilt kernel is retained (SHA-256
`0ea78c5250d4c431b3856a8079d47cf0de0ff2e267b4d53536889715f26a540d`). It is not
byte-identical to the kernel component in the stock boot containers. No binary
replacement or kernel patch is inferred from that difference.
Its decompressed image contains `ubi_attach_mtd_dev`, `ubiblock%d_%d`, SquashFS
and UBIFS implementation strings. This is evidence that the prebuilt contains
these code paths, not a verified kernel configuration or live mount test.

## Implemented storage and recovery

- Crumpet packages its own recovery init/fstab and the common LED assets. The
  eMMC wait, automatic `e2fsck -y`, slot-A fallback, bootloader decoys and BCB
  repair scripts are not packaged. Donut/Cupcake retain their eMMC profile.
- `crumpet-storage.sh --init` attaches the existing `persist` and `userdata`
  containers through `UBI_IOCATT`, validates MTD geometry and parentage, creates
  nodes from sysfs device numbers, and creates system ubiblock mappings. It
  does not format, create or resize volumes. Attach can modify UBI metadata.
  Running without `--init` only inspects existing mappings. Inventory goes to
  `/tmp/crumpet-storage.log`. An unusable inactive system does not hide a good
  active system. An unknown boot-reported slot has no slot-A fallback.
- The core patch recognizes UBIFS, skips block-filesystem probing on UBI
  character volumes, and mounts `/data` and `/cache` as UBIFS. Their file
  backups/restores use TWRP's tar path; wipes remove files while preserving
  UBI layout. `/persist` is read-only and cannot be wiped. The system mounts
  use read-only SquashFS; system image restore uses separate image endpoints.
- `crumpet-ubi update` checks the filesystem header and volume capacity, uses
  `UBI_IOCVOLUP`, handles short I/O and compares the entire written image by
  readback. UBI retains responsibility for ECC, wear levelling and bad blocks.
  System updates remove and recreate ubiblock; removal fails if it is in use.
  Interrupted UBI updates are detectable, but are not an atomic rollback.
- `crumpet-mtd` provides positional boot image backup/write with kernel ECC,
  range checks, eraseblock preservation and readback verification. Encountering
  a bad block aborts before erasing; unverified logical relocation is avoided.
  The TWRP image paths use these helpers instead of generic `flash_image` or
  writing to the complete UBI container. Raw ADB image streaming is rejected;
  these restores need a seekable local image. Raw `/persist` restore is excluded.
- The common eMMC boot-control HAL, `amzn_bcbtool` and generic A/B sideload
  writer stay disabled for Crumpet. The device retains its native A/B layout;
  the explicit NAND workflow below implements full-image OTA staging.

## Rolling NAND BCB

The first eraseblock of `misc` is erased in both backups. Inspecting only
partition offset `0x360` therefore incorrectly suggested missing A/B metadata.
The valid record is in eraseblock 7 in the November 9 backup and block 2 in
the November 10 backup. At each record's offset `0x360`, both contain
`00 41 42 42 01 8f 8e 00`: ABB magic, version 1, and slot A/B flags.

This layout was cross-checked against the original ARM
`system/lib/hw/bootctrl.mt8167.so`, SHA-256
`d92ce74ddd03e53ac79138cde16ebf36656d6f22de49ee43a9e8bf6f6b4d2e17`.
Its Thumb functions at `0x24f0` and `0x25c4` read/write the redundant record;
the scanner at `0x5074` walks eraseblock boundaries and recognizes ABB at
record+`0x360`. The writer uses `mtd_part_atomic_write_rr` at `0x5f90`.
Slot activation sets the selected byte to `0x7f` and lowers the other slot's
priority from 15 to 14 when needed. Marking success preserves priority and
sets the successful bit while clearing tries.

The helper scans all eight good eraseblocks, preserves the complete selected
256-KiB record, writes to an erased spare, publishes its first NAND page last,
verifies the new record, and then retires the old valid records. Missing valid
records or missing erased spares cause an error. Fault-model tests cover a
failed new write retaining the old record. This is not a hardware power-cut
atomicity guarantee; torn NAND pages and ECC failure need physical testing.

`crumpet-bcb.sh` exposes explicit `status`, `backup`, `clear`, hash-checked
`restore`, `activate` and `mark-success` operations. Command/status/recovery
backup is the first 832 bytes **of the selected record**, distinct from the
8-byte slot metadata at `0x360`. There is no automatic consumption or replay
of old recovery commands during startup, and no raw `misc` image writer.

## Full OTA workflow

`tools/prepare_crumpet_ota.py` requires an independently trusted SHA-256 of the
whole Amazon ZIP, Crumpet A/B metadata and a full version-2 payload. It verifies
operation hashes, bounds, complete extent coverage and final partition hashes.
It extracts only `system.img` and `boot.img`; bootloader, TEE and device identity
regions are excluded. Delta/source-dependent updates are rejected. The ZIP
hash is caller-provided authentication, not a new Amazon certificate verifier.

For example, host-side preparation (no device writes):

```sh
python3 tools/prepare_crumpet_ota.py /path/to/update.zip \
  --sha256 TRUSTED_WHOLE_ZIP_SHA256 --slot b --output /path/to/new-plan
```

The plan contains `SHA256SUMS`, `target-slot`, `plan.json` and the two images.
On recovery, `/sbin/crumpet-apply-plan.sh /path/to/plan` rechecks both image
hashes and that the destination is inactive, then writes and verifies system
before boot. By default it leaves slot selection unchanged. Its explicit
`--activate` option updates the rolling BCB only after both writes succeed.
Writers reject an unknown current slot and stale aliases to another volume.
`ro.boot.slot_suffix` must come from the actual boot; metadata for the next boot
is not a safe substitute for identifying the currently running slot.

The local full NS65741/8149 OTA ZIP SHA-256 was
`5b3e6ae0a7fa1e1457308c7237c6bfafcf7d3e5cf17a1acbb68bab90f7cda50b`.
Extraction matched the existing independently extracted originals byte-for-byte:
boot `39abec8713007bdfc27dc97a5e3891256738ec45eb854045a2a4e5cbe231b720`,
system `cdca995c2e6695ce1ee535bc5c4379135f5db31438e9e1c962102a629894f529`.

## Build and validation

Use Omni `twrp-9.0` with the
[amazon-oss recovery](https://github.com/amazon-oss/android_bootable_recovery/tree/da9eb6c84c22b7635143722294f9880c8434b7b4)
revision `da9eb6c84c22b7635143722294f9880c8434b7b4`. Before lunch, apply the
checked, idempotent patch; the Crumpet BoardConfig rejects an unpatched core:

```sh
python3 device/amazon/mt8167-echo/tools/prepare_recovery.py bootable/recovery
source build/envsetup.sh
lunch omni_crumpet-eng
mka recoveryimage
python3 device/amazon/mt8167-echo/tools/inspect_recovery.py out/target/product/crumpet/recovery.img
```

Rebuild the generated recovery ramdisk rather than reusing a stale overlay.
The patch targets this revision and refuses conflicting source changes.

For the Fastboot-only TWRP bundle and start tool, see
[FASTBOOT.md](FASTBOOT.md). Its host-only regression reproduces the proven
bundle exactly and tests the ADB TWRP identity separately from NAND storage.

Run `python tests/check_crumpet.py`. Add
`--backup-root D:/AiAssistant/EchoMuseCrumpet` to check both original backup
sets and the original OTA. Pass `--zig /path/to/zig` if a native C compiler is
not otherwise available. The suite has 25 tests, including the real MTD C
implementation under a memory NAND fault model. Other tests cover shell
mapping, slot guards, OTA ordering and activation, malformed payload rejection,
and original boot/UBI/BCB evidence. Both native helpers also cross-compile as
static 32-bit ARM Linux executables with warnings treated as errors.

These checks do not exercise a real UBI kernel. Required next evidence is a
successful hardware boot with init/recovery logs, mount and read-only backup
tests before write/restore and power-cut testing. Incremental OTA, relocation
of bad blocks in raw boot partitions, full NAND/OOB cloning and automatic
slot-success marking remain outside this implementation. No bootloader unlock
or signature-verification bypass is supplied.

### Ramdisk import repair (2026-11-12)

The cloud-built image (SHA-256
`8493071d6435aab098e66be0ce69badb081f8e447af784248b3491cec9701093`)
has unconditional imports for `init.recovery.logd.rc` and
`init.recovery.vold_decrypt.rc`, but packages neither. Crumpet's overlay now
supplies comment-only files: logd/logcat and Vold decryption are not enabled
in this profile, so no services for absent executables are declared. To enable
those features later, replace these files with their actual service configs.
`crumpet/BoardConfig.mk` now maps these files and the optional Nano config through
`PRODUCT_COPY_FILES` to `recovery/root`. A fresh VM build exposed the Nano import
as missing too, so the overlay provides a comment-only config while Nano is
excluded. `build-crumpet.sh` checks all three files for presence and exact contents
and runs `tools/inspect_recovery.py` before publishing the image. The separate
`init-import-fix` directory below documents the earlier offline repack and is
not the regular build path.
The cloud-tested image-size and libbacktrace dependency fixes are now included
in the local BoardConfig too.

`tools/inspect_recovery.py` reuses the original release inspector and additionally
checks all reachable imports, service executables, the required ueventd/adbd/
recovery declarations, and ELF interpreter presence. Its inspection is read-only.
The original image fails on the missing logd import. A regression test also
covers the missing Vold config, a missing nested LED import and a missing adbd.

The import-only repaired artifact is
`crumpet-nand-build/init-import-fix/recovery.img`, SHA-256
`9aa8af749aaa2053d38db4daac946f119ccb83699ae7392bff5465b75555e6a0`,
15609856 bytes. It preserves every original CPIO entry, its metadata and the
kernel byte-for-byte, adds only the two configs, recompresses the ramdisk and
updates the boot header size and Omni mkbootimg image ID. All 11 reachable init
configs, 5 service executables, and the 101 ARM32 ELF files with 575 shared-library
dependencies pass offline inspection. This artifact has not been boot-tested.

Import failure is **not** evidence that Android 9 stops parsing the parent file:
the exact pinned [Omni init ImportParser](https://github.com/omnirom/android_system_core_old/blob/53782f5cc24fed4e332a0ac89b628f9ab17db0b4/init/import_parser.cpp)
processes imports after parsing and logs each failure while continuing. Thus
the missing imports are repaired, but they do not establish the cause of the
reported missing USB/recovery. A kernel/init log from this exact image is needed
to establish that cause. `TW_NO_SCREEN_UI` also means this target is headless;
a graphical on-device TWRP screen is not configured.
