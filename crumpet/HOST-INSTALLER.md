# Crumpet host installer and image writing

The VM builds `crumpet-twrp-fastboot.zip` from this repository and its freshly
built `recovery.img`. It contains a RAM TWRP Fastboot bundle and the separate
host installer `tools/install_bootchain.py`, all its sources and generic OEM
inputs. No device's IDME, devinfo, MAC addresses or captured MEID are packaged.

## Start RAM TWRP

Install Python 3.9+ and `pip install -r requirements.txt`, plus adb and libusb.
On Windows the stock Fastboot interface `0BB4:0C01` needs WinUSB/libusb; ADB
uses its normal driver. Pass `--adb PATH` and `--libusb-dir DIRECTORY` to the
Fastboot runner when necessary.

Hold Action while applying power to enter **LK Fastboot**. The button
must be held during startup; it is not a power switch. The updated custom
loader supports this entry too: GPIO42 selects the original LK, with native
preloader ATAGs, boot mode 99 and authenticated ATF/MTEE from TEE2. Without
Action it follows the active system slot as before. The runner checks
`product=CRUMPET`, locked state and LK `6f7225e-20230407_002912`.

```sh
python tools/run_crumpet_fastboot.py --bundle crumpet-twrp-b.bundle.bin --check
python tools/run_crumpet_fastboot.py --bundle crumpet-twrp-b.bundle.bin --execute
```

This step writes RAM only and captures the same unit's original LK IDME and
read-only devinfo and preloader ATAGs for TWRP. Its RAM map ends at `0x4f7c0000`
to keep the patched firmware's secure region outside Linux allocations.
No BROM access is needed. The expected ADB transport
is `0123456789ABCDEF`, product `omni_crumpet`, TWRP `3.7.0_9-0`.

## Separate persistent bootchain installation (experimental)

```sh
python tools/install_bootchain.py --work my-dot-plan --dummy-meid
python tools/install_bootchain.py --work my-dot-install --dummy-meid --execute --yes
```

Each `--work` directory must be new. It contains private original IDME/devinfo,
complete Boot0/TEE1 backups, captured preloader ATAGs, generated firmware and `install-plan.json`. Keep it
outside a public repository. A plan run performs reads and RAM writes only.

Original MEID capture is **not qualified**. `--dummy-meid` explicitly chooses
the experimental software value `EchoMuseDummyID1`; this passed the previously
tested Alexa/audio functions on one test Dot, not a universal compatibility
qualification. No eFuse programming is performed.

The installer uses the Crumpet PVT memory/NAND geometry and fixed physical
ranges of Boot0, IDME, misc, TEE1, boot A/B, TEE2 and LK A (for button Fastboot). It checks those ranges,
valid GPT checksums, and absence of overlaps with them. Other GPT entries may
change names, sizes, GUIDs or attributes; MTD indices are resolved by name.
Partitions outside the required ranges are not required to match stock.

The LK version is gated **before** the Fastboot RAM hook runs. In TWRP the
installer validates the captured identity/devinfo against this unit's IDME.
The button path will execute LK A from NAND, so its original executable body
is checked at installation; LK B is not read. Boot A/B and TEE2 contents are
not read or pinned to stock hashes during installation. At boot the loader's
image bounds checks and OEM authentication of ATF/MTEE still apply; a different
image is not automatically compatible merely because installation accepts it.

Use the updated RAM bundle for its original preloader-ATAG capture. The
installer enables button Fastboot by default. `--no-button-fastboot` retains
the older direct-slot-only behavior for compatibility with old captures.
The qualified ATAG memory profile is one 256 MiB rank. The bridge's secure
reservation is preserved in both the LK arguments and the TWRP RAM map.
This path passed Action-to-Fastboot and a subsequent normal EchoMuse slot-A
boot on test unit G090XG10010701FP on 2026-11-17; other units are not yet
hardware-qualified. LK A and the system slots are not written by this installer.

The original Boot0 NAND/BRLYT configuration and preloader bytes must still
match the supported source. Only the six TEE1 blocks being replaced must match
their stock source; untouched TEE1 blocks may differ and are preserved. This
is an installation from the supported original bootchain, not an updater for
arbitrary previously patched target blocks. Required board/devinfo checks,
per-device identity binding and all write/rollback verification remain.

The installer preserves this unit's original BRLYT/GPT and GUIDs and writes
only the preloader region in Boot0 and TEE1 blocks 0,1,2,3,4,11. It checks all
target blocks for bad blocks before erasing, verifies each upload and NAND
readback, then compares both full partitions. A failed operation attempts
verified rollback of every attempted region. Interrupted power cannot be
rolled back automatically; do not disconnect power during installation.
The restricted `/sbin/crumpet-bootchain` helper is built into this TWRP; the
host checks its hash against `recovery-tools.json` from the same VM build.

The loader follows the active slot from native A/B metadata. The signed donor
preloader owns the seven-try/fallback policy. Runtime boot/TEE2 whole-image
hash checks are disabled; Block 11 integrity, image bounds and OEM ATF/MTEE
authentication remain. Authentication failure halts; there is no TEE-less
fallback. TEE2 is shared firmware, not slot B. Installation creates no NAND
profile over UBI and does not write UBI, misc, boot A/B or identity partitions.

## Write other boot and system/slot images from TWRP

TWRP **Install Image** offers Boot A, Boot B, System A Image and System B Image.
Boot images must use the Android boot-image format and fit 15,990,784 bytes;
kernel and ramdisk belong inside `boot.img`, not separate NAND partitions.
System images are written to the existing UBI volume with `UBI_IOCVOLUP`;
the UBI layout is preserved. Arbitrary image content does not imply that its
kernel, DTB, filesystem or userspace will boot on this device.

The same path is available through ADB. First check the active slot:

```sh
adb -s 0123456789ABCDEF shell /sbin/crumpet-bcb.sh status
adb -s 0123456789ABCDEF shell /sbin/crumpet-active-slot.sh
adb -s 0123456789ABCDEF push boot.img /tmp/boot.img
# Example only when A is INACTIVE; otherwise target boot_b.
adb -s 0123456789ABCDEF shell /sbin/crumpet-flash.sh boot_a /tmp/boot.img
```

`crumpet-flash.sh system_a /tmp/system.img` similarly writes an inactive system
volume. An optional third SHA256 argument checks the chosen source image;
there is no factory-image hash whitelist for this image-writing path. Both
backends verify readback. Writes to the active boot/system slot are refused.
`crumpet-backup.sh boot_a /tmp/original-boot-a.img` can back up a boot partition.

For a matching boot+system pair, push a directory with `target-slot` (`a` or
`b`), `boot.img`, `system.img`, and exactly their two entries in `SHA256SUMS`.
`/sbin/crumpet-apply-plan.sh /tmp/plan` validates both hashes and writes the
inactive pair; add `--activate` to switch A/B metadata only after both writes
succeed. Do not activate a slot with an unmatched boot/system pair.

The installer does not reboot automatically. Sync and unmount writable volumes
before a controlled cold start. USB waiting after an early-loader failure is
not evidence of stock Fastboot; check USB enumeration and UART. BROM is not
required for this installation and is not assumed available on other units.
