"""Verify/extract boot+system from a trusted full Crumpet OTA into an inactive-slot plan.

Requires an independently trusted whole-ZIP SHA256. This does not establish a
new signer trust chain, install bootchain partitions, or activate a boot slot.
"""
import argparse
import bz2
import hashlib
import json
import lzma
from pathlib import Path
import struct
import tempfile
import zipfile


def require(condition, message):
    if not condition:
        raise ValueError(message)


def digest(stream, length=None):
    h = hashlib.sha256()
    while length is None or length:
        block = stream.read(1024 * 1024 if length is None else min(length, 1024 * 1024))
        if not block:
            require(length in (None, 0), "Truncated input while hashing")
            break
        h.update(block)
        if length is not None:
            length -= len(block)
    return h.digest()


def varint(blob, offset):
    value = 0
    for shift in range(0, 70, 7):
        require(offset < len(blob), "Truncated protobuf varint")
        byte = blob[offset]
        offset += 1
        value |= (byte & 127) << shift
        if not byte & 128:
            require(value < 1 << 64, "Protobuf integer overflow")
            return value, offset
    raise ValueError("Oversized protobuf varint")


def fields(blob):
    """Read only standard protobuf wire types used by update_metadata.proto."""
    offset, result = 0, {}
    while offset < len(blob):
        tag, offset = varint(blob, offset)
        field, wire = tag >> 3, tag & 7
        require(field > 0, "Invalid protobuf field")
        if wire == 0:
            value, offset = varint(blob, offset)
        elif wire in (1, 2, 5):
            if wire == 2:
                length, offset = varint(blob, offset)
            else:
                length = 8 if wire == 1 else 4
            require(length <= len(blob) - offset, "Truncated protobuf field")
            value = blob[offset:offset + length]
            offset += length
        else:
            raise ValueError("Unsupported protobuf wire type")
        result.setdefault(field, []).append(value)
    return result


def one(message, field, default=None):
    values = message.get(field, [])
    require(len(values) <= 1, "Duplicate singular protobuf field")
    return values[0] if values else default


def payload_manifest(stream):
    header = stream.read(24)
    require(len(header) == 24 and header[:4] == b"CrAU", "Invalid payload header")
    major, length, signature_length = struct.unpack(">QQI", header[4:])
    require(major == 2 and 0 < length <= 32 * 1024 * 1024, "Unsupported payload version/manifest size")
    require(signature_length <= 1024 * 1024, "Oversized metadata signature")
    blob = stream.read(length)
    require(len(blob) == length, "Truncated manifest")
    manifest = fields(blob)
    require(one(manifest, 12, 0) == 0, "Incremental OTA requires unsupported source/delta operations")
    block_size = one(manifest, 3, 4096)
    require(block_size == 4096, "Unexpected Crumpet payload block size")
    selected = {}
    for entry in manifest.get(13, []):
        part = fields(entry)
        name = one(part, 1, b"").decode("ascii")
        if name not in ("boot", "system"):
            continue
        require(name not in selected, "Duplicate payload partition")
        require(6 not in part, "Source partition data is not supported")
        info = fields(one(part, 7, b""))
        size, checksum = one(info, 1), one(info, 2)
        maximum = 15990784 if name == "boot" else 136626176
        require(isinstance(size, int) and 0 < size <= maximum, "Image exceeds verified partition capacity")
        require(isinstance(checksum, bytes) and len(checksum) == 32, "Missing partition SHA256")
        selected[name] = (part, size, checksum)
    require(set(selected) == {"boot", "system"}, "OTA must contain both boot and system")
    return selected, 24 + length + signature_length, block_size


