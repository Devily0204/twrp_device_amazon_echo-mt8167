"""Assemble the project-owned full LK loader stage for Crumpet.

The stage source is ``lk_stage_loader.S``.  It is split into the documented
layout sections and each section is assembled at its runtime address.  The
two tiny jump stubs are generated here because they contain absolute stage
addresses; they are ordinary Thumb instructions, not copied vendor code.

No vendor binary is read or embedded.  keystone is a build-host dependency.
"""
from pathlib import Path
import re

from keystone import KS_ARCH_ARM, KS_MODE_THUMB, Ks

STAGE_ADDR = 0x45971000
STAGE_SIZE = 0x1000

# Section -> (stage offset, maximum length, runtime base or None).  Some
# sections are copied to LK RAM by the worker; they are assembled at their
# destination address so relative branches and absolute calls are correct.
# The stage must stay below the reserved LK endpoint descriptor at
# 0x45971470 (offset 0x470).
SECTIONS = {
    "entry_worker": (0x000, 0x400, None),
    "prolog_hook": (0x500, 0x60, None),
    "init_wrapper": (0x560, 0x60, None),
    "dtb_copy": (0x5C0, 0x2A, 0x41E450A0),
    "boot_linux_hook": (0x5EA, 0x1E, 0x41E450CA),
    "secondary_hook": (0x608, 0x20, 0x41E450E8),
    "kernel_jump": (0x640, 0x60, None),
    "uart_putc": (0x6A0, 0x20, None),
    "kernel_hook": (0x720, 0x1E0, 0x41E45200),
    "endpoint_return": (0x700, 0x10, None),
}
STUBS = {
    "prolog_stub": (0x6C0, 0x41E45060, None),  # target filled below
    "init_stub": (0x6E0, 0x41E45080, None),
}
STUB_LIMIT = 0xB00
ENDPOINT_DESCRIPTOR_OFF = 0x470
DPC_NODE_OFF = 0xB00

_SECTION = re.compile(r"/\* @section: ([a-z_]+) \*/")


def _assemble(source, base):
    assembler = Ks(KS_ARCH_ARM, KS_MODE_THUMB)
    encoded, count = assembler.asm(".syntax unified\n.thumb\n" + source, base)
    if not encoded or count <= 0:
        raise ValueError(f"Stage section at {base:#x} did not assemble")
    return bytes(encoded)


def _split_sections(source):
    matches = list(_SECTION.finditer(source))
    if not matches:
        raise ValueError("lk_stage_loader.S has no sections")
    sections = {}
    for index, match in enumerate(matches):
        name = match.group(1)
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(source)
        if name in sections:
            raise ValueError(f"Duplicate stage section: {name}")
        sections[name] = source[start:end]
    missing = set(SECTIONS) - set(sections)
    if missing:
        raise ValueError(f"Missing stage sections: {sorted(missing)}")
    return sections


def _jump_stub(target):
    """Return a 10-byte Thumb movw/movt/bx stub to an absolute target."""
    lo, hi = target & 0xFFFF, (target >> 16) & 0xFFFF
    return _assemble(
        f"movw r3, #{lo:#06x}\nmovt r3, #{hi:#06x}\nbx r3\n",
        0x41E45000,
    )


def assemble_loader_stage():
    """Return the 4 KiB full-loader stage, or raise ValueError."""
    source = Path(__file__).with_name("lk_stage_loader.S").read_text(encoding="utf-8")
    sections = _split_sections(source)
    image = bytearray(STAGE_SIZE)
    for name, (offset, limit, runtime) in SECTIONS.items():
        base = runtime if runtime is not None else STAGE_ADDR + offset
        code = _assemble(sections[name], base)
        if len(code) > limit:
            raise ValueError(f"Stage section {name} is {len(code):#x}, limit {limit:#x}")
        image[offset:offset + len(code)] = code

    targets = {
        "prolog_stub": STAGE_ADDR + SECTIONS["prolog_hook"][0] + 1,
        "init_stub": STAGE_ADDR + SECTIONS["init_wrapper"][0] + 1,
    }
    for name, (offset, runtime, _) in STUBS.items():
        stub = _jump_stub(targets[name])
        if len(stub) > 0x10:
            raise ValueError(f"Stage stub {name} is too large")
        image[offset:offset + len(stub)] = stub

    if any(image[DPC_NODE_OFF:DPC_NODE_OFF + 0x100]):
        raise ValueError("Stage overlaps the deferred-procedure node")
    if any(image[ENDPOINT_DESCRIPTOR_OFF:0x500]):
        raise ValueError("Stage overlaps the endpoint scratch area")
    if any(image[STUB_LIMIT:DPC_NODE_OFF]):
        raise ValueError("Stage code overlaps the deferred-procedure node")
    return bytes(image)
