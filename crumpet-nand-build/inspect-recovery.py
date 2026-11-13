"""Offline inspection of an Android v0 recovery image and its newc ramdisk."""
import gzip
import argparse
import hashlib
import pathlib
import posixpath
import stat
import struct
import sys

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('image', type=pathlib.Path)
parser.add_argument('--device-tree', type=pathlib.Path, default=pathlib.Path('/workspace/twrp-crumpet-build/src/device/amazon/mt8167-echo'))
args = parser.parse_args()
path = args.image
data = path.read_bytes()
assert data[:8] == b"ANDROID!", "Missing Android boot image header"
ksize, kaddr, rsize, raddr, ssize, saddr, tags, page = struct.unpack_from("<8I", data, 8)
assert page == 2048, f"Unexpected page size: {page}"
assert len(data) <= 15990784, "Image exceeds Crumpet boot-partition-sized artifact ceiling"
assert (kaddr, raddr, saddr, tags) == (0x40008000, 0x44000000, 0x40F00000, 0x4E000000), "Boot addresses differ from stock Crumpet"
assert ssize == 0, "Unexpected second-stage payload"
assert b"bootopt=64S3,32N2,32N2" in data[64:576], "Wrong Crumpet bootopt"
assert hashlib.sha256(data[page:page + ksize]).hexdigest() == "0ea78c5250d4c431b3856a8079d47cf0de0ff2e267b4d53536889715f26a540d", "Prebuilt kernel changed"
align = lambda size: (size + page - 1) // page * page
offset = page + align(ksize)
ramdisk = gzip.decompress(data[offset:offset + rsize])
files = {}
modes = {}
offset = 0
while True:
    header = ramdisk[offset:offset + 110]
    assert header[:6] in (b"070701", b"070702"), "Invalid newc ramdisk"
    fields = [int(header[6 + i * 8:14 + i * 8], 16) for i in range(13)]
    length, namesize = fields[6], fields[11]
    start = offset + 110
    name = ramdisk[start:start + namesize - 1].decode()
    start = (start + namesize + 3) & ~3
    payload = ramdisk[start:start + length]
    if name == "TRAILER!!!":
        break
    name = name.removeprefix("./")
    files[name] = payload
    modes[name] = fields[1]
    offset = (start + length + 3) & ~3

required = [
    "sbin/recovery", "sbin/crumpet-storage.sh", "sbin/crumpet-backup.sh",
    "sbin/crumpet-flash.sh", "sbin/crumpet-bcb.sh", "sbin/crumpet-apply-plan.sh",
    "sbin/crumpet-ubi", "sbin/crumpet-mtd",
    "sbin/led.sh", "sbin/led-common.sh", "sbin/led-anim.sh", "sbin/led-status.sh",
    "etc/recovery.fstab", "init.recovery.mt8167.rc",
    "init.recovery.leds.rc", "init.recovery.led-service.rc",
]
for name in required:
    assert name in files and files[name], f"Missing or empty ramdisk entry: {name}"
    print(f"PRESENT {name}: {len(files[name])} bytes")
for name in ("sbin/recovery", "sbin/crumpet-ubi", "sbin/crumpet-mtd"):
    elf = files[name]
    assert elf[:4] == b"\x7fELF", f"Not ELF: {name}"
    assert elf[4] == 1 and struct.unpack_from("<H", elf, 18)[0] == 40, f"Not 32-bit ARM: {name}"
    print(f"ARM32 {name}")
    if name != 'sbin/recovery':
        phoff = struct.unpack_from('<I', elf, 28)[0]
        phentsize, phnum = struct.unpack_from('<HH', elf, 42)
        headers = [struct.unpack_from('<8I', elf, phoff + i * phentsize) for i in range(phnum)]
        assert all(h[0] != 3 for h in headers), f"Helper has an ELF interpreter: {name}"
        for h in headers:
            if h[0] == 2:
                tags = [struct.unpack_from('<II', elf, h[1] + i)[0] for i in range(0, h[4], 8)]
                assert 1 not in tags, f"Helper has shared-library dependencies: {name}"
        print(f"STATIC {name}")
assert b"LED_COUNT=12" in files["sbin/led.sh"], "Wrong model LED script"
assert b"/sbin/led-status.sh" in files["sbin/recovery"], "Missing recovery status hook"
assert b"/sbin/crumpet-flash.sh" in files["sbin/recovery"], "Missing NAND recovery-core integration"
assert b"ubifs" in files["sbin/recovery"], "Missing UBIFS recovery-core support"
for name in ("sbin/amzn_bcbtool", "sbin/update_engine_sideload", "sbin/e2fsck-data.sh", "sbin/slot-symlinks.sh", "sbin/bcbfix", "sbin/bcbtool", "sbin/resetprop", "fstab.device"):
    assert name not in files, f"Unexpected legacy eMMC asset: {name}"
    print(f"ABSENT {name}")
