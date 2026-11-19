// SPDX-License-Identifier: Apache-2.0
// Linux UBI ioctls keep ECC, wear levelling and bad-block handling in the kernel.
#define _GNU_SOURCE
#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <inttypes.h>
#include <limits.h>
#include <mtd/ubi-user.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/file.h>
#include <sys/ioctl.h>
#include <sys/stat.h>
#include <sys/sysmacros.h>
#include <unistd.h>

static void fail(const char *message) {
    fprintf(stderr, "crumpet-ubi: %s: %s\n", message, strerror(errno));
    exit(1);
}

static void reject(const char *message) {
    errno = EINVAL;
    fail(message);
}

static void text(const char *path, char *out, size_t length) {
    FILE *f = fopen(path, "r");
    if (!f || !fgets(out, (int)length, f)) fail(path);
    out[strcspn(out, "\r\n")] = 0;
    fclose(f);
}

static uint64_t number(const char *path) {
    char value[80], *end;
    text(path, value, sizeof(value));
    if (value[0] < '0' || value[0] > '9') reject(path);
    errno = 0;
    unsigned long long n = strtoull(value, &end, 10);
    if (errno || end == value || *end) reject(path);
    return n;
}

static void node(const char *sysfs, const char *path, int block) {
    char value[80], attr[PATH_MAX];
    unsigned major_number, minor_number;
    snprintf(attr, sizeof(attr), "%s/dev", sysfs);
    text(attr, value, sizeof(value));
    if (sscanf(value, "%u:%u", &major_number, &minor_number) != 2) reject(attr);
    dev_t device = makedev(major_number, minor_number);
    struct stat st;
    if (lstat(path, &st) == 0) {
        if (!(block ? S_ISBLK(st.st_mode) : S_ISCHR(st.st_mode)) || st.st_rdev != device)
            reject("existing device node differs from sysfs");
    } else if (errno != ENOENT || mknod(path, (block ? S_IFBLK : S_IFCHR) | 0600, device)) {
        fail(path);
    }
}

static int mtd_index(const char *label) {
    uint64_t expected = !strcmp(label, "persist") ? 8388608 :
                        !strcmp(label, "userdata") ? 469237760 : 0;
    if (!expected) reject("only persist and userdata can be attached");
    DIR *dir = opendir("/sys/class/mtd");
    if (!dir) fail("MTD sysfs");
    struct dirent *entry;
    int found = -1;
    while ((entry = readdir(dir))) {
        int index, consumed = 0;
        if (sscanf(entry->d_name, "mtd%d%n", &index, &consumed) != 1 || entry->d_name[consumed]) continue;
        char path[PATH_MAX], name[80];
        snprintf(path, sizeof(path), "/sys/class/mtd/mtd%d/name", index);
        text(path, name, sizeof(name));
        if (strcmp(name, label)) continue;
        if (found != -1) reject("ambiguous MTD label");
        snprintf(path, sizeof(path), "/sys/class/mtd/mtd%d/size", index);
        if (number(path) != expected) reject("unexpected MTD size");
        snprintf(path, sizeof(path), "/sys/class/mtd/mtd%d/erasesize", index);
        if (number(path) != 262144) reject("unexpected NAND erase size");
        snprintf(path, sizeof(path), "/sys/class/mtd/mtd%d/writesize", index);
        if (number(path) != 4096) reject("unexpected NAND page size");
        found = index;
    }
    closedir(dir);
    if (found < 0) reject("MTD label not found");
    return found;
}

static int attached(int mtd) {
    DIR *dir = opendir("/sys/class/ubi");
    if (!dir) fail("UBI sysfs");
    struct dirent *entry;
    int found = -1;
    while ((entry = readdir(dir))) {
        int index, consumed = 0;
        if (sscanf(entry->d_name, "ubi%d%n", &index, &consumed) != 1 || entry->d_name[consumed]) continue;
        char path[PATH_MAX];
        snprintf(path, sizeof(path), "/sys/class/ubi/ubi%d/mtd_num", index);
        if (number(path) == (uint64_t)mtd) {
            if (found != -1) reject("MTD attached more than once");
            found = index;
        }
    }
    closedir(dir);
    return found;
}

