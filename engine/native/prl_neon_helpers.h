#ifndef PRL_NEON_HELPERS_H
#define PRL_NEON_HELPERS_H
/* Exact CPU-side packing/validation/folding only. No matrix multiplication.
 * Caller owns nonoverlapping arrays and NPU cache coherency.
 * PRL_USE_NEON=1 requires AArch64; scalar implementation otherwise.
 */
#include <stdint.h>
#include <stddef.h>
#include <math.h>
#include <float.h>
#ifdef __FAST_MATH__
#error "PRL exact helpers forbid fast-math"
#endif
_Static_assert(sizeof(float)==4 && FLT_RADIX==2 && FLT_MANT_DIG==24 && FLT_MAX_EXP==128,
               "IEEE binary32 is required");
#ifndef PRL_USE_NEON
#define PRL_USE_NEON 0
#endif
#if PRL_USE_NEON
#if !defined(__aarch64__)
#error "PRL_USE_NEON=1 requires AArch64"
#endif
#include <arm_neon.h>
#endif

static inline void prl_pack_i8_f32(float *restrict dst,const int8_t *restrict src,size_t count) {
    size_t i=0;
#if PRL_USE_NEON
    const size_t vector_end=count&~(size_t)15;
    for(;i<vector_end;i+=16) {
        int8x16_t bytes=vld1q_s8(src+i);
        int16x8_t lo=vmovl_s8(vget_low_s8(bytes));
        int16x8_t hi=vmovl_s8(vget_high_s8(bytes));
        vst1q_f32(dst+i,vcvtq_f32_s32(vmovl_s16(vget_low_s16(lo))));
        vst1q_f32(dst+i+4,vcvtq_f32_s32(vmovl_s16(vget_high_s16(lo))));
        vst1q_f32(dst+i+8,vcvtq_f32_s32(vmovl_s16(vget_low_s16(hi))));
        vst1q_f32(dst+i+12,vcvtq_f32_s32(vmovl_s16(vget_high_s16(hi))));
    }
#endif
    for(;i<count;++i)dst[i]=(float)src[i];
}

/* completed_segments is the number of prior successful K128 partials,0..31.
 * Each partial is checked finite, integral, and within +/-2097152 before cast.
 * State is checked against its proven invariant +/-completed_segments*2097152.
 * Thus each valid add is within +/-67108864, so INT32 addition cannot overflow.
 * A failure may leave an earlier prefix updated; the caller must poison context.
 */
static inline int prl_checked_accumulate_f32(int32_t *restrict state,
        const float *restrict partial,size_t count,unsigned completed_segments) {
    size_t i=0;int32_t state_bound;
    if(completed_segments>31)return 1;
    state_bound=(int32_t)(completed_segments*2097152U);
#if PRL_USE_NEON
    const size_t vector_end=count&~(size_t)3;
    const float32x4_t minimum=vdupq_n_f32(-2097152.0f),maximum=vdupq_n_f32(2097152.0f);
    const int32x4_t state_minimum=vdupq_n_s32(-state_bound),state_maximum=vdupq_n_s32(state_bound);
    for(;i<vector_end;i+=4) {
        float32x4_t value=vld1q_f32(partial+i);
        uint32x4_t valid=vandq_u32(vcgeq_f32(value,minimum),vcleq_f32(value,maximum));
        if(vminvq_u32(valid)!=UINT32_MAX)return 1;
        int32x4_t integer=vcvtq_s32_f32(value);
        valid=vceqq_f32(vcvtq_f32_s32(integer),value);
        int32x4_t old=vld1q_s32(state+i);
        valid=vandq_u32(valid,vandq_u32(vcgeq_s32(old,state_minimum),vcleq_s32(old,state_maximum)));
        if(vminvq_u32(valid)!=UINT32_MAX)return 1;
        vst1q_s32(state+i,vaddq_s32(old,integer));
    }
#endif
    for(;i<count;++i) {
        float value=partial[i];int32_t integer;
        if(!isfinite(value)||value < -2097152.0f||value > 2097152.0f)return 1;
        integer=(int32_t)value;
        if((float)integer!=value||state[i]<-state_bound||state[i]>state_bound)return 1;
        state[i]+=integer;
    }
    return 0;
}

/* Exact XOR over two selected64-column rows. No cross-subtile folding. */
static inline uint32_t prl_fold_pair64(const int32_t *row_a,const int32_t *row_b) {
#if PRL_USE_NEON
    uint32x4_t fold=vdupq_n_u32(0);
    for(unsigned j=0;j<64;j+=4)
        fold=veorq_u32(fold,veorq_u32(vreinterpretq_u32_s32(vld1q_s32(row_a+j)),
                                   vreinterpretq_u32_s32(vld1q_s32(row_b+j))));
    return vgetq_lane_u32(fold,0)^vgetq_lane_u32(fold,1)^vgetq_lane_u32(fold,2)^vgetq_lane_u32(fold,3);
#else
    uint32_t fold=0;
    for(unsigned j=0;j<64;++j)fold^=(uint32_t)row_a[j]^(uint32_t)row_b[j];
    return fold;
#endif
}
#endif
