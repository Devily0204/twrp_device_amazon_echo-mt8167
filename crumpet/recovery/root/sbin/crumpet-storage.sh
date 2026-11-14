#!/sbin/sh

# CRUMPET_STORAGE_ROOT is only for offline checks against a captured proc/sys tree.
root=${CRUMPET_STORAGE_ROOT:-}
PATH="${root}/sbin:/sbin:/system/bin:${PATH}"
log="${root}/tmp/crumpet-storage.log"
mkdir -p "${root}/tmp" "${root}/dev/block/crumpet" "${root}/dev/crumpet/ubi" "${root}/dev/crumpet/mtd" || exit 1
exec > "$log" 2>&1

mode=${1:-}
case "$mode" in ''|--init) ;; *) echo "Expected --init or no arguments"; exit 1 ;; esac
echo "Crumpet NAND inventory; initialization=${mode:-inspect}"
# Remove only our in-memory mappings, so a failed rerun cannot reuse a stale slot.
rm -f "${root}/dev/block/current-system" "${root}/dev/block/crumpet/system_a" "${root}/dev/block/crumpet/system_b"
for name in system_a system_b userdata cache persist; do
    rm -f "${root}/dev/crumpet/ubi/${name}"
done
if [ ! -r "${root}/proc/mtd" ]; then
    echo "Missing /proc/mtd; NAND mapping unavailable."
    exit 1
fi
cat "${root}/proc/mtd"

userdata_mtd=
while read -r device size erase name; do
    index=${device#mtd}; index=${index%:}
    case "$name" in
        '"boot_a"'|'"boot_b"'|'"misc"')
            label=${name#\"}; label=${label%\"}
            if [ -c "${root}/dev/mtd/mtd${index}" ]; then
                ln -sf "${root}/dev/mtd/mtd${index}" "${root}/dev/crumpet/mtd/${label}" || exit 1
            fi
            ;;
    esac
    case "$name" in
        '"userdata"')
            if [ -n "$userdata_mtd" ]; then
                echo "Duplicate userdata MTD; refusing ambiguous mapping."
                exit 1
            fi
            userdata_mtd=${device#mtd}; userdata_mtd=${userdata_mtd%:}
            ;;
    esac
done < "${root}/proc/mtd"
case "$userdata_mtd" in
    ''|*[!0-9]*) echo "No unique userdata MTD found."; exit 1 ;;
esac

if [ "$mode" = --init ]; then
    "${root}/sbin/crumpet-ubi" attach persist || exit 1
    "${root}/sbin/crumpet-ubi" attach userdata || exit 1
fi
persist_mtd=
while read -r device size erase name; do
    if [ "$name" = '"persist"' ]; then
        [ -z "$persist_mtd" ] || exit 1
        persist_mtd=${device#mtd}; persist_mtd=${persist_mtd%:}
    fi
done < "${root}/proc/mtd"

for volume in "${root}"/sys/class/ubi/ubi*_*; do
    [ -r "$volume/name" ] || continue
    name=$(cat "$volume/name")
    id=${volume##*/ubi}
    parent=${id%_*}
    [ -r "${root}/sys/class/ubi/ubi${parent}/mtd_num" ] || continue
    mtd=$(cat "${root}/sys/class/ubi/ubi${parent}/mtd_num")
    echo "ubi${id}: name=${name} mtd=${mtd} type=$(cat "$volume/type" 2>/dev/null)"
    case "$name" in
        system_a|system_b|userdata|cache) [ "$mtd" = "$userdata_mtd" ] || continue ;;
        persist) [ "$mtd" = "$persist_mtd" ] || continue ;;
        *) continue ;;
    esac
    character="${root}/dev/ubi${id}"
    if [ "$mode" = --init ]; then
        "${root}/sbin/crumpet-ubi" volume "/dev/ubi${id}" || exit 1
    fi
    if [ -c "$character" ]; then
        ln -s "$character" "${root}/dev/crumpet/ubi/${name}" || exit 1
    fi
    case "$name" in system_a|system_b) ;; *) continue ;; esac
    if [ "$mode" = --init ]; then
        if ! "${root}/sbin/crumpet-ubi" block "/dev/ubi${id}"; then
            echo "system volume ${name} has no usable block mapping; keeping its image-restore endpoint."
            continue
        fi
    fi
    # SquashFS is read-only even when its containing UBI volume is dynamic.
    # Legacy ubiblock rejects write opens without necessarily setting sysfs ro.
    block="${root}/dev/block/ubiblock${id}"
    if [ -b "$block" ]; then
        ln -s "$block" "${root}/dev/block/crumpet/${name}" || exit 1
    else
        echo "Missing ubiblock${id}; filesystem mount unavailable."
    fi
done

slot=$("${root}/sbin/crumpet-active-slot.sh") || exit 1
slot="_${slot}"
case "$slot" in
    _a|_b)
        system="${root}/dev/block/crumpet/system${slot}"
        if [ -b "$system" ]; then
            ln -s "$system" "${root}/dev/block/current-system" || exit 1
            echo "Selected system${slot} from the boot-reported slot."
        else
            echo "system${slot} is not exposed as an existing ubiblock device."
        fi
        ;;
    *) echo "Unknown boot slot; refusing to default to slot A." ;;
esac