static void attach(const char *label) {
    int mtd = mtd_index(label), ubi = attached(mtd);
    if (ubi < 0) {
        node("/sys/class/misc/ubi_ctrl", "/dev/ubi_ctrl", 0);
        int fd = open("/dev/ubi_ctrl", O_RDWR | O_CLOEXEC);
        if (fd < 0) fail("ubi_ctrl");
        struct ubi_attach_req request = { .ubi_num = -1, .mtd_num = mtd };
        if (ioctl(fd, UBI_IOCATT, &request) < 0 && errno != EEXIST) fail("UBI attach");
        close(fd);
        ubi = attached(mtd);
        if (ubi < 0) reject("UBI attach produced no matching device");
    }
    printf("%s: mtd%d -> ubi%d\n", label, mtd, ubi);
}

static int volume(const char *path, char *name, size_t name_size, char *sysfs, size_t sysfs_size) {
    int ubi, vol, consumed = 0;
    if (sscanf(path, "/dev/ubi%d_%d%n", &ubi, &vol, &consumed) != 2 || path[consumed] || ubi < 0 || vol < 0)
        reject("expected a canonical /dev/ubiN_M volume");
    snprintf(sysfs, sysfs_size, "/sys/class/ubi/ubi%d_%d", ubi, vol);
    char attr[PATH_MAX];
    snprintf(attr, sizeof(attr), "%s/name", sysfs);
    text(attr, name, name_size);
    const char *parent = !strcmp(name, "persist") ? "persist" : "userdata";
    if (strcmp(name, "persist") && strcmp(name, "system_a") && strcmp(name, "system_b") &&
        strcmp(name, "userdata") && strcmp(name, "cache")) reject("unsupported volume");
    snprintf(attr, sizeof(attr), "/sys/class/ubi/ubi%d/mtd_num", ubi);
    if (number(attr) != (uint64_t)mtd_index(parent)) reject("volume has the wrong MTD parent");
    node(sysfs, path, 0);
    return ubi;
}

static void block(const char *path) {
    char name[80], sysfs[PATH_MAX], block_sysfs[PATH_MAX], block_path[80];
    volume(path, name, sizeof(name), sysfs, sizeof(sysfs));
    if (strcmp(name, "system_a") && strcmp(name, "system_b")) reject("only systems use ubiblock");
    int fd = open(path, O_RDONLY | O_CLOEXEC);
    if (fd < 0) fail(path);
    struct ubi_blkcreate_req request = {{0}};
    if (ioctl(fd, UBI_IOCVOLCRBLK, &request) < 0 && errno != EEXIST) fail("create ubiblock");
    close(fd);
    snprintf(block_sysfs, sizeof(block_sysfs), "/sys/class/block/ubiblock%s", path + 8);
    snprintf(block_path, sizeof(block_path), "/dev/block/ubiblock%s", path + 8);
    node(block_sysfs, block_path, 1);
}

static ssize_t read_retry(int fd, void *data, size_t size) {
    ssize_t n;
    do { n = read(fd, data, size); } while (n < 0 && errno == EINTR);
    return n;
}

