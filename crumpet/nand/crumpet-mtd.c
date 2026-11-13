// Derived from the locally tested CleanDot_20261109/tools/mtdtool.c.
/*
 * mtdtool - minimal, auditable MTD/NAND read/write helper for Android ARM.
 *
 * Built against the Linux 4.4 MTD user ABI:
 *   MEMGETINFO      0x80204d01
 *   MEMERASE        0x40084d02
 *   MEMGETBADBLOCK  0x40084d0b
 *   MEMERASE64      0x40104d14
 *
 * The tool deliberately does not touch OOB/ECC directly. It uses the kernel
 * MTD char driver, which applies the NAND ECC on read and write.
 *
 * Safety:
 *   - every erase/write range must be inside the partition;
 *   - erase ranges must be erase-block aligned;
 *   - bad blocks in the range abort the operation;
 *   - partial writes use read-modify-write of the covering erase block(s);
 *   - every write is read back and compared by default.
 */

#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <inttypes.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#ifndef CRUMPET_NAND_TEST
#include <sys/ioctl.h>
#include <sys/file.h>
#endif
#include <sys/stat.h>
#include <unistd.h>

#define MEMGETINFO 0x80204d01UL
#define MEMERASE 0x40084d02UL
#define MEMGETBADBLOCK 0x40084d0bUL
#define MEMERASE64 0x40104d14UL

struct mtd_info_user {
    uint8_t type;
    uint32_t flags;
    uint32_t size;
    uint32_t erasesize;
    uint32_t writesize;
    uint32_t oobsize;
    uint64_t padding;
};

struct erase_info_user {
    uint32_t start;
    uint32_t length;
};

struct erase_info_user64 {
    uint64_t start;
    uint64_t length;
};

static void die(const char *what) {
    fprintf(stderr, "mtdtool: %s: %s\n", what, strerror(errno));
    exit(1);
}

static void usage(const char *argv0) {
    fprintf(stderr, "Usage: %s info DEV | read DEV OFFSET LENGTH FILE | write DEV OFFSET FILE\n"
                    "  bcb-status DEV | bcb-read DEV FILE | bcb-write DEV FILE\n"
                    "  bcb-activate DEV a|b | bcb-success DEV a|b\n", argv0);
    exit(2);
}

static uint64_t parse_u64(const char *text) {
    errno = 0;
    char *end = NULL;
    unsigned long long value = strtoull(text, &end, 0);
    if (errno != 0 || end == text || *end != '\0' || *text == '-') {
        fprintf(stderr, "mtdtool: invalid number: %s\n", text);
        exit(2);
    }
    return (uint64_t)value;
}

static int open_dev(const char *path, int flags) {
    int fd = open(path, flags);
    if (fd < 0)
        die(path);
    return fd;
}

static void get_info(int fd, const char *path, struct mtd_info_user *info) {
    memset(info, 0, sizeof(*info));
    if (ioctl(fd, MEMGETINFO, info) < 0)
        die("MEMGETINFO");
    if (info->erasesize == 0 || info->writesize == 0) {
        fprintf(stderr, "mtdtool: %s: invalid geometry\n", path);
        exit(1);
    }
}

static int block_is_bad(int fd, uint64_t offset) {
    uint64_t value = offset;
    int ret = ioctl(fd, MEMGETBADBLOCK, &value);
    if (ret < 0)
        die("MEMGETBADBLOCK");
    return ret;
}

static void check_range(const struct mtd_info_user *info, uint64_t offset,
                        uint64_t length) {
    if (length == 0) {
        fprintf(stderr, "mtdtool: zero length\n");
        exit(2);
    }
    if (offset > info->size || length > info->size - offset) {
        fprintf(stderr,
                "mtdtool: range 0x%" PRIx64 "+0x%" PRIx64
                " outside partition size 0x%" PRIx32 "\n",
                offset, length, info->size);
        exit(2);
    }
}

