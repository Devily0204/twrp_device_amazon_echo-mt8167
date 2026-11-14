# Standalone Crumpet TWRP build and Fastboot start

This device tree builds TWRP with Omni `recoveryimage` and packages the result
for the locked LK Fastboot. **The exploit does not require an LK baseline,
Amazon rescue ramdisk, custom binary stage or an external crumpet.py.**

## Components and their provenance

| Component | Generated from |
| --- | --- |
| Recovery kernel | `recovery.img`, built by Omni from the prebuilt `zImage-dtb` already in this repository |
| PVT FDT | FDT #3 (`amzn,crumpet-pvt`) appended to that zImage |
| TWRP initramfs | gzip/newc ramdisk from `recovery.img` |
| MTK kernel header | `mtk_header()` in `tools/build_crumpet_fastboot.py` (magic, length, name, padding) |
| LK-facing dummy initramfs | `minimal_ramdisk()` in the same Python source: a 162-byte valid gzip/newc archive |
| Handoff | Project-owned `tools/lk_stage_minimal.S`, assembled by `lk_stage_minimal.py`; USB code in `lk_fastboot.py` |
| Full LK loader handoff | `--lk-loader`: project-owned `tools/lk_stage_loader.S`, assembled by `lk_stage_loader.py`; reproduces the DPC worker, loader prolog/initializer/DTB hooks and the full kernel-entry path |

The real initramfs is at `0x45990000` (bundle offset `0x990000`). Its size and
end are computed from the actual gzip data. `/chosen/bootargs` and the 249 MiB
memory limit are patched into the appended PVT FDT.

The small dummy initramfs is **not** the TWRP ramdisk and contains no rescue
binaries. It exists only because the LK boot header describes a ramdisk. The
default handoff is a project-owned minimal Thumb stage: cache maintenance,
copying the zImage from `0x45000A00` in LK's Fastboot buffer to its expected
physical address `0x40008000`, publishing the FDT/initramfs from data cache,
entering SVC mode with a dedicated stack, performing the proven LK USB/timer/
watchdog cleanup, preserving the LK SCTLR/MMU state, invalidating the
instruction cache, r0/r1 = 0, and FDT pointer `0x45980000`.
For comparison, `--stage dpc` first queues the same handoff as an LK deferred
procedure call and executes it in the DPC worker context. `--stage direct`
skips the project stage and invokes LK's own loader at `0x41E1D585`. A
different LK revision must not be used without fresh address and hardware
validation.

## 1. Build TWRP on the build VM

The optional existing Ubuntu build environment is driven by
`crumpet-bundle/proxmox-vm/build.sh`
runs `mka recoveryimage`, verifies `init.recovery.logd.rc`,
`init.recovery.nano.rc` and `init.recovery.vold_decrypt.rc`, and writes
`artifacts/recovery.img`. These three imports are produced by the normal
`PRODUCT_COPY_FILES` path, not a post-build repack.

Check the result from this repository:

```sh
python3 tools/inspect_recovery.py out/target/product/crumpet/recovery.img
```

## 2. Build the Fastboot bundle (host only, no USB)

Python 3's standard library covers payload extraction and headers. The
project-owned Thumb stage additionally needs the `keystone` host package. No
binary stage is downloaded or bundled.

```sh
python3 tools/build_crumpet_fastboot.py \
  --recovery out/target/product/crumpet/recovery.img \
  --slot b \
  --output out/fastboot/crumpet-twrp-b.bundle.bin \
  --parts-dir out/fastboot/parts
```

`--slot a|b` only changes the recovery cmdline; it is not a BCB operation.
`--parts-dir` additionally exports `ramdisk.bin`, `fdt.dtb` and
`dummy-initramfs.gz`. These are build outputs, not source inputs.

## 2b. Package for another PC

`tools/package_fastboot.sh` runs the whole host-side
pipeline in one step: it creates a private venv with `keystone-engine` and
`pyusb`, runs the offline tests, builds the bundle and zips the bundle, the
runner, the rebuild sources, the tests and a README into
`crumpet-twrp-fastboot.zip`. `build.sh` calls it automatically after a
successful VM build when the fastboot tools are present in the device tree.

```sh
tools/package_fastboot.sh \
  --repo /path/to/device-tree \
  --recovery artifacts/recovery.img \
  --output artifacts/fastboot
```