static void update(const char *path, const char *image) {
    char name[80], sysfs[PATH_MAX], attr[PATH_MAX];
    volume(path, name, sizeof(name), sysfs, sizeof(sysfs));
    if (!strcmp(name, "persist")) reject("persist restore requires independent device-identity verification");
    snprintf(attr, sizeof(attr), "%s/reserved_ebs", sysfs);
    uint64_t count = number(attr);
    snprintf(attr, sizeof(attr), "%s/usable_eb_size", sysfs);
    uint64_t leb = number(attr);
    if (!leb || count > UINT64_MAX / leb) reject("invalid UBI capacity");
    int input = open(image, O_RDONLY | O_CLOEXEC);
    struct stat st;
    if (input < 0 || fstat(input, &st)) fail(image);
    if (!S_ISREG(st.st_mode) || st.st_size < 4096 || (uint64_t)st.st_size > count * leb) reject("invalid image size");
    unsigned char header[96];
    size_t read_bytes = 0;
    while (read_bytes < sizeof(header)) {
        ssize_t got = read_retry(input, header + read_bytes, sizeof(header) - read_bytes);
        if (got <= 0) fail("image header");
        read_bytes += (size_t)got;
    }
    const unsigned char ubifs[4] = {0x31, 0x18, 0x10, 0x06};
    int system = !strncmp(name, "system_", 7);
    if (memcmp(header, system ? (const unsigned char *)"hsqs" : ubifs, 4)) reject("wrong filesystem image type");
    if (system) {
        uint64_t used = 0;
        for (int i = 7; i >= 0; --i) used = (used << 8) | header[40 + i];
        if (header[28] != 4 || header[29] || header[30] || header[31] ||
            used < sizeof(header) || used > (uint64_t)st.st_size) reject("invalid SquashFS superblock");
    } else if (header[20] != 6) {
        reject("UBIFS image must start with a superblock node");
    }
    if (lseek(input, 0, SEEK_SET) < 0) fail("rewind image");
    // Kernel removal refuses a mounted/in-use block device. No forced unmount.
    if (system) {
        int fd = open(path, O_RDONLY | O_CLOEXEC);
        if (fd < 0) fail(path);
        if (ioctl(fd, UBI_IOCVOLRMBLK) < 0 && errno != ENOENT && errno != ENODEV) fail("remove in-use ubiblock");
        close(fd);
    }
    int output = open(path, O_RDWR | O_CLOEXEC);
    if (output < 0) fail(path);
    int64_t size = st.st_size;
    if (ioctl(output, UBI_IOCVOLUP, &size)) fail("start UBI volume update");
    char buffer[65536];
    int64_t remaining = size;
    while (remaining) {
        size_t amount = remaining > (int64_t)sizeof(buffer) ? sizeof(buffer) : (size_t)remaining;
        ssize_t got = read_retry(input, buffer, amount);
        if (got <= 0) fail("short image read; UBI update is incomplete");
        ssize_t done = 0;
        while (done < got) {
            ssize_t n = write(output, buffer + done, (size_t)(got - done));
            if (n < 0 && errno == EINTR) continue;
            if (n <= 0) fail("UBI write; update is incomplete");
            done += n;
        }
        remaining -= got;
    }
    if (close(output)) fail("close updated UBI volume");
    output = open(path, O_RDONLY | O_CLOEXEC);
    if (output < 0 || lseek(input, 0, SEEK_SET) < 0) fail("open UBI readback");
    char actual[sizeof(buffer)];
    remaining = size;
    while (remaining) {
        size_t amount = remaining > (int64_t)sizeof(buffer) ? sizeof(buffer) : (size_t)remaining;
        ssize_t got = read_retry(input, buffer, amount), done = 0;
        if (got <= 0) fail("readback source");
        while (done < got) {
            ssize_t n = read_retry(output, actual + done, (size_t)(got - done));
            if (n <= 0) fail("short UBI readback");
            done += n;
        }
        if (memcmp(buffer, actual, (size_t)got)) reject("UBI readback differs");
        remaining -= got;
    }
    close(output);
    close(input);
    snprintf(attr, sizeof(attr), "%s/upd_marker", sysfs);
    if (number(attr)) reject("UBI update marker is still set");
    snprintf(attr, sizeof(attr), "%s/corrupted", sysfs);
    if (number(attr)) reject("UBI volume is marked corrupt");
    if (system) block(path);
    printf("%s: verified %" PRId64 " image bytes\n", name, size);
}

int main(int argc, char **argv) {
    int lock = open("/tmp/crumpet-ubi.lock", O_CREAT | O_RDWR | O_CLOEXEC, 0600);
    if (lock < 0 || flock(lock, LOCK_EX | LOCK_NB)) fail("another UBI operation is running");
    if (argc == 3 && !strcmp(argv[1], "attach")) attach(argv[2]);
    else if (argc == 3 && !strcmp(argv[1], "volume")) {
        char name[80], sysfs[PATH_MAX];
        volume(argv[2], name, sizeof(name), sysfs, sizeof(sysfs));
        puts(name);
    }
    else if (argc == 3 && !strcmp(argv[1], "block")) block(argv[2]);
    else if (argc == 4 && !strcmp(argv[1], "update")) update(argv[2], argv[3]);
    else reject("usage: crumpet-ubi attach LABEL | volume /dev/ubiN_M | block /dev/ubiN_M | update /dev/ubiN_M IMAGE");
    close(lock);
    return 0;
}
