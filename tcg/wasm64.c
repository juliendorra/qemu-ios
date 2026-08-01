/* SPDX-License-Identifier: GPL-2.0-or-later */
/*
 * WebAssembly backend with forked TCI, based on tci.c
 *
 * Copyright (c) 2009, 2011, 2016 Stefan Weil
 *
 * This program is free software: you can redistribute it and/or modify
 * it under the terms of the GNU General Public License as published by
 * the Free Software Foundation, either version 2 of the License, or
 * (at your option) any later version.
 *
 * This program is distributed in the hope that it will be useful,
 * but WITHOUT ANY WARRANTY; without even the implied warranty of
 * MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
 * GNU General Public License for more details.
 *
 * You should have received a copy of the GNU General Public License
 * along with this program.  If not, see <http://www.gnu.org/licenses/>.
 */

#include "qemu/osdep.h"
#include "tcg/tcg.h"
#include "tcg/tcg-ldst.h"
#include "tcg/helper-info.h"
#include <ffi.h>
#include <emscripten.h>
#include "wasm64.h"

/*
 * TBs executed more than this value will be compiled to wasm.
 *
 * Upstream uses 1500, which suits a long-running Linux guest: pay to compile
 * only the very hottest blocks. Our workload is a BOOT -- thousands of
 * moderately-warm blocks and few extremely hot ones -- and at 1500 the
 * instrumented counters showed only 208 blocks compiled in an entire iPhone
 * OS 1.0 boot, so essentially all of it ran on the forked TCI interpreter.
 *
 * There is enormous headroom to trade compile time for execution speed: that
 * run peaked at 208 live instances against MAX_INSTANCES of 12000 (1.7%), with
 * zero evictions and zero recompiles. Watch those two counters when changing
 * this -- if they stay at zero while `compiled` rises, the trade is free.
 */
#define INSTANTIATE_NUM_DEFAULT 100

/*
 * ...and it is tunable at RUN TIME, because sweeping it otherwise costs one
 * full wasm rebuild per value. jit_instantiate_num is resolved once, in
 * init_wasm(), from (in order):
 *
 *   1. IT_WASM_INSTANTIATE_NUM     -- native and Node
 *   2. instantiate=<n> in /fw/jit-tune   -- the browser
 *
 * The second exists only because Emscripten's ENV object is not in
 * EXPORTED_RUNTIME_METHODS, so a page cannot set an environment variable; it
 * CAN write a file into MEMFS before startup. The same file carries
 * max_instances=<n> (clamped to the compile-time MAX_INSTANCES, which sizes a
 * static array). Absent file, absent variable: the defaults below, i.e. the
 * measured settings.
 */
static int jit_instantiate_num = INSTANTIATE_NUM_DEFAULT;
#define INSTANTIATE_NUM jit_threshold

#define JIT_TUNE_FILE "/fw/jit-tune"

static long jit_tune_lookup(const char *key, long fallback)
{
    char line[128];
    size_t keylen = strlen(key);
    long value = fallback;
    FILE *f = fopen(JIT_TUNE_FILE, "r");

    if (f == NULL) {
        return fallback;
    }
    while (fgets(line, sizeof(line), f) != NULL) {
        if (strncmp(line, key, keylen) == 0 && line[keylen] == '=') {
            value = strtol(line + keylen + 1, NULL, 10);
        }
    }
    fclose(f);
    return value;
}

static long jit_tunable(const char *env, const char *key, long fallback)
{
    const char *v = getenv(env);

    if (v != NULL && *v != '\0') {
        return strtol(v, NULL, 10);
    }
    return jit_tune_lookup(key, fallback);
}

/*
 * ADAPTIVE THRESHOLD -- ON BY DEFAULT since 2026-07-29 (adaptive=0 disables).
 *
 * A boot and a running app want opposite settings. A boot is a long COLD TAIL
 * -- tens of thousands of blocks executed a few hundred times each -- and wants
 * to compile eagerly. A running app is a small hot working set and tolerates a
 * high threshold. One static number cannot serve both, and the measured cost of
 * getting it wrong is large in both directions: at 1500 only 208 blocks
 * compiled in an entire boot, while at 100 the instance cap saturates partway
 * through and the backend spends the rest of the boot evicting and recompiling.
 *
 * So scale the threshold by how much room is left. While there is headroom,
 * compile anything warm; as the cap fills, spend the remaining slots only on
 * blocks that are genuinely hot -- which also slows the eviction churn instead
 * of feeding it.
 *
 * Recomputed on each compile and each eviction, never per TB execution: the
 * hot path reads one int.
 *
 * Measured on full browser boots of iPhone OS 1.0, chunked, to the SpringBoard
 * home screen:
 *
 *   static 100    home screen 290 s   evicted 72,000   recompiled 4,557
 *   adaptive 100  home screen 258 s   evicted      0   recompiled     0
 *
 * and on the launchd landmark it turned a setting that twice went into the
 * eviction-churn regime (692 s, 736 s) into one that cannot (143 s, 146 s).
 * It has not been slower than the best static value in anything measured,
 * which is why it is the default rather than an option.
 */