static void check_bad_blocks(int fd, const struct mtd_info_user *info,
                             uint64_t offset, uint64_t length) {
    uint64_t first = offset & ~((uint64_t)info->erasesize - 1);
    uint64_t end = (offset + length + info->erasesize - 1) &
                   ~((uint64_t)info->erasesize - 1);
    for (uint64_t block = first; block < end; block += info->erasesize) {
        int bad = block_is_bad(fd, block);
        if (bad < 0)
            die("MEMGETBADBLOCK");
        if (bad) {
            fprintf(stderr,
                    "mtdtool: bad block at 0x%" PRIx64
                    " (erase block 0x%" PRIx32 ")\n",
                    block, info->erasesize);
            exit(1);
        }
    }
}

static void erase_range(int fd, const struct mtd_info_user *info,
                        uint64_t offset, uint64_t length) {
    if ((offset % info->erasesize) != 0 || (length % info->erasesize) != 0) {
        fprintf(stderr,
                "mtdtool: erase range must be aligned to erasesize 0x%" PRIx32
                "\n",
                info->erasesize);
        exit(2);
    }
    if (offset + length <= 0xffffffffULL) {
        struct erase_info_user erase = {(uint32_t)offset, (uint32_t)length};
        if (ioctl(fd, MEMERASE, &erase) < 0)
            die("MEMERASE");
    } else {
        struct erase_info_user64 erase = {offset, length};
        if (ioctl(fd, MEMERASE64, &erase) < 0)
            die("MEMERASE64");
    }
}

static void read_exact(int fd, uint64_t offset, void *buffer, uint64_t length) {
    uint8_t *cursor = (uint8_t *)buffer;
    uint64_t done = 0;
    if (lseek(fd, (off_t)offset, SEEK_SET) < 0)
        die("lseek");
    while (done < length) {
        size_t want = (length - done > 0x10000) ? 0x10000 : (size_t)(length - done);
        ssize_t got = read(fd, cursor + done, want);
        if (got < 0 && errno == EINTR) continue;
        if (got < 0)
            die("read");
        if (got == 0) {
            fprintf(stderr, "mtdtool: short read at 0x%" PRIx64 "\n",
                    offset + done);
            exit(1);
        }
        done += (uint64_t)got;
    }
}

static void write_exact(int fd, uint64_t offset, const void *buffer,
                        uint64_t length) {
    const uint8_t *cursor = (const uint8_t *)buffer;
    uint64_t done = 0;
    if (lseek(fd, (off_t)offset, SEEK_SET) < 0)
        die("lseek");
    while (done < length) {
        size_t want = (length - done > 0x10000) ? 0x10000 : (size_t)(length - done);
        ssize_t put = write(fd, cursor + done, want);
        if (put < 0 && errno == EINTR) continue;
        if (put < 0)
            die("write");
        if (put == 0) {
            fprintf(stderr, "mtdtool: short write at 0x%" PRIx64 "\n",
                    offset + done);
            exit(1);
        }
        done += (uint64_t)put;
    }
}

static void verify_range(int fd, uint64_t offset, const void *expected,
                         uint64_t length) {
    uint8_t *buffer = malloc(0x10000);
    if (!buffer)
        die("malloc");
    const uint8_t *want = (const uint8_t *)expected;
    uint64_t done = 0;
    if (lseek(fd, (off_t)offset, SEEK_SET) < 0)
        die("lseek");
    while (done < length) {
        size_t chunk = (length - done > 0x10000) ? 0x10000 : (size_t)(length - done);
        ssize_t got = read(fd, buffer, chunk);
        if (got < 0 && errno == EINTR) continue;
        if (got < 0)
            die("read(verify)");
        if (got == 0) {
            fprintf(stderr, "mtdtool: short verify read\n");
            exit(1);
        }
        if (memcmp(buffer, want + done, (size_t)got) != 0) {
            uint64_t bad_at = offset + done;
            for (size_t i = 0; i < (size_t)got; ++i) {
                if (buffer[i] != want[done + i]) {
                    bad_at = offset + done + i;
                    break;
                }
            }
            fprintf(stderr, "mtdtool: verify mismatch at 0x%" PRIx64 "\n",
                    bad_at);
            exit(1);
        }
        done += (uint64_t)got;
    }
    free(buffer);
}

static uint64_t file_size(FILE *file) {
    if (fseek(file, 0, SEEK_END) != 0)
        die("fseek");
    long size = ftell(file);
    if (size < 0)
        die("ftell");
    if (fseek(file, 0, SEEK_SET) != 0)
        die("fseek");
    return (uint64_t)size;
}

