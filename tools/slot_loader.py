"""Build Block 4's self-contained, read-only A/B boot payload.

misc is read one native 4 KiB page per erase block.  The first \0ABB\1 record
selects boot_a/boot_b. Diagnostics check success/tries directly; firmware
uses the priorities AFTER the donor preloader has consumed the current try.
It preserves that last attempt even when the resulting counter is zero.
No retry counters are written by this payload. The
selected Android/MTK image is bounds-checked before kernel, ramdisk and the
prepared DTB are copied to their validated header addresses.  Failure restores
the native hooks and returns to the original carrier at 0x200000.

The read sequence and command gate are shared with the byte-identical original
Stage2 (71ed4211...).  r9 remains the frame pointer throughout; no CRPECHO1,
Stage2 or UBI operation is present.
"""
import hashlib
import runpy
import struct

import firmware_profile as reader

HERE = reader.HERE
E = 0x43000000
GATE, FRAME, OPS, HASH = E + 0x1800, E + 0x2000, E + 0x2100, E + 0x2200
DTB = HERE / "crumpet-cmdline.dtb"
TWRP_DTB = HERE / "crumpet-twrp.dtb"
BLOCK, STAGE_SIZE = 0x40000, 0x10000
FIRMWARE_BRIDGE_OFFSET, FIRMWARE_MEID_OFFSET, FIRMWARE_OEM_OFFSET = 0x14000, 0x15100, 0x16000
FASTBOOT_ARGS_OFFSET, FASTBOOT_STUB_OFFSET = 0x3B000, 0x3B400
MISC_IMAGE = 0x47000000
BOOT_IMAGE = 0x48000000
MISC, MISC_RECORD, MISC_RECORDS = 0x00A00000, 0x40000, 8
BOOT_A, BOOT_B, BOOT_SIZE = 0x01300000, 0x02740000, 0x00F40000
NAND_PAGE, PAGE, MTK_HDR = 0x1000, 0x800, 0x200
FALLBACK = 0x200000
BCB_MAGIC = 0x42424100          # "\0ABB" at record+0x360
MAGIC_ANDR, MAGIC_OID = 0x52444E41, 0x2144494F
MTK_MAGIC, ZIMAGE_MAGIC = 0x58881688, 0x016F2818
KERNEL_ADDR, RAMDISK_ADDR, TAGS_ADDR = 0x40008000, 0x44000000, 0x4E000000


