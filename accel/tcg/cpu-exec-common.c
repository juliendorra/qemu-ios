/*
 *  emulator main execution loop
 *
 *  Copyright (c) 2003-2005 Fabrice Bellard
 *
 * This library is free software; you can redistribute it and/or
 * modify it under the terms of the GNU Lesser General Public
 * License as published by the Free Software Foundation; either
 * version 2.1 of the License, or (at your option) any later version.
 *
 * This library is distributed in the hope that it will be useful,
 * but WITHOUT ANY WARRANTY; without even the implied warranty of
 * MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
 * Lesser General Public License for more details.
 *
 * You should have received a copy of the GNU Lesser General Public
 * License along with this library; if not, see <http://www.gnu.org/licenses/>.
 */

#include "qemu/osdep.h"
#include "exec/log.h"
#include "system/tcg.h"
#include "qemu/plugin.h"
#include "internal-common.h"

bool tcg_allowed;

#ifdef EMSCRIPTEN
#define WASM_IO_SPLIT_BITS 12
#define WASM_IO_SPLIT_SIZE (1 << WASM_IO_SPLIT_BITS)

typedef struct WasmIOSplitEntry {
    vaddr pc;
    bool valid;
} WasmIOSplitEntry;

static __thread WasmIOSplitEntry wasm_io_splits[WASM_IO_SPLIT_SIZE];
static __thread int wasm_io_split_active = -1;
static __thread uint64_t wasm_io_split_count;

bool wasm_io_split_enabled(void)
{
    if (unlikely(wasm_io_split_active < 0)) {
        /* Proven default; /fw/io-split containing 0 is the A/B fallback. */
        char value = '1';
        FILE *f = fopen("/fw/io-split", "r");

        if (f != NULL) {
            if (fread(&value, 1, 1, f) != 1) {
                value = '1';
            }
            fclose(f);
        }
        wasm_io_split_active = value != '0';
    }
    return wasm_io_split_active;
}

static unsigned wasm_io_split_hash(vaddr pc)
{
    return ((pc >> 1) ^ (pc >> (WASM_IO_SPLIT_BITS + 1))) &
           (WASM_IO_SPLIT_SIZE - 1);
}

bool wasm_io_split_known(vaddr pc)
{
    WasmIOSplitEntry *entry;

    if (!wasm_io_split_enabled()) {
        return false;
    }
    entry = &wasm_io_splits[wasm_io_split_hash(pc)];
    return entry->valid && entry->pc == pc;
}

void wasm_io_split_record(vaddr pc)
{
    WasmIOSplitEntry *entry;

    if (!wasm_io_split_enabled()) {
        return;
    }
    entry = &wasm_io_splits[wasm_io_split_hash(pc)];
    if (!entry->valid || entry->pc != pc) {
        entry->pc = pc;
        entry->valid = true;
        wasm_io_split_count++;
    }
}

uint64_t wasm_io_split_learned(void)
{
    return wasm_io_split_count;
}

/*
 * The browser profile attributes a large share of the vCPU to Emscripten's
 * JS-throw implementation of siglongjmp, but a sampled stack cannot tell us
 * why QEMU unwound. Keep this diagnostic dormant unless the viewer stages
 * /fw/exit-profile, then report a cheap power-of-two histogram. In
 * particular, distinguish deterministic-I/O recompiles from architectural
 * exceptions: their fixes are completely different.
 */
typedef struct WasmExitProfile {
    uint64_t total;
    uint64_t noexc_io;
    uint64_t noexc_other;
    uint64_t arch[8];
    uint64_t hlt;
    uint64_t yield;
    uint64_t atomic;
    uint64_t other;
    int enabled;
} WasmExitProfile;

static __thread WasmExitProfile wasm_exit_profile = { .enabled = -1 };