static bool jit_adaptive;
static int jit_threshold;          /* effective; == jit_instantiate_num when off */

#define EM_JS_PRE(ret, name, args, body...) EM_JS(ret, name, args, body)

#define DEC_PTR(p) bigintToI53Checked(p)
#define ENC_PTR(p) BigInt(p)
#if defined(WASM64_MEMORY64_2)
#define ENC_WASM_TABLE_IDX(i) Number(i)
#else
#define ENC_WASM_TABLE_IDX(i) i
#endif

EM_JS_PRE(void*, instantiate_wasm, (void *wasm_begin,
                                    int wasm_size,
                                    void *import_vec_begin,
                                    int import_vec_size,
                                    int direct_table),
{
    const memory_v = new DataView(HEAP8.buffer);
    const wasm = HEAP8.subarray(DEC_PTR(wasm_begin),
                                DEC_PTR(wasm_begin) + wasm_size);
    var helper = {};
    helper.u = () => {
        return (Asyncify.state != Asyncify.State.Unwinding) ? 1 : 0;
    };
    for (var i = 0; i < import_vec_size / 8; i++) {
        const idx = memory_v.getBigInt64(
            DEC_PTR(import_vec_begin) + i * 8, true);
        helper[i] = wasmTable.get(ENC_WASM_TABLE_IDX(idx));
    }
    const mod = new WebAssembly.Module(new Uint8Array(wasm));
    const inst = new WebAssembly.Instance(mod, {
            "env" : {
                "memory" : wasmMemory,
            },
            "helper" : helper,
    });

    Module.__wasm_tb.inst_gc_registry.register(inst, "tbinstance");

    /*
     * Every TB export is a new function.  addFunction() nevertheless scans
     * the main table on first use and records every export in Emscripten's
     * functionsInTableMap WeakMap so it can deduplicate later additions.
     * That work can never find a duplicate here and showed up as
     * MapPrototypeSet in the vCPU profile while the cold JIT was compiling.
     * Keep Emscripten's slot allocator and table mirror, but skip its
     * duplicate-function map entirely.
     */
    if (direct_table) {
        const func_idx = getEmptyTableSlot();
        setWasmTableEntry(func_idx, inst.exports.start);
        return ENC_PTR(func_idx);
    }
    return ENC_PTR(addFunction(inst.exports.start, 'ii'));
});

EM_JS_PRE(void, release_wasm_tb, (void *func_idx_ptr, int direct_table),
{
    const func_idx = DEC_PTR(func_idx_ptr);

    if (direct_table) {
        setWasmTableEntry(func_idx, null);
        freeTableIndexes.push(func_idx);
    } else {
        removeFunction(func_idx);
    }
});

__thread uintptr_t tci_tb_ptr;

static void tci_args_l(uint32_t insn, const void *tb_ptr, void **l0)
{
    int diff = sextract32(insn, 12, 20);
    *l0 = diff ? (void *)tb_ptr + diff : NULL;
}

static void tci_args_r(uint32_t insn, TCGReg *r0)
{
    *r0 = extract32(insn, 8, 4);
}

static void tci_args_nl(uint32_t insn, const void *tb_ptr,
                        uint8_t *n0, void **l1)
{
    *n0 = extract32(insn, 8, 4);
    *l1 = sextract32(insn, 12, 20) + (void *)tb_ptr;
}

static void tci_args_rl(uint32_t insn, const void *tb_ptr,
                        TCGReg *r0, void **l1)
{
    *r0 = extract32(insn, 8, 4);
    *l1 = sextract32(insn, 12, 20) + (void *)tb_ptr;
}

static void tci_args_rr(uint32_t insn, TCGReg *r0, TCGReg *r1)
{
    *r0 = extract32(insn, 8, 4);
    *r1 = extract32(insn, 12, 4);
}

static void tci_args_ri(uint32_t insn, TCGReg *r0, tcg_target_ulong *i1)
{
    *r0 = extract32(insn, 8, 4);
    *i1 = sextract32(insn, 12, 20);
}

static void tci_args_rrm(uint32_t insn, TCGReg *r0,
                         TCGReg *r1, MemOpIdx *m2)
{
    *r0 = extract32(insn, 8, 4);
    *r1 = extract32(insn, 12, 4);
    *m2 = extract32(insn, 16, 16);
}

static void tci_args_rrr(uint32_t insn, TCGReg *r0, TCGReg *r1, TCGReg *r2)
{
    *r0 = extract32(insn, 8, 4);
    *r1 = extract32(insn, 12, 4);
    *r2 = extract32(insn, 16, 4);
}

static void tci_args_rrs(uint32_t insn, TCGReg *r0, TCGReg *r1, int32_t *i2)
{
    *r0 = extract32(insn, 8, 4);
    *r1 = extract32(insn, 12, 4);
    *i2 = sextract32(insn, 16, 16);
}

static void tci_args_rrbb(uint32_t insn, TCGReg *r0, TCGReg *r1,
                          uint8_t *i2, uint8_t *i3)
{
    *r0 = extract32(insn, 8, 4);
    *r1 = extract32(insn, 12, 4);
    *i2 = extract32(insn, 16, 6);
    *i3 = extract32(insn, 22, 6);
}

