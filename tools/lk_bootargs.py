"""Prepare captured preloader ATAGs for LK with this bridge's secure memory."""
import struct

SIZE = 1024


def prepare(raw):
    if len(raw) != SIZE or struct.unpack_from("<2I", raw) != (3, 0x88610001):
        raise ValueError("missing original preloader ATAG capture")
    cursor, tags, seen = 0, bytearray(), set()
    while cursor + 8 <= SIZE:
        words, tag = struct.unpack_from("<2I", raw, cursor)
        if words == 0:
            break
        end = cursor + words * 4
        if words < 2 or end > SIZE or tag in seen:
            raise ValueError("invalid or duplicate preloader ATAG")
        seen.add(tag)
        value = bytearray(raw[cursor:end])
        if tag == 0x88610002:
            if words != 3:
                raise ValueError("unexpected native boot-mode tag")
            struct.pack_into("<I", value, 8, 99)
        elif tag == 0x88610005:
            # Ponytail: qualified single-rank 256 MiB Crumpet layout only.
            # Other DRAM profiles need their own validated memory handoff.
            if words != 60:
                raise ValueError("unexpected native memory tag size")
            rank = struct.unpack_from("<2I", value, 8)
            memory = struct.unpack_from("<2Q", value, 0x28)
            reserved = struct.unpack_from("<2Q", value, 0xE0)
            stock = (rank, memory, reserved) == ((1, 0xF9C0000), (0x40000000, 0xF9C0000), (0x4F9C0000, 0x40000))
            bridge = (rank, memory, reserved) == ((1, 0xF7C0000), (0x40000000, 0xF7C0000), (0x4F7C0000, 0x640000))
            if not (stock or bridge) or struct.unpack_from("<I", value, 0x20)[0] != 1:
                raise ValueError("unsupported preloader DRAM/TEE reservations")
            struct.pack_into("<I", value, 12, 0xF7C0000)
            struct.pack_into("<Q", value, 0x30, 0xF7C0000)
            struct.pack_into("<2Q", value, 0xE0, 0x4F7C0000, 0x640000)
        # This LK parser recognizes tags 1..22. Drop unknown trailing RAM
        # padding records, which it ignores and which are not boot inputs.
        if 0x88610001 <= tag <= 0x88610016:
            tags += value
        cursor = end
    else:
        raise ValueError("unterminated preloader ATAG list")
    if not {0x88610001, 0x88610002, 0x88610004, 0x88610005} <= seen or len(tags) + 8 > SIZE:
        raise ValueError("incomplete preloader ATAG list")
    return bytes(tags).ljust(SIZE, b"\0")
