// Production RMW algorithm, with only the MTD syscalls replaced by a NAND model.
#undef NDEBUG
#include <assert.h>
#include <errno.h>
#include <fcntl.h>
#include <setjmp.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

static int model_open(const char*, int, ...);
static int model_ioctl(int, unsigned long, void*);
static ssize_t model_read(int, void*, size_t);
static ssize_t model_write(int, const void*, size_t);
static off_t model_seek(int, off_t, int);
static int model_close(int);
static int model_sync(int);
static void model_exit(int);
#define CRUMPET_NAND_TEST
#define LOCK_EX 2
#define LOCK_NB 4
#define flock(fd, flags) (0)
#define open(...) model_open(__VA_ARGS__)
#define ioctl(...) model_ioctl(__VA_ARGS__)
#define read(...) model_read(__VA_ARGS__)
#define write(...) model_write(__VA_ARGS__)
#define lseek(...) model_seek(__VA_ARGS__)
#define close(...) model_close(__VA_ARGS__)
#define fsync(...) model_sync(__VA_ARGS__)
#define exit(...) model_exit(__VA_ARGS__)
#define main crumpet_mtd_program_main
#include "../crumpet/nand/crumpet-mtd.c"
#undef main
#undef exit

enum { ERASE = 262144, CAPACITY = 8 * ERASE };
static unsigned char nand_data[CAPACITY], before[CAPACITY];
static size_t position;
static int erases, bad_block, interrupt_read, interrupt_write, corrupt_readback, wrote, fail_write;
static jmp_buf failure;
static int exit_status;

static int model_open(const char* path, int flags, ...) { (void)path; (void)flags; return 100; }
static int model_close(int fd) { assert(fd == 100); return 0; }
static int model_sync(int fd) { assert(fd == 100); return 0; }
static void model_exit(int code) { exit_status = code; longjmp(failure, 1); }

static int model_ioctl(int fd, unsigned long command, void* arg) {
    assert(fd == 100);
    if (command == MEMGETINFO) {
        struct mtd_info_user* info = arg;
        *info = (struct mtd_info_user){.type=4, .size=CAPACITY, .erasesize=ERASE, .writesize=4096, .oobsize=256};
        return 0;
    }
    if (command == MEMGETBADBLOCK) return (int)(*(uint64_t*)arg / ERASE) == bad_block;
    assert(command == MEMERASE);
    struct erase_info_user* range = arg;
    assert(range->start % ERASE == 0 && range->length % ERASE == 0);
    assert(range->start + range->length <= CAPACITY);
    for (size_t block = range->start; block < range->start + range->length; block += ERASE)
        assert((int)(block / ERASE) != bad_block);
    memset(nand_data + range->start, 255, range->length);
    erases += range->length / ERASE;
    return 0;
}

static off_t model_seek(int fd, off_t offset, int mode) {
    assert(fd == 100 && mode == SEEK_SET && offset >= 0 && offset <= CAPACITY);
    position = (size_t)offset;
    return offset;
}

static ssize_t model_read(int fd, void* out, size_t length) {
    assert(fd == 100);
    if (interrupt_read) { interrupt_read = 0; errno = EINTR; return -1; }
    if (length > 8192) length = 8192;
    assert(position + length <= CAPACITY);
    memcpy(out, nand_data + position, length);
    if (corrupt_readback && wrote && length) ((unsigned char*)out)[0] ^= 1;
    position += length;
    return (ssize_t)length;
}

static ssize_t model_write(int fd, const void* input, size_t length) {
    assert(fd == 100);
    if (interrupt_write) { interrupt_write = 0; errno = EINTR; return -1; }
    if (fail_write) { errno = EIO; return -1; }
    if (length > 4096) length = 4096;
    assert(position % 4096 == 0 && length % 4096 == 0 && position + length <= CAPACITY);
    const unsigned char* data = input;
    for (size_t i = 0; i < length; i++) {
        assert((nand_data[position+i] & data[i]) == data[i]);
        nand_data[position+i] = data[i];
    }
    position += length;
    wrote = 1;
    return (ssize_t)length;
}

static void reset_model(void) {
    for (size_t i=0; i<CAPACITY; i++) nand_data[i] = (unsigned char)(i * 31 + i / 4096);
    memcpy(before, nand_data, CAPACITY);
    position=0; erases=0; bad_block=-1; interrupt_read=0; interrupt_write=0;
    corrupt_readback=0; wrote=0; fail_write=0; exit_status=0;
}

