# Crumpet TWRP NAND build — 2026-11-11

Target: **omni_crumpet-eng, 32-bit ARM**, Echo Dot 3 Refresh. This is the NAND
implementation supplied in the incremental patch, not the earlier diagnostic
profile. No device was accessed or flashed. Cloud environment configuration was
not edited or published during the build task.

## Result

- `recovery.img`: **15,636,480 bytes**, maximum **15,990,784**, margin **354,304**.
- SHA-256: `8493071d6435aab098e66be0ce69badb081f8e447af784248b3491cec9701093`.
- `lunch omni_crumpet-eng` and `mka -j4 recoveryimage` completed successfully.
- Kernel remains SHA-256 `0ea78c5250d4c431b3856a8079d47cf0de0ff2e267b4d53536889715f26a540d`.
- Header: kernel `0x40008000`, ramdisk `0x44000000`, second `0x40f00000`
  with zero payload bytes, tags `0x4e000000`, Android page size 2048.
- Ramdisk: 370 entries, matching NAND fstab/init and all device/LED overlay files.
  Both `crumpet-ubi` and `crumpet-mtd` are static ARM32 ELF executables. NAND
  backup, flash, BCB and OTA-plan scripts are present; eMMC BCB/sideload helpers
  and old eMMC init scripts are absent.
- All 101 packed ELF files are ARM32. All 575 checked `DT_NEEDED` dependencies
  are available; the packaged Android linker supplies SONAME `ld-android.so`.
- Offline suite: **23 tests discovered, 20 passed, 3 skipped**. The skips require
  original device backups and original OTA files absent from this machine.

These are build and offline validation results. Hardware boot, NAND I/O,
UBI/UBIFS operation, restore, OTA and power-loss behavior remain unverified here.
The artifact size ceiling does not specify an installation destination or imply
that Crumpet has a dedicated recovery partition.

## Sources and changes

`manifest.xml` is the actual `repo manifest -r` output for all 227 managed source
projects. It records source revisions; it does not capture uncommitted patches.

- Minimal Omni manifest: `minimal-manifest-twrp/platform_manifest_twrp_omni`,
  `twrp-9.0`, `f3f57994a2aa2c92303df24bb01caff6850f7463`.
- Recovery: `amazon-oss/android_bootable_recovery`, `android-9`,
  `da9eb6c84c22b7635143722294f9880c8434b7b4`, plus `recovery-core.patch`.
- BCB source: `bengris32/bcbtool`, `master`,
  `98506d322351850dab688687f2a3a948e64f3495`; its eMMC tool is not packaged.
- Device tree: `Devily0204/twrp_device_amazon_echo-mt8167`,
  `f79b0e5cd1ef4fb164f21414350b6927473b8b7a`, plus
  **`device-tree-combined.patch`**. This checkout was copied locally into
  `device/amazon/mt8167-echo`; it is recorded separately from the repo manifest.
- Supplied incremental patch SHA-256 was verified as
  `70264531dac83a47308ea01e778c912f9f00e4fd1e161c431377813fbcb6c2dc`.

The combined device patch includes the supplied NAND changes and two additional
Crumpet-only build fixes. First, the initial image was 18,452,480 bytes and failed
the real size limit. The supported options `TW_EXCLUDE_PYTHON`,
`TW_EXCLUDE_BASH`, `TW_EXCLUDE_NANO`, `TW_EXCLUDE_MTP` are enabled, and
`TARGET_USERIMAGES_USE_F2FS` is false. The recovery uses `/sbin/sh`; OTA extraction
runs on the host. ADB and USB-OTG remain packaged. MTP is excluded.

Second, the old TWRP relink list omitted `libunwindstack.so` and `libdexfile.so`,
which are required by `libbacktrace.so`. These and `liblzma.so` are explicitly
included through `TARGET_RECOVERY_DEVICE_MODULES` and
`TW_RECOVERY_ADDITIONAL_RELINK_FILES`. `cloud-build-fixes.patch` contains only
these additional changes, to apply after the two supplied patches.

The original `/workspace/twrp_device_amazon_echo-mt8167` checkout is unchanged.
Changes are in `/workspace/twrp-crumpet-build/src/device/amazon/mt8167-echo`.
No commits or pushes were made.

## Rebuild in this workspace

The prepared Ubuntu 20.04 container is `twrp-crumpet-u2004`. It has Java 8u452,
Python 2.7.18 and the old ncurses runtime libraries. Host interpreter defaults
were not changed. Build commands run as UID 1000. No worktree is needed: the
cloud task already has its own workspace.

```sh
cd /workspace/twrp-crumpet-build/src
python3 device/amazon/mt8167-echo/tools/prepare_recovery.py bootable/recovery
python3 device/amazon/mt8167-echo/tests/check_crumpet.py
docker --host=unix:///var/run/docker.sock start twrp-crumpet-u2004
docker --host=unix:///var/run/docker.sock exec -u 1000:1000 twrp-crumpet-u2004 \
  bash /workspace/twrp-crumpet-build/build-crumpet.sh
python3 /workspace/twrp-crumpet-build/inspect-recovery.py \
  out/target/product/crumpet/recovery.img
```

The core preparation script was checked for idempotence. When changing the
profile, move only the generated `out/target/product/crumpet/recovery/root`
aside before rebuilding, so removed files cannot persist in an incremental
ramdisk. Valid object outputs can be reused. The old oversize image and original
generated ramdisk are retained outside the source tree in `failed-artifacts`;
they are not included in this release bundle.

`inspect-recovery.py` accepts `--device-tree /path/to/patched/device-tree` when
used outside this workspace. It reads images offline and performs no device I/O.

## Sync notes

Initial GitHub pack downloads failed with `curl 92` HTTP/2 `INTERNAL_ERROR`.
HTTP/1.1 alone did not resolve all large transfers. Three repositories were
recovered with native filtered Git metadata fetches, official commit-specific
GitHub archives, verification of every blob against its Git object ID, native
`git hash-object` imports, and successful `git fsck`. The normal repo sync and a
final complete local checkout then succeeded. TLS and Git object verification
remained enabled. The SDK ultimately synced normally; its parallel archive was
not needed for the checkout.

The bundle contains the final build log, offline tests, image inspection,
ramdisk inventory, packed fstab, source manifest and patches. It contains no
original device backups or private filesystem contents.
