/*
 * Crumpet full LK loader handoff - project source, no vendor payload.
 *
 * This stage reproduces the proven LK boot path instead of jumping directly
 * into the zImage:
 *
 *   USB callback -> stage_entry -> dpc_queue(stage_worker)
 *   stage_worker -> move boot header + kernel down by 0xA00
 *                -> relocate the LK-facing dummy ramdisk
 *                -> install the kernel-entry hook
 *                -> publish the downloaded-image node, boot mode 4
 *                -> quiesce USB/timer/watchdog/LED
 *                -> patch LK's loader prolog, initializer and DTB copier
 *                -> enter the stock loader at 0x41e1d585
 *
 * The loader then performs the normal LK boot-image setup.  Our tiny jump
 * stubs run from LK RAM (0x41e45060/0x41e45080/0x41e450a0) and enter the
 * routines in this stage.  The kernel-entry hook finally enters the TWRP
 * zImage at 0x40008000 with r2 = 0x45980000, the project-patched PVT FDT.
 *
 * Layout (assembled as separate snippets by lk_stage_loader.py):
 *   0x45971000  stage_entry + stage_worker
 *   0x45971500  loader prolog hook
 *   0x45971560  boot-image initializer wrapper
 *   0x459715c0  embedded-DTB copier (copied to 0x41e450a0)
 *   0x459715ea  boot_linux wrapper (copied to 0x41e450ca)
 *   0x45971608  secondary wrapper (copied to 0x41e450e8)
 *   0x45971640  final kernel jump
 *   0x459716a0  UART byte
 *   0x459716c0  jump stub for the prolog hook (copied to 0x41e45060)
 *   0x459716e0  jump stub for the initializer (copied to 0x41e45080)
 *   0x45971470  LK endpoint scratch area (must stay zero)
 *   0x45971700  endpoint completion no-op (bx lr)
 *   0x45971720  full kernel-entry hook (copied to 0x41e45200)
 *   0x45971b00  deferred-procedure node (must stay zero)
 *
 * No Amazon code, no binary stage, no NAND operation and no persistent state
 * is used.  The only runtime patches are the documented LK call sites.
 */
.syntax unified
.thumb

/* ===================== 0x45971000: entry + worker ===================== */
/* @section: entry_worker */
/* The USB callback enters here.  Publish the bulk-IN endpoint state and a
 * deferred worker, then return to LK exactly like the proven callback. */
stage_entry:
    push {r4, r5, r6, r7, lr}

    movw r4, #0x5ea4
    movt r4, #0x41e4                  @ LK bulk-IN endpoint state
    movs r0, #0
    movs r1, #0x12
