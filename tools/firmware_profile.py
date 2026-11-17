"""Fixed native NAND ABI for the signed donor; no device identity or USB code."""
import hashlib
import json
import struct
from pathlib import Path
HERE = Path(__file__).resolve().parent
ASSETS = HERE.parent / "assets/bootchain"
PROFILE = json.loads((ASSETS / "profile.json").read_text())
DONOR = ASSETS / "preloader_donor.img"
DONOR_SHA = PROFILE["hashes"][DONOR.name]
CONTEXT = 0x42000080
CTRL = 0x21500d
READ = 0x213b95
READ_COMMANDS = (0,5,0x30,0x70,0x90,0xe0,0xec,0xee,0xff)
# Physical ranges used by the donor/loader or installer; all other GPT
# partitions may vary, provided they do not overlap these ranges.
REQUIRED_PARTITIONS = {
    "brhgptpl_0": (0, 0x40000),
    "idme_nand": (0x600000, 0x200000),
    "misc": (0xa00000, 0x200000),
    "tee1": (0xe00000, 0x500000),
    "boot_a": (0x1300000, 0xf40000),
    "tee2": (0x2240000, 0x500000),
    "boot_b": (0x2740000, 0xf40000),
}
TEE1_BLOCKS = (0, 1, 2, 3, 4, 11)

def asset(name):
    data = (ASSETS / name).read_bytes()
    if hashlib.sha256(data).hexdigest() != PROFILE["hashes"][name]:
        raise ValueError("changed firmware asset: " + name)
    return data

def assemble_arm(base, instructions, literals):
    words = list(instructions)
    pool = base + 4 * len(words)
    for index, value in enumerate(words):
        if isinstance(value, tuple):
            if len(value) == 3:
                _, condition, target = value
                words[index] = condition << 28 | 0x0a000000 | ((target - index - 2) & 0xffffff)
            else:
                register, literal = value
                offset = pool + 4 * literal - (base + 4 * index + 8)
                if not 0 <= offset < 4096:
                    raise ValueError("ARM literal is out of range")
                words[index] = 0xe59f0000 | register << 12 | offset
    return struct.pack('<%dI' % len(words), *words) + struct.pack('<%dI' % len(literals), *literals)