static void input_file(const char* path, size_t size, unsigned char value) {
    FILE* f = fopen(path, "wb");
    assert(f);
    for (size_t i=0;i<size;i++) assert(fputc(value,f) != EOF);
    assert(fclose(f) == 0);
}

int main(int argc, char** argv) {
    assert(argc == 2);
    const char* image = argv[1];
    reset_model(); input_file(image,832,0);
    interrupt_read=1; interrupt_write=1;
    cmd_write("memory",0,image,1);
    assert(erases==1 && !memcmp(nand_data+832,before+832,CAPACITY-832));
    for (size_t i=0;i<832;i++) assert(nand_data[i]==0);
    assert(!memcmp(nand_data+0x360,before+0x360,7));

    reset_model(); input_file(image,8192,42);
    cmd_write("memory",ERASE-4096,image,1);
    assert(erases==2 && !memcmp(nand_data,before,ERASE-4096));
    assert(!memcmp(nand_data+ERASE+4096,before+ERASE+4096,CAPACITY-ERASE-4096));
    for(size_t i=ERASE-4096;i<ERASE+4096;i++) assert(nand_data[i]==42);

    reset_model(); input_file(image,2*ERASE,77); bad_block=1;
    if (!setjmp(failure)) { cmd_write("memory",0,image,1); assert(0); }
    assert(exit_status && erases==0 && !memcmp(nand_data,before,CAPACITY));

    reset_model(); input_file(image,4096,9); corrupt_readback=1;
    if (!setjmp(failure)) { cmd_write("memory",0,image,1); assert(0); }
    assert(exit_status && erases==1);

    reset_model(); input_file(image,4096,9); fail_write=1;
    if (!setjmp(failure)) { cmd_write("memory",0,image,1); assert(0); }
    assert(exit_status && erases==1);

    reset_model(); input_file(image,4096,9);
    if (!setjmp(failure)) { cmd_write("memory",CAPACITY-2048,image,1); assert(0); }
    assert(exit_status && erases==0 && !memcmp(nand_data,before,CAPACITY));

    // Real backups carry exactly this header in different nonzero RR blocks.
    reset_model(); memset(nand_data,255,CAPACITY);
    memset(nand_data+7*ERASE,0,ERASE);
    memcpy(nand_data+7*ERASE+0x360,"\0ABB\1\x8f\x8e\0",8);
    memcpy(before,nand_data,CAPACITY);
    cmd_bcb("bcb-activate","memory","b");
    assert(erases==2 && !memcmp(nand_data+0x360,"\0ABB\1\x8e\x7f\0",8));
    for(size_t i=7*ERASE;i<CAPACITY;i++) assert(nand_data[i]==255);
    assert(!memcmp(nand_data,before+7*ERASE,0x365));
    assert(!memcmp(nand_data+0x367,before+7*ERASE+0x367,ERASE-0x367));
    cmd_bcb("bcb-success","memory","b");
    assert(!memcmp(nand_data+ERASE+0x360,"\0ABB\1\x8e\x8f\0",8));

    // Failure before publishing the first page leaves the old record selected.
    reset_model(); memset(nand_data,255,CAPACITY);
    memset(nand_data+7*ERASE,0,ERASE);
    memcpy(nand_data+7*ERASE+0x360,"\0ABB\1\x8f\x8e\0",8);
    memcpy(before,nand_data,CAPACITY); fail_write=1;
    if (!setjmp(failure)) { cmd_bcb("bcb-activate","memory","b"); assert(0); }
    assert(exit_status && erases==1);
    assert(!memcmp(nand_data+7*ERASE,before+7*ERASE,ERASE));
    for(size_t i=0;i<4096;i++) assert(nand_data[i]==255);

    // An unknown/empty misc partition is never initialized by guessing.
    reset_model(); memset(nand_data,255,CAPACITY);
    if (!setjmp(failure)) { cmd_bcb("bcb-activate","memory","b"); assert(0); }
    assert(exit_status && erases==0);

    reset_model();
    for(int i=0;i<8;i++) memcpy(nand_data+i*ERASE+0x360,"\0ABB\1",5);
    if (!setjmp(failure)) { cmd_bcb("bcb-activate","memory","b"); assert(0); }
    assert(exit_status && erases==0);
    puts("MTD model: partial/cross-block RMW, preserved metadata, bad-block preflight, EINTR, short I/O, verification and failure cases passed");
    return 0;
}