static void tci_args_rrrc(uint32_t insn,
                          TCGReg *r0, TCGReg *r1, TCGReg *r2, TCGCond *c3)
{
    *r0 = extract32(insn, 8, 4);
    *r1 = extract32(insn, 12, 4);
    *r2 = extract32(insn, 16, 4);
    *c3 = extract32(insn, 20, 4);
}

static void tci_args_rrrrrc(uint32_t insn, TCGReg *r0, TCGReg *r1,
                            TCGReg *r2, TCGReg *r3, TCGReg *r4, TCGCond *c5)
{
    *r0 = extract32(insn, 8, 4);
    *r1 = extract32(insn, 12, 4);
    *r2 = extract32(insn, 16, 4);
    *r3 = extract32(insn, 20, 4);
    *r4 = extract32(insn, 24, 4);
    *c5 = extract32(insn, 28, 4);
}

static bool tci_compare32(uint32_t u0, uint32_t u1, TCGCond condition)
{
    bool result = false;
    int32_t i0 = u0;
    int32_t i1 = u1;
    switch (condition) {
    case TCG_COND_EQ:
        result = (u0 == u1);
        break;
    case TCG_COND_NE:
        result = (u0 != u1);
        break;
    case TCG_COND_LT:
        result = (i0 < i1);
        break;
    case TCG_COND_GE:
        result = (i0 >= i1);
        break;
    case TCG_COND_LE:
        result = (i0 <= i1);
        break;
    case TCG_COND_GT:
        result = (i0 > i1);
        break;
    case TCG_COND_LTU:
        result = (u0 < u1);
        break;
    case TCG_COND_GEU:
        result = (u0 >= u1);
        break;
    case TCG_COND_LEU:
        result = (u0 <= u1);
        break;
    case TCG_COND_GTU:
        result = (u0 > u1);
        break;
    default:
        g_assert_not_reached();
    }
    return result;
}

static bool tci_compare64(uint64_t u0, uint64_t u1, TCGCond condition)
{
    bool result = false;
    int64_t i0 = u0;
    int64_t i1 = u1;
    switch (condition) {
    case TCG_COND_EQ:
        result = (u0 == u1);
        break;
    case TCG_COND_NE:
        result = (u0 != u1);
        break;
    case TCG_COND_LT:
        result = (i0 < i1);
        break;
    case TCG_COND_GE:
        result = (i0 >= i1);
        break;
    case TCG_COND_LE:
        result = (i0 <= i1);
        break;
    case TCG_COND_GT:
        result = (i0 > i1);
        break;
    case TCG_COND_LTU:
        result = (u0 < u1);
        break;
    case TCG_COND_GEU:
        result = (u0 >= u1);
        break;
    case TCG_COND_LEU:
        result = (u0 <= u1);
        break;
    case TCG_COND_GTU:
        result = (u0 > u1);
        break;
    default:
        g_assert_not_reached();
    }
    return result;
}

static uint64_t tci_qemu_ld(CPUArchState *env, uint64_t taddr,
                            MemOpIdx oi, const void *tb_ptr)
{
    MemOp mop = get_memop(oi);
    uintptr_t ra = (uintptr_t)tb_ptr;

    switch (mop & MO_SSIZE) {
    case MO_UB:
        return helper_ldub_mmu(env, taddr, oi, ra);
    case MO_SB:
        return helper_ldsb_mmu(env, taddr, oi, ra);
    case MO_UW:
        return helper_lduw_mmu(env, taddr, oi, ra);
    case MO_SW:
        return helper_ldsw_mmu(env, taddr, oi, ra);
    case MO_UL:
        return helper_ldul_mmu(env, taddr, oi, ra);
    case MO_SL:
        return helper_ldsl_mmu(env, taddr, oi, ra);
    case MO_UQ:
        return helper_ldq_mmu(env, taddr, oi, ra);
    default:
        g_assert_not_reached();
    }
}

static void tci_qemu_st(CPUArchState *env, uint64_t taddr, uint64_t val,
                        MemOpIdx oi, const void *tb_ptr)
{
    MemOp mop = get_memop(oi);
    uintptr_t ra = (uintptr_t)tb_ptr;

    switch (mop & MO_SIZE) {
    case MO_UB:
        helper_stb_mmu(env, taddr, val, oi, ra);
        break;
    case MO_UW:
        helper_stw_mmu(env, taddr, val, oi, ra);
        break;
    case MO_UL:
        helper_stl_mmu(env, taddr, val, oi, ra);
        break;
    case MO_UQ:
        helper_stq_mmu(env, taddr, val, oi, ra);
        break;
    default:
        g_assert_not_reached();
    }
}

static __thread int thread_idx;

static inline int32_t get_counter_local(void *tb_ptr)
{
    return get_counter(tb_ptr, thread_idx);
}

static inline void set_counter_local(void *tb_ptr, int v)
{
    set_counter(tb_ptr, thread_idx, v);
}

