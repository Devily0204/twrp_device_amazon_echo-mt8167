# Crumpet build VM (2026-11-13)

Proxmox VM **106**, `crumpet-build`, DHCP address `192.168.0.92`.
Debian 13 amd64, 4 vCPUs, 8 GiB RAM, 6 GiB guest swap, 160 GiB thin disk on
`local-lvm`. The original VMs were left running with their existing settings.

From Windows PowerShell:

```powershell
ssh -i "$env:USERPROFILE/.ssh/crumpet_build_vm_ed25519" -o "UserKnownHostsFile=$env:USERPROFILE/.ssh/crumpet_build_vm_known_hosts" builder@192.168.0.92
```

On the VM:

```sh
cd /workspace/twrp-crumpet-build
./build.sh
```

The wrapper runs `mka -j4 recoveryimage` in Ubuntu 20.04 with Java 8 and
Python 2.7, verifies the installed Init configs and inspects the actual image
with Debian's Python 3. The verified result is `artifacts/recovery.img`.
This directory contains a local copy of the VM result and its build log and
SHA-256. Image size: 15,767,552 bytes (limit 15,990,784). No device was flashed.

The Ubuntu base is pinned by digest in `Dockerfile.cloud`. All 176 added or
changed package versions exported by the Cloud build match the VM container.
The container is built locally; it is not a byte-for-byte export of Docker's
writable layer.

All 227 managed Git project paths and HEADs match the Cloud source snapshot.
The Recovery Core is the original local Cloud commit
`c01e81879d01041fc4751bc6d842389b64e141d4`, verified and imported from
`cloud-source/recovery-local-commits.bundle`. A local mirror on the VM keeps
that commit available for subsequent `repo sync`. The original device-tree
commit is `1a164f8b77fe443e0c40838d6b6759c0346ec69b` on branch
`crumpet-nand-support`.

The active branch `codex/crumpet-local` adds the local MTD fsync adjustment,
Init configuration copy rules, the two local Init configs plus the Nano
config found missing on a fresh build, and the updated checks. It retains the
Cloud branch as its parent. Neither branch was pushed. Fastboot tooling is
not part of this recoveryimage build. No private backups, external bootloader
payloads or new vendor binaries were migrated.

Validation: 22 tests passed and 3 backup/OTA checks were skipped on the VM
because those input files were not copied. All 11 reachable Init configs,
5 service executables, and 575 dependencies of 101 ARM32 ELF files pass
offline inspection. Hardware boot and NAND operation were not tested.

The older images and build archives in the Windows checkout were left in place.
The VM output is a new artifact, not a replacement for the cloud reference.
