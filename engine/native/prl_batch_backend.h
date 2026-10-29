#ifndef PRL_BATCH_BACKEND_H
#define PRL_BATCH_BACKEND_H
#include <stdint.h>
#ifdef __cplusplus
extern "C" {
#endif
enum { PRL_OK=0, PRL_ARGUMENT=1, PRL_DRIVER=2, PRL_DEADLINE=3,
       PRL_NUMERICAL=4, PRL_BUSY=5, PRL_POISONED=6 };
enum { PRL_BATCH_I32=0, PRL_BATCH_F32=1 };
typedef struct {
    uint64_t abi_version, struct_bytes, segments, executed_macs, completed_macs;
    uint64_t wall_ns, thread_cpu_ns, driver_wall_ns, driver_us, driver_cycles;
    int32_t status;
    uint32_t poisoned;
} prl_profile;
/* Optional detailed wall-clock profile, ABI1,112bytes. Stage intervals do not
 * overlap; timers themselves and other loop work are outside these intervals.
 * Failure exits may leave a partially populated final interval uncounted. */
typedef struct {
    uint64_t abi_version,struct_bytes,buffer_mode;
    uint64_t pack_a_ns,pack_b_ns,input_copy_ns,input_flush_ns,driver_ns;
    uint64_t query_profile_ns,output_invalidate_ns,output_copy_ns;
    uint64_t accumulate_ns,fold_ns,hash_ns;
} prl_detail;
int prl_batch_get_detail(void *context,prl_detail *out);
/* One context/process. Linux little endian. Native model A[128,256],B[256,128],C[256,256],
 * datatype INT32 or FP32 as selected, NONE quantization. No CPU MatMul fallback.
 * Model and library hashes must be bound by the caller's validation gate. */
void *prl_batch_create(const char *model_path,unsigned format);
/* An[M,4096], Bn[4096,N], signed INT8 row major; immutable until return.
 * M/N in[256,16384], multiples256; bases multiples256 within arrays.
 * hashes_bytes must equal16384. On success512 hashes, macs=268,435,456.
 * Order: hash_index=(rb*4+cb)*32+i, rb/cb in[0,3], i in[0,31].
 * Candidate rows [row_base+rb*64+i, +32]; cols[col_base+cb*64, +63].
 * Every128-K partial is cumulatively folded into its own tile transcript.
 * Profile ABI1. Invalid args do not poison; driver/numerical/deadline do.
 * Whole scan8s budget, dispatch500ms requested. Separate process watchdog
 * remains necessary for driver calls that do not return. */
int prl_scan_batch(void *context,const int8_t *an,uint64_t a_bytes,
                   const int8_t *bn,uint64_t b_bytes,
                   uint32_t m,uint32_t n,uint32_t k,
                   uint32_t row_base,uint32_t col_base,
                   const uint8_t commit_a[32],uint8_t *hashes_out,
                   uint64_t hashes_bytes,uint64_t *macs_out);
int prl_batch_get_profile(void *context,prl_profile *out);
const char *prl_batch_last_error(void);
void prl_batch_destroy(void *context);
#ifdef __cplusplus
}
#endif
#endif