static inline struct WasmInstanceInfo *get_info_local(void *tb_ptr)
{
    return get_info(tb_ptr, thread_idx);
}

static inline void set_info_local(void *tb_ptr, struct WasmInstanceInfo *info)
{
    set_info(tb_ptr, thread_idx, info);
}

/*
 * inc_counter increments the execution counter in the specified TB.
 * If the counter reaches the limit, it returns true otherwise returns false.
 */
static inline bool inc_counter(void *tb_ptr)
{
    int32_t counter = get_counter_local(tb_ptr);
    if ((counter >= 0) && (counter < INSTANTIATE_NUM)) {
        set_counter_local(tb_ptr, counter + 1);
    } else {
        return true; /* enter to wasm TB */
    }
    return false;
}

static __thread struct WasmContext ctx = {
    .tb_ptr = 0,
    .stack = NULL,
    .do_init = 1,
    .buf128 = NULL,
};

static uintptr_t tcg_qemu_tb_exec_tci(CPUArchState *env)
{
    uint32_t *tb_ptr = get_tci_ptr(ctx.tb_ptr);
    tcg_target_ulong regs[TCG_TARGET_NB_REGS];
    uint64_t stack[(TCG_STATIC_CALL_ARGS_SIZE + TCG_STATIC_FRAME_SIZE)
                   / sizeof(uint64_t)];

    regs[TCG_AREG0] = (tcg_target_ulong)env;
    regs[TCG_REG_CALL_STACK] = (uintptr_t)stack;

    for (;;) {
        uint32_t insn;
        TCGOpcode opc;
        TCGReg r0, r1, r2, r3, r4;
        tcg_target_ulong t1;
        uint8_t pos, len;
        TCGCond condition;
        uint32_t tmp32;
        uint64_t taddr;
        MemOpIdx oi;
        int32_t ofs;
        void *ptr;

        insn = *tb_ptr++;
        opc = extract32(insn, 0, 8);

        switch (opc) {
        case INDEX_op_call:
            {
                void *call_slots[MAX_CALL_IARGS];
                ffi_cif *cif;
                void *func;
                unsigned i, s, n;

                tci_args_nl(insn, tb_ptr, &len, &ptr);
                func = ((void **)ptr)[0];
                cif = ((void **)ptr)[1];

                n = cif->nargs;
                for (i = s = 0; i < n; ++i) {
                    ffi_type *t = cif->arg_types[i];
                    call_slots[i] = &stack[s];
                    s += DIV_ROUND_UP(t->size, 8);
                }

                /* Helper functions may need to access the "return address" */
                tci_tb_ptr = (uintptr_t)tb_ptr;
                ffi_call(cif, func, stack, call_slots);
            }

            switch (len) {
            case 0: /* void */
                break;
            case 1: /* uint32_t */
                /*
                 * The result winds up "left-aligned" in the stack[0] slot.
                 * Note that libffi has an odd special case in that it will
                 * always widen an integral result to ffi_arg.
                 */
                if (sizeof(ffi_arg) == 8) {
                    regs[TCG_REG_R0] = (uint32_t)stack[0];
                } else {
                    regs[TCG_REG_R0] = *(uint32_t *)stack;
                }
                break;
            case 2: /* uint64_t */
                memcpy(&regs[TCG_REG_R0], stack, 8);
                break;
            case 3: /* Int128 */
                memcpy(&regs[TCG_REG_R0], stack, 16);
                break;
            default:
                g_assert_not_reached();
            }
            break;
        case INDEX_op_and:
            tci_args_rrr(insn, &r0, &r1, &r2);
            regs[r0] = regs[r1] & regs[r2];
            break;
        case INDEX_op_or:
            tci_args_rrr(insn, &r0, &r1, &r2);
            regs[r0] = regs[r1] | regs[r2];
            break;
        case INDEX_op_xor:
            tci_args_rrr(insn, &r0, &r1, &r2);
            regs[r0] = regs[r1] ^ regs[r2];
            break;
        case INDEX_op_add:
            tci_args_rrr(insn, &r0, &r1, &r2);
            regs[r0] = regs[r1] + regs[r2];
            break;
        case INDEX_op_sub:
            tci_args_rrr(insn, &r0, &r1, &r2);
            regs[r0] = regs[r1] - regs[r2];
            break;
        case INDEX_op_mul:
            tci_args_rrr(insn, &r0, &r1, &r2);
            regs[r0] = regs[r1] * regs[r2];
            break;
        case INDEX_op_extract:
            tci_args_rrbb(insn, &r0, &r1, &pos, &len);
            regs[r0] = extract64(regs[r1], pos, len);
            break;
        case INDEX_op_sextract:
            tci_args_rrbb(insn, &r0, &r1, &pos, &len);
            regs[r0] = sextract64(regs[r1], pos, len);
            break;
        case INDEX_op_shl:
            tci_args_rrr(insn, &r0, &r1, &r2);
            regs[r0] = regs[r1] << (regs[r2] % TCG_TARGET_REG_BITS);
            break;
        case INDEX_op_shr:
            tci_args_rrr(insn, &r0, &r1, &r2);
            regs[r0] = regs[r1] >> (regs[r2] % TCG_TARGET_REG_BITS);
            break;
        case INDEX_op_sar:
            tci_args_rrr(insn, &r0, &r1, &r2);
            regs[r0] = ((tcg_target_long)regs[r1]
                        >> (regs[r2] % TCG_TARGET_REG_BITS));
            break;
        case INDEX_op_neg:
            tci_args_rr(insn, &r0, &r1);
            regs[r0] = -regs[r1];
            break;
        case INDEX_op_setcond:
            tci_args_rrrc(insn, &r0, &r1, &r2, &condition);
            regs[r0] = tci_compare64(regs[r1], regs[r2], condition);
            break;
        case INDEX_op_movcond:
            tci_args_rrrrrc(insn, &r0, &r1, &r2, &r3, &r4, &condition);
            tmp32 = tci_compare64(regs[r1], regs[r2], condition);
            regs[r0] = regs[tmp32 ? r3 : r4];
            break;
        case INDEX_op_tci_setcond32:
            tci_args_rrrc(insn, &r0, &r1, &r2, &condition);
            regs[r0] = tci_compare32(regs[r1], regs[r2], condition);
            break;
        case INDEX_op_tci_movcond32:
            tci_args_rrrrrc(insn, &r0, &r1, &r2, &r3, &r4, &condition);
            tmp32 = tci_compare32(regs[r1], regs[r2], condition);
            regs[r0] = regs[tmp32 ? r3 : r4];
            break;
        case INDEX_op_mov:
            tci_args_rr(insn, &r0, &r1);
            regs[r0] = regs[r1];
            break;
        case INDEX_op_tci_movi:
            tci_args_ri(insn, &r0, &t1);
            regs[r0] = t1;
            break;
        case INDEX_op_tci_movl:
            tci_args_rl(insn, tb_ptr, &r0, &ptr);
            regs[r0] = *(tcg_target_ulong *)ptr;
            break;
        case INDEX_op_ld:
            tci_args_rrs(insn, &r0, &r1, &ofs);
            ptr = (void *)(regs[r1] + ofs);
            regs[r0] = *(tcg_target_ulong *)ptr;
            break;
        case INDEX_op_ld8u:
            tci_args_rrs(insn, &r0, &r1, &ofs);
            ptr = (void *)(regs[r1] + ofs);
            regs[r0] = *(uint8_t *)ptr;
            break;
        case INDEX_op_ld8s:
            tci_args_rrs(insn, &r0, &r1, &ofs);
            ptr = (void *)(regs[r1] + ofs);
            regs[r0] = *(int8_t *)ptr;
            break;
        case INDEX_op_ld16u:
            tci_args_rrs(insn, &r0, &r1, &ofs);
            ptr = (void *)(regs[r1] + ofs);
            regs[r0] = *(uint16_t *)ptr;
            break;
        case INDEX_op_ld16s:
            tci_args_rrs(insn, &r0, &r1, &ofs);
            ptr = (void *)(regs[r1] + ofs);
            regs[r0] = *(int16_t *)ptr;
            break;
        case INDEX_op_st:
            tci_args_rrs(insn, &r0, &r1, &ofs);
            ptr = (void *)(regs[r1] + ofs);
            *(tcg_target_ulong *)ptr = regs[r0];
            break;
        case INDEX_op_st8:
            tci_args_rrs(insn, &r0, &r1, &ofs);
            ptr = (void *)(regs[r1] + ofs);
            *(uint8_t *)ptr = regs[r0];
            break;
        case INDEX_op_st16:
            tci_args_rrs(insn, &r0, &r1, &ofs);
            ptr = (void *)(regs[r1] + ofs);
            *(uint16_t *)ptr = regs[r0];
            break;
        case INDEX_op_ld32u:
            tci_args_rrs(insn, &r0, &r1, &ofs);
            ptr = (void *)(regs[r1] + ofs);
            regs[r0] = *(uint32_t *)ptr;
            break;
        case INDEX_op_ld32s:
            tci_args_rrs(insn, &r0, &r1, &ofs);
            ptr = (void *)(regs[r1] + ofs);
            regs[r0] = *(int32_t *)ptr;
            break;
        case INDEX_op_st32:
            tci_args_rrs(insn, &r0, &r1, &ofs);
            ptr = (void *)(regs[r1] + ofs);
            *(uint32_t *)ptr = regs[r0];
            break;
        case INDEX_op_divs:
            tci_args_rrr(insn, &r0, &r1, &r2);
            regs[r0] = (int64_t)regs[r1] / (int64_t)regs[r2];
            break;
        case INDEX_op_divu:
            tci_args_rrr(insn, &r0, &r1, &r2);
            regs[r0] = (uint64_t)regs[r1] / (uint64_t)regs[r2];
            break;
        case INDEX_op_rems:
            tci_args_rrr(insn, &r0, &r1, &r2);
            regs[r0] = (int64_t)regs[r1] % (int64_t)regs[r2];
            break;
        case INDEX_op_remu:
            tci_args_rrr(insn, &r0, &r1, &r2);
            regs[r0] = (uint64_t)regs[r1] % (uint64_t)regs[r2];
            break;
        case INDEX_op_tci_divs32:
            tci_args_rrr(insn, &r0, &r1, &r2);
            regs[r0] = (int32_t)regs[r1] / (int32_t)regs[r2];
            break;
        case INDEX_op_tci_divu32:
            tci_args_rrr(insn, &r0, &r1, &r2);
            regs[r0] = (uint32_t)regs[r1] / (uint32_t)regs[r2];
            break;
        case INDEX_op_tci_rems32:
            tci_args_rrr(insn, &r0, &r1, &r2);
            regs[r0] = (int32_t)regs[r1] % (int32_t)regs[r2];
            break;
        case INDEX_op_tci_remu32:
            tci_args_rrr(insn, &r0, &r1, &r2);
            regs[r0] = (uint32_t)regs[r1] % (uint32_t)regs[r2];
            break;
        case INDEX_op_ctpop:
            tci_args_rr(insn, &r0, &r1);
            regs[r0] = ctpop64(regs[r1]);
            break;
        case INDEX_op_clz:
            tci_args_rrr(insn, &r0, &r1, &r2);
            regs[r0] = regs[r1] ? clz64(regs[r1]) : regs[r2];
            break;
        case INDEX_op_ctz:
            tci_args_rrr(insn, &r0, &r1, &r2);
            regs[r0] = regs[r1] ? ctz64(regs[r1]) : regs[r2];
            break;
        case INDEX_op_tci_clz32:
            tci_args_rrr(insn, &r0, &r1, &r2);
            tmp32 = regs[r1];
            regs[r0] = tmp32 ? clz32(tmp32) : regs[r2];
            break;
        case INDEX_op_tci_ctz32:
            tci_args_rrr(insn, &r0, &r1, &r2);
            tmp32 = regs[r1];
            regs[r0] = tmp32 ? ctz32(tmp32) : regs[r2];
            break;
        case INDEX_op_rotl:
            tci_args_rrr(insn, &r0, &r1, &r2);
            regs[r0] = rol64(regs[r1], regs[r2] & 63);
            break;
        case INDEX_op_rotr:
            tci_args_rrr(insn, &r0, &r1, &r2);
            regs[r0] = ror64(regs[r1], regs[r2] & 63);
            break;
        case INDEX_op_tci_rotl32:
            tci_args_rrr(insn, &r0, &r1, &r2);
            regs[r0] = rol32(regs[r1], regs[r2] & 31);
            break;
        case INDEX_op_tci_rotr32:
            tci_args_rrr(insn, &r0, &r1, &r2);
            regs[r0] = ror32(regs[r1], regs[r2] & 31);
            break;
        case INDEX_op_br:
            tci_args_l(insn, tb_ptr, &ptr);
            tb_ptr = ptr;
            continue;
        case INDEX_op_brcond:
            tci_args_rl(insn, tb_ptr, &r0, &ptr);
            if (regs[r0]) {
                tb_ptr = ptr;
            }
            break;
        case INDEX_op_exit_tb:
            tci_args_l(insn, tb_ptr, &ptr);
            ctx.tb_ptr = 0;
            return (uintptr_t)ptr;
        case INDEX_op_goto_tb:
            tci_args_l(insn, tb_ptr, &ptr);
            if (tb_ptr != *(void **)ptr) {
                tb_ptr = *(void **)ptr;
                ctx.tb_ptr = tb_ptr;
                if (inc_counter(tb_ptr)) {
                    return 0; /* enter to wasm TB */
                }
                tb_ptr = get_tci_ptr(tb_ptr);
            }
            break;
        case INDEX_op_goto_ptr:
            tci_args_r(insn, &r0);
            ptr = (void *)regs[r0];
            if (!ptr) {
                ctx.tb_ptr = 0;
                return 0;
            }
            tb_ptr = ptr;
            ctx.tb_ptr = tb_ptr;
            if (inc_counter(tb_ptr)) {
                return 0; /* enter to wasm TB */
            }
            tb_ptr = get_tci_ptr(tb_ptr);
            break;
        case INDEX_op_qemu_ld:
            tci_args_rrm(insn, &r0, &r1, &oi);
            taddr = regs[r1];
            regs[r0] = tci_qemu_ld(env, taddr, oi, tb_ptr);
            break;
        case INDEX_op_tci_qemu_ld_rrr:
            tci_args_rrr(insn, &r0, &r1, &r2);
            taddr = regs[r1];
            oi = regs[r2];
            regs[r0] = tci_qemu_ld(env, taddr, oi, tb_ptr);
            break;
        case INDEX_op_qemu_st:
            tci_args_rrm(insn, &r0, &r1, &oi);
            taddr = regs[r1];
            tci_qemu_st(env, taddr, regs[r0], oi, tb_ptr);
            break;
        case INDEX_op_tci_qemu_st_rrr:
            tci_args_rrr(insn, &r0, &r1, &r2);
            taddr = regs[r1];
            oi = regs[r2];
            tci_qemu_st(env, taddr, regs[r0], oi, tb_ptr);
        case INDEX_op_mb:
            /* Ensure ordering for all kinds */
            smp_mb();
            break;
        default:
            g_assert_not_reached();
        }
    }
}