def extract_partition(payload, part, size, expected, data_start, block_size, output):
    with output.open("xb+") as image:
        image.truncate(size)
        occupied = []
        for raw in part.get(8, []):
            operation = fields(raw)
            kind = one(operation, 1)
            require(kind in (0, 1, 6, 7, 8), "Unsupported delta/source operation")
            require(4 not in operation and 9 not in operation, "Source-copy/delta operation is not supported")
            extents = []
            for raw_extent in operation.get(6, []):
                extent = fields(raw_extent)
                start, count = one(extent, 1), one(extent, 2)
                require(isinstance(start, int) and isinstance(count, int) and count > 0, "Invalid extent")
                offset, length = start * block_size, count * block_size
                require(offset < size and length <= size - offset, "Extent outside destination image")
                require(all(offset + length <= a or b <= offset for a, b in occupied), "Overlapping extents")
                occupied.append((offset, offset + length))
                extents.append((offset, length))
            amount = sum(length for _, length in extents)
            require(0 < amount <= 64 * 1024 * 1024, "Oversized/empty operation")
            if kind in (6, 7):
                require(one(operation, 3, 0) == 0, "ZERO/DISCARD must not contain a data blob")
                decoded = bytes(amount)
            else:
                offset, length = one(operation, 2, 0), one(operation, 3, 0)
                require(0 < length <= 64 * 1024 * 1024, "Invalid operation data length")
                payload.seek(data_start + offset)
                data = payload.read(length)
                require(len(data) == length, "Truncated operation data")
                checksum = one(operation, 8)
                require(isinstance(checksum, bytes) and len(checksum) == 32 and
                        hashlib.sha256(data).digest() == checksum, "Operation SHA256 mismatch")
                if kind == 0:
                    decoded = data
                else:
                    decoder = bz2.BZ2Decompressor() if kind == 1 else lzma.LZMADecompressor()
                    decoded = decoder.decompress(data, max_length=amount + 1)
                    require(decoder.eof and not decoder.unused_data, "Invalid compressed operation")
                require(len(decoded) <= amount, "Operation expands past its destination")
                decoded += bytes(amount - len(decoded))
            offset = 0
            for destination, length in extents:
                image.seek(destination)
                image.write(decoded[offset:offset + length])
                offset += length
        occupied.sort()
        cursor = 0
        for start, end in occupied:
            require(start == cursor, "Payload leaves an unspecified image range")
            cursor = end
        require(cursor == size, "Payload does not cover complete partition image")
        image.seek(0)
        require(digest(image, size) == expected, "Final partition SHA256 mismatch")
        image.seek(0)
        magic = image.read(8)
        require(magic == b"ANDROID!" if output.stem == "boot" else magic[:4] == b"hsqs",
                "Extracted image has the wrong filesystem/boot magic")


def prepare(ota, expected_sha256, output, slot):
    require(slot in ("a", "b"), "Invalid target slot")
    require(len(expected_sha256) == 64 and all(c in "0123456789abcdef" for c in expected_sha256), "Invalid trusted ZIP hash")
    with ota.open("rb") as stream:
        require(digest(stream).hex() == expected_sha256, "Whole OTA ZIP SHA256 mismatch")
    require(not output.exists(), "Output directory already exists")
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(ota) as archive, tempfile.TemporaryDirectory(prefix="crumpet-ota-", dir=output.parent) as temp:
        require(len(archive.namelist()) == len(set(archive.namelist())), "Duplicate ZIP entry")
        metadata = dict(line.split("=", 1) for line in archive.read("META-INF/com/android/metadata").decode().splitlines() if "=" in line)
        require(metadata.get("pre-device") == "crumpet" and metadata.get("ota-type") == "AB", "OTA is not for Crumpet")
        plan = {"source_sha256": expected_sha256, "target_slot": slot, "activation": "not-performed", "images": {}}
        with archive.open("payload.bin") as payload:
            partitions, start, block_size = payload_manifest(payload)
            for name in ("system", "boot"):
                part, size, checksum = partitions[name]
                extract_partition(payload, part, size, checksum, start, block_size, Path(temp) / (name + ".img"))
                plan["images"][name] = {"size": size, "sha256": checksum.hex()}
        (Path(temp) / "SHA256SUMS").write_text("".join(f'{plan["images"][name]["sha256"]}  {name}.img\n' for name in ("system", "boot")), newline="\n")
        (Path(temp) / "target-slot").write_text(slot + "\n", newline="\n")
        (Path(temp) / "plan.json").write_text(json.dumps(plan, indent=2) + "\n", newline="\n")
        Path(temp).rename(output)
    return plan


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("ota", type=Path)
    parser.add_argument("--sha256", required=True, help="Independently trusted SHA256 of the complete OTA ZIP")
    parser.add_argument("--slot", choices=("a", "b"), required=True, help="Inactive destination slot; checked again on-device")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(prepare(args.ota, args.sha256, args.output, args.slot), indent=2))