def build(force_b=False, uart=False, reset_at=None, wdt=False, recovery=False, smc=False,
          nonsecure=False, full_memory=False, embedded_dtb=False, twrp=False, chainload=False,
          stage2=False, stage2_copy_reset=False, stage2_expected=0, stage2_secure=False,
          stock_lk=False, stock_lk_boot4=False, stock_lk_boot4_reset=False,
          stock_lk_handoff=False, firmware=None):
    if not firmware:
        raise ValueError("this packaged generator supports the firmware handoff only")
    E = 0x46C00000 if firmware else 0x43000000
    GATE, FRAME, OPS, HASH = E + 0x1800, E + 0x2000, E + 0x2100, E + 0x2200
    if firmware and any((force_b, reset_at, wdt, recovery, smc, nonsecure, full_memory,
                         embedded_dtb, twrp, chainload, stage2, stage2_copy_reset,
                         stage2_expected, stage2_secure, stock_lk, stock_lk_boot4,
                         stock_lk_boot4_reset, stock_lk_handoff)):
        raise ValueError("Firmware boot cannot be combined with diagnostic handoffs")
    if firmware and (len(firmware["meid"]) != 16 or firmware["meid"] in (bytes(16), b"\xff" * 16)):
        raise ValueError("Firmware requires the live BootROM MEID of the IDME-pinned test unit")
    if stage2 or stage2_copy_reset or stage2_expected or stage2_secure:
        raise ValueError("Retired Stage2 path: 0x18000000 is a NAND offset, not executable DRAM; use tee1_block11")
    if stock_lk_handoff:
        raise ValueError("Retired raw preloader continuation: no valid Thumb/caller frame; use a complete firmware handoff")
    if stock_lk_boot4_reset:
        from keystone import Ks, KS_ARCH_ARM, KS_MODE_THUMB
        ks = Ks(KS_ARCH_ARM, KS_MODE_THUMB)
        enc, _ = ks.asm(
            "movw r0,#0x7008; movt r0,#0x1000; movw r1,#0x1971; str r1,[r0]; "
            "movw r0,#0x7000; movt r0,#0x1000; movw r1,#1; movt r1,#0x2200; "
            "str r1,[r0]; movw r0,#0x7014; movt r0,#0x1000; movw r1,#0x1209; "
            "str r1,[r0]; b .")
        raw = bytes(enc)
        assert len(raw) <= 0x60
        raw = raw.ljust((len(raw) + 3) & ~3, b"\0")
        stub = list(struct.unpack("<%dI" % (len(raw) // 4), raw))
    elif stock_lk_boot4:
        # Exact loader-prolog hook from the working TWRP LK stage.
        bundle = (reader.transport.ROOT / "EchoMuseCrumpet/FinalSolution/package/"
                  "crumpet-twrp-b.bundle.bin").read_bytes()
        hook = bundle[0x971500:0x971560]
        stub = list(struct.unpack("<24I", hook))
    elif stage2:
        # Non-secure entry stub: SCR.NS=1, then ldr pc,[pc,#-4] to 0x18000000.
        stub = [0xEE113F11, 0xE3833001, 0xEE013F11, 0xF57FF06F,
                0xE51FF004, 0x18000000]
    else:
        stub = ([0xEE113F11, 0xE3833001, 0xEE013F11, 0xF57FF06F] if nonsecure else []) + [
            0xE3A00000, 0xE3A01000, 0xE59F2000, 0xE59FF000,
            0 if embedded_dtb else TAGS_ADDR, KERNEL_ADDR]
    stub_size = len(stub) * 4
    donor = reader.DONOR.read_bytes()
    assert hashlib.sha256(donor).hexdigest() == reader.DONOR_SHA
    dtb = firmware["dtb"] if firmware else (TWRP_DTB if twrp else DTB).read_bytes()
    if wdt:
        import build_dtb
        original_cmdline = build_dtb.CMDLINE
        build_dtb.CMDLINE += " initcall_blacklist=mtk_wdt_driver_init"
        boot_a = (reader.HERE / "unsupported-diagnostic-boot-a").read_bytes()
        dtb = build_dtb.rebuild(build_dtb.select_dtb(boot_a), diagnostic_no_firmware=True)
        build_dtb.CMDLINE = original_cmdline
    if recovery:
        import build_dtb
        boot_b = (reader.HERE / "unsupported-diagnostic-boot-b").read_bytes()
        ramdisk_size = struct.unpack_from("<I", boot_b, 16)[0]
        original_cmdline = build_dtb.CMDLINE
        build_dtb.CMDLINE = original_cmdline.replace("skip_initramfs ", "").replace(
            "root=/dev/dm-0", f"root=/dev/ram initrd={RAMDISK_ADDR:#x},{ramdisk_size:#x} rdinit=/init")
        dtb = build_dtb.rebuild(build_dtb.select_dtb(boot_b), diagnostic_no_firmware=True)
        build_dtb.CMDLINE = original_cmdline

    if not full_memory and not firmware:
        patch_memory = runpy.run_path(str(
            reader.transport.ROOT / "EchoMuseCrumpet/FinalSolution/package/tools/build_crumpet_fastboot.py"
        ))["patch_memory_249m"]
        dtb = patch_memory(dtb)

    def patched_offset(pattern, expect):
        start = dtb.find(pattern)
        assert start >= 0 and dtb.find(pattern, start + 1) < 0
        end = start + len(pattern) - 1
        assert dtb[end:end + 1] == expect
        return end

    if not twrp:
        slot_off = patched_offset(b"androidboot.slot_suffix=_a", b"a")
        ubi_block_off = patched_offset(b"ubi.block=1,0", b"0")
        ubi_vol_off = patched_offset(b"ubiblock1_0", b"0")
    assert 0x2400 + len(dtb) <= (FIRMWARE_BRIDGE_OFFSET if firmware else STAGE_SIZE)
    assemble = reader.assemble_arm
    code, literals, labels = [], [], {}

    def load(register, value):
        code.append((register, len(literals)))
        literals.append(value)

    def call(address):
        load(12, address)
        code.append(0xE12FFF3C)

    def branch(condition, label):
        code.append(("b", condition, label))

    def emit(char):
        if reset_at == char:
            for address, value in ((0x10007008, 0x1971), (0x10007000, 0x22000001),
                                   (0x10007014, 0x1209)):
                load(0, address)
                load(1, value)
                code.append(0xE5801000)
            label = f"reset_{char}_{len(labels)}"
            labels[label] = len(code)
            branch(14, label)
        elif uart:
            code.append(0xE92D0003)        # push {r0,r1}
            load(0, ord(char))
            displacement = ((E + 0x1A00) - (E + len(code) * 4 + 8)) // 4
            code.extend((0xEB000000 | (displacement & 0xFFFFFF), 0xE8BD0003))

    # Exact original Stage2 preamble and native-context validation.
    load(13, 0x237F00)
    load(9, FRAME)
    emit("S")
    code.extend((0xE3A0A000, 0xE10F4000, 0xEE115F10, 0xEE106F10,
                 0xE5894014, 0xE5895018, 0xE589601C,
                 0xE20400FF, 0xE35000D3, ("b", 1, "halt"),
                 0xE3150005, ("b", 1, "halt")))
    load(0, 0x410FD041)
    code.extend((0xE1560000, ("b", 1, "halt")))
    for offset, value in ((0x98, 0x20000000), (0x9C, 4096), (0xA0, 256),
                          (0xA4, 0x40000), (0xC4, 64),
                          (0xDC, 0x42002040), (0xE0, 0x42003040),
                          (0x14, reader.CTRL), (0x74, 0x214E01)):
        load(0, reader.CONTEXT + offset)
        code.append(0xE5901000)
        load(2, value)
        code.extend((0xE1510002, ("b", 1, "fail")))

    if firmware:
        # Normal NAND boot does not pass through Kamakiri's WDT disable.
        # Disable and verify it before the full boot/TEE2 hashes and copies.
        load(0, 0x10007000)
        code.extend((0xE5901000, 0xE5891034, 0xE3110001, ("b", 0, "wdt_was_off")))
        emit("W")
        branch(14, "disable_wdt")
        labels["wdt_was_off"] = len(code)
        emit("w")
        labels["disable_wdt"] = len(code)
        load(1, 0x22000000)
        code.extend((0xE5801000, 0xE5901000, 0xE5891038,
                     0xE3110001, ("b", 1, "fail")))

    patches = ((0x2110F4, (0x47702000,)),
               (0x213BB8, (0x43C0201D, 0xBF004770)),
               (0x213E3C, (0x43C0201D, 0xBF004770)),
               (reader.CONTEXT + 0x14, (GATE,)))
    restores = []
    for address, values in patches:
        for index, value in enumerate(values):
            load(0, address + index * 4)
            load(1, value)
            code.append(0xE5801000)
            prior = reader.CTRL if address == reader.CONTEXT + 0x14 else struct.unpack_from(
                "<I", donor, 0x8300 + address - 0x201000 + index * 4)[0]
            restores.append((address + index * 4, prior))
    code.extend((0xE3A0A001, 0xE3A00000, 0xF57FF04F,
                 0xEE070F15, 0xF57FF04F, 0xF57FF06F))

    def read_call():
        # Same result/status sequence as the original three-read Stage2.
        load(0, reader.CONTEXT)
        load(1, OPS)
        call(reader.READ)
        code.extend((0xE5890020, 0xE3500000, ("b", 4, "fail")))
        load(0, FRAME + 36)
        code.extend((0xE5900000, 0xE3500000, ("b", 1, "fail")))

    def read_object(physical_reg, size_reg, destination_reg):
        # Reinitialize every 64-bit operation field; round the native read to
        # 4 KiB exactly as the original object_read helper does.
        load(0, OPS)
        code.extend((0xE3A01000, 0xE3A0200C))
        tag = f"zero_ops_{len(labels)}"
        labels[tag] = len(code)
        code.extend((0xE4801004, 0xE2522001, ("b", 1, tag)))
        load(0, OPS)
        code.extend((0xE3A01004, 0xE5801000))
        code.append(0xE1A01000 | physical_reg)
        code.append(0xE5801008)
        load(2, NAND_PAGE - 1)
        code.extend((0xE0801002 | (size_reg << 16),
                     0xE1A01621, 0xE1A01601, 0xE5801010))
        code.append(0xE1A01000 | destination_reg)
        code.append(0xE5801018)
        read_call()
        if not firmware:
            # Retain the historical diagnostic reader's call sequence.
            # Firmware boot checks geometry and authenticated containers;
            # it does not hash entire boot/TEE2 partitions or read chunks.
            code.extend((0xE1A00000 | destination_reg, 0xE1A01000 | size_reg))
            load(2, HASH)
            call(0x2187C9)

    if firmware.get("fastboot_args") is not None:
        from lk_bootargs import prepare
        args = firmware["fastboot_args"]
        if len(args) != 1024 or struct.unpack_from("<2I", args) != (3, 0x88610001):
            raise ValueError("Fastboot needs the bounded original preloader ATAG list")
        if prepare(args) != args:
            raise ValueError("Fastboot arguments must select mode 99 and reserve the bridge firmware")
        # MT8167 GPIO42: GPIO regmap, NOT the EINT controller at 0x1000b000.
        # Only this pin's mux/direction/pull bits and its shared input-enable
        # bit are touched. The selected input remains usable by gpio-keys.
        load(0, 0x10005380)
        code.extend((0xE5901000, 0xE3C11D07, 0xE5801000))  # clear mode bits 6..8
        load(1, 0x400)
        for address in (0x10005028, 0x10005904, 0x10005624, 0x10005524):
            load(0, address)              # input, IES, pull-up select/enable
            code.append(0xE5801000)
        code.append(0xF57FF04F)
        load(0, 4096)
        labels["button_settle"] = len(code)
        code.extend((0xE2500001, ("b", 1, "button_settle")))
        load(0, 0x10005220)
        code.extend((0xE5901000, 0xE3110B01, ("b", 1, "normal_slot")))
        emit("B")
        code.extend((0xE3A00001, 0xE589003C))
        # Read into a staging window, then omit the MTK header when copying
        # LK to its native link address. Executing at link+512 corrupts it.
        load(5, 0x100000)
        load(6, 0x3A000)
        load(7, BOOT_IMAGE)
        read_object(5, 6, 7)
        code.append(0xE5970000)
        load(1, MTK_MAGIC)
        code.extend((0xE1500001, ("b", 1, "fail"), 0xE5970004))
        load(1, 233532)
        code.extend((0xE1500001, ("b", 1, "fail")))
        load(0, BOOT_IMAGE + MTK_HDR)
        load(1, 0x41E00000)
        load(2, 233532)
        labels["copy_lk_body"] = len(code)
        code.extend((0xE4903004, 0xE4813004, 0xE2522004, ("b", 1, "copy_lk_body")))
        load(0, E + FASTBOOT_ARGS_OFFSET)
        load(1, TAGS_ADDR)
        load(2, len(args))
        labels["copy_lk_args"] = len(code)
        code.extend((0xE4903004, 0xE4813004, 0xE2522004, ("b", 1, "copy_lk_args")))
        branch(14, "firmware_prepare")
        labels["normal_slot"] = len(code)

    if stock_lk:
        # Replace the rescue LK in RAM with the verified stock lk_a image.
        # The stock LK then reads the BCB and boots the active slot.
        load(5, 0x100000)
        load(6, 0x180000)
        load(7, 0x41E00000)
        read_object(5, 6, 7)
        if stock_lk_boot4 or stock_lk_boot4_reset:
            # Copy the boot-mode-4 hook to 0x41E45060 and force the LK's
            # loader prolog at 0x41E1D584 through it.
            load(0, E + 0x2300)
            load(1, 0x41E45060)
            code.append(0xE3A02000 | stub_size)
            labels["copy_lk_hook"] = len(code)
            code.extend((0xE4D03001, 0xE4C13001, 0xE2522001,
                         ("b", 1, "copy_lk_hook")))
            load(0, 0x41E1D584)
            load(1, 0xFD6CF027)
            code.append(0xE5801000)
            load(2, stub_size)
            load(0, 0x41E45060)
            publish_cache(0, 2, "lk_hook")
            load(2, 4)
            load(0, 0x41E1D584)
            publish_cache(0, 2, "lk_patch")
        code.extend((0xE3A00000, 0xE5890010))
        branch(14, "cleanup")

    if stage2:
        # Stage2 lives in free TEE1 block 11 (physical 0x10C0000).  Read it to
        # the proven 0x48000000 staging window first; the TEE reader does not
        # reliably DMA to 0x18000000.
        load(5, 0x10C0000)
        load(6, 0x10000)
        load(7, 0x48000000)
        read_object(5, 6, 7)
        if stage2_expected:
            load(4, 0x48000000)
            code.append(0xE5940000)
            load(1, stage2_expected)
            code.extend((0xE1500001, ("b", 1, "fail")))
        code.extend((0xE3A00000, 0xE5890010))
        branch(14, "cleanup")

    # 1) Search all eight misc erase blocks, one native page at a time.
    load(4, 0)
    load(5, MISC)
    load(6, NAND_PAGE)
    load(7, MISC_IMAGE)
    labels["misc_scan"] = len(code)
    emit("1")
    read_object(5, 6, 7)
    emit("m")
    code.append(0xE5970360)                  # ldr r0,[r7,#0x360]
    load(1, BCB_MAGIC)
    code.extend((0xE1500001, ("b", 1, "misc_next")))
    code.append(0xE5D70364)                  # ldrb r0,[r7,#0x364]
    code.extend((0xE3500001, ("b", 0, "slot_found")))
    labels["misc_next"] = len(code)
    load(1, MISC_RECORD)
    code.extend((0xE0855001, 0xE2844001, 0xE3540008,
                 ("b", 1, "misc_scan"), ("b", 14, "fail")))

    # 2) Prefer higher priority (A on ties), then try the other bootable slot.
    # Read-only policy: positive priority AND success/tries; never decrement.
    # r4/r5 hold raw slot bytes, r6/r7 priorities; r9 stays FRAME.
    labels["slot_found"] = len(code)
    emit("F")
    if force_b:
        # Diagnostic only: exercise the known-good system_b without a misc write.
        load(5, BOOT_B)
        emit("B")
    else:
        if firmware:
            # The pinned donor's 0x201f20 selector has ALREADY consumed this
            # boot's try and cleared priorities of previously exhausted slots.
            # A positive-priority, zero-tries slot is its LAST allowed boot.
            # Requiring tries here would skip attempt seven; writing again
            # would consume twice. The diagnostic entry has no such prelude.
            code.extend((0xE5D74365, 0xE5D75366, 0xE204600F, 0xE205700F,
                         0xE1560007, ("b", 3, "firmware_prefer_b"),
                         0xE3560000, ("b", 1, "slot_a"), ("b", 14, "fail")))
            labels["firmware_prefer_b"] = len(code)
            code.extend((0xE3570000, ("b", 1, "slot_b"), ("b", 14, "fail")))
        else:
            code.extend((0xE5D74365, 0xE5D75366, 0xE204600F, 0xE205700F,
                         0xE3560000, 0x03A04000, 0xE3570000, 0x03A05000,
                         0xE1560007, ("b", 3, "prefer_b")))
            code.extend((0xE31400F0, ("b", 1, "slot_a"),
                         0xE31500F0, ("b", 1, "slot_b"), ("b", 14, "fail")))
            labels["prefer_b"] = len(code)
            code.extend((0xE31500F0, ("b", 1, "slot_b"),
                         0xE31400F0, ("b", 1, "slot_a"), ("b", 14, "fail")))
        labels["slot_a"] = len(code)
        load(5, BOOT_A)
        emit("A")
        if firmware and uart:
            load(0, MISC_IMAGE + 0x365)
            code.extend((0xE5D00000, 0xE2000070, 0xE1A00220, 0xE2800030))
            call(E + 0x1A00)                # post-preloader remaining tries
        branch(14, "boot")
        labels["slot_b"] = len(code)
        load(5, BOOT_B)
        emit("B")
        if firmware and uart:
            load(0, MISC_IMAGE + 0x366)
            code.extend((0xE5D00000, 0xE2000070, 0xE1A00220, 0xE2800030))
            call(E + 0x1A00)

    # 3) Read the complete boot partition in bounded 4 MiB operations.
    labels["boot"] = len(code)
    load(4, BOOT_SIZE)
    load(7, BOOT_IMAGE)
    labels["boot_read"] = len(code)
    load(6, 0x400000)
    code.extend((0xE1540006, ("b", 2, "boot_read_size"), 0xE1A06004))
    labels["boot_read_size"] = len(code)
    read_object(5, 6, 7)
    emit("b")
    code.extend((0xE0855006, 0xE0877006, 0xE0444006,
                 0xE3540000, ("b", 1, "boot_read")))
    emit("R")
    # 4) Accept only the known Crumpet Android/MTK geometry.  In particular,
    # zero lengths and unbounded addresses must never reach a copy loop.
    load(4, BOOT_IMAGE)
    code.append(0xE5940000)
    load(1, MAGIC_ANDR)
    code.extend((0xE1500001, ("b", 1, "fail")))
    code.append(0xE5940004)
    load(1, MAGIC_OID)
    code.extend((0xE1500001, ("b", 1, "fail")))
    for offset in (0x18, 0x28):              # second_size and dt_size
        code.append(0xE5940000 | offset)
        code.extend((0xE3500000, ("b", 1, "fail")))
    code.append(0xE5940024)
    load(1, PAGE)
    code.extend((0xE1500001, ("b", 1, "fail")))
    code.extend((0xE5946008, 0xE594700C, 0xE5948010,
                 0xE594A014, 0xE594B020))
    load(1, MTK_HDR)
    code.extend((0xE1560001, ("b", 9, "fail")))
    load(1, 0x01000000)
    code.extend((0xE1560001, ("b", 8, "fail"),
                 0xE3580000, ("b", 0, "fail"),
                 0xE1580001, ("b", 8, "fail")))
    if firmware:
        # The fixed stock RAM-console reservation starts at 0x44400000.
        load(1, 0x400000)
        code.extend((0xE1580001, ("b", 8, "fail")))
    for register, address in ((7, KERNEL_ADDR), (10, RAMDISK_ADDR), (11, TAGS_ADDR)):
        load(1, address)
        code.extend((0xE1500001 | (register << 16), ("b", 1, "fail")))

    load(0, BOOT_IMAGE + PAGE)
    code.append(0xE5901000)
    load(2, MTK_MAGIC)
    code.extend((0xE1510002, ("b", 1, "fail")))
    code.append(0xE5901004)
    load(2, MTK_HDR)
    code.extend((0xE0462002, 0xE1510002, ("b", 1, "fail")))
    code.append(0xE5940A24)                  # zImage magic at +0xA00+0x24
    load(1, ZIMAGE_MAGIC)
    code.extend((0xE1500001, ("b", 1, "fail")))

    # ramdisk source = base + page + align(kernel_size, page).
    load(0, BOOT_IMAGE + PAGE)
    load(1, PAGE - 1)
    code.append(0xE0862001)
    load(1, 0xFFFFF800)
    code.extend((0xE0022001, 0xE0800002))
    load(1, BOOT_IMAGE + BOOT_SIZE)
    code.extend((0xE0411000, 0xE1580001, ("b", 8, "fail")))
    code.extend((0xE5D01000, 0xE351001F, ("b", 1, "fail"),
                 0xE5D01001, 0xE351008B, ("b", 1, "fail"),
                 0xE5D01002, 0xE3510008, ("b", 1, "fail")))

    # Copy ramdisk first so r0 can then be reused for the kernel source.
    emit("H")
    code.extend((0xE1A0100A, 0xE1A02008))
    labels["copy_ramdisk"] = len(code)
    code.extend((0xE4D03001, 0xE4C13001, 0xE2522001,
                 ("b", 1, "copy_ramdisk")))
    if firmware:
        emit("h")
    load(0, BOOT_IMAGE + PAGE + MTK_HDR)
    # ARM imm12 includes rotation bits: literal 0x200 would encode zero.
    code.extend((0xE1A01007, 0xE2462C02))       # sub r2,r6,#512
    labels["copy_kernel"] = len(code)
    code.extend((0xE4D03001, 0xE4C13001, 0xE2522001,
                 ("b", 1, "copy_kernel")))
    if firmware:
        emit("k")

    # 5) Publish the prepared stock-cmdline DTB and select its A/B UBI volume.
    load(0, E + 0x2400)
    code.extend((0xE1A0100B, 0xE3A02000))
    load(2, len(dtb))
    labels["copy_dtb"] = len(code)
    code.extend((0xE4D03001, 0xE4C13001, 0xE2522001,
                 ("b", 1, "copy_dtb")))
    load(0, BOOT_B + BOOT_SIZE)          # r5 advanced by the chunked read
    code.extend((0xE1550000, ("b", 1, "dtb_done")))
    if not twrp:
        for offset, char in ((slot_off, "b"), (ubi_block_off, "1"), (ubi_vol_off, "1")):
            load(0, offset)
            code.append(0xE08B0000)
            load(1, ord(char))
            code.append(0xE5C01000)
    labels["dtb_done"] = len(code)
    if firmware:
        # The native DT carries an initrd pair; match the selected header size.
        load(0, TAGS_ADDR + firmware["initrd_end_offset"])
        code.extend((0xE08A1008, 0xE6BF1F31, 0xE5801000))
        emit("d")

    def publish_cache(base_reg, size_reg, tag):
        code.extend((0xE1A04000 | base_reg, 0xE0805000 | (base_reg << 16) | size_reg,
                     0xE2455001, 0xE3E0301F, 0xE0055003, 0xE2855020, 0xE0044003))
        label = f"publish_{tag}_{len(labels)}"
        labels[label] = len(code)
        code.extend((0xEE074F9A, 0xE2844020, 0xE1540005, ("b", 3, label), 0xF57FF04F))

    load(2, MTK_HDR)
    code.append(0xE0462002)
    publish_cache(7, 2, "kernel")
    publish_cache(10, 8, "ramdisk")
    load(2, len(dtb))
    publish_cache(11, 2, "dtb")
    if firmware:
        emit("p")
        labels["firmware_prepare"] = len(code)
        # All NAND reads finish while the donor context and command gate exist.
        load(4, 0x500000)
        load(5, 0x2240000)                  # physical TEE2, never written
        load(7, 0x49000000)
        emit("2")
        labels["firmware_read"] = len(code)
        load(6, 0x400000)
        code.extend((0xE1540006, ("b", 2, "firmware_read_size"), 0xE1A06004))
        labels["firmware_read_size"] = len(code)
        read_object(5, 6, 7)
        emit("e")
        code.extend((0xE0855006, 0xE0877006, 0xE0444006,
                     0xE3540000, ("b", 1, "firmware_read")))
        emit("t")                          # TEE2 read complete, NOT a hash check

        def copy_words(source, destination, size, tag):
            assert size and size % 4 == 0
            load(0, source)
            load(1, destination)
            load(2, size)
            labels[tag] = len(code)
            code.extend((0xE4903004, 0xE4813004, 0xE2522004, ("b", 1, tag)))

        copy_words(E + FIRMWARE_BRIDGE_OFFSET, 0x40C00000, len(firmware["bridge"]), "copy_bridge")
        if firmware.get("fastboot_args") is not None:
            code.extend((0xE599003C, 0xE3500000, ("b", 0, "bridge_kernel")))
            # Keep the original OEM ATF/MTEE authentication/handoff. Only
            # its final nonsecure destination and LK's r4 argument differ.
            copy_words(E + FASTBOOT_STUB_OFFSET, 0x40C00390, 20, "copy_lk_stub")
            labels["bridge_kernel"] = len(code)
        copy_words(E + FIRMWARE_OEM_OFFSET, 0x40C10000, len(firmware["oem"]), "copy_oem")
        copy_words(0x49000200, 0x40C35200, 0xD600, "copy_atf_container")
        copy_words(0x4900DA00, 0x40C42A00, 0x232C00, "copy_tee_container")

        if firmware.get("meid_probe"):
            call(0x40C01200)

        # The carrier restores ROM BSS; neither its global pointer nor the
        # old Donor MEID field is a qualified identity source here. The live
        # GET_ME_ID capture is bound to this package's SHA and installer IDME.
        load(0, E + FIRMWARE_MEID_OFFSET)
        load(1, 0x40C0F10C)
        code.extend((0xE3A02000, 0xE3E03000))
        for i in range(4):
            code.extend((0xE5904000 | (i * 4), 0xE5814000 | (i * 4),
                         0xE1822004, 0xE0033004))
        code.extend((0xE3520000, ("b", 0, "meid_zero"),
                     0xE3730001, ("b", 0, "meid_erased")))
        emit("M")
        for offset, value in ((0, 0x43505549), (4, 1), (8, 0x8167)):
            load(0, 0x40C0F100 + offset)
            load(1, value)
            code.append(0xE5801000)
        load(0, 0x40C00000)
        load(2, 0x280000)
        publish_cache(0, 2, "firmware")
    emit("J")
    code.extend((0xE3A00000, 0xE5890010, ("b", 14, "cleanup")))

    if firmware:
        labels["meid_zero"] = len(code)
        emit("Z")
        branch(14, "fail")
        labels["meid_erased"] = len(code)
        emit("E")
    labels["fail"] = len(code)
    emit("X")
    code.extend((0xE3A00001, 0xE5890010))
    labels["cleanup"] = len(code)
    for address, value in restores:
        load(0, address)
        load(1, value)
        code.append(0xE5801000)
    if chainload or stock_lk:
        # The stock LK expects the preloader's MMU/cache context.
        code.extend((0xE3A00000, 0xF57FF04F, 0xEE070F15,
                     0xF57FF04F, 0xF57FF06F, 0xE5990010, 0xE3500000,
                     ("b", 1, "fallback")))
    else:
        code.extend((0xEE115F10, 0xE3C55005))
        if firmware:
            # The historical bridge runs with I-cache disabled, including
            # when its SRAM OEM copy replaces previously executed donor code.
            code.append(0xE3C55A01)          # bic r5,r5,#0x1000
        code.extend((0xEE015F10,
                     0xE3A00000, 0xF57FF04F, 0xEE070F15,
                     0xF57FF04F, 0xF57FF06F, 0xE5990010, 0xE3500000,
                     ("b", 1, "fallback")))
    emit("K")
    if wdt:
        # Diagnostic: force a warm return after 31 s; the button selects BROM.
        for address, value in ((0x10007004, 0xF808), (0x10007008, 0x1971),
                               (0x10007000, 0x22000001)):
            load(0, address)
            load(1, value)
            code.append(0xE5801000)
    if firmware:
        # No return to donor code after the OEM code/context is replaced.
        load(15, 0x40C005E0)
    elif stock_lk_handoff:
        load(15, 0x20e486)
    elif stock_lk:
        load(0, 4)
        load(15, 0x41E00200)
    elif stage2:
        # Copy the staged Stage2 to 0x18000000 after the MMU is off.
        load(5, 0x48000000)
        load(7, 0x18000000)
        load(6, 0x10000)
        labels["copy_stage2"] = len(code)
        code.extend((0xE4953004, 0xE4873004, 0xE2566004,
                     ("b", 1, "copy_stage2")))
        if stage2_copy_reset:
            for address, value in ((0x10007008, 0x1971), (0x10007000, 0x22000001),
                                   (0x10007014, 0x1209)):
                load(0, address)
                load(1, value)
                code.append(0xE5801000)
        if stage2_secure:
            load(15, 0x18000000)
        else:
            # Enter the Stage2 non-secure through a DRAM stub.
            load(0, E + 0x2300)
            load(1, 0x40000000)
            code.append(0xE3A02000 | stub_size)
            labels["copy_stage2_stub"] = len(code)
            code.extend((0xE4D03001, 0xE4C13001, 0xE2522001,
                         ("b", 1, "copy_stage2_stub")))
            load(2, stub_size)
            load(0, 0x40000000)
            publish_cache(0, 2, "stage2_stub")
            load(15, 0x40000000)
    elif chainload:
        # Let the preloader-loaded stock LK read the BCB and boot the slot.
        # 0x41E00200 is the LK code entry after its GFH header; boot mode 4
        # is the normal boot path used by the stock preloader.
        load(0, 4)
        load(15, 0x41E00200)
    elif smc:
        # Stock LK's ATF path: SMC 0x82000200(kernel_entry, dtb, 0, 1).
        # The direct jump skips the secure-monitor entry the stock chain uses.
        load(0, 0x82000200)
        load(1, KERNEL_ADDR)
        load(2, TAGS_ADDR)
        load(3, 0)
        load(4, 1)
        code.append(0xE1600070)
        branch(14, "fallback")
    else:
        # Match the proven original handoff: a 24-byte stub at 0x40000000.
        load(0, E + 0x2300)
        load(1, 0x40000000)
        code.append(0xE3A02000 | stub_size)
        labels["copy_entry_stub"] = len(code)
        code.extend((0xE4D03001, 0xE4C13001, 0xE2522001,
                     ("b", 1, "copy_entry_stub")))
        load(2, stub_size)
        load(0, 0x40000000)
        publish_cache(0, 2, "entry_stub")
        load(15, 0x40000000)
    labels["fallback"] = len(code)
    load(15, FALLBACK)
    labels["halt"] = len(code)
    code.extend((0xE320F003, ("b", 14, "halt")))

    code = [("b", word[1], labels[word[2]]) if isinstance(word, tuple) and len(word) == 3 else word
            for word in code]
    body = assemble(E, code, literals)
    assert len(body) < GATE - E

    gate = [0xE202C006, 0xE35C0002, ("b", 1, -1), 0xE201C0FF]
    for command in reader.READ_COMMANDS:
        gate.extend((0xE35C0000 | command, ("b", 0, -1)))
    gate.extend(((3, 0), 0xE593C000, 0xE28CC001,
                 0xE583C000, 0xE5831004, 0xE12FFF1E))
    allowed = len(gate)
    gate.append((15, 1))
    gate = [("b", word[1], allowed) if isinstance(word, tuple) and len(word) == 3 else word
            for word in gate]
    guard = assemble(GATE, gate, [FRAME + 36, reader.CTRL])
    assert len(guard) < 0x200

    image = bytearray(b"\xff" * BLOCK)
    image[:0x2400] = bytes(0x2400)
    image[:len(body)] = body
    image[GATE - E:GATE - E + len(guard)] = guard
    if uart:
        # Shared, bounded TX-ready wait keeps adjacent phase markers reliable.
        from keystone import Ks, KS_ARCH_ARM, KS_MODE_ARM
        raw, _ = Ks(KS_ARCH_ARM, KS_MODE_ARM).asm(
            "push {r1,r2,r3,lr}; mov r3,r0; ldr r0,=0x11005000; mov r2,#4096; "
            "poll: ldr r1,[r0,#0x14]; tst r1,#0x20; bne ready; subs r2,r2,#1; bne poll; b done; "
            "ready: str r3,[r0]; done: pop {r1,r2,r3,pc}", addr=E + 0x1A00)
        assert len(raw) < 0x200
        image[0x1A00:0x1A00 + len(raw)] = bytes(raw)
    image[FRAME - E:FRAME - E + 64] = b"CRPNLD01" + struct.pack(
        "<14I", 1, 0, 0, 0, 0, 0, 0xFFFFFFFF, 0, 0,
        reader.CONTEXT, MISC_IMAGE, 0, 0, 0)
    struct.pack_into("<12I", image, OPS - E, 4, 0, MISC, 0,
                     NAND_PAGE, 0, MISC_IMAGE, 0, 0, 0, 0, 0)
    struct.pack_into("<%dI" % len(stub), image, 0x2300, *stub)
    image[0x2400:0x2400 + len(dtb)] = dtb
    if firmware:
        image[FIRMWARE_BRIDGE_OFFSET:FIRMWARE_BRIDGE_OFFSET + len(firmware["bridge"])] = firmware["bridge"]
        assert len(firmware["bridge"]) <= (0x2000 if firmware.get("meid_probe") else 0x1100)
        image[FIRMWARE_MEID_OFFSET:FIRMWARE_MEID_OFFSET + 16] = firmware["meid"]
        image[FIRMWARE_OEM_OFFSET:FIRMWARE_OEM_OFFSET + len(firmware["oem"])] = firmware["oem"]
        if firmware.get("fastboot_args") is not None:
            from keystone import Ks, KS_ARCH_ARM, KS_MODE_ARM
            stub = bytes(Ks(KS_ARCH_ARM, KS_MODE_ARM).asm(
                f"ldr r4,={TAGS_ADDR:#x}; ldr pc,=0x41e00000", addr=0x40C00390)[0])
            assert len(stub) <= 20
            image[FASTBOOT_ARGS_OFFSET:FASTBOOT_ARGS_OFFSET + len(firmware["fastboot_args"])] = firmware["fastboot_args"]
            image[FASTBOOT_STUB_OFFSET:FASTBOOT_STUB_OFFSET + 20] = stub.ljust(20, b"\0")
    return bytes(image)


if __name__ == "__main__":
    image = build()
    (HERE / "nand-slot-loader.RAM-ONLY.bin").write_bytes(image)
    print("DIRECT_SLOT_LOADER_BUILD_OK", len(image), hashlib.sha256(image[:STAGE_SIZE]).hexdigest())