static void wasm_exit_profile_report(FILE *f, const WasmExitProfile *p)
{
    fprintf(f,
            "[WASM-EXIT] total=%llu io=%llu noexc=%llu learned=%llu "
            "arch0=%llu undef=%llu swi=%llu pabt=%llu dabt=%llu "
            "irq=%llu fiq=%llu bkpt=%llu hlt=%llu yield=%llu "
            "atomic=%llu other=%llu\n",
            (unsigned long long)p->total,
            (unsigned long long)p->noexc_io,
            (unsigned long long)p->noexc_other,
            (unsigned long long)wasm_io_split_learned(),
            (unsigned long long)p->arch[0],
            (unsigned long long)p->arch[1],
            (unsigned long long)p->arch[2],
            (unsigned long long)p->arch[3],
            (unsigned long long)p->arch[4],
            (unsigned long long)p->arch[5],
            (unsigned long long)p->arch[6],
            (unsigned long long)p->arch[7],
            (unsigned long long)p->hlt,
            (unsigned long long)p->yield,
            (unsigned long long)p->atomic,
            (unsigned long long)p->other);
}

static void wasm_exit_profile_note(CPUState *cpu)
{
    WasmExitProfile *p = &wasm_exit_profile;
    int excp = cpu->exception_index;

    if (unlikely(p->enabled < 0)) {
        FILE *f = fopen("/fw/exit-profile", "r");

        p->enabled = f != NULL;
        if (f != NULL) {
            fclose(f);
        }
    }
    if (!p->enabled) {
        return;
    }

    p->total++;
    if (excp == -1) {
        if (cpu->cflags_next_tb & CF_MEMI_ONLY) {
            p->noexc_io++;
        } else {
            p->noexc_other++;
        }
    } else if (excp >= 0 && excp < ARRAY_SIZE(p->arch)) {
        p->arch[excp]++;
    } else if (excp == EXCP_HLT) {
        p->hlt++;
    } else if (excp == EXCP_YIELD) {
        p->yield++;
    } else if (excp == EXCP_ATOMIC) {
        p->atomic++;
    } else {
        p->other++;
    }

    if (p->total == 1 || (p->total & 1023) == 0) {
        FILE *f;

        wasm_exit_profile_report(stderr, p);
        f = fopen("/fw/exit-profile-results", "w");
        if (f != NULL) {
            wasm_exit_profile_report(f, p);
            fclose(f);
        }
    }
}
#endif

bool tcg_cflags_has(CPUState *cpu, uint32_t flags)
{
    return cpu->tcg_cflags & flags;
}

void tcg_cflags_set(CPUState *cpu, uint32_t flags)
{
    cpu->tcg_cflags |= flags;
}

uint32_t curr_cflags(CPUState *cpu)
{
    uint32_t cflags = cpu->tcg_cflags;

    /*
     * Record gdb single-step.  We should be exiting the TB by raising
     * EXCP_DEBUG, but to simplify other tests, disable chaining too.
     *
     * For singlestep and -d nochain, suppress goto_tb so that
     * we can log -d cpu,exec after every TB.
     */
    if (unlikely(cpu->singlestep_enabled)) {
        cflags |= CF_NO_GOTO_TB | CF_NO_GOTO_PTR | CF_SINGLE_STEP | 1;
    } else if (qatomic_read(&one_insn_per_tb)) {
        cflags |= CF_NO_GOTO_TB | 1;
    } else if (qemu_loglevel_mask(CPU_LOG_TB_NOCHAIN)) {
        cflags |= CF_NO_GOTO_TB;
    }

    return cflags;
}

/* exit the current TB, but without causing any exception to be raised */
void cpu_loop_exit_noexc(CPUState *cpu)
{
    cpu->exception_index = -1;
    cpu_loop_exit(cpu);
}

void cpu_loop_exit(CPUState *cpu)
{
#ifdef EMSCRIPTEN
    wasm_exit_profile_note(cpu);
#endif
    /* Undo the setting in cpu_tb_exec.  */
    cpu->neg.can_do_io = true;
    /* Undo any setting in generated code.  */
    qemu_plugin_disable_mem_helpers(cpu);
    siglongjmp(cpu->jmp_env, 1);
}

void cpu_loop_exit_restore(CPUState *cpu, uintptr_t pc)
{
    if (pc) {
        cpu_restore_state(cpu, pc);
    }
    cpu_loop_exit(cpu);
}

void cpu_loop_exit_atomic(CPUState *cpu, uintptr_t pc)
{
    /* Prevent looping if already executing in a serial context. */
    g_assert(!cpu_in_serial_context(cpu));
    cpu->exception_index = EXCP_ATOMIC;
    cpu_loop_exit_restore(cpu, pc);
}