/*
 * The maximum number of instances that can exist simultaneously
 *
 * If this limit is reached and a new instance is required, older instances are
 * removed to allow creation of new ones without exceeding the browser's limit.
 *
 * Raised from upstream's 12000 (2026-07-29). Hitting the cap is far worse than
 * it sounds: can_add_instance() then returns false, so the JIT stops compiling
 * ENTIRELY and every newly-hot block runs interpreted, while reclaim waits on
 * a JS FinalizationRegistry that may not run for a long time. An iPhone OS 1.0
 * boot saturated 12000 during driver matching and then made no further
 * progress for ~500 s.
 *
 * This is a heuristic guard against a browser limit, not the limit itself.
 * Raising it trades resident memory for keeping compilation alive; the
 * instrumented counters (compiled/recompiled/evicted/live) show directly
 * whether the working set now fits.
 */
#define MAX_INSTANCES 48000

/* Effective cap: MAX_INSTANCES sizes the static ring, so this may be lowered
 * at run time (max_instances= in /fw/jit-tune) but never raised past it. */
static int jit_max_instances = MAX_INSTANCES;
static bool jit_direct_table;

static int instances_global;

/*
 * Effective threshold from cap pressure. The steps are coarse on purpose --
 * this is a heuristic about where the remaining slots should go, and a smooth
 * curve would only make the counters harder to read.
 */
