/*
 * Crumpet minimal LK handoff - project source, no vendor payload.
 *
 * LK downloads the bundle at 0x45000000 and enters this Thumb code at
 * 0x45971000. The recovery zImage follows its generated MTK header at
 * 0x45000A00. Linux expects that zImage at 0x40008000, so copy it there before
 * entering it. The PVT FDT and real TWRP initramfs remain in the download
 * buffer at 0x45980000 and 0x45990000 respectively.
 *
 * No LK loader, Amazon code, NAND operation or persistent state is used.
 */
.syntax unified
.thumb

/* DIRECT_ENTRY_BEGIN */
/* The USB callback can be in IRQ mode. Enter SVC with a dedicated RAM stack. */
cpsid if
mrs r0, cpsr
bic r0, r0, #0x1f
orr r0, r0, #0x13
msr cpsr_c, r0                    @ CPSR.M = SVC
movw r0, #0x0000
movt r0, #0x4597
mov sp, r0                        @ SVC stack top = 0x45970000, grows downward
push {r4, r5, r6, r7, lr}
/* DIRECT_ENTRY_END */
/* CANARY_ENTRY */
movs r0, #0x53                    @ 'S': stage entry
bl uart_putc

/* Make the stage itself visible to the instruction fetcher. */
movw r4, #0x1000
movt r4, #0x4597                  @ stage start = 0x45971000
movw r5, #0x2000
movt r5, #0x4597                  @ stage end = 0x45972000
bl clean_invalidate_range

/* Flush the downloaded kernel before reading it through the data cache. */
movw r7, #0x613f
movt r7, #0x005e                  @ header KERNEL_SIZE = 0x005e613f
movw r4, #0x0a00
movt r4, #0x4500                  @ source = 0x45000a00
mov r5, r4
add r5, r5, r7                    @ exclusive source end
bl clean_invalidate_range
movs r0, #0x43                    @ 'C': source cache flushed, copy begins
bl uart_putc

/* Copy the header-declared KERNEL_SIZE (0x5e613f), including its zero tail. */
movw r6, #0x0a00
movt r6, #0x4500                  @ read cursor = 0x45000a00
movw r5, #0x8000
movt r5, #0x4000                  @ write cursor = 0x40008000
movw r7, #0x613f
movt r7, #0x005e                  @ header KERNEL_SIZE = 0x005e613f
copy_kernel:
ldrb r3, [r6], #1
strb r3, [r5], #1
subs r7, #1
bne copy_kernel
/* CANARY_COPY */

/* Clean the copied kernel, including its partial final cache line. */
movw r4, #0x8000
movt r4, #0x4000
movw r5, #0x8000
movt r5, #0x4000
movw r7, #0x613f
movt r7, #0x005e
add r5, r5, r7
bl clean_invalidate_range

/* Publish FDT and the real TWRP initramfs before turning caches off. */
movw r4, #0x8000
movt r4, #0x4598
movw r5, #0x3000
movt r5, #0x4629                  @ exclusive end of the downloaded bundle
bl clean_invalidate_range
/* CANARY_CACHE */

movs r0, #0x4b                    @ 'K': copied kernel is ready to enter
bl uart_putc

/* Match the proven LK worker's peripheral cleanup before entering Linux. */
movw r3, #0x42f9
movt r3, #0x41e1
blx r3                              @ USB device controller stop
movw r0, #0x504c
movt r0, #0x41e5
movw r3, #0xbbf5
movt r3, #0x41e1
blx r3                              @ cancel LK's pending timer
movw r3, #0x2d1d
movt r3, #0x41e1
blx r3                              @ watchdog disable
movs r0, #3
movw r3, #0xbe85
movt r3, #0x41e1
blx r3                              @ stock boot LED indication
/* CANARY_CLEANUP */
/* MMU_OFF_OPTION */

/* Match the proven LK kernel-entry hook: preserve SCTLR/MMU state. */
cpsid if
dsb sy
movs r0, #0
mcr p15, 0, r0, c7, c5, 0       @ ICIALLU, invalidate I-cache
dsb sy
isb sy
/* CANARY_MMU */
/* CANARY_VERIFY */

movs r0, #0                       @ Linux r0 = 0
movs r1, #0                       @ Linux machine ID = 0 (FDT boot)
movw r2, #0x8000
movt r2, #0x4598                  @ Linux FDT pointer = 0x45980000
movw r5, #0x8000
movt r5, #0x4000                  @ copied TWRP zImage = 0x40008000
bx r5

/* r4 = first address, r5 = exclusive end; rounds the end to a cache line. */
clean_invalidate_range:
sub r5, r5, #1
movw r3, #0xffe0
and r5, r5, r3
add r5, r5, #0x20
and r4, r4, r3
cache_line:
mcr p15, 0, r4, c7, c10, 1      @ DCCIMVAC by MVA, clean+invalidate
adds r4, #0x20
cmp r4, r5
blo cache_line
dsb sy
bx lr

/* LK has already initialized UART0 at 0x11005000 (921600 baud). */
uart_putc:
movw r1, #0x5000
movt r1, #0x1100
str r0, [r1]
bx lr

/* The builder pads the rest of this 4 KiB stage with zeros. */
