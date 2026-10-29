#ifndef PRL_A_CACHE_H
#define PRL_A_CACHE_H
/* Exact reusable packing only. No matrix multiplication or pool/proof logic.
 * Caller must keep source immutable throughout a scan. The source snapshot
 * makes cache reuse safe across changed contents at the same address.
 */
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#define PRL_A_ROWS 256U
#define PRL_A_K 4096U
#define PRL_A_SEGMENT_K 128U
#define PRL_A_RAW_BYTES ((size_t)PRL_A_ROWS*PRL_A_K)
#define PRL_A_PACKED_BYTES (PRL_A_RAW_BYTES*sizeof(float))
typedef struct {
    int8_t *raw;
    float *packed;
    uint8_t commit[32];
    uint32_t m,k,row_base;
    int valid;
    uint64_t hits,misses,validated_bytes;
} prl_a_cache;

static int prl_a_cache_init(prl_a_cache *cache) {
    if(!cache)return 0;
    memset(cache,0,sizeof(*cache));
    cache->raw=malloc(PRL_A_RAW_BYTES);
    cache->packed=malloc(PRL_A_PACKED_BYTES);
    if(!cache->raw||!cache->packed) {
        free(cache->raw);free(cache->packed);memset(cache,0,sizeof(*cache));return 0;
    }
    return 1;
}
static void prl_a_cache_free(prl_a_cache *cache) {
    if(!cache)return;
    free(cache->raw);free(cache->packed);memset(cache,0,sizeof(*cache));
}
/* Return1 for exact hit,0 for repack,-1 for invalid arguments/state. The raw
 * pointer is the start of the requested contiguous256x4096 row block.
 * Comparing all raw bytes, not a pointer or hash, is the final reuse gate.
 */
static int prl_a_cache_prepare(prl_a_cache *cache,const int8_t *raw,
                              uint32_t m,uint32_t k,uint32_t row_base,
                              const uint8_t commit[32]) {
    unsigned p,r,t;
    if(!cache||!cache->raw||!cache->packed||!raw||!commit||k!=PRL_A_K||
       m<PRL_A_ROWS||m>16384||m%PRL_A_ROWS||row_base%PRL_A_ROWS||
       row_base>m-PRL_A_ROWS)return -1;
    if(cache->valid&&cache->m==m&&cache->k==k&&cache->row_base==row_base&&
       !memcmp(cache->commit,commit,32)) {
        cache->validated_bytes+=PRL_A_RAW_BYTES;
        if(!memcmp(cache->raw,raw,PRL_A_RAW_BYTES)) { ++cache->hits;return 1; }
    }
    cache->valid=0;
    memcpy(cache->raw,raw,PRL_A_RAW_BYTES);
    for(p=0;p<PRL_A_K/PRL_A_SEGMENT_K;++p)
        for(r=0;r<PRL_A_ROWS;++r)for(t=0;t<PRL_A_SEGMENT_K;++t)
            cache->packed[((size_t)p*PRL_A_ROWS+r)*PRL_A_SEGMENT_K+t]=
                (float)cache->raw[(size_t)r*PRL_A_K+p*PRL_A_SEGMENT_K+t];
    cache->m=m;cache->k=k;cache->row_base=row_base;
    memcpy(cache->commit,commit,32);++cache->misses;cache->valid=1;return 0;
}
static const float *prl_a_cache_segment(const prl_a_cache *cache,unsigned segment) {
    if(!cache||!cache->valid||segment>=PRL_A_K/PRL_A_SEGMENT_K)return NULL;
    return cache->packed+(size_t)segment*PRL_A_ROWS*PRL_A_SEGMENT_K;
}
#endif
