/* C reference contestant: libzstd's one-shot API, same CLI contract as the
 * Rust contestants. One process, one call, no shell in between.
 *
 *   zstd-c-driver encode <in> <out> --level N
 *   zstd-c-driver decode <in> <out>
 *
 * Links libzstd (static in CI, from the pinned v1.5.7 source build).
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include <zstd.h>

static unsigned char *read_file(const char *path, size_t *len) {
    FILE *f = fopen(path, "rb");
    if (!f) return NULL;
    if (fseek(f, 0, SEEK_END) != 0) { fclose(f); return NULL; }
    long sz = ftell(f);
    if (sz < 0) { fclose(f); return NULL; }
    rewind(f);
    unsigned char *buf = (unsigned char *)malloc((size_t)sz ? (size_t)sz : 1);
    if (!buf) { fclose(f); return NULL; }
    size_t got = fread(buf, 1, (size_t)sz, f);
    fclose(f);
    if (got != (size_t)sz) { free(buf); return NULL; }
    *len = got;
    return buf;
}

static int write_file(const char *path, const void *data, size_t len) {
    FILE *f = fopen(path, "wb");
    if (!f) {
        fprintf(stderr, "cannot write %s\n", path);
        return 1;
    }
    size_t put = fwrite(data, 1, len, f);
    int bad = fclose(f) != 0 || put != len;
    return bad ? 1 : 0;
}

int main(int argc, char **argv) {
    if (argc < 4) {
        fprintf(stderr, "usage: %s <encode|decode> <in> <out> [--level N]\n", argv[0]);
        return 2;
    }
    const char *op = argv[1];
    const char *inp = argv[2];
    const char *outp = argv[3];
    int level = 1;
    for (int i = 4; i < argc; i++) {
        if (strcmp(argv[i], "--level") == 0 && i + 1 < argc) {
            level = atoi(argv[++i]);
        }
    }

    size_t n = 0;
    unsigned char *data = read_file(inp, &n);
    if (!data) {
        fprintf(stderr, "cannot read %s\n", inp);
        return 1;
    }

    if (strcmp(op, "encode") == 0) {
        size_t cap = ZSTD_compressBound(n);
        if (cap == 0) cap = 1;
        unsigned char *out = (unsigned char *)malloc(cap);
        if (!out) { free(data); return 1; }
        size_t r = ZSTD_compress(out, cap, data, n, level);
        if (ZSTD_isError(r)) {
            fprintf(stderr, "compress: %s\n", ZSTD_getErrorName(r));
            return 1;
        }
        if (write_file(outp, out, r)) { free(out); free(data); return 1; }
        printf("{\"impl\":\"zstd-c\",\"version\":\"%s\",\"op\":\"encode\","
               "\"level_requested\":%d,\"level_effective\":%d,\"in\":%zu,\"out\":%zu}\n",
               ZSTD_versionString(), level, level, n, r);
        free(out);
    } else if (strcmp(op, "decode") == 0) {
        unsigned long long csize = ZSTD_getFrameContentSize(data, n);
        size_t cap = (csize == ZSTD_CONTENTSIZE_UNKNOWN || csize == ZSTD_CONTENTSIZE_ERROR)
                         ? (size_t)1 << 31
                         : (size_t)csize;
        if (cap == 0) cap = 1;  /* empty frame: one byte of slack keeps zstd happy */
        unsigned char *out = (unsigned char *)malloc(cap);
        ZSTD_DCtx *dctx = ZSTD_createDCtx();
        if (!out || !dctx) { free(out); free(data); ZSTD_freeDCtx(dctx); return 1; }
        size_t r = ZSTD_decompressDCtx(dctx, out, cap, data, n);
        if (ZSTD_isError(r)) {
            fprintf(stderr, "decompress: %s\n", ZSTD_getErrorName(r));
            ZSTD_freeDCtx(dctx);
            free(out);
            free(data);
            return 1;
        }
        int bad = write_file(outp, out, r);
        printf("{\"impl\":\"zstd-c\",\"version\":\"%s\",\"op\":\"decode\",\"in\":%zu,\"out\":%zu}\n",
               ZSTD_versionString(), n, r);
        ZSTD_freeDCtx(dctx);
        free(out);
        if (bad) { free(data); return 1; }
    } else {
        fprintf(stderr, "unknown op: %s\n", op);
        free(data);
        return 2;
    }
    free(data);
    return 0;
}
