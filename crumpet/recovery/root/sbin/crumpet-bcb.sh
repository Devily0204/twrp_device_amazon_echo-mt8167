#!/sbin/sh
# Explicit Android command/status/recovery record I/O. Never run during startup.
# Resolve the stock rolling NAND record; never consume stale --wipe_data commands.
set -eu
PATH=/sbin:/system/bin:$PATH
device=$(readlink -f /dev/crumpet/mtd/misc)
[ "$#" -ge 1 ] || exit 2
case "$1" in
    backup)
        [ "$#" = 2 ] || exit 2
        crumpet-mtd bcb-read "$device" "$2"
        sha256sum "$2"
        ;;
    clear|restore)
        temp=$(mktemp -d /tmp/crumpet-bcb.XXXXXX)
        trap 'rm -f "$temp/record"; rmdir "$temp"' EXIT
        if [ "$1" = clear ]; then
            [ "$#" = 1 ] || exit 2
            dd if=/dev/zero of="$temp/record" bs=832 count=1 2>/dev/null
        else
            [ "$#" = 3 ] && [ "$(wc -c < "$2")" = 832 ] || exit 2
            hash=$(sha256sum "$2"); hash=${hash%% *}
            [ "$hash" = "$3" ] || { echo "BCB record SHA256 mismatch" >&2; exit 1; }
            cp "$2" "$temp/record"
        fi
        crumpet-mtd bcb-write "$device" "$temp/record"
        ;;
    status)
        [ "$#" = 1 ] || exit 2
        crumpet-mtd bcb-status "$device"
        ;;
    activate|mark-success)
        [ "$#" = 2 ] || exit 2
        case "$2" in a|b) ;; *) exit 2 ;; esac
        if [ "$1" = mark-success ]; then
            [ "$(getprop ro.boot.slot_suffix)" = "_$2" ] || exit 1
            crumpet-mtd bcb-success "$device" "$2"
        else
            crumpet-mtd bcb-activate "$device" "$2"
        fi
        ;;
    *) echo "Usage: crumpet-bcb.sh status | backup FILE | clear | restore FILE SHA256 | activate a|b | mark-success a|b" >&2; exit 2 ;;
esac
