# Build from clean main with local changes

VM: `crumpet-build` (106), `192.168.0.92`. Run on the VM:

```sh
cd /workspace/twrp-crumpet-build
./build.sh
```

The device tree is on the original `main` at `f79b0e5`; the Recovery Core
is at its original base `da9eb6c`. Both indexes are clean. The build changes
from the Cloud and Windows are in the working trees. No new commits were
created. Existing branches and pre-build patches are retained under
`artifacts/pre-uncommitted.P3ZhNYah/`.

The rebuild finished successfully after the previous generated ramdisk was moved
aside. All three Init configurations and the packed image passed inspection.
The VM suite passed 22 tests and skipped three that need the original backups
or OTA. The output is `artifacts/recovery.img` on the VM; the image, checksum
and log here are copies. No device was flashed.

Prior build images, manifests, logs and scripts from the Windows build folder
were saved separately on the VM in `artifacts/windows-build-evidence.tar.gz`.
IDE settings, caches and the separate bootloader exploit workflow are not part
of this source or artifact migration.
