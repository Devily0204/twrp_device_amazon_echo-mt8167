#!/bin/bash
set -eo pipefail
cd /workspace/twrp-crumpet-build/src
export JAVA_HOME=/usr/lib/jvm/java-8-openjdk-amd64
export PATH="$JAVA_HOME/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
export LANG=C LC_ALL=C
export BUILD_USERNAME=builder BUILD_HOSTNAME=crumpet-cloud
java -version
python --version
. build/envsetup.sh
set +e
lunch omni_crumpet-eng
lunch_result=$?
set -e
if [ "$lunch_result" -ne 0 ] || [ "${TARGET_PRODUCT:-}" != omni_crumpet ]; then
    echo "ERROR: lunch omni_crumpet-eng failed (exit $lunch_result)" >&2
    exit 1
fi
if [ "$(get_build_var TARGET_ARCH)" != arm ]; then
    echo 'ERROR: target architecture is not 32-bit ARM' >&2
    exit 1
fi
mka -j4 recoveryimage
test -s out/target/product/crumpet/recovery.img

# Verify the regular product-copy path, not an offline post-build repack.
for name in init.recovery.logd.rc init.recovery.nano.rc init.recovery.vold_decrypt.rc; do
    source_file="device/amazon/mt8167-echo/crumpet/recovery/root/$name"
    output_file="out/target/product/crumpet/recovery/root/$name"
    test -s "$output_file" || {
        echo "ERROR: missing init import config: $output_file" >&2
        exit 1
    }
    cmp "$source_file" "$output_file" || {
        echo "ERROR: init import config differs from overlay: $name" >&2
        exit 1
    }
    echo "VERIFIED: $name"
done
python3 device/amazon/mt8167-echo/tools/inspect_recovery.py out/target/product/crumpet/recovery.img
cp out/target/product/crumpet/recovery.img /workspace/twrp-crumpet-build/artifacts/recovery.img
sha256sum /workspace/twrp-crumpet-build/artifacts/recovery.img
