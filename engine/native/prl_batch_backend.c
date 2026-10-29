/* Real prepared-data PRL arithmetic backend. All matrix products run on NPU.
 * CPU only packs signed inputs, accumulates partials, folds and hashes.
 * The caller is responsible for protocol preparation/proof and job freshness.
 */
#define _POSIX_C_SOURCE 200809L
#include "prl_batch_backend.h"
#include <vip_lite.h>
#include <stdatomic.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <math.h>
#include <float.h>
#ifdef __FAST_MATH__
#error "Exact PRL backend forbids fast-math"
#endif
_Static_assert(sizeof(float)==4 && FLT_RADIX==2 && FLT_MANT_DIG==24 && FLT_MAX_EXP==128, "IEEE binary32 required");
#ifndef PRL_BUFFER_MODE
#define PRL_BUFFER_MODE 0
#endif
#if PRL_BUFFER_MODE < 0 || PRL_BUFFER_MODE > 2
#error "PRL_BUFFER_MODE must be0(default),1(cached),2(scratch)"
#endif
#include "prl_neon_helpers.h"
#ifndef PRL_A_CACHE
#define PRL_A_CACHE 0
#endif
#if PRL_A_CACHE != 0 && PRL_A_CACHE != 1
#error "PRL_A_CACHE must be0 or1"
#endif
#if PRL_A_CACHE
#include "prl_a_cache.h"
#endif
#define SEGMENTS 32U
#define BATCH_SIZE 256U
#define SUBTILES (BATCH_SIZE/64U)
#define HASH_COUNT (SUBTILES*SUBTILES*32U)
#define HASH_BYTES (HASH_COUNT*32U)
#define ELEMENTS (BATCH_SIZE*BATCH_SIZE)
#define INPUT_ELEMENTS (BATCH_SIZE*128U)
#define TIMEOUT_MS 500U
#define BUDGET_NS 8000000000ULL
#define PARTIAL_MACS ((uint64_t)BATCH_SIZE*BATCH_SIZE*128ULL)
#define TILE_MACS ((uint64_t)BATCH_SIZE*BATCH_SIZE*4096ULL)
#define MAGIC 0x50524c43U
static atomic_flag runtime_owned=ATOMIC_FLAG_INIT;
static _Thread_local char last_error[512];
typedef struct {
    uint32_t magic;
    int initialized, prepared, poisoned;
    unsigned format;
    atomic_flag busy;
    vip_network net;
    vip_buffer buffers[3];
    void *mapped[3];
    prl_profile profile;
    prl_detail detail;
    void *scratch[3];
#if PRL_A_CACHE
    prl_a_cache a_cache;
#endif
} context;
static int fail(int code,const char *fmt,...) {
    va_list ap;va_start(ap,fmt);vsnprintf(last_error,sizeof(last_error),fmt,ap);va_end(ap);return code;
}
const char *prl_batch_last_error(void) { return last_error; }
static uint64_t ticks(clockid_t id) {
    struct timespec t;
    if(clock_gettime(id,&t))return 0;
    return (uint64_t)t.tv_sec*1000000000ULL+(uint64_t)t.tv_nsec;
}
static int query(vip_network net,unsigned idx,int output,unsigned x,unsigned y,unsigned format,vip_buffer_create_params_t *p) {
    vip_status_e st;memset(p,0,sizeof(*p));
#define Q(prop,ptr) do { st=output?vip_query_output(net,idx,prop,ptr):vip_query_input(net,idx,prop,ptr);if(st!=VIP_SUCCESS)return 0; } while(0)
    Q(VIP_BUFFER_PROP_NUM_OF_DIMENSION,&p->num_of_dims);
    Q(VIP_BUFFER_PROP_SIZES_OF_DIMENSION,p->sizes);
    Q(VIP_BUFFER_PROP_DATA_FORMAT,&p->data_format);
    Q(VIP_BUFFER_PROP_QUANT_FORMAT,&p->quant_format);
#undef Q
    if(p->num_of_dims!=2||p->sizes[0]!=x||p->sizes[1]!=y||p->data_format!=(format==PRL_BATCH_F32?VIP_BUFFER_FORMAT_FP32:VIP_BUFFER_FORMAT_INT32)||p->quant_format!=VIP_BUFFER_QUANTIZE_NONE)return 0;
    p->memory_type=VIP_BUFFER_MEMORY_TYPE_DEFAULT;return 1;
}
static uint32_t rotr(uint32_t v,unsigned n) { return(v>>n)|(v<<(32-n)); }
static void g(uint32_t *v,unsigned a,unsigned b,unsigned c,unsigned d,uint32_t x,uint32_t y) {
    v[a]+=v[b]+x;v[d]=rotr(v[d]^v[a],16);v[c]+=v[d];v[b]=rotr(v[b]^v[c],12);
    v[a]+=v[b]+y;v[d]=rotr(v[d]^v[a],8);v[c]+=v[d];v[b]=rotr(v[b]^v[c],7);
}
static void keyed64(uint32_t out[8],const uint32_t key[8],const uint32_t in[16]) {
    const uint32_t iv[4]={0x6A09E667,0xBB67AE85,0x3C6EF372,0xA54FF53A};
    const unsigned perm[16]={2,6,3,10,7,0,4,13,1,11,12,5,9,14,15,8};
    uint32_t v[16],m[16],next[16];unsigned i,r;
    memcpy(v,key,32);memcpy(v+8,iv,16);v[12]=v[13]=0;v[14]=64;v[15]=27;memcpy(m,in,64);
    for(r=0;r<7;++r) {
        g(v,0,4,8,12,m[0],m[1]);g(v,1,5,9,13,m[2],m[3]);g(v,2,6,10,14,m[4],m[5]);g(v,3,7,11,15,m[6],m[7]);
        g(v,0,5,10,15,m[8],m[9]);g(v,1,6,11,12,m[10],m[11]);g(v,2,7,8,13,m[12],m[13]);g(v,3,4,9,14,m[14],m[15]);
        for(i=0;i<16;++i)next[i]=m[perm[i]];
        memcpy(m,next,sizeof(m));
    }
    for(i=0;i<8;++i)out[i]=v[i]^v[i+8];
}
static void cleanup(context *c) {
    unsigned i;
    /* A poisoned driver's teardown may itself block: caller's watchdog owns
     * final process termination. No CPU fallback or automatic retry here. */
    if(c->prepared)(void)vip_finish_network(c->net);
    for(i=0;i<3;++i) {
        if(c->mapped[i])(void)vip_unmap_buffer(c->buffers[i]);
        if(c->buffers[i])(void)vip_destroy_buffer(c->buffers[i]);
        free(c->scratch[i]);
    }
    if(c->net)(void)vip_destroy_network(c->net);
    if(c->initialized)(void)vip_destroy();
#if PRL_A_CACHE
    prl_a_cache_free(&c->a_cache);
#endif
    c->magic=0;free(c);atomic_flag_clear(&runtime_owned);
}
void *prl_batch_create(const char *model_path,unsigned format) {
    context *c;unsigned version,cid=0,count=0,i;uint32_t endian=1;
    const size_t sizes[3]={INPUT_ELEMENTS*4,INPUT_ELEMENTS*4,ELEMENTS*4};
    vip_buffer_create_params_t params[3];vip_uint32_t timeout=TIMEOUT_MS;
    last_error[0]=0;
    if(!model_path||!model_path[0]||*(uint8_t*)&endian!=1||format>PRL_BATCH_F32) { fail(PRL_ARGUMENT,"Nonempty model path, little-endian host and format 0/1 required");return NULL; }
    if(atomic_flag_test_and_set(&runtime_owned)) { fail(PRL_BUSY,"Only one VIPLite context per process is allowed");return NULL; }
    c=calloc(1,sizeof(*c));
    if(!c) { atomic_flag_clear(&runtime_owned);fail(PRL_ARGUMENT,"Context allocation failed");return NULL; }
    atomic_flag_clear(&c->busy);c->magic=MAGIC;c->format=format;
#define INIT_CALL(x) do { vip_status_e s=(x);if(s!=VIP_SUCCESS) { fail(PRL_DRIVER,"%s failed status=%d",#x,(int)s);goto bad; } } while(0)
    version=vip_get_version();
    if(version<0x20000U||version>=0x30000U) { fail(PRL_DRIVER,"VIPLite v2 required, got %u",version);goto bad; }
    INIT_CALL(vip_init());c->initialized=1;
    INIT_CALL(vip_query_hardware(VIP_QUERY_HW_PROP_CID,sizeof(cid),&cid));
    if(cid!=0x1000003bU) { fail(PRL_DRIVER,"Wrong NPU CID %08x",cid);goto bad; }
    INIT_CALL(vip_create_network(model_path,0,VIP_CREATE_NETWORK_FROM_FILE,&c->net));
    INIT_CALL(vip_query_network(c->net,VIP_NETWORK_PROP_INPUT_COUNT,&count));
    if(count!=2) { fail(PRL_DRIVER,"Model input count must be2");goto bad; }
    INIT_CALL(vip_query_network(c->net,VIP_NETWORK_PROP_OUTPUT_COUNT,&count));
    if(count!=1) { fail(PRL_DRIVER,"Model output count must be1");goto bad; }
    if(!query(c->net,0,0,128,BATCH_SIZE,format,&params[0])||!query(c->net,1,0,BATCH_SIZE,128,format,&params[1])||!query(c->net,0,1,BATCH_SIZE,BATCH_SIZE,format,&params[2])) {
        fail(PRL_DRIVER,"Model shape/format mismatch: need selected I32/F32 NONE A[128,256],B[256,128],C[256,256]");goto bad;
    }
    for(i=0;i<3;++i) {
#if PRL_BUFFER_MODE == 1
        INIT_CALL(vip_create_buffer_cache(&params[i],sizeof(params[i]),&c->buffers[i]));
#else
        INIT_CALL(vip_create_buffer(&params[i],sizeof(params[i]),&c->buffers[i]));
#endif
        if(vip_get_buffer_size(c->buffers[i])!=sizes[i]) { fail(PRL_DRIVER,"Unexpected buffer %u byte size",i);goto bad; }
        c->mapped[i]=vip_map_buffer(c->buffers[i]);
        if(!c->mapped[i]) { fail(PRL_DRIVER,"Mapping buffer %u failed",i);goto bad; }
#if PRL_BUFFER_MODE == 2
        if(posix_memalign(&c->scratch[i],64,sizes[i])) { fail(PRL_ARGUMENT,"Scratch buffer %u allocation failed",i);goto bad; }
#endif
    }
#if PRL_A_CACHE
    if(format==PRL_BATCH_F32&&!prl_a_cache_init(&c->a_cache)) { fail(PRL_ARGUMENT,"A cache allocation failed");goto bad; }
#endif
    INIT_CALL(vip_set_network(c->net,VIP_NETWORK_PROP_SET_TIME_OUT,&timeout));
    INIT_CALL(vip_prepare_network(c->net));c->prepared=1;
    INIT_CALL(vip_set_input(c->net,0,c->buffers[0]));INIT_CALL(vip_set_input(c->net,1,c->buffers[1]));
    INIT_CALL(vip_set_output(c->net,0,c->buffers[2]));
    c->profile.abi_version=1;c->profile.struct_bytes=sizeof(c->profile);
    c->detail.abi_version=1;c->detail.struct_bytes=sizeof(c->detail);c->detail.buffer_mode=PRL_BUFFER_MODE;return c;
bad:
    cleanup(c);return NULL;
#undef INIT_CALL
}
int prl_scan_batch(void *opaque,const int8_t *an,uint64_t a_bytes,const int8_t *bn,uint64_t b_bytes,
                  uint32_t m,uint32_t n,uint32_t k,uint32_t row_base,uint32_t col_base,
                  const uint8_t commit_a[32],uint8_t *hashes_out,uint64_t hashes_bytes,uint64_t *macs_out) {
    context *c=opaque;int rc=PRL_OK;unsigned p,r,j,t,rb,cb,hi;uint64_t start,cpu_start,now,dispatch,phase;
    int32_t state[ELEMENTS]={0};uint32_t trans[HASH_COUNT][16]={{0}},key[8],hashes[HASH_COUNT][8];
    last_error[0]=0;
    if(hashes_out&&hashes_bytes>=HASH_BYTES)memset(hashes_out,0,HASH_BYTES);
    if(macs_out)*macs_out=0;
    if(!c||c->magic!=MAGIC||!an||!bn||!commit_a||!hashes_out||hashes_bytes!=HASH_BYTES||!macs_out||k!=4096||
       m<BATCH_SIZE||n<BATCH_SIZE||m>16384||n>16384||m%BATCH_SIZE||n%BATCH_SIZE||row_base%BATCH_SIZE||col_base%BATCH_SIZE||
       row_base>m-BATCH_SIZE||col_base>n-BATCH_SIZE||a_bytes!=(uint64_t)m*k||b_bytes!=(uint64_t)k*n)
        return fail(PRL_ARGUMENT,"Invalid pointer/shape/byte-size/base: row-major Mx4096 and4096xN required");
    if(atomic_flag_test_and_set(&c->busy))return fail(PRL_BUSY,"Context already in use");
    memset(&c->profile,0,sizeof(c->profile));c->profile.abi_version=1;c->profile.struct_bytes=sizeof(c->profile);
    memset(&c->detail,0,sizeof(c->detail));c->detail.abi_version=1;c->detail.struct_bytes=sizeof(c->detail);c->detail.buffer_mode=PRL_BUFFER_MODE;
    if(c->poisoned) { rc=fail(PRL_POISONED,"Context poisoned by previous failure; recreate in fresh worker process");goto finish; }
    memcpy(key,commit_a,32);start=ticks(CLOCK_MONOTONIC);cpu_start=ticks(CLOCK_THREAD_CPUTIME_ID);
    if(!start||!cpu_start) { rc=fail(PRL_DEADLINE,"Clock unavailable");goto poison; }
#if PRL_A_CACHE
    if(c->format==PRL_BATCH_F32) {
        phase=ticks(CLOCK_MONOTONIC);
        if(prl_a_cache_prepare(&c->a_cache,an+(uint64_t)row_base*k,m,k,row_base,commit_a)<0) {
            rc=fail(PRL_ARGUMENT,"Invalid internal A cache state");goto poison;
        }
        c->detail.pack_a_ns+=ticks(CLOCK_MONOTONIC)-phase;
    }
#endif
#define RUN_CALL(x) do { vip_status_e s=(x);if(s!=VIP_SUCCESS) { rc=fail(PRL_DRIVER,"%s failed status=%d",#x,(int)s);goto poison; } } while(0)
    for(p=0;p<SEGMENTS;++p) {
        vip_inference_profile_t driver;
        now=ticks(CLOCK_MONOTONIC);
        if(!now||now-start>BUDGET_NS) { rc=fail(PRL_DEADLINE,"Scan exceeded8sec before segment%u",p);goto poison; }
        void *pack_a=PRL_BUFFER_MODE==2?c->scratch[0]:c->mapped[0];
        void *pack_b=PRL_BUFFER_MODE==2?c->scratch[1]:c->mapped[1];
        void *partials=PRL_BUFFER_MODE==2?c->scratch[2]:c->mapped[2];
#if PRL_BUFFER_MODE == 2
        const void *copy_a=pack_a;
#endif
        phase=ticks(CLOCK_MONOTONIC);
        if(c->format==PRL_BATCH_F32) {
#if PRL_A_CACHE
            const float *cached=prl_a_cache_segment(&c->a_cache,p);
            if(!cached) { rc=fail(PRL_ARGUMENT,"A cache segment unavailable");goto poison; }
#if PRL_BUFFER_MODE == 2
            /* Cache already contains packed ordinary RAM; copy directly to
             * the NPU buffer below rather than copying via scratch again. */
            copy_a=cached;
#else
            memcpy(pack_a,cached,INPUT_ELEMENTS*4);
#endif
#else
            float *a=pack_a;
            for(r=0;r<BATCH_SIZE;++r)prl_pack_i8_f32(a+r*128,an+(uint64_t)(row_base+r)*k+p*128,128);
#endif
        } else {
            int32_t *a=pack_a;
            for(r=0;r<BATCH_SIZE;++r)for(t=0;t<128;++t)a[r*128+t]=(int32_t)an[(uint64_t)(row_base+r)*k+p*128+t];
        }
        c->detail.pack_a_ns+=ticks(CLOCK_MONOTONIC)-phase;
        phase=ticks(CLOCK_MONOTONIC);
        if(c->format==PRL_BATCH_F32) {
            float *b=pack_b;
            for(t=0;t<128;++t)prl_pack_i8_f32(b+t*BATCH_SIZE,bn+(uint64_t)(p*128+t)*n+col_base,BATCH_SIZE);
        } else {
            int32_t *b=pack_b;
            for(t=0;t<128;++t)for(j=0;j<BATCH_SIZE;++j)b[t*BATCH_SIZE+j]=(int32_t)bn[(uint64_t)(p*128+t)*n+col_base+j];
        }
        c->detail.pack_b_ns+=ticks(CLOCK_MONOTONIC)-phase;
#if PRL_BUFFER_MODE == 2
        phase=ticks(CLOCK_MONOTONIC);
        memcpy(c->mapped[0],copy_a,INPUT_ELEMENTS*4);
        memcpy(c->mapped[1],pack_b,INPUT_ELEMENTS*4);
        c->detail.input_copy_ns+=ticks(CLOCK_MONOTONIC)-phase;
#endif
        phase=ticks(CLOCK_MONOTONIC);
        RUN_CALL(vip_flush_buffer(c->buffers[0],VIP_BUFFER_OPER_TYPE_FLUSH));
        RUN_CALL(vip_flush_buffer(c->buffers[1],VIP_BUFFER_OPER_TYPE_FLUSH));
        c->detail.input_flush_ns+=ticks(CLOCK_MONOTONIC)-phase;
        dispatch=ticks(CLOCK_MONOTONIC);RUN_CALL(vip_run_network(c->net));
        now=ticks(CLOCK_MONOTONIC)-dispatch;
        c->profile.driver_wall_ns+=now;c->detail.driver_ns+=now;
        c->profile.segments++;c->profile.executed_macs+=PARTIAL_MACS;
        phase=ticks(CLOCK_MONOTONIC);
        memset(&driver,0,sizeof(driver));RUN_CALL(vip_query_network(c->net,VIP_NETWORK_PROP_PROFILING,&driver));
        if(!driver.inference_time||!driver.total_cycle) { rc=fail(PRL_DRIVER,"Missing driver profiling at segment%u",p);goto poison; }
        c->profile.driver_us+=driver.inference_time;c->profile.driver_cycles+=driver.total_cycle;
        c->detail.query_profile_ns+=ticks(CLOCK_MONOTONIC)-phase;
        phase=ticks(CLOCK_MONOTONIC);
        RUN_CALL(vip_flush_buffer(c->buffers[2],VIP_BUFFER_OPER_TYPE_INVALIDATE));
        c->detail.output_invalidate_ns+=ticks(CLOCK_MONOTONIC)-phase;
#if PRL_BUFFER_MODE == 2
        phase=ticks(CLOCK_MONOTONIC);
        memcpy(partials,c->mapped[2],ELEMENTS*4);
        c->detail.output_copy_ns+=ticks(CLOCK_MONOTONIC)-phase;
#endif
        phase=ticks(CLOCK_MONOTONIC);
        if(c->format==PRL_BATCH_F32) {
            if(prl_checked_accumulate_f32(state,(const float*)partials,ELEMENTS,p)) {
                rc=fail(PRL_NUMERICAL,"Invalid FP32 partial or cumulative state at segment%u",p);goto poison;
            }
        } else for(t=0;t<ELEMENTS;++t) {
            int64_t partial=((int32_t*)partials)[t],sum=(int64_t)state[t]+partial;
            if(partial < -2097152LL || partial > 2097152LL || sum<INT32_MIN||sum>INT32_MAX) {
                rc=fail(PRL_NUMERICAL,"Impossible I32 partial/cumulative at segment%u element%u",p,t);goto poison;
            }
            state[t]=(int32_t)sum;
        }
        c->detail.accumulate_ns+=ticks(CLOCK_MONOTONIC)-phase;
        phase=ticks(CLOCK_MONOTONIC);
        /* Sixteen independent64x64 tiles. Do not fold across tile columns,
         * and pair rows only within the same64-row tile. */
        for(rb=0;rb<SUBTILES;++rb)for(cb=0;cb<SUBTILES;++cb)for(r=0;r<32;++r) {
            uint32_t fold=0,old;
            hi=(rb*SUBTILES+cb)*32+r;old=trans[hi][p%16];
            fold=prl_fold_pair64(state+(rb*64+r)*BATCH_SIZE+cb*64,
                                 state+(rb*64+r+32)*BATCH_SIZE+cb*64);
            trans[hi][p%16]=((old<<13)|(old>>19))^fold;
        }
        c->detail.fold_ns+=ticks(CLOCK_MONOTONIC)-phase;
        now=ticks(CLOCK_MONOTONIC);
        if(!now||now-start>BUDGET_NS) { rc=fail(PRL_DEADLINE,"Scan exceeded8sec after segment%u",p);goto poison; }
    }
    phase=ticks(CLOCK_MONOTONIC);
    for(r=0;r<HASH_COUNT;++r)keyed64(hashes[r],key,trans[r]);
    c->detail.hash_ns+=ticks(CLOCK_MONOTONIC)-phase;
    now=ticks(CLOCK_MONOTONIC);
    if(!now||now-start>BUDGET_NS) { rc=fail(PRL_DEADLINE,"Scan exceeded8sec after hash");goto poison; }
    memcpy(hashes_out,hashes,HASH_BYTES);*macs_out=TILE_MACS;c->profile.completed_macs=TILE_MACS;
    goto timed;
poison:
    c->poisoned=1;
timed:
    now=ticks(CLOCK_MONOTONIC);c->profile.wall_ns=now>=start?now-start:0;
    now=ticks(CLOCK_THREAD_CPUTIME_ID);c->profile.thread_cpu_ns=now>=cpu_start?now-cpu_start:0;
finish:
    c->profile.status=rc;c->profile.poisoned=(uint32_t)c->poisoned;
    atomic_flag_clear(&c->busy);return rc;
#undef RUN_CALL
}
int prl_batch_get_profile(void *opaque,prl_profile *out) {
    context *c=opaque;
    if(!c||c->magic!=MAGIC||!out)return fail(PRL_ARGUMENT,"Invalid context/profile pointer");
    if(atomic_flag_test_and_set(&c->busy))return fail(PRL_BUSY,"Context already in use");
    memcpy(out,&c->profile,sizeof(*out));atomic_flag_clear(&c->busy);return PRL_OK;
}
int prl_batch_get_detail(void *opaque,prl_detail *out) {
    context *c=opaque;
    if(!c||c->magic!=MAGIC||!out)return fail(PRL_ARGUMENT,"Invalid context/detail pointer");
    if(atomic_flag_test_and_set(&c->busy))return fail(PRL_BUSY,"Context already in use");
    memcpy(out,&c->detail,sizeof(*out));atomic_flag_clear(&c->busy);return PRL_OK;
}
void prl_batch_destroy(void *opaque) {
    context *c=opaque;
    if(!c)return;
    if(c->magic!=MAGIC) { fail(PRL_ARGUMENT,"Invalid context");return; }
    if(atomic_flag_test_and_set(&c->busy)) { fail(PRL_BUSY,"Cannot destroy active scan");return; }
    cleanup(c);
}