static void cmd_info(const char *dev) {
    int fd = open_dev(dev, O_RDONLY);
    struct mtd_info_user info;
    get_info(fd, dev, &info);
    printf("dev=%s\n", dev);
    printf("type=%u flags=0x%" PRIx32 " size=%" PRIu32
           " erasesize=%" PRIu32 " writesize=%" PRIu32
           " oobsize=%" PRIu32 "\n",
           info.type, info.flags, info.size, info.erasesize, info.writesize,
           info.oobsize);
    close(fd);
}

static void cmd_read(const char *dev, uint64_t offset, uint64_t length,
                     const char *output) {
    int fd = open_dev(dev, O_RDONLY);
    struct mtd_info_user info;
    get_info(fd, dev, &info);
    check_range(&info, offset, length);
    FILE *out = fopen(output, "wb");
    if (!out)
        die(output);
    uint8_t *buffer = malloc(0x10000);
    if (!buffer)
        die("malloc");
    uint64_t done = 0;
    if (lseek(fd, (off_t)offset, SEEK_SET) < 0)
        die("lseek");
    while (done < length) {
        size_t want = (length - done > 0x10000) ? 0x10000 : (size_t)(length - done);
        ssize_t got = read(fd, buffer, want);
        if (got < 0 && errno == EINTR) continue;
        if (got < 0)
            die("read");
        if (got == 0) {
            fprintf(stderr, "mtdtool: short read at 0x%" PRIx64 "\n",
                    offset + done);
            exit(1);
        }
        if (fwrite(buffer, 1, (size_t)got, out) != (size_t)got)
            die("fwrite");
        done += (uint64_t)got;
    }
    free(buffer);
    fclose(out);
    printf("read=%" PRIu64 " offset=0x%" PRIx64 " output=%s\n", done, offset,
           output);
    close(fd);
}

static void cmd_write(const char *dev, uint64_t offset, const char *input,
                      int verify) {
    FILE *in = fopen(input, "rb");
    if (!in)
        die(input);
    uint64_t input_size = file_size(in);
    int fd = open_dev(dev, O_RDWR);
    struct mtd_info_user info;
    get_info(fd, dev, &info);
    check_range(&info, offset, input_size);
    check_bad_blocks(fd, &info, offset, input_size);

    const uint64_t block_size = info.erasesize;
    const int aligned =
        (offset % block_size == 0) && (input_size % block_size == 0);

    if (aligned) {
        uint8_t *block = malloc(block_size);
        if (!block)
            die("malloc");
        for (uint64_t pos = 0; pos < input_size; pos += block_size) {
            if (fread(block, 1, block_size, in) != block_size) {
                fprintf(stderr, "mtdtool: short input read at 0x%" PRIx64 "\n",
                        pos);
                exit(1);
            }
            erase_range(fd, &info, offset + pos, block_size);
            write_exact(fd, offset + pos, block, block_size);
            if (verify)
                verify_range(fd, offset + pos, block, block_size);
            printf("wrote block offset=0x%" PRIx64 " size=0x%" PRIx64
                   " verify=%d\n",
                   offset + pos, block_size, verify);
        }
        free(block);
    } else {
        uint64_t first = offset & ~(block_size - 1);
        uint64_t end = (offset + input_size + block_size - 1) &
                       ~(block_size - 1);
        uint64_t span = end - first;
        uint8_t *buffer = malloc((size_t)span);
        uint8_t *data = malloc((size_t)input_size);
        if (!buffer || !data)
            die("malloc");
        read_exact(fd, first, buffer, span);
        if (fread(data, 1, (size_t)input_size, in) != (size_t)input_size) {
            fprintf(stderr, "mtdtool: short input read\n");
            exit(1);
        }
        memcpy(buffer + (size_t)(offset - first), data, (size_t)input_size);
        for (uint64_t pos = 0; pos < span; pos += block_size) {
            erase_range(fd, &info, first + pos, block_size);
            write_exact(fd, first + pos, buffer + pos, block_size);
            if (verify)
                verify_range(fd, first + pos, buffer + pos, block_size);
        }
        printf("wrote rmw first=0x%" PRIx64 " offset=0x%" PRIx64
               " size=0x%" PRIx64 " span=0x%" PRIx64 " verify=%d\n",
               first, offset, input_size, span, verify);
        free(buffer);
        free(data);
    }
    fclose(in);
    /* An MTD char fd supports NAND readback, but not fsync (EINVAL here). */
    if (fsync(fd) && errno != EINVAL && errno != ENOTSUP) die("sync MTD");
    close(fd);
}

