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
export PYTHONDONTWRITEBYTECODE=1

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
(cd "$REPO" && CRUMPET_RECOVERY="$RECOVERY" "$PY" -m unittest discover -s tests -p 'test_*.py')

# Use a fresh staging directory; publish only a completely built zip.
PKG=$(mktemp -d "$OUT/package.XXXXXXXX")
trap 'rm -rf -- "$PKG"' EXIT
mkdir -p "$PKG/tools" "$PKG/tests"
"$PY" "$REPO/tools/build_crumpet_fastboot.py" \
  --recovery "$RECOVERY" --slot b --lk-loader \
  --output "$PKG/crumpet-twrp-b.bundle.bin"
# Capture original per-device identity in RAM before the recovery handoff.
"$PY" "$REPO/tools/device_data.py" --bundle "$PKG/crumpet-twrp-b.bundle.bin"

for tool in run_crumpet_fastboot.py lk_fastboot.py build_crumpet_fastboot.py \
  lk_stage_loader.py lk_stage_loader.S lk_stage_minimal.py lk_stage_minimal.S \
  install_bootchain.py build_bootchain.py device_data.py firmware_formats.py \
  firmware_profile.py firmware_dtb.py slot_loader.py lk_bootargs.py; do
  cp "$REPO/tools/$tool" "$PKG/tools/"
done
cp -r "$REPO/assets" "$PKG/"
cp "$REPO/tests/test_fastboot.py" "$REPO/tests/test_bootchain.py" "$PKG/tests/"
cp "$REPO/crumpet/HOST-INSTALLER.md" "$PKG/README.md"
cp "$REPO/crumpet/FASTBOOT.md" "$PKG/BUILD.md"
printf 'keystone-engine\npyusb\n' > "$PKG/requirements.txt"
# Bind the host installer to the native helper in this exact VM recovery.
PYTHONPATH="$REPO/tools" "$PY" - "$RECOVERY" "$PKG/recovery-tools.json" <<'PY'
import gzip, hashlib, json, sys
from pathlib import Path
import build_crumpet_fastboot as b
image = Path(sys.argv[1]).read_bytes()
raw = gzip.decompress(b.extract_ramdisk(image))
entries = {name: raw[offset:offset+length] for name, offset, length in b.cpio_entries(raw)}
helper = entries['sbin/crumpet-bootchain']
assert helper[:4] == b'\x7fELF'
Path(sys.argv[2]).write_text(json.dumps({
    'recovery_sha256': hashlib.sha256(image).hexdigest(),
    'crumpet-bootchain_sha256': hashlib.sha256(helper).hexdigest()
}, indent=2) + '\n')
PY
(cd "$PKG" && "$PY" -m unittest discover -s tests -p 'test_*.py')
"$PY" - "$PKG" "$OUT/crumpet-twrp-fastboot" <<'PY'
import hashlib, shutil, sys
from pathlib import Path
archive = Path(shutil.make_archive(sys.argv[2] + '.new', 'zip', sys.argv[1]))
final = Path(sys.argv[2] + '.zip')
archive.replace(final)
final.with_suffix('.zip.sha256').write_text(hashlib.sha256(final.read_bytes()).hexdigest() + '  ' + final.name + '\n')
print(final)
PY
