# Repository working method

## Script tests first

For guest UI, Wi-Fi, power, browser, and HTTPS work, use a reusable scripted
test whenever possible. Do not spend one agent/LLM round trip per tap, key,
wait, screenshot, or log check.

- Put repeatable guest interaction behind QMP and run the whole case in one
  local process.
- Make tests emit machine-readable results plus screenshots and relevant log
  offsets, so failures can be diagnosed after the run.
- Manual tap-by-tap work is only for discovering a missing coordinate/state or
  investigating a failed script. Fold every useful discovery back into the
  harness before continuing repeated tests.
- Prefer state/evidence-based waits over fixed sleeps. A fixed delay is
  acceptable only when the guest exposes no observable readiness signal.
- Cover Wi-Fi association, DHCP/DNS/network traffic, HTTP, HTTPS, redirects,
  subresources, input, and power as separate cases. Do not let sleep/wake
  testing stand in for broader acceptance coverage.
- Reuse `scripts/ipod-https-acceptance.py` for Safari/HTTPS cases and
  `scripts/wifi-dev/sdio-trace-boot.py` for SDIO bring-up. Extend those scripts
  instead of recreating manual sequences.

All firmware diagnosis and scripted tests must use staged NAND/NOR copies.
Never mutate the installed app's only firmware copy while diagnosing.

## Debugging the guest (S5L8900 iBoot / kernel)

This is the proven loop for chasing a guest-side hang, panic, or bad branch. It
is deterministic and does not depend on an external debugger. The iPod Touch 1G
sleep/wake work (`SLEEP_WAKE_INVESTIGATION.md`, on the `ipod_touch_1g` line) is
the worked example — dozens of findings, all via this loop.

**The loop:**

1. **Disassemble** the extracted iBoot/kernel to locate the suspect instruction.
   Use capstone (`CS_ARCH_ARM`; `CS_MODE_THUMB` for iBoot/kernel, `CS_MODE_ARM`
   for the libc/veneer region). iBoot loads at VA `0x18000000` (file offset 0);
   the reset vector and most code are Thumb. Confirm a load base empirically by
   matching a known string pointer (e.g. a literal that points at `"miu_init"`).
2. **Instrument, then `ninja`.** Prefer a small C hook or a guest-memory patch
   in the machine model over any single-stepping:
   - *C hook* — add a `fprintf(stderr, ...)` in the relevant device model
     (`hw/arm/ipod_touch_*.c`) or, for a control-flow bug, in the CPU exception
     path (`target/arm/`, e.g. log the pre-abort PC/LR in `arm_cpu_do_interrupt`).
   - *Guest patch* — write bytes into guest memory at load/reset to change or
     trap behavior (patch a `b .` loop, force a wrapper to return, `MOVS R0,#0 +
     BX LR` to stub a check). These are diagnostic-only and must be called out.
   - Rebuild the merged QEMU 11 engine: `ninja -C build-ipod11`. Confirm the
     machine is present: `./build-ipod11/qemu-system-arm -M help | grep -i iphone`.
3. **Observe** the staged boot:
   - **Serial to a file:** `-serial file:serial.log`; grep for phase markers.
   - **Monitor over a unix socket:** `-monitor unix:/tmp/qemu-mon.sock,server,nowait`,
     then `info registers` (sample `R15`/PC), `xp/Nxw <addr>` (physical memory —
     bypasses the MMU), to read state at a hang. A silent hang is usually a
     panic/abort, not a stall: always sample PC before concluding it hung.
   - **`-d` trace flags:** `-d unimp` (unimplemented MMIO), `-d int,cpu_reset`
     (exceptions with CPU state), `-d exec,nochain` (per-TB PC trace — huge, so
     kill quickly after the trigger). Route with `-D file`.
   - Bound **every** boot with a hard timeout (macOS has no `timeout(1)`; use a
     Python watchdog). An untimed boot once wedged a session for two hours.

**Determinism — use `-icount`.** Without it, `QEMU_CLOCK_VIRTUAL` tracks wall
time, so timer/interrupt-driven behavior (and any fault that depends on it)
shifts with host speed — and single-stepping makes the guest clock *fly*,
moving the bug. Add `-icount shift=3` for a repeatable timer/interrupt timeline
that matches the fast path. Reproduce the bug under `-icount` first, then debug.

**Breakpoints / external debuggers (last resort, and note the traps):**

- No cross-`gdb` is installed by default, and hand-rolling a gdb-remote (RSP)
  client is not worth it — reach for the C-hook/patch loop above instead.
- If you do attach a debugger to the gdbstub (`-S -gdb tcp::1234`): iBoot at
  `0x18000000` is a **read-only** region, so **software** breakpoints (`Z0`)
  silently fail there — use **hardware** breakpoints (`Z1`). A real `gdb`
  handles this automatically; a naive client does not.
- Never single-step long stretches; it is slow and (without `-icount`) distorts
  timing. A one-shot breakpoint to capture registers at a known address is fine.

## Firmware and derived-artifact policy

- Prefer reproducible construction from a user-supplied IPSW over requiring a
  physical-device dump. A dump may be used as an optional comparison oracle,
  but must not become the only supported input when a documented constructor
  is practical.
- Do not commit IPSWs, decrypted Apple images, generated NOR/NAND trees,
  device-unique data, or physical dumps. Keep only extraction/construction
  code, format documentation, hashes/manifests, and tests in the repository.
- Record provenance for every local artifact used in a result: device/build,
  source kind (IPSW or device), cryptographic hash, constructor version, and
  any guest-file modifications. Do not describe an artifact as a device dump
  without evidence.
- For S5L8900 NAND work, follow the historically proven qemu-ios approach:
  construct the sparse physical page tree and its spare/VFL/FTL/WMR metadata
  from an IPSW-derived filesystem. Treat emulated DFU/restore as a separate
  higher-fidelity track, not a prerequisite for first boot.
- Do not bypass guest storage validation or patch guest FTL reads in the
  production path. Such patches are diagnostic-only and must be called out in
  test results.
- Public project history is technical provenance, not a legal conclusion.
  Avoid asserting that an artifact or distribution is lawful; keep repository
  policy focused on user-supplied inputs and non-distribution of Apple data.