// Stock bootctrl.mt8167.so scans eraseblock-aligned redundant records for the
// little-endian ABB magic at record+0x360. No sequence number is stored.
// A new record is verified before old valid records are erased. Publish its
// first NAND page last, so an interrupted tail write cannot publish its magic.
static int rr_load(int fd, const struct mtd_info_user *info, uint8_t *record,
                   int *valid, int *spare) {
    if (info->size != 2097152 || info->erasesize != 262144 || info->writesize != 4096) {
        errno = EINVAL; die("unexpected misc RR geometry");
    }
    uint8_t *buffer = malloc(info->erasesize);
    if (!buffer) die("RR allocation");
    int chosen = -1;
    *spare = -1;
    for (int i = 0; i < 8; ++i) {
        valid[i] = 0;
        uint64_t offset = (uint64_t)i * info->erasesize;
        if (block_is_bad(fd, offset)) continue;
        read_exact(fd, offset, buffer, info->erasesize);
        int recognized = !memcmp(buffer + 0x360, "\0ABB\1", 5);
        if (recognized) {
            valid[i] = 1;
            if (chosen == -1) { chosen = i; memcpy(record, buffer, info->erasesize); }
        } else {
            size_t j;
            for (j = 0; j < info->erasesize && buffer[j] == 255; ++j) {}
            if (j == info->erasesize && *spare == -1) *spare = i;
        }
    }
    free(buffer);
    if (chosen < 0) { errno = EINVAL; die("no valid NAND RR BCB; initialization is not inferred"); }
    return chosen;
}

static void rr_store(int fd, const struct mtd_info_user *info, const uint8_t *record,
                     const int *valid, int spare) {
    if (spare < 0) { errno = ENOSPC; die("no erased good spare BCB block; old record retained"); }
    uint64_t offset = (uint64_t)spare * info->erasesize;
    check_bad_blocks(fd, info, offset, info->erasesize);
    erase_range(fd, info, offset, info->erasesize);
    write_exact(fd, offset + info->writesize, record + info->writesize, info->erasesize - info->writesize);
    write_exact(fd, offset, record, info->writesize);
    /* fsync may be EINVAL; verify and retire regardless. */
    if (fsync(fd) && errno != EINVAL && errno != ENOTSUP) die("sync new BCB record");
    verify_range(fd, offset, record, info->erasesize);
    uint8_t *blank = malloc(info->writesize);
    if (!blank) die("BCB retirement allocation");
    memset(blank, 255, info->writesize);
    for (int i = 0; i < 8; ++i) {
        if (valid[i]) {
            uint64_t old = (uint64_t)i * info->erasesize;
            check_bad_blocks(fd, info, old, info->erasesize);
            erase_range(fd, info, old, info->erasesize);
            verify_range(fd, old, blank, info->writesize);
        }
    }
    if (fsync(fd) && errno != EINVAL && errno != ENOTSUP) die("sync BCB commit");
    free(blank);
    printf("BCB committed to redundant block %d; old valid records retired\n", spare);
}

