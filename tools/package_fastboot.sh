#!/bin/bash
# Package a self-contained Crumpet TWRP Fastboot download.
#
#   ./package-fastboot.sh --repo <device tree> --recovery <recovery.img> --output <dir>
#
# Steps: install the host build dependencies in a private venv, run the repo
# tests, build the bundle from the recovery image, and zip the bundle together
# with the runner scripts and instructions. The resulting zip is all a second
# PC needs (besides Python 3, pyusb, libusb/WinUSB and adb).
set -euo pipefail

REPO=""
RECOVERY=""
OUT=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --repo) REPO=$(realpath "$2"); shift 2 ;;
    --recovery) RECOVERY=$(realpath "$2"); shift 2 ;;
    --output) OUT=$(realpath -m "$2"); shift 2 ;;
    *) echo "unknown argument: $1" >&2; exit 1 ;;
  esac
done
[[ -n "$REPO" && -n "$RECOVERY" && -n "$OUT" ]] || {
  echo "usage: $0 --repo <device tree> --recovery <recovery.img> --output <dir>" >&2
  exit 1
}
[[ -f "$REPO/tools/build_crumpet_fastboot.py" ]] || { echo "no fastboot tools in $REPO/tools" >&2; exit 1; }
[[ -f "$RECOVERY" ]] || { echo "recovery image not found: $RECOVERY" >&2; exit 1; }

mkdir -p "$OUT"
VENV="$OUT/.venv"
if [[ ! -x "$VENV/bin/python" ]]; then
  python3 -m venv "$VENV"
fi
"$VENV/bin/pip" install --quiet --upgrade pip
"$VENV/bin/pip" install --quiet keystone-engine pyusb
PY="$VENV/bin/python"

echo "== offline tests =="
(cd "$REPO" && "$PY" -m unittest discover -s tests -p "test_fastboot.py")

PKG="$OUT/package"
rm -rf "$PKG"
mkdir -p "$PKG/tools" "$PKG/tests"

echo "== bundle =="
"$PY" "$REPO/tools/build_crumpet_fastboot.py" \
  --recovery "$RECOVERY" --slot b --lk-loader \
  --output "$PKG/crumpet-twrp-b.bundle.bin"

# The bundle and its manifest must stay together; the runner validates both.
cp "$REPO/tools/run_crumpet_fastboot.py" "$REPO/tools/lk_fastboot.py" "$PKG/tools/"
# Rebuild sources so the bundle can also be rebuilt from any recovery.img.
cp "$REPO/tools/build_crumpet_fastboot.py" "$REPO/tools/lk_stage_loader.py" \
   "$REPO/tools/lk_stage_loader.S" "$REPO/tools/lk_stage_minimal.py" \
   "$REPO/tools/lk_stage_minimal.S" "$PKG/tools/"
cp "$REPO/tests/test_fastboot.py" "$PKG/tests/"
cp "$REPO/crumpet/FASTBOOT.md" "$PKG/BUILD.md"

# Optional test fixture image (only if it exists in the source tree).
if [[ -f "$REPO/crumpet-nand-build/recovery.img" ]]; then
  mkdir -p "$PKG/crumpet-nand-build"
  cp "$REPO/crumpet-nand-build/recovery.img" "$PKG/crumpet-nand-build/"
fi

cat > "$PKG/README.md" <<'EOF'
# Crumpet TWRP Fastboot package

Boot TWRP 3.7.0_9-0 on a **stock, locked** Amazon Echo Dot 3rd Gen refresh
(crumpet / MT8167) directly from its stock LK Fastboot. No unlock, no flash,
no BROM, no NAND/preloader/TEE changes. Everything happens in RAM; a power
cycle restores the stock boot path.

## One-time host setup

- Python 3 with `pyusb` (and `keystone-engine` only to rebuild the bundle).
- libusb: `libusb-1.0.dll` on Windows; pass its directory via `--libusb-dir`.
- Windows: bind the Fastboot interface `VID_0BB4&PID_0C01` to WinUSB/libusb
  with Zadig. ADB needs its normal driver for `18D1:D001`.
- `adb` on PATH (or pass `--adb <path>`).

## Run

1. Put the Dot into stock Fastboot (hold Action while applying power).
2. Read-only gates:

   ```powershell
   py tools\run_crumpet_fastboot.py --bundle crumpet-twrp-b.bundle.bin --check
   ```

   Requires `product=CRUMPET`, `unlock_status=false` and LK build
   `6f7225e-20230407_002912`. Do not continue if these do not match: the stage
   addresses are pinned to exactly this LK build.

3. Start TWRP:

   ```powershell
   py tools\run_crumpet_fastboot.py --bundle crumpet-twrp-b.bundle.bin --execute
   ```

   Success = ADB `0123456789ABCDEF`, `state=recovery`, `product=omni_crumpet`,
   `ro.twrp.version=3.7.0_9-0`.

The same bundle works for every crumpet Dot with that exact LK build.

## Rebuild the bundle

```sh
python3 -m pip install keystone-engine
python3 tools/build_crumpet_fastboot.py \
  --recovery <recovery.img> --slot b --lk-loader \
  --output crumpet-twrp-b.bundle.bin
python3 -m unittest discover -s tests -p test_fastboot.py
```

`BUILD.md` documents the full device-tree/recovery build and the Fastboot
layout. `proxmox-vm/package-fastboot.sh` in the device tree produces this
package from a fresh VM build.
EOF

cat > "$PKG/requirements.txt" <<'EOF'
keystone-engine
pyusb
EOF

(cd "$OUT" && rm -f crumpet-twrp-fastboot.zip && \
  python3 -c "import shutil; shutil.make_archive('crumpet-twrp-fastboot', 'zip', 'package')")

echo "== result =="
ls -l "$OUT/crumpet-twrp-fastboot.zip"
sha256sum "$PKG/crumpet-twrp-b.bundle.bin"
echo "package: $OUT/crumpet-twrp-fastboot.zip"