clear_endpoint:
    str r0, [r4], #4
    subs r1, #1
    bne clear_endpoint

    movw r4, #0x5ea4
    movt r4, #0x41e4
    movw r0, #0x1700
    movt r0, #0x4597                  @ endpoint completion no-op
    str r0, [r4, #4]
    movs r0, #1
    strb r0, [r4, #0x10]
    strb r0, [r4, #0x11]
    movw r0, #0x200
    strh r0, [r4, #0x12]
    mov.w r0, #0x20000
    str r0, [r4, #0xc]
    movs r0, #1
    strb r0, [r4, #0x20]

    /* The stage arrived through the download buffer; make it fetchable. */
    movw r4, #0x1000
    movt r4, #0x4597
    movw r5, #0x2000
    movt r5, #0x4597
clean_stage:
    mcr p15, 0, r4, c7, c10, 1       @ DCCIMVAC
    adds r4, #0x20
    cmp r4, r5
    blo clean_stage
    dsb sy

    /* Publish the deferred-procedure list node at 0x45971b00. */
    movw r0, #0x5b28
    movt r0, #0x41e4                  @ LK DPC list head
    movw r1, #0x1b00
    movt r1, #0x4597
    str r1, [r0]
    str r1, [r0, #4]
    str r0, [r1]
    str r0, [r1, #4]
    movs r2, #0x100
    str r2, [r1, #8]

    adr r0, stage_worker
    adds r0, #1                       @ Thumb entry
    movs r1, #0
    movs r2, #1
    movw r3, #0xb135
    movt r3, #0x41e1                  @ LK dpc_queue
    blx r3

    pop {r4, r5, r6, r7, pc}

/* The deferred worker runs in LK task context. */
stage_worker:
    push {r4, r5, r6, r7, lr}

    /* Move header + kernel down by 0xA00: 0x44fff600 <- 0x45000000. */
    movw r0, #0xf600
    movt r0, #0x44ff
    movw r1, #0x0000
    movt r1, #0x4500
    movw r2, #0x7000
    movt r2, #0x005e
    bl lk_memcpy

    /* Relocate the LK-facing dummy ramdisk by one page and publish it. */
    movw r4, #0xf614
    movt r4, #0x44ff                  @ boot header ramdisk address
    movw r5, #0x6800
    movt r5, #0x455e
    str r5, [r4]
    movw r0, #0x6800
    movt r0, #0x455e
    movw r1, #0x7000
    movt r1, #0x455e
    movw r2, #0x9cec
    movt r2, #0x0038                  @ 0x00389cec (proven span)
    bl lk_memcpy

    /* Skip LK's downloaded-image verification gate. */
    movw r4, #0x1c68
    movt r4, #0x41e0
    movw r5, #0x2001
    movt r5, #0x4770                  @ movs r0,#1; bx lr
    str r5, [r4]
    mcr p15, 0, r4, c7, c10, 1
    dsb sy

    /* Install the full kernel-entry hook at 0x41e45200 (0x1e0 bytes). */
    movw r0, #0x5200
    movt r0, #0x41e4
    movw r1, #0x1720
    movt r1, #0x4597
    movw r2, #0x01e0
    bl lk_memcpy
    movw r4, #0x5200
    movt r4, #0x41e4
    movw r5, #0x5400
    movt r5, #0x41e4
clean_hook:
    mcr p15, 0, r4, c7, c10, 1
    adds r4, #0x20
    cmp r4, r5
    blo clean_hook
    dsb sy

    /* Redirect LK's final kernel jump through the hook. */
    movw r4, #0xd278
    movt r4, #0x41e1
    movw r5, #0xf027
    movt r5, #0xffc2                  @ bl 0x41e45200
    str r5, [r4]
    mcr p15, 0, r4, c7, c10, 1
    dsb sy

    /* Publish the downloaded-image node and select boot mode 4. */
    movw r0, #0x5b28
    movt r0, #0x41e4
    movw r1, #0xf5f4
    movt r1, #0x44ff
    str r1, [r0]
    str r1, [r0, #4]
    str r0, [r1]
    str r0, [r1, #4]
    movw r2, #0x0a0c
    movt r2, #0x00a0
    str r2, [r1, #8]
    movw r4, #0xf2b4
    movt r4, #0x41e3
    movs r0, #4
    str r0, [r4]

    /* Same peripheral quiesce sequence as the proven worker. */
    movw r3, #0x42f9
    movt r3, #0x41e1
    blx r3                            @ USB device controller stop
    movw r0, #0x504c
    movt r0, #0x41e5
    movw r3, #0xbbf5
    movt r3, #0x41e1
    blx r3                            @ cancel LK's pending timer
    movw r3, #0x2d1d
    movt r3, #0x41e1
    blx r3                            @ watchdog disable
    movs r0, #3
    movw r3, #0xbe85
    movt r3, #0x41e1
    blx r3                            @ stock boot LED indication

    /* The loader's debug UART path polls TX-ready forever after USB stop. */
    movw r4, #0x0e38
    movt r4, #0x41e1
    movw r5, #0x22ff
    movt r5, #0x0212                  @ movs r2,#0xff; lsls r2,r2,#8
    str r5, [r4]
    movw r5, #0x3a01
    movt r5, #0xd1fd                  @ subs r2,#1; bne
    str r5, [r4, #4]
    mcr p15, 0, r4, c7, c10, 1
    dsb sy

    /* Publish the loader-prolog trampoline at 0x41e45060. */
    movw r0, #0x5060
    movt r0, #0x41e4
    movw r1, #0x16c0
    movt r1, #0x4597
    movs r2, #0x10
    bl lk_memcpy
    movw r4, #0x5060
    movt r4, #0x41e4
    mcr p15, 0, r4, c7, c10, 1
    movw r4, #0xd584
    movt r4, #0x41e1
    movw r5, #0xf027
    movt r5, #0xfd6c                  @ bl 0x41e45060
    str r5, [r4]
    mcr p15, 0, r4, c7, c10, 1

    /* Publish the boot-image initializer trampoline at 0x41e45080. */
    movw r0, #0x5080
    movt r0, #0x41e4
    movw r1, #0x16e0
    movt r1, #0x4597
    movs r2, #0x10
    bl lk_memcpy
    movw r4, #0x5080
    movt r4, #0x41e4
    mcr p15, 0, r4, c7, c10, 1
    movw r4, #0xd69a
    movt r4, #0x41e1
    movw r5, #0xf027
    strh r5, [r4]
    movw r5, #0xfcf1                  @ bl 0x41e45080
    strh r5, [r4, #2]
    mcr p15, 0, r4, c7, c10, 1

    /* Publish the DTB block + wrappers at 0x41e450a0 (0x68 bytes). */
    movw r0, #0x50a0
    movt r0, #0x41e4
    movw r1, #0x15c0
    movt r1, #0x4597
    movw r2, #0x0068
    bl lk_memcpy
    movw r4, #0x50a0
    movt r4, #0x41e4
    movw r5, #0x5120
    movt r5, #0x41e4
clean_dtb:
    mcr p15, 0, r4, c7, c10, 1
    adds r4, #0x20
    cmp r4, r5
    blo clean_dtb
    dsb sy
    /* 0x41e1c3d2 -> bl 0x41e450a0 (embedded-DTB copier). */
    movw r4, #0xc3d2
    movt r4, #0x41e1
    movw r5, #0xf028
    strh r5, [r4]
    movw r5, #0xfe65
    strh r5, [r4, #2]
    mcr p15, 0, r4, c7, c10, 1
    /* 0x41e1c378 -> bl 0x41e450e8 (secondary wrapper). */
    movw r4, #0xc378
    movt r4, #0x41e1
    movw r5, #0xf028
    movt r5, #0xfeb6
    str r5, [r4]
    mcr p15, 0, r4, c7, c10, 1
    /* 0x41e1d4f6 -> bl 0x41e450ca (boot_linux wrapper). */
    movw r4, #0xd4f6
    movt r4, #0x41e1
    movw r5, #0xf027
    strh r5, [r4]
    movw r5, #0xfde8
    strh r5, [r4, #2]
    mcr p15, 0, r4, c7, c10, 1

    /* Always take the loader's successful-size branch. */
    movw r4, #0xc5c4
    movt r4, #0x41e1
    movw r5, #0xe00c                  @ b 0x41e1c5e0
    strh r5, [r4]
    mcr p15, 0, r4, c7, c10, 1
    dsb sy

    movs r0, #0
    mcr p15, 0, r0, c7, c5, 0         @ ICIALLU
    dsb sy
    isb sy

    mov ip, lr
    movw r3, #0xd585
    movt r3, #0x41e1
    bx r3                             @ stock LK loader, Thumb

/* Tail-call LK's ARM memcpy at 0x41e20858. */
lk_memcpy:
    movw r3, #0x0858
    movt r3, #0x41e2
    bx r3

/* ===================== 0x45971500: loader prolog ===================== */
/* @section: prolog_hook */
/* 0x41e1d584 is patched to "bl 0x41e45060"; the stub jumps here.  Rebuild
 * the loader's stack frame and register context, then enter its boot-mode-4
 * label at 0x41e1d694 with lr = 0x41e1d695. */
loader_prolog_hook:
    push.w {r4, r5, r6, r7, r8, sb, lr}
    sub.w sp, sp, #0x19c
    str.w ip, [sp, #0x1b4]
    movw r6, #0x8c98
    movt r6, #0x41e3
    movw r4, #0x2af1
    movt r4, #0x41e3
    movw r7, #0xf2b4
    movt r7, #0x41e3
    movw sb, #0x2ae5
    movt sb, #0x41e3
    movs r5, #0
    movs r8, #0
    movs sl, #0
    movs fp, #0
    movs r0, #4
    movs r1, #0
    movs r2, #0
    movw r3, #0x1f8
    movs ip, #0
    movw lr, #0xd695
    movt lr, #0x41e1
    bx lr

/* ===================== 0x45971560: initializer ===================== */
/* @section: init_wrapper */
/* 0x41e1d69a is patched to "bl 0x41e45080"; the stub jumps here.  Disable
 * the initializer's post-load gate, call the original routine and continue
 * at 0x41e1d777 (past the loader's re-checks). */
boot_init_wrapper:
    push {r0, r1, r2, r3, lr}
    pop {r0, r1, r2, r3}
    movw r0, #0xd834
    movt r0, #0x41e1
    movw r1, #0xe004                  @ b past the gate
    strh r1, [r0]
    mcr p15, 0, r0, c7, c10, 1
    dsb sy
    movs r2, #0
    mcr p15, 0, r2, c7, c5, 0
    dsb sy
    isb sy
    movw r3, #0x1a79
    movt r3, #0x41e1
    blx r3                            @ original boot-image initializer
    add sp, #4
    movw r3, #0xd777
    movt r3, #0x41e1
    bx r3

/* ===================== 0x459715c0: DTB copier ===================== */
/* @section: dtb_copy */
/* Assembled at 0x41e450a0 and copied there by the worker.  After the worker
 * moved the kernel down by 0xA00, the PVT DTB embedded in the zImage (kernel
 * offset 0x563a2b) is at 0x45563a2b.  Copy it to LK's tags address and return
 * the pointer the loader expects. */
embedded_dtb_copy:
    movw r0, #0x0000
    movt r0, #0x4e00
    movw r1, #0x3a2b
    movt r1, #0x4556
    movw r2, #0xca81
dtb_copy_loop:
    ldrb r3, [r1], #1
    strb r3, [r0], #1
    subs r2, #1
    bne dtb_copy_loop
    movw r0, #0x0064
    movt r0, #0x4e00
    bx lr

/* ===================== 0x459715ea: boot_linux wrapper ===================== */
/* @section: boot_linux_hook */
/* Assembled at 0x41e450ca (DTB block + 0x2a); called from 0x41e1d4f6. */
boot_linux_hook:
    push {r0, r1, r2, r3, r4, lr}
    movs r0, #0x41
    movw r3, #0x5000
    movt r3, #0x1100
    str r0, [r3]
    movw r3, #0xc241
    movt r3, #0x41e1
    blx r3
    pop {r0, r1, r2, r3, r4, lr}
    bx lr

/* ===================== 0x45971608: secondary wrapper ===================== */
/* @section: secondary_hook */
/* Assembled at 0x41e450e8 (DTB block + 0x48); called from 0x41e1c378. */
secondary_hook:
    push {r0, r1, r2, r3, r4, lr}
    movs r0, #0x42
    movw r3, #0x5000
    movt r3, #0x1100
    str r0, [r3]
    movw r3, #0xc22d
    movt r3, #0x41e1
    blx r3
    pop {r0, r1, r2, r3, r4, lr}
    bx lr

/* ===================== 0x45971640: final kernel jump ===================== */
/* @section: kernel_jump */
kernel_jump:
    push {r0, r1, r2, r3, r4, r5, lr}
    movs r0, #0x44
    movw ip, #0x16a1
    movt ip, #0x4597
    blx ip
    movw r3, #0x0000
    movt r3, #0x4e00
    ldr r0, [r3]
    movw ip, #0x16a1
    movt ip, #0x4597
    blx ip
    ldr r0, [r3, #4]
    movw ip, #0x16a1
    movt ip, #0x4597
    blx ip
    movw r3, #0x9eb0
    movt r3, #0x4e00
    ldr r0, [r3]
    movw ip, #0x16a1
    movt ip, #0x4597
    blx ip
    pop {r0, r1, r2, r3, r4, r5, lr}
    movs r0, #0
    movs r1, #0
    movw r2, #0x0000
    movt r2, #0x4598
    movw r5, #0x8000
    movt r5, #0x4000
    bx r5

/* ===================== 0x459716a0: UART byte ===================== */
/* @section: uart_putc */
uart_putc:
    push {r0, r1}
    movw r1, #0x5000
    movt r1, #0x1100
    str r0, [r1]
    movs r0, #0xa
    str r0, [r1]
    pop {r0, r1}
    bx lr

/* ===================== 0x45971720: kernel entry hook ===================== */
/* @section: kernel_hook */
/* Assembled at 0x41e45200 and copied there by the worker.  This is the full
 * LK kernel-entry path: header check, kernel copy to 0x4F000000, MTK image
 * load, then the final jump with r2=0x4F000000 on success. */
kernel_entry_hook:
    push {r0, r1, r2, r3, r4, lr}
    movs r0, #0x4d
    movw ip, #0x16a1
    movt ip, #0x4597
    blx ip
    mov r4, r2
    ldr r6, [r4, #4]
    rev r6, r6
    movw r5, #0x0000
    movt r5, #0x0200
    cmp r6, r5
    bhs hook_fail
    movw r0, #0x0000
    movt r0, #0x4f00
    mov r1, r4
    mov r2, r6
    movw r3, #0x0858
    movt r3, #0x41e2
    blx r3
    movw r0, #0x0000
    movt r0, #0x4f00
    movw r5, #0x0000
    movt r5, #0x0200
    rev r5, r5
    str r5, [r0, #4]
    movw r1, #0x019f
    movt r1, #0x41e3
    movw ip, #0x1217
    movt ip, #0x41e2
    blx ip
    cmp r0, #0
    blt hook_fail
    mov r7, r0
    sub sp, #0x10
    movs r5, #0
    str r5, [sp]
    movw r5, #0x0000
    movt r5, #0x4000
    rev r5, r5
    str r5, [sp, #4]
    movs r5, #0
    str r5, [sp, #8]
    movw r5, #0x0000
    movt r5, #0x0f9c
    rev r5, r5
    str r5, [sp, #0xc]
    mov r3, sp
    movs r0, #0x10
    str r0, [sp, #-4]!
    movw r0, #0x0000
    movt r0, #0x4f00
    mov r1, r7
    movw r2, #0x00ba
    movt r2, #0x41e3
    movw ip, #0x15a9
    movt ip, #0x41e2
    blx ip
    add sp, #0x14
    cmp r0, #0
    bne hook_fail
    movw r0, #0x0000
    movt r0, #0x4f00
    movw r1, #0x3c38
    movt r1, #0x41e3
    movw ip, #0x1217
    movt ip, #0x41e2
    blx ip
    cmp r0, #0
    blt hook_fail
    mov r7, r0
    movw r0, #0x0000
    movt r0, #0x4f00
    mov r1, r7
    movw r2, #0x419f
    movt r2, #0x41e3
    movw r3, #0x5330
    movt r3, #0x41e4
    movw r5, #0x009a
    str r5, [sp, #-4]!
    movw ip, #0x15a9
    movt ip, #0x41e2
    blx ip
    add sp, #4
    cmp r0, #0
    bne hook_fail
    movs r0, #0x50
    movw ip, #0x16a1
    movt ip, #0x4597
    blx ip
    movs r5, #1
    b hook_continue
hook_fail:
    movs r0, #0x46
    movw ip, #0x16a1
    movt ip, #0x4597
    blx ip
    movs r5, #0
hook_continue:
    pop {r0, r1, r2, r3, r4, lr}
    cbz r5, hook_skip
    movw r2, #0x0000
    movt r2, #0x4f00
hook_skip:
    movw r3, #0x1641
    movt r3, #0x4597
    bx r3

/* ===================== 0x45971700: endpoint return ===================== */
/* @section: endpoint_return */
/* Stored (even) in the bulk-IN endpoint's completion slot.  LK calls it in
 * Thumb state; it only has to return. */
endpoint_return:
    bx lr

/* The three 10-byte jump stubs at 0x45971680/0x459716a0/0x459716c0 are
 * generated by lk_stage_loader.py; they contain movw/movt/bx to this stage. */
