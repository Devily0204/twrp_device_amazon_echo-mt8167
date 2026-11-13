#!/sbin/sh
set -eu
PATH=/sbin:/system/bin:$PATH
[ "$#" = 2 ] || { echo "Usage: crumpet-backup.sh boot_a|boot_b FILE" >&2; exit 2; }
case "$1" in boot_a|boot_b) ;; *) exit 2 ;; esac
device=$(readlink -f "/dev/crumpet/mtd/$1")
[ ! -e "$2" ] || { echo "Backup destination already exists" >&2; exit 1; }
crumpet-mtd read "$device" 0 15990784 "$2"
