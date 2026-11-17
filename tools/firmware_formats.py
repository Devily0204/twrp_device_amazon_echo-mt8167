"""Factory IDME parsing; no factory identity is stored in this module."""
import re
import struct
from build_crumpet_fastboot import fdt_properties

def mtd_partition(table, name, size):
    """Resolve only a needed MTD name; unrelated entries and indices may vary."""
    matches = re.findall(r'^mtd([0-9]+): ([0-9a-fA-F]+) ([0-9a-fA-F]+) "'
                         + re.escape(name) + r'"\s*$', table, re.MULTILINE)
    if (len(matches) != 1 or int(matches[0][1], 16) != size
            or int(matches[0][2], 16) != 0x40000):
        raise ValueError("unexpected MTD mapping/geometry: " + name)
    return "/dev/mtd/mtd" + str(int(matches[0][0]))

def idme_records(data):
    """Decode the saved factory v2.1 record layout; values stay in memory."""
    if len(data) < 16 or data[:12] != b"beefdeed2.1\0":
        raise ValueError("Unknown or truncated IDME container")
    count, = struct.unpack_from("<I", data, 12)
    if not 1 <= count <= 256:
        raise ValueError("IDME record count outside bound")
    cursor, records = 16, {}
    for _ in range(count):
        if cursor + 28 > len(data):
            raise ValueError("Truncated IDME record")
        name = data[cursor:cursor + 16].split(b"\0")[0].decode("ascii")
        length, exported, permissions = struct.unpack_from("<III", data, cursor + 16)
        if (not re.fullmatch(r"[A-Za-z0-9_.-]{1,16}", name) or name in records or
                length > 65536 or cursor + 28 + length > len(data) or
                exported not in (0, 1) or permissions & ~0o777):
            raise ValueError("Invalid IDME record name, bounds or attributes")
        records[name] = {"offset": cursor, "bytes": length, "exported": exported,
                         "permissions": permissions, "value": data[cursor + 28:cursor + 28 + length]}
        cursor = (cursor + 28 + length + 3) & ~3
    return records
