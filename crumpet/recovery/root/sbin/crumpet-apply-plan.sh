#!/sbin/sh
# Full images only. No bootloader/TEE/preloader update. Activation is explicit.
set -eu
root=${CRUMPET_STORAGE_ROOT:-}
PATH="${root}/sbin:/sbin:/system/bin:${PATH}"
[ "$#" = 1 ] || { [ "$#" = 2 ] && [ "$2" = --activate ]; } || { echo "Usage: crumpet-apply-plan.sh DIRECTORY [--activate]" >&2; exit 2; }
activate=${2:-}
cd "$1"
target=$(cat target-slot)
active=$("${root}/sbin/crumpet-active-slot.sh")
active="_${active}"
case "$target:$active" in a:_b|b:_a) ;; *) echo "Plan is not for the inactive slot" >&2; exit 1 ;; esac
# Verify the complete pair before touching either target. The preparer creates
# exactly these two entries, never caller-supplied shell commands or partitions.
system_hash=
boot_hash=
while read -r hash name extra; do
    [ -z "$extra" ] && [ "${#hash}" = 64 ] || exit 2
    case "$hash" in *[!0-9a-f]*) exit 2 ;; esac
    case "$name" in
        system.img) [ -z "$system_hash" ] || exit 2; system_hash=$hash ;;
        boot.img) [ -z "$boot_hash" ] || exit 2; boot_hash=$hash ;;
        *) exit 2 ;;
    esac
done < SHA256SUMS
[ -n "$system_hash" ] && [ -n "$boot_hash" ] || exit 2
actual=$(sha256sum system.img); [ "${actual%% *}" = "$system_hash" ] || exit 1
actual=$(sha256sum boot.img); [ "${actual%% *}" = "$boot_hash" ] || exit 1
sh "${root}/sbin/crumpet-flash.sh" "system_${target}" "$PWD/system.img" "$system_hash"
sh "${root}/sbin/crumpet-flash.sh" "boot_${target}" "$PWD/boot.img" "$boot_hash"
if [ "$activate" = --activate ]; then
    sh "${root}/sbin/crumpet-bcb.sh" activate "$target"
    echo "Both images verified; redundant BCB now selects slot ${target} for the next boot."
else
    echo "Inactive slot ${target} verified. No activation performed; the active slot is unchanged."
fi
