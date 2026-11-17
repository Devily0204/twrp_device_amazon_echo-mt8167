#!/bin/bash
set -euo pipefail
cd -- "$(dirname -- "$0")"
exec 9>.build.lock
flock -n 9 || { echo "Crumpet build already running" >&2; exit 1; }
sudo docker start twrp-crumpet-u2004 >/dev/null
sudo docker exec twrp-crumpet-u2004 bash /workspace/twrp-crumpet-build/build-crumpet-local.sh
python3 src/device/amazon/mt8167-echo/tools/inspect_recovery.py src/out/target/product/crumpet/recovery.img
cp src/out/target/product/crumpet/recovery.img artifacts/recovery.img
sha256sum artifacts/recovery.img >artifacts/SHA256SUMS

# The host installer and Fastboot bundle are required build artifacts.
src/device/amazon/mt8167-echo/tools/package_fastboot.sh \
    --repo src/device/amazon/mt8167-echo \
    --recovery artifacts/recovery.img \
    --output artifacts/fastboot

