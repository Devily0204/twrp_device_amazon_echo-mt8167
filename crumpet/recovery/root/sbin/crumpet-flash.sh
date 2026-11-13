#!/sbin/sh
set -eu
root=${CRUMPET_STORAGE_ROOT:-}
PATH="${root}/sbin:/sbin:/system/bin:${PATH}"
[ "$#" -ge 2 ] && [ "$#" -le 3 ] || { echo "Usage: crumpet-flash.sh TARGET IMAGE [SHA256]" >&2; exit 2; }
target=$1
image=$2
[ -f "$image" ] || exit 2
hash=$(sha256sum "$image"); hash=${hash%% *}
if [ "$#" = 3 ]; then
    [ "${#3}" = 64 ] || exit 2
    case "$3" in *[!0-9a-f]*) exit 2 ;; esac
    [ "$hash" = "$3" ] || { echo "Image SHA256 mismatch" >&2; exit 1; }
fi

case "$target" in
    boot_a|boot_b|system_a|system_b)
        slot=$(getprop ro.boot.slot_suffix)
        case "$slot" in _a|_b) ;; *) echo "Unknown active slot" >&2; exit 1 ;; esac
        [ "_${target##*_}" != "$slot" ] || { echo "Refusing to overwrite active slot" >&2; exit 1; }
        ;;
    userdata|cache) ;;
    *) echo "Unsupported image target" >&2; exit 2 ;;
esac

case "$target" in
    boot_a|boot_b)
        [ "$(dd if="$image" bs=8 count=1 2>/dev/null)" = 'ANDROID!' ] || { echo "Not an Android boot image" >&2; exit 1; }
        bytes=$(wc -c < "$image")
        [ "$bytes" -ge 2048 ] && [ "$bytes" -le 15990784 ] || exit 1
        device=$(readlink -f "${root}/dev/crumpet/mtd/${target}")
        [ "$(cat "${root}/sys/class/mtd/${device##*/}/name")" = "$target" ] || { echo "MTD alias points to another partition" >&2; exit 1; }
        "${root}/sbin/crumpet-mtd" write "$device" 0 "$image"
        ;;
    *)
        device=$(readlink -f "${root}/dev/crumpet/ubi/${target}")
        [ "$(cat "${root}/sys/class/ubi/${device##*/}/name")" = "$target" ] || { echo "UBI alias points to another volume" >&2; exit 1; }
        "${root}/sbin/crumpet-ubi" update "$device" "$image"
        ;;
esac
echo "$target: source SHA256 $hash; device readback verified"
