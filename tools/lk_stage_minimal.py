"""Assemble the project-owned Crumpet LK stage and diagnostic reset canaries.

keystone is a build-host dependency, not a payload. The stage itself is plain
Thumb source and does not reuse any binary stage or vendor code.
"""
from pathlib import Path

from keystone import KS_ARCH_ARM, KS_MODE_THUMB, Ks

STAGE_ADDR = 0x45971000
STAGE_OFF = 0x971000
STAGE_ENTRY_THUMB = STAGE_ADDR + 1
STAGE_SIZE = 0x1000
CANARIES = ("entry", "copy", "cache", "mmu", "verify", "cleanup")

DPC_ENTRY = r"""
/* Publish a deferred worker, then return to LK like the proven stage. */
cpsid if
push {r4, r5, r6, r7, lr}
movw r4, #0x5ea4
movt r4, #0x41e4                  @ bulk-IN endpoint
movs r0, #0
movs r1, #0x12
clear_in_endpoint:
str r0, [r4], #4
subs r1, #1
bne clear_in_endpoint

movw r4, #0x1470
movt r4, #0x4597                  @ stage-local endpoint descriptor
movw r0, #0x1450
movt r0, #0x4597
str r0, [r4, #4]
movs r0, #1
strb r0, [r4, #0x10]
strb r0, [r4, #0x11]
movw r0, #0x200
strh r0, [r4, #0x12]
mov.w r0, #0x20000
str r0, [r4, #0xc]
movs r0, #1
strb.w r0, [r4, #0x20]

movw r4, #0x1000
movt r4, #0x4597
movw r5, #0x2000
movt r5, #0x4597
bl clean_invalidate_range

movw r0, #0x5b28
movt r0, #0x41e4                  @ LK DPC list head
movw r1, #0x001b
movt r1, #0x4597                  @ stage-local linked-list node
str r1, [r0]
str r1, [r0, #4]
str r0, [r1]
str r0, [r1, #4]
movw r2, #0x100
str r2, [r1, #8]
adr r0, worker_start
adds r0, #1                       @ deferred worker, Thumb
movs r1, #0
movs r2, #1
movw r3, #0xb135
movt r3, #0x41e1
blx r3                            @ LK dpc_queue(worker, 0, 1)
pop {r4, r5, r6, r7, pc}
"""

WATCHDOG_RESET = r"""
/* MT8167 watchdog reset: visible as a preloader re-enumeration on USB. */
watchdog_reset:
movw r0, #0x7008
movt r0, #0x1000
movw r1, #0x1971
str r1, [r0]
movw r0, #0x7000
movt r0, #0x1000
movw r1, #0x0001
movt r1, #0x2200
str r1, [r0]
movw r0, #0x7014
movt r0, #0x1000
movw r1, #0x1209
str r1, [r0]
canary_wait:
b canary_wait
"""

MMU_OFF = r"""
/* Exact LK arch cleanup used immediately before its kernel-entry hook. */
movs r0, #3
movw r3, #0xa29c
movt r3, #0x41e1
blx r3                            @ ARM: IRQ/FIQ/Abt + cache disable
movw r3, #0xadcf
movt r3, #0x41e1
blx r3                            @ Thumb: read SCTLR, clear M, write back
"""

VERIFY_AND_RESET = r"""
/* Reset only when the copied kernel, FDT and TWRP gzip data are intact. */
verify_and_reset:
movw r4, #0x8000
movt r4, #0x4000
ldr r0, [r4, #0x24]
movw r1, #0x2818
movt r1, #0x016f
cmp r0, r1
bne canary_wait
movw r4, #0x0000
movt r4, #0x4598
ldr r0, [r4]
movw r1, #0x0dd0
movt r1, #0xedfe
cmp r0, r1
bne canary_wait
movw r4, #0x0000
movt r4, #0x4599
ldrb r0, [r4]
cmp r0, #0x1f
bne canary_wait
ldrb r0, [r4, #1]
cmp r0, #0x8b
bne canary_wait
ldrb r0, [r4, #2]
cmp r0, #0x08
bne canary_wait
b watchdog_reset
"""


def assemble_stage(canary=None, dpc=False, mmu_off=False, lk_loader=False):
    if lk_loader:
        if canary is not None or mmu_off:
            raise ValueError("LK loader mode is incompatible with canaries and --mmu-off")
        from lk_stage_loader import assemble_loader_stage
        return assemble_loader_stage()
    source = Path(__file__).with_suffix(".S").read_text(encoding="utf-8")
    if mmu_off:
        marker = "/* MMU_OFF_OPTION */"
        if source.count(marker) != 1:
            raise ValueError("Missing MMU option marker")
        source = source.replace(marker, MMU_OFF, 1)
    if dpc:
        begin = "/* DIRECT_ENTRY_BEGIN */"
        end = "/* DIRECT_ENTRY_END */"
        if source.count(begin) != 1 or source.count(end) != 1:
            raise ValueError("Missing direct-entry markers")
        direct_entry = source[source.index(begin):source.index(end) + len(end)]
        dpc_entry = DPC_ENTRY + "\nworker_start:\n" + direct_entry
        source = source[:source.index(begin)] + dpc_entry + source[source.index(end) + len(end):]
    if canary is not None:
        if canary not in CANARIES:
            raise ValueError(f"Unknown stage canary: {canary}")
        marker = f"/* CANARY_{canary.upper()} */"
        if source.count(marker) != 1:
            raise ValueError(f"Missing or duplicate stage marker: {marker}")
        branch = "b verify_and_reset" if canary == "verify" else "b watchdog_reset"
        source = source.replace(marker, branch, 1)
        padding = "/* The builder pads the rest of this 4 KiB stage with zeros. */"
        if source.count(padding) != 1:
            raise ValueError("Missing stage padding marker")
        additions = WATCHDOG_RESET + (VERIFY_AND_RESET if canary == "verify" else "")
        source = source.replace(padding, additions + "\n" + padding, 1)
    assembler = Ks(KS_ARCH_ARM, KS_MODE_THUMB)
    encoded, count = assembler.asm(source, STAGE_ADDR)
    if not encoded or count <= 0:
        raise ValueError("Minimal LK stage did not assemble")
    code = bytes(encoded)
    if len(code) > STAGE_SIZE:
        raise ValueError(f"LK stage too large: {len(code):#x}")
    return code.ljust(STAGE_SIZE, b"\0")
