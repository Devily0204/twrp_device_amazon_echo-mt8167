#!/sbin/sh
# Resolve the active slot from the redundant boot control block (BCB), not from
# the boot-reported property. Fastboot RAM boots can inject a fabricated
# androidboot.slot_suffix; the BCB remains the authoritative record.
set -eu
root=${CRUMPET_STORAGE_ROOT:-}
PATH="${root}/sbin:/sbin:/system/bin:${PATH}"
status=$("${root}/sbin/crumpet-mtd" bcb-status "${root}/dev/mtd/mtd9") || exit 1
a=$(printf '%s\n' "$status" | sed -n 's/.*slot_a=\([0-9a-f]*\).*/\1/p')
b=$(printf '%s\n' "$status" | sed -n 's/.*slot_b=\([0-9a-f]*\).*/\1/p')
[ -n "$a" ] && [ -n "$b" ] || { echo "BCB slot state unavailable" >&2; exit 1; }
a=$((16#$a)); b=$((16#$b))
[ "$a" != "$b" ] || { echo "Ambiguous BCB slot state" >&2; exit 1; }
if [ "$a" -gt "$b" ]; then echo a; else echo b; fi