static void jit_update_threshold(void)
{
    int live, pressure_num;

    if (!jit_adaptive) {
        return;
    }
    live = qatomic_read(&instances_global);
    pressure_num = live / (jit_max_instances / 100 + 1);   /* percent */
    if (pressure_num < 50) {
        jit_threshold = jit_instantiate_num;               /* headroom: eager */
    } else if (pressure_num < 80) {
        jit_threshold = jit_instantiate_num * 4;
    } else {
        jit_threshold = jit_instantiate_num * 16;
    }
}

/* Avoid overwrapping of begin/end pointers */
#define INSTANCES_BUF_MAX (MAX_INSTANCES + 1)

static __thread struct WasmInstanceInfo instances[INSTANCES_BUF_MAX];
static __thread int instances_begin;
static __thread int instances_end;

/*
 * Diagnostic counters. The browser caps how many WebAssembly instances a page
 * may hold, so this backend evicts and recompiles; if the guest's hot working
 * set is larger than MAX_INSTANCES that degenerates into thrash -- compile,
 * evict, recompile -- which looks exactly like "the JIT is mysteriously slow".
 *
 * Reported to stderr every JIT_STATS_EVERY instantiations, which is cheap and
 * needs no environment variable (getenv is awkward to set in a browser). The
 * ratio that matters is recompiles/compiles: near zero is healthy, approaching
 * one means the working set does not fit.
 */