The zip is the only artifact a second PC needs (plus Python 3, pyusb,
libusb/WinUSB and adb). The bundle is built from the recovery image on the
host; it is not downloaded to the device.

The stage hash must be identical on every host
(`33e0b99370e1576667e8237e67a87c34c5d148ebccea5d1c40bc1bb2338a9a07`).
The bundle hash may differ between Python/zlib versions because gzip output
varies; that is cosmetic, not a functional difference. The tests validate
properties of the payload instead of exact gzip bytes.


Use `--fsync-fix` only for an older ramdisk; the current C source already
contains the MTD `fsync()` compatibility fix.

## 3. Check the locked Fastboot (read-only)

Host requirements: Python 3, PyUSB, libusb and adb. These are host tools, not
Amazon payloads or binaries committed to the device tree. `--libusb-dir` names
the directory containing the host's `libusb-1.0.dll` on Windows; `--adb` can
point to the adb executable when it is not on `PATH`.

```powershell
py tools\run_crumpet_fastboot.py `
  --bundle out\fastboot\crumpet-twrp-b.bundle.bin `
  --libusb-dir C:\path\to\libusb `
  --check
```

The check reads product `CRUMPET`, lock state `false`, LK
`6f7225e-20230407_002912`, and the maximum download size, and verifies the
bundle and manifest. It sends no exploit packets.

## 4. Start TWRP (volatile RAM operation)

Put the **test device** into its documented Fastboot mode first. The tool does
not operate another device or perform a Taster/power sequence itself.

```powershell
py tools\run_crumpet_fastboot.py `
  --bundle out\fastboot\crumpet-twrp-b.bundle.bin `
  --libusb-dir C:\path\to\libusb `
  --adb C:\path\to\adb.exe `
  --execute `
  --log out\fastboot\run-independent.json
```

The runner downloads to RAM and enters the stage selected by the bundle
manifest (default `0x45971001`, optionally LK's direct loader at `0x41E1D585`).
The overflow uses eight separate 512-byte USB transfers followed by one
2048-byte transfer; coalescing the first eight packets is not equivalent.
No flash, unlock, BROM, preloader/TEE change or shell-kit installation occurs.
A power cycle restores the stock boot path. TWRP is accepted only with
`state=recovery`, `device=crumpet`, product `omni_crumpet` or `crumpet`,
serial `0123456789ABCDEF` and `ro.twrp.version=3.7.0_9-0`.

The direct-loader path is a new independent implementation. Test results and
any subsequent stage work must be recorded separately; a successful baseline
or a successful build alone is not a hardware success claim.

## Diagnostic reset canaries (not a normal TWRP start)

To bisect the handoff without relying on UART,
`--canary entry|copy|cache|mmu|verify|cleanup` inserts a project-owned watchdog reset
at that milestone instead of entering Linux. The milestones are SVC entry,
completed kernel copy, completed cache publication, and completed handoff
preparation at the MMU-state checkpoint. `verify` resets only after also
validating the copied zImage magic, FDT magic and TWRP gzip signature;
`cleanup` additionally proves that LK's USB stop, timer cancel, watchdog
disable and stock LED call all returned. Example:

```powershell
py tools\build_crumpet_fastboot.py `
  --recovery out\target\product\crumpet\recovery.img `
  --slot b --canary mmu `
  --output out\fastboot\crumpet-twrp-b-canary-mmu.bundle.bin
```

Run that bundle with the normal `--execute` command and a short `--timeout`.
A reached canary resets the SoC and the host observes preloader USB
`0E8D:2000`; the runner's subsequent TWRP wait is expected to fail. Start with
`mmu`, then move backwards to `cache`, `copy`, or `entry` only when the later
milestone is not reached. Canary bundles are diagnostic artifacts and must not
be used for installation.

## Offline tests

```sh
python3 -m unittest discover -s tests -p "test_fastboot.py" -v
```

The tests validate recovery/FDT extraction, generated MTK header, the valid
tiny newc dummy, bundle layout and SHA-256/ID, direct-loader packets, manifest
rejection of persistent operations, and the TWRP ADB identity. They do not use
an external baseline or a rescue payload.

## Distribution note

The builder and protocol implementation are source code in this repository.
Do not add baseline bundles, rescue ramdisks, binary stages, generated recovery
images or other vendor binaries. The historical prebuilt kernel and its
provenance still require the separate license review described in the project
documentation; that history is not changed by the new builder.
