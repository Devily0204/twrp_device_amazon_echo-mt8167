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
a=$(printf "%d" "0x$a")
b=$(printf "%d" "0x$b")
# The active slot is chosen by priority (low nibble), not by the raw byte.
pa=$((a & 0x0f)); pb=$((b & 0x0f))
if [ "$pa" -gt "$pb" ]; then
  echo a
elif [ "$pb" -gt "$pa" ]; then
  echo b
else
  # Equal priority: fall back to the remaining tries/success nibble.
  ta=$((a >> 4)); tb=$((b >> 4))
  if [ "$ta" -gt "$tb" ]; then
    echo a
  elif [ "$tb" -gt "$ta" ]; then
    echo b
  else
    echo "Ambiguous BCB slot state" >&2; exit 1
  fi
fi