#define JIT_STATS_EVERY 16
static uint64_t jit_compiles;      /* modules instantiated */
static uint64_t jit_recompiles;    /* instantiations of a TB evicted earlier */
static uint64_t jit_evictions;     /* instances dropped to stay under the cap */

static void add_instance(wasm_tb_func tb_func, void *tb_ptr)
{
    instances[instances_end].tb_func = tb_func;
    instances[instances_end].tb_ptr = tb_ptr;
    set_info_local(tb_ptr, &(instances[instances_end]));
    instances_end  = (instances_end + 1) % INSTANCES_BUF_MAX;

    qatomic_inc(&instances_global);
    jit_update_threshold();

    /* Report the first compile too: knowing WHEN the JIT starts doing work at
     * all separates "not compiling" from "compiling and thrashing". */
    if (++jit_compiles == 1 || jit_compiles % JIT_STATS_EVERY == 0) {
        fprintf(stderr,
                "[JIT] compiled=%llu recompiled=%llu evicted=%llu live=%d/%d "
                "threshold=%d\n",
                (unsigned long long)jit_compiles,
                (unsigned long long)jit_recompiles,
                (unsigned long long)jit_evictions,
                qatomic_read(&instances_global), jit_max_instances,
                jit_threshold);
    }
}

static __thread int instance_pending_gc;
static __thread int instance_done_gc;

static void remove_old_instances(void)
{
    int num;
    if (instance_pending_gc > 0) {
        return;
    }
    if (instances_begin <= instances_end) {
        num = instances_end - instances_begin;
    } else {
        num = instances_end + (INSTANCES_BUF_MAX - instances_begin);
    }
    /* removes the half of the oldest instances in the buffer */
    num /= 2;
    for (int i = 0; i < num; i++) {
        release_wasm_tb((void *)instances[instances_begin].tb_func,
                        jit_direct_table);
        instances[instances_begin].tb_ptr = NULL;
        instances_begin = (instances_begin + 1) % INSTANCES_BUF_MAX;
        jit_evictions++;
    }
    jit_update_threshold();
    instance_pending_gc += num;
}

static bool can_add_instance(void)
{
    return qatomic_read(&instances_global) < jit_max_instances;
}