static void cmd_bcb(const char *command, const char *dev, const char *argument) {
    int writing = strcmp(command, "bcb-status") && strcmp(command, "bcb-read");
    int fd = open_dev(dev, writing ? O_RDWR : O_RDONLY);
    struct mtd_info_user info;
    get_info(fd, dev, &info);
    uint8_t *record = malloc(info.erasesize);
    if (!record) die("BCB allocation");
    int valid[8], spare;
    int chosen = rr_load(fd, &info, record, valid, &spare);
    if (!strcmp(command, "bcb-status")) {
        printf("record_block=%d slot_a=%02x slot_b=%02x\n", chosen, record[0x365], record[0x366]);
    } else if (!strcmp(command, "bcb-read")) {
        FILE *f = fopen(argument, "wb");
        if (!f || fwrite(record, 1, 832, f) != 832 || fclose(f)) die("BCB record backup");
    } else if (!strcmp(command, "bcb-write")) {
        FILE *f = fopen(argument, "rb");
        if (!f || file_size(f) != 832 || fread(record, 1, 832, f) != 832) die("BCB record input must be 832 bytes");
        fclose(f);
        rr_store(fd, &info, record, valid, spare);
    } else {
        if (!argument || strlen(argument) != 1 || (*argument != 'a' && *argument != 'b')) {
            errno = EINVAL; die("expected slot a or b");
        }
        unsigned slot = (unsigned)(*argument - 'a');
        uint8_t *selected = record + 0x365 + slot, *other = record + 0x365 + (1 - slot);
        if (!strcmp(command, "bcb-activate")) {
            *selected = 0x7f; // Stock HAL: priority 15, seven tries, not yet successful.
            if ((*other & 15) == 15) *other = (*other & 0xf0) | 14;
        } else if (!strcmp(command, "bcb-success")) {
            *selected = (*selected & 15) | 0x80;
        } else { errno = EINVAL; die("unknown BCB operation"); }
        rr_store(fd, &info, record, valid, spare);
    }
    free(record);
    close(fd);
}

static int allow_device(const char *path) {
    int index, used = 0;
    if (sscanf(path, "/dev/mtd/mtd%d%n", &index, &used) != 1 || path[used] || index < 0) {
        fprintf(stderr, "Expected canonical /dev/mtd/mtdN\n");
        exit(2);
    }
    char namepath[128], name[80];
    snprintf(namepath, sizeof(namepath), "/sys/class/mtd/mtd%d/name", index);
    FILE *f = fopen(namepath, "r");
    if (!f || !fgets(name, sizeof(name), f)) die("MTD name");
    fclose(f);
    name[strcspn(name, "\r\n")] = 0;
    if (strcmp(name, "boot_a") && strcmp(name, "boot_b") && strcmp(name, "misc")) {
        fprintf(stderr, "Raw I/O allowed only for boot_a, boot_b and misc; UBI/bootloader/identity regions excluded\n");
        exit(2);
    }
    struct stat st;
    if (stat(path, &st) || !S_ISCHR(st.st_mode)) {
        errno = EINVAL;
        die("not an MTD character device");
    }
    int fd = open_dev(path, O_RDONLY);
    struct mtd_info_user info;
    get_info(fd, path, &info);
    close(fd);
    uint32_t expected = !strcmp(name, "misc") ? 2097152 : 15990784;
    if ((info.type != 4 && info.type != 8) || info.size != expected ||
        info.erasesize != 262144 || info.writesize != 4096) {
        errno = EINVAL;
        die("unexpected Crumpet NAND geometry");
    }
    return !strcmp(name, "misc");
}

int main(int argc, char **argv) {
    if (argc < 3) usage(argv[0]);
    int lock = open("/tmp/crumpet-mtd.lock", O_CREAT | O_RDWR, 0600);
    if (lock < 0 || flock(lock, LOCK_EX | LOCK_NB)) die("another MTD operation is running");
    int misc = allow_device(argv[2]);
    const char *command = argv[1];
    if (!strcmp(command, "bcb-status") && argc == 3 && misc) cmd_bcb(command, argv[2], NULL);
    else if ((!strcmp(command, "bcb-read") || !strcmp(command, "bcb-write") ||
              !strcmp(command, "bcb-activate") || !strcmp(command, "bcb-success")) && argc == 4 && misc)
        cmd_bcb(command, argv[2], argv[3]);
    else if (!strcmp(command, "info") && argc == 3) cmd_info(argv[2]);
    else if (!strcmp(command, "read") && argc == 6)
        cmd_read(argv[2], parse_u64(argv[3]), parse_u64(argv[4]), argv[5]);
    else if (!strcmp(command, "write") && argc == 5 && !misc)
        cmd_write(argv[2], parse_u64(argv[3]), argv[4], 1);
    else usage(argv[0]);
    close(lock);
    return 0;
}