tree = args.device_tree
for overlay in ('leds', 'crumpet'):
    root = tree / overlay / 'recovery/root'
    for source in root.rglob('*'):
        if source.is_file():
            name = source.relative_to(root).as_posix()
            assert files.get(name) == source.read_bytes(), f"Packed file differs from {overlay} overlay: {name}"

def resolve(name):
    seen = set()
    while name in files and stat.S_ISLNK(modes[name]):
        assert name not in seen, f"Symlink cycle: {name}"
        seen.add(name)
        target = files[name].decode()
        name = target.lstrip('/') if target.startswith('/') else posixpath.normpath(posixpath.join(posixpath.dirname(name), target))
    assert name in files, f"Missing symlink target: {name}"
    return files[name]

elf_count, dependency_count = 0, 0
missing_dependencies = []
loader = files['sbin/linker']
loader_phoff = struct.unpack_from('<I', loader, 28)[0]
loader_phsize, loader_phnum = struct.unpack_from('<HH', loader, 42)
loader_headers = [struct.unpack_from('<8I', loader, loader_phoff + i * loader_phsize) for i in range(loader_phnum)]
loader_dynamic = [(tag, value) for h in loader_headers if h[0] == 2 for tag, value in [struct.unpack_from('<II', loader, h[1] + i) for i in range(0, h[4], 8)]]
loader_straddr = next(value for tag, value in loader_dynamic if tag == 5)
loader_load = next(h for h in loader_headers if h[0] == 1 and h[2] <= loader_straddr < h[2] + h[4])
loader_soname_offset = loader_load[1] + loader_straddr - loader_load[2] + next(value for tag, value in loader_dynamic if tag == 14)
assert loader[loader_soname_offset:loader.index(b'\0', loader_soname_offset)] == b'ld-android.so', 'Unexpected Android linker SONAME'
for name, elf in files.items():
    if not elf.startswith(b'\x7fELF'):
        continue
    elf_count += 1
    assert elf[4:6] == b'\x01\x01' and struct.unpack_from('<H', elf, 18)[0] == 40, f"Wrong packed ELF architecture: {name}"
    phoff = struct.unpack_from('<I', elf, 28)[0]
    phentsize, phnum = struct.unpack_from('<HH', elf, 42)
    headers = [struct.unpack_from('<8I', elf, phoff + i * phentsize) for i in range(phnum)]
    dynamic = [(tag, value) for h in headers if h[0] == 2 for tag, value in [struct.unpack_from('<II', elf, h[1] + i) for i in range(0, h[4], 8)]]
    needed = [value for tag, value in dynamic if tag == 1]
    if not needed:
        continue
    straddr = next(value for tag, value in dynamic if tag == 5)
    load = next(h for h in headers if h[0] == 1 and h[2] <= straddr < h[2] + h[4])
    string_offset = load[1] + straddr - load[2]
    for value in needed:
        start = string_offset + value
        lib = elf[start:elf.index(b'\0', start)].decode()
        candidates = [prefix + lib for prefix in ('sbin/', 'lib/', 'system/lib/', 'system_root/system/lib/')]
        # Bionic's loader supplies its own SONAME without a separate .so file.
        if lib == 'ld-android.so':
            candidates.insert(0, 'sbin/linker')
        found = next((p for p in candidates if p in files), None)
        if not found or not resolve(found).startswith(b'\x7fELF'):
            missing_dependencies.append((name, lib))
        dependency_count += 1
for name, lib in missing_dependencies:
    print(f"MISSING DT_NEEDED {name}: {lib}")
assert not missing_dependencies, 'Packed ELF dependencies are incomplete'
print(f"ELF CLOSURE {elf_count} ARM32 files; {dependency_count} DT_NEEDED dependencies present")
print(f"SIZE {len(data)} bytes (artifact ceiling: 15990784)")
print(f"SHA256 {hashlib.sha256(data).hexdigest()}")
print(f"HEADER kernel={ksize} ramdisk={rsize} second={ssize} pagesize={page}")
print(f"ADDRESSES kernel={kaddr:#x} ramdisk={raddr:#x} second={saddr:#x} tags={tags:#x}")
print(f"RAMDISK {len(files)} entries")
path.with_name("ramdisk-files.txt").write_text("\n".join(sorted(files)) + "\n")
path.with_name("recovery.fstab").write_bytes(files["etc/recovery.fstab"])