static wasm_tb_func get_instance_from_tb(void *tb_ptr)
{
    struct WasmInstanceInfo *elm = get_info_local(tb_ptr);
    if (elm == NULL) {
        return NULL;
    }
    if (elm->tb_ptr != tb_ptr) {
        /*
         * This TB was instantiated before, but has been removed. Set counter to
         * the max value so that this will be instantiated.
         */
        jit_recompiles++;      /* the thrash signal: evicted, now wanted again */
        set_counter_local(tb_ptr, INSTANTIATE_NUM);
        set_info_local(tb_ptr, NULL);
        return NULL;
    }
    return elm->tb_func;
}

static void check_gc_completion(void)
{
    if (instance_done_gc > 0) {
        qatomic_sub(&instances_global, instance_done_gc);
        instance_pending_gc -= instance_done_gc;
        instance_done_gc = 0;
    }
}

EM_JS_PRE(void, init_wasm_js, (void *instance_done_gc),
{
    Module.__wasm_tb = {
        inst_gc_registry: new FinalizationRegistry((i) => {
            if (i == "tbinstance") {
                const memory_v = new DataView(HEAP8.buffer);
                let v = memory_v.getInt32(DEC_PTR(instance_done_gc), true);
                memory_v.setInt32(DEC_PTR(instance_done_gc), v + 1, true);
            }
        })
    };
});

#define MAX_EXEC_NUM 50000
static __thread int exec_cnt = MAX_EXEC_NUM;
static inline void trysleep(void)
{
    /*
     * Even during running TBs continuously, try to return the control
     * to the browser periodically and allow browsers doing tasks.
     */
    if (--exec_cnt == 0) {
        if (!can_add_instance()) {
            emscripten_sleep(0);
            check_gc_completion();
        }
        exec_cnt = MAX_EXEC_NUM;
    }
}

static int thread_idx_max;

static void init_wasm(void)
{
    if (qatomic_read(&thread_idx_max) == 0) {
        long n = jit_tunable("IT_WASM_INSTANTIATE_NUM", "instantiate",
                             INSTANTIATE_NUM_DEFAULT);
        long cap = jit_tunable("IT_WASM_MAX_INSTANCES", "max_instances",
                               MAX_INSTANCES);

        jit_instantiate_num = (n > 0 && n <= INT32_MAX) ? (int)n
                                                        : INSTANTIATE_NUM_DEFAULT;
        jit_max_instances = (cap > 0 && cap <= MAX_INSTANCES) ? (int)cap
                                                              : MAX_INSTANCES;
        jit_adaptive = jit_tunable("IT_WASM_JIT_ADAPTIVE", "adaptive", 1) != 0;
        jit_direct_table = jit_tunable("IT_WASM_DIRECT_TABLE", "direct_table",
                                       1) != 0;
        jit_threshold = jit_instantiate_num;
        fprintf(stderr, "[JIT] tuning: instantiate=%d max_instances=%d "
                "adaptive=%d direct_table=%d\n", jit_instantiate_num,
                jit_max_instances, jit_adaptive, jit_direct_table);
    }
    thread_idx = qatomic_fetch_inc(&thread_idx_max);
    ctx.stack = g_malloc(TCG_STATIC_CALL_ARGS_SIZE + TCG_STATIC_FRAME_SIZE);
    ctx.buf128 = g_malloc(16);
    ctx.tci_tb_ptr = (uint32_t *)&tci_tb_ptr;
    init_wasm_js(&instance_done_gc);
}

static __thread bool initdone;

uintptr_t tcg_qemu_tb_exec(CPUArchState *env, const void *v_tb_ptr)
{
    if (!initdone) {
        init_wasm();
        initdone = true;
    }
    ctx.env = env;
    ctx.tb_ptr = (void *)v_tb_ptr;
    while (true) {
        trysleep();
        uintptr_t res;
        wasm_tb_func tb_func = get_instance_from_tb(ctx.tb_ptr);
        if (tb_func) {
            /*
             * Call the Wasm instance
             */
            res = call_wasm_tb(tb_func, &ctx);
        } else if (!inc_counter(ctx.tb_ptr)) {
            /*
             * Run it on TCI because the counter value is small
             */
            res = tcg_qemu_tb_exec_tci(env);
        } else if (!can_add_instance()) {
            /*
             * Too many instances has been created, try removing older
             * instances and keep running this TB on TCI
             */
            remove_old_instances();
            check_gc_completion();
            res = tcg_qemu_tb_exec_tci(env);
        } else {
            /*
             * Instantiate and run the Wasm module
             */
            struct WasmTBHeader *header = (struct WasmTBHeader *)ctx.tb_ptr;
            tb_func = (wasm_tb_func)instantiate_wasm(header->wasm_ptr,
                                                     header->wasm_size,
                                                     header->import_ptr,
                                                     header->import_size,
                                                     jit_direct_table);
            add_instance(tb_func, ctx.tb_ptr);
            res = call_wasm_tb(tb_func, &ctx);
        }
        if (!ctx.tb_ptr) {
            return res;
        }
    }
}
