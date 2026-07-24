# iPhone 2G (M68AP) bring-up — session handoff

This is a working log for booting **iPhone OS 1.x** on the `-M iPhone-2G`
machine. It records the path taken, what is proven, the dead ends, and the
next concrete steps, so the next person (or LLM) can continue without redoing
the investigation. Read `IPHONE_2G.md` first for the board design; this file is
the live bring-up state.

---

## 2026-07-24 — Baseband lab: trace-driven, parallel S-Gold2 iteration in minutes

The WiFi bring-up template (trace the driver's protocol, answer until it is
satisfied) is now tooled for the baseband, with the modem brain moved OUT of
compiled C so an iteration costs an edit + relaunch, not a rebuild:

- **`scripts/sgold2d.py`** — external S-Gold2 modem. QEMU wires uart1 to a
  unix socket (`IT_M68AP_NO_BASEBAND=1` disables only the built-in C stub;
  uart1 then falls through to the *second* `-serial` argument); this daemon
  answers AT commands per a JSON ruleset that is **hot-reloaded on mtime
  change** (edit responses mid-boot, no restart). Every byte both ways is
  logged (`raw.log` hexdump, `lines.log` conversation, `events.jsonl` for
  diffing), and `summary.json` — refreshed after every command — lists the
  **unmatched commands**: exactly what AppleBaseband asked that the ruleset
  had no specific answer for. That file is the iteration loop.
- **`scripts/baseband-rules/*.json`** — rulesets: `silent.json` (record-only),
  `stub.json` (faithful replica of the C stub), `eager.json` (standard AT
  init/status answers as a first hypothesis). Format: ordered regex rules
  with group-substituted responses, optional delays, state vars and guards.
- **`scripts/baseband-lab.py`** — parallel, self-judging matrix runner: one
  M68AP instance per ruleset (plus special names `none` = known-good silence
  baseline, `builtin` = the C stub under `IT_BASEBAND_TRACE`), each watched
  until a decisive verdict — `springboard_reached` (SpringBoard config / FB
  attach markers), `baseband_retry_loop` (serial stall + ≥20 AppleBaseband
  lines: the known failure signature), `stalled`, or `timeout` — then PC
  samples (deadlock vs crawl), framebuffer non-black %, serial tail, and the
  modem summary are folded into `matrix.json` + a printed comparison table.
  A whole hypothesis matrix costs one boot's wall time.
- **`IT_BASEBAND_TRACE=<path|stderr>`** (in `hw/arm/ipod_touch_baseband.c`)
  — same byte-level trace for the in-QEMU stub, so the shipping-app
  configuration is observable too.

Usage (the standard experiment):

    python3 scripts/baseband-lab.py --logs /tmp/bblab \
        --rules scripts/baseband-rules/silent.json \
                scripts/baseband-rules/stub.json builtin

Then read each instance's `modem/lines.log` (what AppleBaseband said) and
`modem/summary.json` (what went unanswered), edit/fork a ruleset, re-run the
matrix. Defaults point at `m68ap-artifacts/stage/` (sbpatch iBoot, fresh
NAND); override with `--iboot/--nor/--nand`.

### First capture results (the exact run above; verdicts in 114–158 s wall, in parallel)

| instance | verdict | serial lines | uart1 conversation |
|---|---|---|---|
| silent  | stalled (panic → KDP wait) | 2084 | ~54 unanswered iBoot retries |
| stub    | baseband_retry_loop        | 1617 | 3 commands, then nothing |
| builtin | baseband_retry_loop        | 1617 | identical to stub |

Four findings, each of which reframes the plan:

1. **The Python replica is faithful.** `stub` (external socket modem) and
   `builtin` (in-QEMU C stub) produce byte-identical progress: 1617 serial
   lines, 22 AppleBaseband lines, the same stall, the same storm PC
   (`AppleS5L8900XSerial 0xc04bd868`) in the terminal samples. All further
   iteration can happen in Python without touching C.
2. **The entire known uart1 conversation is iBoot's, not the kernel's.**
   `AT+xdrv=9,1,0;` is iBoot's baseband-NVRAM read (the serial lines
   `Read 0 bytes from nvram` / `Installing WIFI Calibration` ARE these uart1
   transactions): answered `+XDRV: 9,1,0,0,NULL` it completes in ~1 s;
   unanswered it retries at 1 Hz inside 5 s-per-read windows and errors out.
   Then one `AT+cgsn;` (IMEI query — today answered with a bare `OK`, no
   IMEI payload; `eager.json` has a real-shaped answer to try). That is ALL
   the uart1 traffic in the whole boot.
3. **The kernel-era AppleBaseband stalls WITHOUT writing a single byte to
   uart1.** After `AppleBaseband::start` / `config ... starting on
   AppleBaseband, 10` the retry loop runs with an empty uart1 trace. So the
   baseband-on stall is NOT an unanswered AT command; the driver is waiting
   on something baseband-initiated or out-of-band (unsolicited bytes after
   its reset sequence, a GPIO/host-wake line, flow control). `sgold2d.py`
   grew a top-level `"unsolicited"` ruleset feature (timed/periodic sends,
   timers re-fire on hot reload) specifically to probe this.
4. **Total silence is worse than a shallow stub.** With no answers, iBoot's
   NVRAM reads add ~20 s of timeouts and the kernel later panics at
   `IOIpodUSBDevice::start` (caller `0xC012D963`) into the KDP debugger
   wait. Caution: this socket-connected-but-mute config is NOT the same as
   the known-good `IT_M68AP_NO_BASEBAND=1` bare config that reaches
   SpringBoard, and the panic sits in the same timing-sensitive USB-start
   region as the old observer-artifact retain — likely another timing
   perturbation (the three 5 s NVRAM delays), not a real baseband
   dependency. Run a `none` instance alongside before believing anything
   about this panic.

### Second matrix (eager vs stub vs none) — the USB-start panic is a RACE

Run 2 (same defaults, 900 s cap): `none` **reached SpringBoard under the
lab** (fb_attach ×2, SpringBoard[…], sim_status + activation lines;
verdict `springboard_reached` at t≈72 s wall) — baseline and verdict path
both validated. But `eager` and `stub` both **panicked at
`IOIpodUSBDevice::start` (caller `0xC012D963`)** at serial line ~1276,
the very panic run 1's `silent` hit — while run 1's `stub`, byte-identical
config, instead sailed past USB start into the 1617-line baseband stall.

Conclusion: the `IOIpodUSBDevice::start` panic is a **nondeterministic
race**, not a response-content effect. Score so far: 3/4 socket-wired
boots hit it, 0/3 non-socket boots (builtin C stub or bare `none`). The
socket chardev's asynchronous RX delivery (a few hundred µs vs the C
stub's in-MMIO instant reply) plausibly widens the same timing window the
observer plugin used to hit (the IOPMrootDomain under-retain family).
The eager-IMEI hypothesis is therefore UNTESTED — both candidates died
before the kernel baseband phase.

Lab upgrades from this lesson (committed): a `panic` marker + immediate
`panicked` verdict (no more mislabeling as `stalled`, and ~90 s faster),
and duplicate-ruleset support (`--rules stub.json stub.json stub.json`
auto-suffixes instances) so flake rates can be measured directly.

Next moves (in order of information-per-minute):
1. **Measure the flake**: `--rules stub.json ×3 none builtin` — how often
   does a socket boot pass USB start? If ≥1/3 passes, matrices simply need
   repeats; if ~0, chase the race itself (or add a tiny artificial delay
   in the C stub to reproduce it deterministically, which would also
   pin down the IOPMrootDomain window).
2. Re-test `eager` vs `stub` with repeats — the IMEI question is still
   open.
3. Since the kernel driver is uart1-mute, chase its non-UART inputs: what
   GPIOs/registers does AppleBaseband poll (openiboot `hardware/radio.h`:
   BB_ON 0x1807, RADIO_ON 0x1507, BB_RESET…) and what does our GPIO model
   return for them? A ruleset alone cannot fix a GPIO wait.
4. Probe with `"unsolicited"` sends (e.g. periodic `\r\nOK\r\n`, `RING`,
   `+XDRV` status lines) to learn whether ANY baseband-initiated traffic
   moves the driver.
5. Keep `none` AND `builtin` controls in every matrix (see
   `DEVICE_BRINGUP_PLAYBOOK.md`, step 5 — this rule is what exposed the
   race in two runs).

### Run 3 (flake measurement) — the panic is HOST CONTENTION, not socket asynchrony

Next-move 1 executed: `stub.json ×3 + none + builtin`, all five launched
**simultaneously**. Result: `stub`/`stub-2`/`stub-3` **and `builtin`** all
panicked at `IOIpodUSBDevice::start` at wall 20.1 s (17 AppleBaseband
lines each); only `none` reached SpringBoard (134.8 s — slow, again from
contention). A panicking `builtin` kills run 2's socket-asynchrony theory:
the C stub replies in-MMIO with zero latency and still panicked. The real
variable is **host CPU contention while the guest is inside its USB-start
window (~15–25 s into boot)** — five QEMUs booting in lockstep stretch
driver starts and flip the IOPMrootDomain under-retain race. (`none`
survives because with no baseband chatter its boot timeline is shifted.)

Fixes committed to the lab:
- **`--stagger-secs N`** (default 30): delays instance i by i×N so at most
  one guest occupies the USB-start window at a time.
- **`IT_BASEBAND_RULES=<path>`** in `hw/arm/ipod_touch_baseband.c`: the
  built-in stub's response table externalized to a TAB-separated `.rules`
  file (prefix→response, `\r\n` escapes, `{int}`/`{rest}` tokens,
  `default` row, `-` = silent). Passing a `.rules` path to `--rules` uses
  the in-QEMU stub (instance suffix `-ct`) — hypothesis iteration with NO
  rebuild but with in-MMIO instant-reply timing, sidestepping the socket
  path entirely. `scripts/baseband-rules/stub.rules` and `eager.rules`
  replicate their JSON namesakes.

### Run 4 (staggered, 2026-07-24) — content and latency BOTH ruled out; the driver is deaf

Protocol: 30 s stagger. Seats: `builtin` (solo), then `stub.json` +
`stub.rules` + `eager.rules` in one staggered invocation, plus one
`stub.rules` solo re-run.

| instance | transport | verdict | wall | bb lines |
|---|---|---|---|---|
| builtin  | in-MMIO C stub        | baseband_retry_loop | 112.3 | 22 |
| stub     | external socket modem | baseband_retry_loop | 112.3 | 22 |
| stub-ct  | in-MMIO, stub.rules   | panicked (USB race) → re-run: baseband_retry_loop | 18.1 / 112.2 | 17 / 22 |
| eager-ct | in-MMIO, eager.rules  | baseband_retry_loop | 112.2 | 22 |

Flake rate under stagger: 1 panic in 5 staggered boots (vs 4/4
simultaneous socket+builtin in run 3) — stagger works well enough to
iterate; repeat any `panicked` seat once.

Findings:
- **Response content is irrelevant.** `eager` (real-shaped IMEI/SIM/
  registration answers) is byte-for-byte the same outcome as the shallow
  stub: identical verdict, wall time, 22 AppleBaseband lines, ~1615
  serial lines. The run-2 "IMEI question" is now answered: NO.
- **Response latency is irrelevant.** In-MMIO instant replies
  (`stub-ct`/`eager-ct`) behave identically to the ~ms-async socket
  modem.
- **The kernel driver is uart1-deaf and uart1-mute.** The C-table traces
  show the whole boot's uart1 traffic is three iBoot-era commands
  (`AT+xdrv=9,1,0;` ×3, 13–15 s guest time), answered instantly; then
  zero bytes either way through the entire retry loop. (Run-4 detail:
  iBoot still re-sends `AT+xdrv` ~1 Hz even when answered instantly —
  its NVRAM loop is time-boxed, not response-gated.)
- One cosmetic gap: `stub.json` leaves iBoot's `AT+cgsn;` unmatched
  (bare-OK default); add an IMEI row if it ever matters.

Consequence: rulesets alone CANNOT unstick AppleBaseband. The stall input
must be non-UART. Next moves, reordered:
1. **GPIO**: instrument what AppleBaseband reads/polls (openiboot
   `hardware/radio.h`: BB_ON 0x1807, RADIO_ON 0x1507, BB_RESET,
   host-wake) and what our GPIO model returns; make the "baseband
   present/awake" lines read as asserted.
2. **Unsolicited probes** (`sgold2d.py` `"unsolicited"` rules): periodic
   `\r\nOK\r\n`, `RING`, `+XDRV` lines — does ANY baseband-initiated
   byte move the driver? (Socket path is usable again now that staggering
   tames the panic.)
3. Only then revisit AT content.

### Runs 5–7: the stall was never the baseband — it was the UART model. ROOT CAUSE FOUND

Chasing next-move 1 reframed everything, in four steps.

**GPIO ruled out by instrumentation** (`IT_GPIO_TRACE=<path|stderr>` in
`hw/arm/ipod_touch_gpio.c`, per-(dir,addr,pc) dedup). A stalling `builtin`
boot and a SpringBoard `none` boot produce essentially IDENTICAL GPIO
traces (71 accesses; diff = one extra late read): iBoot pokes port7-pin1
before each AT command, the kernel configures pins at ~16.5 s + three
one-shot reads at 17 s, then NOTHING in either boot. Nobody polls GPIO
during the stall.

**The stall is in userland, not the kernel driver.** Reading the stall
tail properly: the "baseband_retry_loop" boots get ALL the way through
root mount, launchd, mDNSResponder, and park at `lockdown[20]:
data_ark_load` — one step before `lookup_baseband_info`, the step where
the `none` boot declares the baseband dead (29× AppleBasebandUserClient
attach→terminate churn) and sails on to SpringBoard. In stalling boots
the user client NEVER attaches. (The ≥20-AppleBaseband-lines verdict
signature counts the audio/serial-layer lines every boot prints; the
name "baseband_retry_loop" was a misnomer all along.)

**Run 5 killed the response hypothesis entirely.** New `.rules` seats
`silent` (everything mute), `xdrv-only`, `cgsn-only`: ALL THREE stall.
A byte-mute stub is guest-visibly identical to `none` in traffic — yet
`none` boots and `silent` doesn't. The only remaining difference is
INSIDE the uart model: with a NULL chardev the whole `UTXH` branch is
skipped, so no `UINTSP_TXD` ever latches; with any chardev, iBoot's
transmits latch TXD pending that survives into the kernel handoff.

**Run 6, register-level proof** (`IT_UART_TRACE=<path|stderr>` +
`IT_UART_TRACE_CHANNEL`, in `hw/char/exynos4210_uart.c`): the stalling
boot's kernel spends 15k+ trace lines in a two-instruction loop at
`0xc04bd868/78` — AppleS5L8900XSerial's ISR reading `UTRSTAT` and
writing `UTRSTAT=0` forever. The stale iBoot-era TXD storms the ISR the
moment lockdownd opens the baseband tty. That is the "uart1-mute
AppleBaseband": the driver is livelocked in its own ISR before it ever
transmits. (Same PC the run-1 samples flagged.)

The driver's register idiom tells us what the real S5L8900 UART must do
(it never writes UINTP/UINTSP with a non-zero value — ALL its acking is
`UCON` mode toggles and `UTRSTAT` writes):

1. `UCON` direction mode = 00 (disable) withdraws that direction's
   pending interrupt (S3C-family documented behaviour). Driver init
   writes `UCON=0x400` before unmasking.
2. `UFCON` Rx-FIFO-reset must also clear `UTRSTAT` data-ready/timeout
   and pending RXD — otherwise iBoot's last consumed-response state
   reads back as a ghost byte after the kernel's FIFO reset.
3. A `UTRSTAT` write is the Tx-interrupt ack (clears TXD pending) —
   there is no other ack in the driver's vocabulary, and the PL192 VIC
   has no sub-pending register to do it elsewhere.

All three implemented in `hw/char/exynos4210_uart.c` (alongside the
earlier edge-triggered-TXD storm fix, which stands).

**Run 7 validation, fix 1 alone**: `silent-ct` → **springboard_reached**
— the FIRST chardev-connected uart1 boot ever to get there. `stub-ct`
got further than ever: AppleBasebandUserClient attaches, **CommCenter
launches**, and the kernel driver transmitted its FIRST uart1 byte
('a' of a lowercase `at` command) — then hit the ghost-Rx/TXD-ack storm
(`UTRSTAT=0x7` ISR loop), which fixes 2+3 target.

Watch out: two parallel seats sharing one `IT_UART_TRACE` path
interleave in the same file — use one trace path per seat.

### Run 8 detour — an ack must never SYNTHESIZE an interrupt

First cut of fixes 2+3 called full `update_irq()` from the ack paths.
`update_irq` has a side effect: it re-raises RXD whenever the Rx FIFO is
non-empty. Result: a `UTRSTAT` write with leftover iBoot bytes still in
the FIFO fired a brand-new uart1 interrupt in the middle of the kernel's
serial init — and every answering seat (stub, builtin, cgsn-only) died
6/6 in the `IOIpodUSBDevice::start` panic, which that spurious-IRQ
timing shift makes ~deterministic (silent/none, no RX bytes, kept
passing). Fixed with `update_irq_line()` — ack paths recompute
UINTP/the IRQ line from CURRENT pending bits only. Lesson for every
future ack: clear-and-recompute, never re-derive new events.

### Run 9 — SPRINGBOARD WITH A LIVE BASEBAND; CommCenter talks AT; new frontier is transport mode

Last piece: after the handshake the ISR stormed again — `update_irq`
held RXD asserted as long as ANY byte sat in the FIFO (level), but the
driver acks in the ISR and drains from a separate thread, so the ISR
re-entered forever and the reader never ran. RXD is now an EVENT like
TXD: raised on byte arrival (receive path), on trigger-level crossing,
and on Rx timeout; acked by the `UTRSTAT` write (which clears TXD+RXD
pending; FIFO contents and UTRSTAT/UFSTAT data-ready remain for the
reader thread).

With all four UART semantics in place (`hw/char/exynos4210_uart.c`:
edge TXD from b045bb7e2b, UCON-disable clears pending, Rx-FIFO-reset
clears rx status+pending, UTRSTAT-write acks TXD+RXD; all acks via
side-effect-free `update_irq_line`):

    stub-ct  →  springboard_reached at 84.3 s wall, baseband ALIVE

CommCenter runs a real kernel-era AT session against the C-table stub
(6.6k trace lines): `at`, `ate0`, `at+xsio?`, `at+ipr=750000` (baud
raise), `at+xlog=0/2`, `at+xtransportmode` — all currently answered
with the default bare `OK` — then switches to an HDLC-style framed
transport (`0x7E`-flagged frames on the wire), gets no valid frames
back, resets the link (`at` … `at+xtransportmode` again) and loops.
SpringBoard boots regardless.

So the baseband bring-up ladder now stands at:
1. DONE — kernel driver alive, AT conversation rule-drivable
   (`IT_BASEBAND_RULES` / `sgold2d.py` both usable; extend rulesets
   with real `+XSIO:` answers etc. and see what changes).
2. NEXT — the post-`at+xtransportmode` framed protocol (Apple reliable
   serial layer / MUX). Real telephony/SIM/activation state lives
   behind it. Options: answer `at+xtransportmode` with an error to keep
   CommCenter in plain AT mode (cheap probe), or implement the framing.
3. Verify the `-ct` results replicate on the socket modem path
   (sgold2d) now that the storms are gone.

### Run 10 — plain AT mode is NOT enough; the framed layer is mandatory

`atmode.rules` (answers `at+xtransportmode` with `ERROR`, plus
real-shaped `+XSIO: 4`, IMEI, `+CPIN: READY`, `+CREG`, `+CSQ`, `+COPS`,
`+CIMI`): still reaches SpringBoard, and lockdownd DID advance —
`lookup_baseband_info: We now have SIM status` — but CommCenter ignores
the `ERROR` and switches to framing anyway (device stays
`[Unactivated]`). Refusing transport mode does not keep it in AT mode;
the framed layer is required, not optional.

### Run 11 — the framed transport is SLIP; echo advances the guest but is not an ACK

`IT_BASEBAND_FRAME_ECHO=1` (in `hw/arm/ipod_touch_baseband.c`) collects
each `0xC0`-delimited frame and echoes it back verbatim. Under echo the
guest STOPS spamming one frame 460× and instead walks a real sequence
(first four frames, `<E` = echoed):

    C0 00 2f 00 d0 01 7e C0
    C0 00 2f 00 d0 02 7d C0
    C0 00 2f 00 d0 03 fc C0
    C0 00 3f 00 db dc 04 7b 17 C0   (type 0x3f, longer)

then gives up: `CommCenter[13]: Baseband reset: AT response timeout`,
and the AT/transport cycle restarts. So echo reaches the right layer
(behaviour changed) but a verbatim bounce is not a valid reply.

Decoded structure:
- **SLIP framing** (RFC 1055): `0xC0` = END delimiter, `0xDB` = ESC;
  frame 4's `db dc` is ESC+ESC_END, i.e. a literal `0xC0` in the
  payload → unescaped frame 4 = `00 3f 00 c0 04 7b 17`. The stub's
  echo collector must SLIP-unescape on the way in and re-escape out
  (currently it doesn't — it round-trips raw, which is why the literal
  `0xC0` frame's checksum can't validate).
- Header `00 [LEN] 00 …`: `LEN` 0x2f vs 0x3f distinguishes message
  types/sizes; byte after header increments `01,02,03,04` = a sequence
  counter; trailing byte(s) a checksum (2f frames: seq 01→`7e`,
  02→`7d`, 03→`fc`; not a plain XOR/sum — needs the kext's algorithm).

This is `AppleReliableSerialLayer`'s wire format and is undocumented
(theiphonewiki has AT commands, not this MUX). The tractable next step
is to disassemble the kext's frame builder/validator (it maps at the
`0xc04bd…` PCs the traces already flag) to recover the checksum and the
expected ACK frame, rather than guess. A cheaper interim probe: reply
to each frame with a fixed `C0 00 2f 00 d0 <echo-seq> <cksum> C0`-shaped
ACK once the checksum is known, and watch whether the sequence advances
past 4.

**Disassembly target located** (needs the kernelcache): CORRECTION to an
earlier claim in commit c21c352aba — the `xtransportmode` string at FS
offset ~45.66M is **CommCenter** (`/System/Library/Frameworks/
CoreTelephony.framework/Support/CommCenter`, userland), which SENDS the
AT command; it is NOT the frame builder. The framing lives in the kernel
kext `AppleReliableSerialLayer`, prelinked into the ENCRYPTED
kernelcache. It has zero pointer xrefs to `xtransportmode` (that string
is CommCenter's), so grepping the root FS for it is a red herring.

### Run 12 — the frame builder decrypted, and the protocol IS H5 (BCSP three-wire UART)

`scripts/extract-kernelcache.py` turns the 8900 cache into a raw ARM
Mach-O: strip the 0x800 header, AES-128-CBC (key = the S5L8900 GID
`188458A6D15034DFE386F23B61D43774`, already in
`hw/arm/ipod_touch_8900_engine.h`; iv 0), then Apple LZSS
(`complzss`; canonical ring buffer `r = N-F`, an off-by-one there
corrupts every ~4th word). Out comes `MH_MAGIC` + 140 prelinked kexts;
`AppleReliableSerialLayer` __cstrings land at vm ~0xc048e3c0 and settle
it outright:

    "H5/%d-Enabling h5 (snooped at+xtransportmode)"
    "%s: AppleReliableSerialLayer recevied unexpected serial event ..."
    "H5/%d-> waitLineBreak, @%d called %dms"

So after `at+xtransportmode` the link switches to **H5 / BCSP
"Three-wire UART"** — a documented Bluetooth-family transport, not a
bespoke Apple format. No further disassembly of the checksum was needed:
the captured frames match the H5 spec byte-for-byte.

- SLIP framing (RFC 1055): `C0` delim, `DB` esc (`DB DC`=C0, `DB DD`=DB).
- 4-byte header: `b0`=SEQ(0-2) ACK(3-5) CRC-present(6) reliable(7);
  `b1`=type(0-3) len-low(4-7); `b2`=len-high; `b3`=`~(b0+b1+b2)&0xff`
  (the header checksum — validated on all four captured frames).
- type `0xf` = Link Control; payloads are the standard H5
  link-establishment magic: **SYNC `01 7e`, SYNC-RESP `02 7d`, CONFIG
  `03 fc`, CONFIG-RESP `04 7b`** (the exact BlueZ/hciattach constants).

Why run 11's echo failed: bouncing the guest's SYNC back looks like an
inbound SYNC, never a SYNC-RESP, so the link state machine never moved
(guest walked SYNC→SYNC→… then AT-timeout). The fix is a real H5
link-establishment responder: SYNC→SYNC-RESP, CONFIG→CONFIG-RESP, SLIP
unescape in / escape out. Implemented behind `IT_BASEBAND_H5=1` in
`hw/arm/ipod_touch_baseband.c` (replaces the throwaway
`IT_BASEBAND_FRAME_ECHO` probe). Run 12 result: pending — see below /
next entry.

Next once the link is up: CommCenter will exchange real H5 *reliable*
packets (seq/ack matter, `b0` bit7 set, and CRC-present frames need the
16-bit CCITT CRC in `b1`-type≠0xf packets) carrying HCI-like baseband
commands — SIM, registration, activation. That is the layer above link
establishment; disassemble `AppleReliableSerialLayer`'s packet handlers
(now that the kext is decrypted and mapped) for the command set.

---

## 2026-07-23 BREAKTHROUGH — UART Tx-interrupt storm fixed; M68AP now REACHES SPRINGBOARD

The configd/launchd freeze was a **UART Tx-interrupt storm** in the emulator.
`hw/char/exynos4210_uart.c`'s `update_irq()` re-asserted `UINTSP_TXD` on every
call while the Tx FIFO merely sat at/below the trigger level (i.e. continuously
when idle-empty). The S5L8900 driver (`AppleS5L8900XSerial`) acks TXD, update_irq
instantly re-raises it, and every UART the guest actually opens livelocks the CPU
in the driver's IRQ handler. The iPod dodges it (polled console only); the iPhone
opens the baseband (uart1) and bluetooth (uart3) lines and wedged.

How it was found (repeatable, no plugin): PC-sampling at the freeze
(`scripts/m68ap-freeze-probe.py`) showed CPU idle + `AppleS5L8900XSerial`
(`0xc04bd868`) + `AppleARMPL192VIC` only — symbolicated by mapping PCs to the
kernelcache's kmod_info. A QMP register dump confirmed uart0/1/3
`UINTP=UINTSP=0x4` (TXD), `UFSTAT=0`, TXD unmasked, VIC RAW `0x1b000000`.

Fix (committed): make TXD edge-triggered — assert only on a real `UTXH`
transmit, not continuously in `update_irq`. N45AP regression-checked (still
reaches `Configuring SpringBoard for N45AP` + framebuffer attach).

**Result: with the fix + `IT_M68AP_NO_BASEBAND=1`, M68AP boots to SpringBoard.**
`SpringBoard[15]` runs (`[Unactivated]`), `IOMobileFramebufferUserClient::attach`
+ `IOCoreSurfaceRootUserClient` attach — the display bring-up N45AP does. Guest
clock 20s->75s, 1611->1918 serial lines. Furthest the port has ever booted.

**Two remaining layers:**
1. **Baseband-on stall — NOT a fixable UART storm (2026-07-24 result, don't retry).**
   A register dump at the baseband-on stall did show a real spurious-RX-timeout
   defect (uart1 `UTRSTAT=0x7`/`UINTSP=0x5` with `UFSTAT=0`: Rx-ready + RXD
   pending on an EMPTY FIFO, because `UCON[11]` makes `timeout_int` re-assert
   RXD unconditionally). The symmetric fix — gate the Rx-timeout interrupt on
   `fifo_elements_number(&s->rx) > 0` in `exynos4210_uart_timeout_int` — was
   tried and **did NOT unblock the boot**: PC-sampling still showed the identical
   storm signature (`AppleS5L8900XSerial 0xc04bd868` + VIC) and the same
   `00:00:20` stall with `AppleBaseband` looping attach/detach ~22x. So the
   baseband-on interrupts are **real Tx/Rx traffic from AppleBaseband
   retry-looping against the incomplete S-Gold2 stub**, not the spurious-timeout
   bug. The Rx fix was reverted (correct-but-ineffective; re-derive from a
   register dump if ever revisited — do not re-add speculatively). The genuine
   blocker is the **AppleBaseband <-> S-Gold2 init handshake** (a deep
   baseband-emulation track), so the pragmatic path to SpringBoard stays
   `IT_M68AP_NO_BASEBAND=1`.
2. **SpringBoard display/activation stall** (baseband silenced) — after the
   framebuffer attach SpringBoard stalls (no LCD base flips, kernel FB black):
   `[Unactivated]`, telephony-less. `lockdown` logs `lookup_baseband_info: We now
   have SIM status` / `determine_activation_state: ... has not changed`, so
   activation queries the baseband — likely coupled to layer 1.

---

## 2026-07-23 MAJOR REFRAME — the black screen is display auto-sleep + scanout, NOT a SpringBoard failure

**The long-running "SpringBoard never reaches a visible frame" premise was wrong.**
Proven on the *working iPod* (N45AP) this session with new capture-independent tooling:

- Booted N45AP exactly like the shipping app (real-time, `-serial null`, no observer).
  On-screen output (`screendump`, which follows the LCD scanout) is **black** for 10+ min.
- But reading guest RAM directly (QMP `pmemsave`) shows the kernel framebuffers at
  **0x0f400000 / 0x0f496000 are 47% non-black and hold a complete, correct SpringBoard
  home screen** (status bar, Safari/YouTube/Calendar "Thursday 23"/Contacts/Clock/
  Calculator/Settings, Music/Videos/Photos/iTunes dock). Rendered PNG proof was captured.
- An LCD base trace (`IT_LCD_TRACE=1`, added to `hw/arm/ipod_touch_lcd.c`) shows the OS
  **page-flipping** `w1_framebuffer_base` across 0x0f400000→0x0f496000→0x0fe00000 (triple
  buffer, each ~4/6 visible), then **`Merlot panel entered sleep` + `PMU powered panel off`**
  — the OS auto-sleeps the display after idle (no input in a headless boot).

So the screen is black in captures because (a) the panel **auto-sleeps** after ~1 min of
no input (`panel_off=true` → `lcd_refresh` paints black), and (b) a one-shot `screendump`
catches a **mid-flip back-buffer**. A continuous display (real SDL/60 Hz) shows the front
buffer. **This is shared iPod+iPhone behavior — not the M68AP blocker.** Do not treat a
black `screendump` as "SpringBoard failed"; dump the FB bases from RAM instead.

**New repeatable tooling (committed):**
- `scripts/fb-snapshot.py` — boot a board, pause via QMP, dump 0x0fe00000/0x0f400000/
  0x0f496000 from RAM, measure non-black %, render each to PPM. `--samples/--sample-interval`
  for a live timeline; `--observer --stabilize-root-domain` for M68AP. QMP (not HMP —
  HMP-over-socket readline echo mangles rapid `pmemsave`).
- `scripts/boot-frame-probe.py` — periodic `screendump` darkness timeline; `--serial-null`
  / `--no-observer` to match the app. Real time = **omit** `-icount` (shift=-1 is invalid).
- `hw/arm/ipod_touch_lcd.c` gains `IT_LCD_TRACE=1` logging of every window-base program.

**What this means for M68AP:** the real gap is **not** "no visible frame". It is that
M68AP does not reach `Configuring SpringBoard for M68AP` — in lightweight runs its serial
freezes at the launchd hand-off (`BTServer: No bluetooth on this device`, ~line 1611) and
never prints the SpringBoard config marker that N45AP prints (~100 s in). The open question
is whether M68AP's SpringBoard renders to 0x0f400000 at all. Memory map: main RAM
0x08000000–0x10000000.

---

## 2026-07-23 SECOND REFRAME — the "power-management retain" is an OBSERVER ARTIFACT

The diagnostic `stabilize-root-domain` retain (the `IOPMrootDomain: attached at free()`
work-around) is **not needed for a plain boot**. It was a Heisenbug created by the
observer plugin's own instruction hooking.

Booting `-M iPhone-2G` with the real m68ap iBoot/NOR/NAND and **no plugin at all**
(`scripts/fb-snapshot.py --board m68ap` without `--observer`), across three runs:

- `attached at free`: **0** occurrences; a single kernel banner (no reboot);
- `IOIpodUSBDevice` starts normally (present ~10×) — it is *not* skipped;
- reaches `BSD root: disk0s1`, `/dev/disk0s2 on /private/var`, `launchd[1]: BOOT_TIME`,
  `configd`, `mDNSResponder`.

The observer runs, by contrast, apply `skip_usb_start` and hook the service-iteration
PCs; that instrumentation is what perturbs timing into the path that frees IOPMrootDomain.
**Do not reintroduce the retain.** A WIP in-emulator port (an `arm_debug_check_breakpoint`
hook + `cpu_breakpoint_insert` at the two service PCs) was built and then reverted: it was
both unnecessary *and* its BP_CPU breakpoints never fired for arm1176 (guest debug is off;
a machine-side PC hook needs a different mechanism than `cpu_breakpoint_insert`, e.g. a
translate-time hook — noted for future in-emulator tracing, but not needed here).

### The real remaining blocker (now free of instrumentation confounds)

Observer-free, M68AP boots **fast** to launchd + configd + mDNSResponder, then the serial
**freezes at ~line 1609** (`configd[16]: loading com.apple.SystemConfiguration.MobileWatchdog`)
with a black framebuffer and no SpringBoard exec marker, for the rest of an 8-minute run.
So the stall is **real and not observer-induced**, and appears to sit at the early launchd
service phase (configd) — possibly before SpringBoard even execs, not deep in dyld as the
older observer traces suggested. This is the divergence to chase next; the confound that
made "slow vs hung" undecidable (the observer overhead) is now gone. Note the ~12× TCG
slowdown: at the freeze the guest clock is only ~20 s in. Next step: determine whether the
guest is deadlocked or crawling (sample the guest PC over time via a QMP `human-monitor-command
info registers` — use a dedicated boot with the monitor free, since `fb-snapshot.py` holds
the single QMP connection), and whether launchd ever execs SpringBoard.

---

## ► NEXT-SESSION PROMPT (start here)

> **M68AP NOW MOUNTS BOTH IPHONE PARTITIONS AND STARTS LAUNCHD SERVICES.
> SPRINGBOARD HAS NOT YET BEEN CONFIRMED.**
>
> The breakthrough came from reproducing the working iPod Touch 1G path at the
> NAND-controller identification boundary. N45AP advertises eight valid NAND
> chips and derives `PAGES_PER_SUBLK 1024`. M68AP advertises four valid chips,
> four absent ID slots, and derives `PAGES_PER_SUBLK 512`. QEMU had advertised
> eight identical chips to both boards, so the M68AP kernel used the wrong
> logical-to-physical mapping. The earlier "one unknown `FTL_Open` context
> check" theory is superseded.
>
> With board-aware chip identification and a four-bank M68AP constructor, the
> real 4A102 kernel clean-opens FTL, reads the real extents header at
> `bank3/25858.page`, validates both HFS B-trees, returns zero from
> `_vfs_mountroot`, and prints `BSD root: disk0s1`. The corresponding N45AP
> regression still advertises eight chips and reaches SpringBoard.
>
> **The first post-root failure is now isolated to one IOKit object-lifetime
> difference.** The N45AP and M68AP USB drivers contain the same start routine
> and both find the same `usb-otg` service. During that lookup, however, the
> M68AP `IOPMrootDomain` candidate has references `0x00010002`; its N45AP
> counterpart has `0x00130016`. Iterator teardown drops M68AP to its last
> reference and triggers `IOPMrootDomain: attached at free()`.
>
> The apparent post-exec stop was a second fixed-eight assumption in QEMU:
> ADM multi-page command `0x200` assigned pages in groups of eight. M68AP's
> four-page loader request therefore returned zero/stale data. The staged HFS
> contained the correct dyld instruction at offset `0xd580`; runtime memory did
> not. Striping over the board's active bank count restores valid dyld
> execution.
>
> iPhone 1.1.4 then exposed a real layout difference: its `/etc/fstab` requires
> `disk0s1` as read-only root and `disk0s2` at `/private/var`, while the iPod
> seed uses one writable root. `build-m68ap-nand.py --data-hfs` now emits both
> GPT/HFS partitions. A bounded run discovers and mounts both, starts launchd
> services and mDNSResponder, and no longer reports the former libz failure or
> reboots.
>
> SpringBoard launch is now directly observed. Launchd requests the expected
> executable, the M68AP kernel returns zero from `execve`, and the assigned
> process does not enter the common exit path during the bounded run.
> Process-specific trap logging shows dyld loading the same dependency sequence
> as a clean N45AP oracle through event 280. Both guests then enter the same
> initializer phase and match through event 313, with successful kernel
> returns. Both then execute corresponding libSystem allocator and Mach-O
> section-scan paths for hundreds of thousands of observed blocks. N45AP does
> not request its next 64 KiB allocation until after at least one million such
> blocks. M68AP still has no visible frame. The direct root-domain reference
> edit remains diagnostic-only.
>
> Always stage NAND/NOR/iBoot and bound the boot with the acceptance harness.
> `_new.page` files are incomplete write captures and are not replayed, so
> restore/format work remains a separate future fidelity track.

**The former blocker (solved 2026-07-21, kept for the record):** after
`FTL_Init [OK]`, iBoot Data-Aborted inside `memmove` (`DFAR=0x18100000`,
count `r2=0xffe6121d`). Root cause: the constructor's full-page 0xFF
"production BBT" fill corrupted the `DEVICEINFOBBT` page's own length field.
The loader at `0x18015fa0` does `memcmp(page, "DEVICEINFOBBT", 0x10)` then
`memmove(dst, page+0x38, *(u32 *)(page+0x34))` — with the page 0xFF-filled
past the marker, the count was 0xFFFFFFFF and the copy ran off the iBoot RAM
window. Fix (landed): `build-m68ap-nand.py` writes count `0x200` at +0x34
(4096 blocks/bank ÷ 8) and 0xFF only across the 0x200-byte bitmap at +0x38,
zeros elsewhere — the same shape as the N45AP page (whose count is 0).

## Goal

Boot a real iPhone OS 1.x image to SpringBoard on `-M iPhone-2G`, reusing the
S5L8900 emulation that already boots the iPod Touch 1G (`-M iPod-Touch`).

## Current state (one line)

The M68AP board, boot chain, four-bank NAND, AppleNANDFTL clean-open, root and
data HFS partitions, dyld, and launchd service startup now work. A diagnostic
retain remains necessary for the under-retained M68AP `IOPMrootDomain`.
SpringBoard exec succeeds and its live process advances through dyld's
dependency graph and into initialization, but has not yet reached its
configuration marker or a visible frame. A 300-second low-overhead run reaches
the same state. The working-N45AP comparison now proves that both guests pass
initializer event 313 and enter corresponding long-running libSystem allocator
and Mach-O section scans. The first semantic divergence has not yet been
captured.

## 2026-07-23 launchd breakthrough — active-bank ADM reads and disk0s2

- The successful kernel exec was not an idle launchd wait. A paired
  `trace-user-blocks` run showed both boards reach dyld `0x2fe0d580`; N45AP
  executes `0xe92d40f0`, while M68AP received `0x00000000` and walked through
  page-sized blank regions. The mounted M68AP HFS file contains the correct
  `0xe92d40f0`, proving construction and catalog lookup were sound.
- ADM command `0x200` used `num_pages / 8` and banks `0..7` unconditionally.
  A four-page M68AP request performed zero assignments. It now stripes every
  requested page across `nand_state->num_banks`; the eight-bank N45AP behavior
  is unchanged. After rebuilding, M68AP executes hundreds of valid dyld blocks
  and reaches launchctl.
- The next failure explicitly named `/dev/disk0s2`. The IPSW-derived iPhone
  `/etc/fstab` requires `disk0s1 / hfs ro` and
  `disk0s2 /private/var hfs rw,noexec,nodev`; N45AP requires only a writable
  root. The prior one-partition constructor could not satisfy that.
- `build-m68ap-nand.py --data-hfs` now writes a second Apple-HFS GPT entry and
  four-bank-interleaved data image. Structural fixtures verify both entries and
  the first data page while retaining all recorded N45AP metadata hashes.
- Evidence:
  `/private/tmp/m68ap-four-bank-adm-fix-20260723/` proves corrected dyld
  execution; `/private/tmp/m68ap-two-partition-long-20260723/result.json`
  records both mounts and `launchd_started: true`. Serial includes
  `/dev/disk0s2 on /private/var`, launchd service messages, Wi-Fi completion,
  and mDNSResponder startup. A 90-second bounded framebuffer capture is still
  fully black, so a visible SpringBoard frame is not being mistaken for a
  missing serial marker. Generated firmware and images remain uncommitted.

## 2026-07-23 SpringBoard execution boundary

- The reusable observer now recognizes kernel `_execve` requests and correlates
  concurrent calls by kernel stack. SpringBoard's request is
  `/System/Library/CoreServices/SpringBoard.app/SpringBoard`; its correctly
  paired kernel return is `error=0`.
- Correlation at stable post-prologue instructions assigns SpringBoard process
  `0xc0c2f244` in the recorded deterministic run. The common `_exit1` path is
  also process-aware; SpringBoard does not traverse it during the bounded run.
  This rules out an immediate launch or loader termination.
- The S5L8900 software-interrupt observer resolves the current process from the
  per-CPU state and records only SpringBoard's traps. Its program counter stays
  in dyld while it opens and maps a non-repeating dependency list: UIKit,
  CoreGraphics, Foundation, GraphicsServices, LayerKit, the telephony
  frameworks, WebKit, audio, MBX, OpenGLES, and CoreVideo.
- A staged clean N45AP oracle built from the verified `nand.pack` reaches
  SpringBoard and provides the decisive paired boundary. Both guests have
  identical dependency semantics through event 280, then enter corresponding
  initializer code. M68AP continues through event 313 and every observed
  kernel return is zero.
- The post-event-313 observer corrected the former boundary. Both builds return
  through their generic `syscall` wrappers into corresponding `munmap`,
  `allocate_pages`, small/large malloc, `malloc_zone_calloc`, string, copy, and
  Mach-O section-scan paths. The iPod uses Snowbird 3A101a libSystem while the
  iPhone uses LittleBear 4A102 libSystem, so their virtual addresses differ but
  symbol-level flow agrees. N45AP executes at least one million observed
  libSystem blocks before event 314 requests 64 KiB. M68AP was observed through
  at least 810,000 such blocks without a next SpringBoard trap before the
  latest probe was interrupted. This count is a progress indicator, not a
  strict per-process metric: a user-mode block callback cannot identify the
  current process after a scheduler switch.
- Therefore the current gap is after a successful event-313 return inside
  initialization. Do not revisit FTL restore, root construction, launchd
  policy, executable lookup, or dependency loading unless their combined
  acceptance gates regress. Extend the observer with scheduler-aware process
  tracking, retain only SpringBoard's post-313 blocks, and compare symbol-level
  flow with N45AP until their first divergence. Do not interpret a block-count
  cap as a guest stop.
- Evidence:
  `/private/tmp/m68ap-kernel-exec-threaded-20260723/`,
  `/private/tmp/m68ap-process-exit-v2-20260723/`,
  `/private/tmp/m68ap-springboard-objects-20260723/`,
  `/private/tmp/n45ap-clean-springboard-traps-v2-20260723/`, and
  `/private/tmp/m68ap-springboard-close-return-20260723/`. The 300-second
  low-overhead result was
  `/private/tmp/m68ap-long-low-overhead-20260723/result.json`. These transient
  local artifacts are not committed and may be reclaimed; reproduce them with
  the scripted command below.

## 2026-07-23 post-event-313 handoff — current continuation point

The former statement that M68AP stopped after event 313 is superseded. It was
an observer-limit artifact. The paired result now establishes:

1. N45AP and M68AP have the same dependency semantics through event 280.
2. Both enter corresponding initializer code and complete events 281–313.
3. Event 313 returns zero to each build's generic `syscall` wrapper.
4. Both then execute corresponding libSystem allocation and Mach-O inspection
   paths. The first 64 observed blocks resolve in the same semantic order even
   though their addresses differ because N45AP uses Snowbird 3A101a libSystem
   and M68AP uses LittleBear 4A102 libSystem.
5. N45AP executes at least 1,000,000 observed libSystem blocks before event 314
   requests a 64 KiB mapping and then reaches SpringBoard. M68AP reached at
   least 810,000 observed libSystem blocks without a next SpringBoard trap in
   the interrupted comparison run. This proves forward progress beyond event
   313, but does not yet locate the first divergence.

The block counts are deliberately qualified. The observer arms on a
process-identified SpringBoard kernel return, but a user-mode block callback
cannot identify the current process after scheduling. The first uninterrupted
path is reliable; a long aggregate count can include another process. The next
observer must track scheduler process changes and filter the block stream
before using counts as a parity measure.

Do not use the current app-bundle defaults for a reproduction without checking
their hashes. Two stale inputs consumed otherwise-valid bounded runs:

- NOR SHA-256 `7bf1668e...` fails device-tree loading. Use the reconstructed
  NOR SHA-256
  `89716fae81ac2817ddefe79a81e8ed3df878a5a5e4a5fd66c2563293e1726362`.
- Extracted iBoot SHA-256
  `17bb2b762e32b7230334fd453bd9ffe408b514f497b3df26911f5023ca6d5f22`
  still enforces the image policy. Stage the output of
  `scripts/patch-m68ap-iboot.py`; the verified patched SHA-256 is
  `ab9d4136ed12f96d5d20e147932b523f93f2c653f880d72b8d9e4456728a4ff4`.
- Use a verified two-partition M68AP NAND pack, not the app bundle's older
  sparse diagnostic pages. The acceptance harness always copies it to a
  writable stage.

Reproduce the focused M68AP case with explicit inputs:

```sh
python3 scripts/iphone-nand-acceptance.py \
  --skip-n45ap \
  --iboot-m68ap <patched-m68ap-iboot> \
  --nor-m68ap <reconstructed-m68ap-nor> \
  --nand-m68ap <verified-two-partition-m68ap-nand-directory> \
  --timeout 220 \
  --icount-shift -1 \
  --observer-plugin build-ipod11/contrib/plugins/libm68ap-ftl-trace.dylib \
  --observer-args 'profile={profile},service-observer-only=true,stabilize-root-domain=true,trace-execve=true,trace-details=false,trace-springboard-resume-after=313,log={observer_log}' \
  --logs <new-output-directory>
```

Run the N45AP oracle with the same plugin arguments, `--skip-m68ap`, and
`--nand-n45ap <clean-pack-only-n45ap-directory>`, changing
`stabilize-root-domain` to `false`. Never run from installed loose N45AP pages
when a clean pack-only oracle is available.

The exact continuation is:

1. Locate a stable scheduler/context-switch point in each 1.1.4 kernel profile
   and maintain the current process in the observer.
2. Record only SpringBoard's post-313 blocks, summarized by symbol and edge
   counts rather than one log line per execution.
3. Resolve M68AP addresses against LittleBear 4A102 `libSystem.B.dylib` and
   N45AP addresses against Snowbird 3A101a `libSystem.B.dylib`; compare symbols
   and control-flow edges, not raw virtual addresses.
4. Stop at the first process-specific divergence or SpringBoard's next trap.
   If M68AP reaches the N45AP event-314 allocation, advance the same automated
   comparison to the next mismatch.

Storage construction, AppleNANDFTL clean-open, both HFS mounts, launchd policy,
SpringBoard lookup, SpringBoard exec, dependency loading, and the event-313
kernel return are closed gates. Do not reopen them unless the acceptance
markers regress. The diagnostic root-domain stabilization remains the separate
production-quality issue.

## 2026-07-22 automated pre-FTL isolation — superseded by board-count result

- `scripts/m68ap-ftl-trace.py` rebuilds a staged current NOR and scratch iBoot,
  verifies a full-root NAND provenance manifest and metadata hash, bounds QEMU,
  and emits `result.json`, serial, plugin, and preparation logs. It exits in
  about 19 seconds on the current stop; repeated interaction is not required.
- The current rebuilt NOR SHA-256 is
  `89716fae81ac2817ddefe79a81e8ed3df878a5a5e4a5fd66c2563293e1726362`.
  The older `7bf1668e...` NOR was stale and failed device-tree loading; do not
  use it as a kernel-storage reference.
- A fresh 266 MB HFS+ root image generated a 136,532-page NAND. Packing it into
  a 288,901,732-byte `nand.pack` changed the stop time from about 21 to 19
  seconds but not the result, ruling out sparse page-file lookup overhead.
- The captured assertion is `IOPMrootDomain: attached at free()`. The decoded
  return chain reaches `AppleARMFunction::withProvider`, called first for the
  M68 SDIO `function-device_reset`. Its encoded parent is
  `IOFunctionParent004040E0`, which correctly resolves to
  `/device-tree/arm-io/gpio`; the GPIO driver logs a successful start.
- A targeted registration trace proves GPIO startup calls
  `AppleARMFunction::registerFunctionParent`, constructs
  `IOFunctionParent004040E0`, and invokes `setProperty` on the GPIO service
  before SDIO waits for it. Publication is therefore complete; the remaining
  boundary is `waitForService` matching/retention and candidate enumeration.
- Declining SDIO start moves the identical wait to AppleBaseband's
  `function-bb_rst`. Making platform-function lookup globally unavailable
  causes an earlier S5L driver stop. These were scoped diagnostic experiments,
  not candidate fixes.
- `scripts/inspect-apple-device-tree.py` accepts a raw tree, IMG2 container, or
  full NOR and emits all `function-*` properties with resolved parent nodes.
  Its M68/N45 comparison shows all references resolve; M68 adds telephone and
  other board-specific GPIO functions, including SDIO `function-device_reset`,
  while N45 SDIO uses `function-power_enable`.
- These observations remain useful for the current post-root service-ordering
  comparison, but the conclusion that the run stopped before FTL was caused by
  the former eight-bank M68AP model. The four-bank result below supersedes that
  storage boundary.

## Reference — key addresses, reusable artifacts, gotchas (consolidated 2026-07-21)

**Key iBoot-204.3.14 (M68AP) addresses (VA base 0x18000000; file offset = VA − 0x18000000):**

| What | VA |
|---|---|
| `load_macho_image` (kernelcache loader) | `0x1800d544` |
| `dt_load` (device-tree loader) | `0x1800d060`; called `0x1800e07a`; post-load result `0x1800d0a6` |
| `image_find_by_type('dtre'=0x64747265)` | `0x18008376`→`0x180083c0`; image-list head `0x180211a8` |
| `image_load` → worker | `0x18008340` → `0x180088cc` |
| IMG2 validator | `0x18008478` — magic@`0x18008498`, hdr-CRC@`0x180084a4` (CRC fn `0x18007780`), flags2 bit24@`0x180084aa`, epoch@`0x18008520`, `+0x3e0` sig memcmp@`0x180084f8` |
| worker signed-path `+0x20` memcmp | `0x18008a0a` |
| secure-boot decider (allow-unsigned) | `0x18005984`; config word `0x18022fa0` (seeded `0x2c0000` @ `security_init 0x18005a28`); CHIPID read `0x180018e4` → HW reg `0x3e500004` |
| **secure-boot bypass patch** | file `0x5990`: `00 20`→`01 20` |
| Signature crypto | `SHA1` fn `0x18003284` (HW SHA1 engine `0x38000000`); `AES` op `0x18001790` (HW AES `0x38c00000`); key const `0x18020200`, IV `0x18020210`, extra const `0x180201d4` |
| VFL_Open / `_LoadVFLCxt` | `0x18016194`; block-read helper `0x18016120`; **BBT-bitmap skip** `tst/beq` `0x18016220`/`0x18016222` (file `0x16222`); "fail bank" print `0x18016344` (line 768) |
| WMR_Init / DEVICEINFOBBT loader | `0x180164a0` / `0x18015fa0` |
| UART: `uart_write` CTS spin / UART1 UMSTAT | `0x18003c9e` / MMIO `0x3cc0401c` |
| Emulator MMIO bases | SHA1 `0x38000000`, AES `0x38c00000`, CHIPID `0x3e500000`, UART1 `0x3cc04000` |

**Key 4A102 kernelcache AppleNANDFTL addresses** (decompressed Mach-O VA; ARM
mode, not Thumb; reproduce with `scripts/analyze-m68ap-ftl-open.py`):

| What | VA |
|---|---|
| WMR loaded call to `FTL_Open` | load target `0xc046df94`; call `0xc046df98` |
| Whimory `FTL_Open` | `0xc047302c` |
| Context version loads | `+0x7f8` at `0xc0473518`; `+0x7fc` at `0xc0473520` |
| `_LoadFTLCxt` failure log | `0xc04737f8` |
| `_FTLRestore` fallback | call `0xc0473814`; target `0xc0471ab8` |

The version literals are `0x46560000` and `0xb9a9ffff`, exactly matching the
generator and installed N45AP metadata page at `bank7/25855.page`. The page at
`bank0/25728.page` is only the context-index spare marker and its 2 KiB data
area is zero; it must not be passed to the metadata inspector. The real metadata
page has SHA-256 `4877ba691c75b2134949c3e5a048700dc627d04ba0d65be842382c45519b7e3c`.
Their match rules out the simplest
"M68AP wants a different `dwVersion`" theory; a runtime trace must identify
whether context selection, the copied page, or a mapping/EC/log-table read fails.

**IMG2 signature scheme (for signing NOR images faithfully instead of patching):**
`+0x3e0` (0x20 B) = `AES(key@0x18020200, SHA1(header[0:0x3e0]))`; `+0x20` (0x40 B) =
payload hash; flags2 `+0x1c` bit 1 = "signed"; `+0x64` = CRC32(header[0:0x64]).
Signing order (per image): set flags2 (bit24+bit1) + fix `+0x64`; lldb-capture the
worker's expected `+0x20` at `0x18008a0a`; write it + fix `+0x64`; lldb-capture the
validator's expected `+0x3e0` at `0x180084f8` (now over the final header); write it.
**Captured dtre values (for THIS dtre content only — recapture if it changes):**
`+0x1c=0x01000002`; `+0x20 = 9607d927 bd39a9bf cc74064e 39b62ad2 788b3a73 51976319
2ad530e1 61ed05b2 48e4e156 965ab1b1 f2ca47ff b9b71b98 8e5dda68 3cac2ce4 540c8d0a
b0002dd1`; `+0x3e0 = ed93ebc3 c2335655 354a436b 128b925f d388d35d 345e8cd8 1dbde5a1
187d0e5a`. Proven: an UNPATCHED iBoot accepts the signed dtre (logs `image … type
dtre`). Blocked from completing only by the timer-consistency bug (`0x180034bc`).

**VFDecrypt (root FS) key split (in `scripts/decrypt-m68ap-rootfs.sh`):** the 72-hex
key = `aes_key(bytes 0:16) || hmacsha1_key(bytes 16:36)`; v2 encrcdsa header:
blocksize@+0x34, datasize(u64)@+0x38, dataoffset(u64)@+0x40 (BE). Per-block IV =
HMAC-SHA1(hmac_key, blockno_BE)[0:16]; AES-128-CBC per `blocksize` chunk from
`dataoffset`. Key for `022-3894-4.dmg`:
`d0a0c0977bd4b6350b256d6650ec9eca419b6f961f593e74b7e5b93e010b698ca6cca1fe`.

**Session-temp artifacts (not committed; regenerate, do not assume they remain):**
`filesystem-m68ap-readonly.img` (real 266 MB root FS), a full NAND generated
from it, `iboot_204_m68ap_sbpatch.bin`, and any diagnostic VFL patch. The
2026-07-22 audit found that the surviving `m68ap-artifacts/stage-full/nand`
directory is stale (old corrupt BBT plus an invalid kernelcache payload), so it
is a negative fixture, not the kernel-FTL oracle. `scripts/analyze-m68ap-ftl-open.py`
does not require a saved decrypted Mach-O; it decrypts/decompresses in memory.

**Process gotchas / dead ends (don't repeat):**
- **Disk:** each full NAND is ~288 MB (136 K page files); the scratchpad fills the
  volume and then EVERY command ENOSPC-fails (even the harness output file). Delete
  old NAND trees / boot-log dirs between runs. `rm` of a 136 K-file tree takes >120 s.
- **Debugger:** only `lldb` is present (no `gdb`); it drives QEMU's gdbstub via
  `gdb-remote` with `breakpoint set --hardware`. Always wrap in a host
  `( sleep N; pkill -9 lldb qemu-system-arm )` watchdog. Break too EARLY (before an
  operand is set) and registers read wrong (e.g. broke at `0x18008a02` instead of the
  memcmp call `0x18008a0a`).
- **Run kernel boots in REAL TIME** (`--icount-shift -1`); under icount the kernel
  lacks wall-clock and parks in iBoot's UART loop before the banner.
- **NAND is write-ONLY** (`_new.page` never read back) — so no `_FTLRestore`/format
  can persist; only a clean `FTL_Open` boots. See the reframe below.
- **CHIPID/dev-mode is a dead end** for unsigned images (config bit 4 never set).
- **The BBT is NOT the kernel-FTL cause** (isolation test proved it).
- **`_FTLRestore`/DFU restore are dead ends** until a real write/erase/persistence
  model exists.

## Session log — 2026-07-22 (FTL_Open statically located; version pair ruled out)

Converted the kernelcache investigation into a reproducible, fixture-free tool:

- `scripts/analyze-m68ap-ftl-open.py` parses the `89001.0` container, decrypts it
  with the existing S5L8900 GID key through OpenSSL, decompresses `complzss`,
  verifies Adler-32, parses the Mach-O segments, and locates the stripped
  AppleNANDFTL call chain from its strings/literal references. It emits JSON and
  writes no decrypted firmware unless `--macho-out` is explicitly requested.
- For the 1.1.4/4A102 artifact, compressed size `0x332059` expands to `0x5bc520`;
  Adler-32 is `0xd363ef9b`, and the Mach-O SHA-256 is
  `82e538d71c8d867566f26678f08e8ecf27b74eab8e6a9e2ec30608ebbbcf2b5f`.
- `FTL_Open` is ARM code at `0xc047302c`, called by WMR at `0xc046df98`.
  Its failure sink logs `_LoadFTLCxt` at `0xc04737f8`, then calls
  `_FTLRestore` at `0xc0473814` (target `0xc0471ab8`).
- The code loads context offsets `0x7f8/0x7fc` and compares them with
  `0x46560000/0xb9a9ffff`. The generator, installed N45AP base, and staged M68AP
  metadata page (`bank7/25855.page`) all contain exactly that pair and share full-page SHA-256
  `4877ba691c75b2134949c3e5a048700dc627d04ba0d65be842382c45519b7e3c`.
  Therefore do not change `dwVersion`; the failure is elsewhere or the wrong
  bytes reach the check at runtime.
- A bounded acceptance rerun caught an artifact trap rather than a new emulator
  bug: the surviving full scratch NAND still had the superseded BBT count
  `0xffffffff` and Data-Aborted in iBoot. On a staged clone, replacing only the
  eight BBT pages with the validated `count=0x200` pages restored iBoot
  `VFL_Open [OK]` / `FTL_Open [OK]`, after which the stale payload reported
  `Kernelcache image not valid`. Regenerate the full M68AP root NAND before the
  kernel trace; do not mutate or promote this scratch tree.

## Session log — 2026-07-22 (iPod comparison clears M68AP root mount)

The working N45AP boot was observed at the same controller and filesystem
boundaries as M68AP. That direct comparison found the board distinction that
the earlier traces had missed:

- N45AP returns eight valid NAND IDs, reports `BANKS_TOTAL 8`, derives
  `PAGES_PER_SUBLK 1024`, and performs the expected eight-page interleaved read.
- M68AP returns four valid IDs followed by four absent (`0xffffffff`) slots,
  reports `BANKS_TOTAL 4`, and derives `PAGES_PER_SUBLK 512`.
- The QEMU NAND model previously returned a valid identical ID for every slot
  on both boards. Restricting later data reads to four banks did not fix the
  identification handshake, which is why the earlier four-bank experiment was
  a false negative.

The production correction adds a board-aware active-bank count to the shared
NAND controller, makes both direct and ADM identification report absent M68AP
slots explicitly, and makes `build-m68ap-nand.py` default to four active banks
for M68AP while retaining eight for N45AP. The M68AP metadata is emitted in its
four-bank location; the speculative bank-7 mirror is removed.

A bounded M68AP run with that layout proves the complete storage chain:

- four valid and four absent NAND IDs;
- iBoot kernel load and kernel FTL clean-open;
- the real HFS extents header delivered at `bank3/25858.page`;
- extents and catalog B-tree validation;
- `_vfs_mountroot` return 0 and HFS mount result 0; and
- `BSD root: disk0s1`.

Machine-readable evidence is in
`/private/tmp/m68ap-four-bank-identify-test-20260722/result.json`. A second run
from a freshly generated current-default four-bank tree reproduced the result
as `ROOT_MOUNTED` in
`/private/tmp/m68ap-four-bank-current-root-20260722/result.json`; this run used
the explicitly reported diagnostic root-domain retention to make the existing
startup-order race deterministic, but no guest storage bytes were patched. The paired
N45AP regression is
`/private/tmp/n45ap-four-bank-board-regression-20260722/result.json`; it keeps
eight valid IDs, reaches the Darwin kernel and `BSD root`, logs launchd's
`BOOT_TIME` marker, and prints `Configuring SpringBoard for N45AP`. Neither
result directory is a repository artifact.

This materially closes the iPhone/iPod storage gap. It does not yet establish
SpringBoard parity. Detailed M68AP observation proceeds well beyond root mount,
while lightweight runs expose a timing/order-sensitive legacy IOKit failure at
USB, then SDIO, then baseband when those consumers request GPIO-backed platform
functions. The next reusable test should compare N45AP and M68AP service
publication/consumer order in one run and emit ordered events to JSON.

That comparison harness now exists as `scripts/compare-s5l8900-startup.py`.
Against the paired logs above it reports 116 shared service-start events. N45AP
reaches root, launchd, and SpringBoard without a panic. The 75-second detailed
M68AP run reaches root without a panic but not launchd before its bounded stop;
it has the diagnostic USB-start decline and root-domain retention explicitly
recorded. The corresponding lightweight M68AP run reaches
`IOIpodUSBDevice::start` and panics at that exact point, while N45AP starts and
registers the same service and continues through storage to SpringBoard.

The DeviceTree comparison also narrows the difference. Both boards register the
charger before `IOIpodUSBDevice` and both charger USB functions ultimately use
the GPIO parent. N45AP's SDIO uses GPIO `function-power_enable`; M68AP replaces
that with GPIO `function-device_reset` and adds five baseband GPIO functions.
All descriptors resolve structurally. Therefore the next target is the first
M68AP `IOIpodUSBDevice::start` platform-function lookup and its service
ownership/order, compared with the successful N45AP call—not another broad
platform-function bypass.

Static inspection and a bounded unadjusted M68AP trace now identify that call
exactly. `IOIpodUSBDevice::start` is at `0xc04cb198`. It builds a service match
for the literal `usb-otg`, calls `waitForService`, and, if that returns, asks
`AppleARMFunction` for the literal `function-usb_500_100`. The unadjusted trace
stops inside `waitForService` before the function constructor executes, with
`IOPMrootDomain: attached at free()`. The GPIO driver had already published
`IOFunctionParent004040E0`; no NAND read or HFS operation is involved. Evidence
is `/private/tmp/m68ap-usb-start-trace-20260722/result.json`.

That paired observation is now complete. `scripts/analyze-s5l8900-usb-start.py`
finds both stripped routines directly from their strings and loaded calls. The
driver bodies have the same instruction layout: N45AP starts at `0xc04c10c4`,
waits at `0xc0134fba`, and enumerates at `0xc0134b1c`; M68AP uses
`0xc04cb198`, `0xc01351de`, and `0xc0134d40`, respectively. The observer's
board profiles record the same semantic candidate sequence without assuming
shared virtual addresses.

Both boards match the final `usb-otg` candidate with references `0x00060008`.
The decisive earlier candidate is `IOPMrootDomain`: M68AP presents it with
`0x00010002`, while N45AP presents its corresponding third candidate with
`0x00130016`. M68AP iterator teardown then reaches `0x00010001` and asserts
because the object is still attached. A diagnostic-only adjustment to
`0x00020002` lets teardown return `0x00010001`; USB registers normally and the
same run continues through SDIO, baseband, Wi-Fi, and `BSD root: disk0s1`.

The longer bounded run also closed that uncertainty. Instrumented `bsd_init`
reaches every post-mount checkpoint and returns. The stripped launchd loader at
`0xc00f7000` selects `/sbin/launchd`, calls exec at `0xc00f7078`, and receives
result zero at `0xc00f707c`. Subsequent process-aware observation (documented
above) proves launchd starts its service set and successfully execs
SpringBoard; the active boundary is now inside SpringBoard's initializer phase.

Evidence from the current runs is
`/private/tmp/n45ap-service-sequence-20260723.log`,
`/private/tmp/m68ap-service-sequence-four-bank-kernel-20260723.log`, and
`/private/tmp/m68ap-init-exec-progress-20260723.log`. These are local evidence,
not repository artifacts.

Machine-readable comparison output is
`/private/tmp/s5l8900-startup-comparison-20260722.json`; the DeviceTree output is
`/private/tmp/s5l8900-device-tree-comparison-20260722.json`.

## Session log — 2026-07-22 (earlier kernel FTL placement result — superseded)

> Historical diagnostic record only. Mirroring the context into bank 3 allowed
> one intermediate clean-open, but it did not model the board correctly. The
> four-bank controller-identification result above removes that mirror and also
> clears the later zero-buffer/HFS failure described in this section.

The scripted runtime trace identified and corrected the first failing storage
edge:

- `scripts/m68ap-ftl-trace.py` records context scan/read verdicts and physical
  NAND context-page transitions. `scripts/m68ap-ftl-batch.py` retries bounded
  boots automatically and stops at the first FTL verdict.
- The 4A102 kernel accepts the context-index marker, then reads context offset
  `0x1ff`. Its VFL maps that offset to `bank3/25855.page`; the historical iBoot
  path reads the byte-identical context at `bank7/25855.page`.
- Before the correction, `bank3/25855.page` was absent, so the kernel observed
  spare type `0x00` and failed before its version check. This disproves the
  earlier broad “different context validation” description: the failure was a
  specific physical placement difference.
- `build-m68ap-nand.py` now emits the same metadata page at both locations for
  M68AP only. The N45AP constructor output is unchanged. A bounded boot then
  observed spare type `0x43`, version `0x46560000`, complement `0xb9a9ffff`, and
  `FTL_OPEN_SUCCESS` on the first attempt. iBoot also continued to clean-open.
- A longer run created `IOFlashBlockDevice`, registered `disk0s1`, recognized
  the GUID partition, and repeatedly reported `BSD root: disk0s1`. It has not
  mounted HFSX or started launchd/SpringBoard yet.

The active boundary is now the HFS extents B-tree after successful disk
discovery. Follow-up scripted traces established all of these points:

- `_bsd_init` calls `_vfs_mountroot` at `0xc01a3ec2`; at `0xc01a3ec8` the
  nonzero return branches directly back to root selection.
- The selected root is consistently `disk0s1` (`rootdev=0x0e000001`) and
  `_bdevvp` has produced a non-null root vnode. The outer return is 19 only
  because `_vfs_mountroot` discards the individual handler error and exhausts
  the handler list.
- The `hfs` mount-root callback is present at `0xc00da1d1`. Its primary
  volume-header buffer read succeeds. Physical NAND tracing observes the
  expected `HX`, version 5 word (`0x05005848`) at bank 3/page 25856 offset
  `0x400`.
- The deeper HFS mount returns error 5 (`EIO`). A unique-block trace of its
  first attempt reaches `BTOpenPath` at `0xc00e3af4`, then
  `_MacToVFSError`, and returns immediately. Thus the first failing filesystem
  phase is opening the extents B-tree, not finding the disk or accepting the
  primary volume header.
- The staged image's volume header declares a 4096-byte allocation block and
  an extents file of 2,387,968 bytes / 583 blocks in one extent beginning at
  allocation block 4. The generated physical bytes begin at bank 3/page 25857,
  but the page-25857 observation in the first sparse trace occurred during
  iBoot, before the kernel trace markers. The kernel makes no extents data-page
  NAND request during the failing mount, so the earlier instruction to verify
  a direct kernel page-25857 transfer was too early.
- Targeted `BTOpenPath` tracing proves its block-size setup and
  `GetBTreeBlock`/`buf_meta_bread` call return 0. The subsequent
  `_VerifyHeader` at `0xc00e5490` returns `-32730`, the kernel's
  `fsBTInvalidHeaderErr`; its executed basic blocks isolate the first failed
  check to `nodeSize`. The complete 38-byte in-memory `BTHeaderRec` is zero,
  including `nodeSize=0`.
- This is not a buffer-cache shortcut. The first read executes
  `_hfs_vnop_strategy`, maps logical block 0 to device block 8, dispatches
  through the `disk0s1` vnode (`dev=0x0e000001`), and reaches the dynamically
  registered storage strategy at `0xc045c7bd`. That prelinked wrapper enters
  `0xc045c4d0`, reports successful completion, but leaves the buffer zero and
  never reaches the NAND data-page request path.
- The next deterministic step is to trace the executed path within
  `0xc045c4d0` and its provider-vtable call, identifying where block 8 stops
  before AppleNANDFTL. Do not alter the HFS seed or bypass `VerifyHeader` unless
  that trace demonstrates a constructor error. Once the real extents header
  reaches the buffer, continue to catalog opening, mount success, launchd, and
  SpringBoard, followed by an N45AP regression run.

Evidence is machine-readable in
`/private/tmp/m68ap-hfs-stage-trace/result.json` and
`/private/tmp/m68ap-hfs-mountfs-blocks/result.json`, with the narrowed checks in
`/private/tmp/m68ap-verify-header-path-20260722/result.json`,
`/private/tmp/m68ap-bt-buffer-path-20260722/result.json`, and
`/private/tmp/m68ap-bt-storage-strategy-20260722/result.json`. The last run uses
the harness's `--stop-at-verify-failure` gate and completes in about 19 seconds
instead of consuming the full timeout. These are staged local results, not
repository artifacts. The root-domain reference adjustment used to make these
runs deterministic is opt-in and diagnostic only; no firmware or NAND file was
modified by it.

## Session log — 2026-07-21 (generator faithful; BBT isolated; kernel clean-open wall)

Corrected the earlier "needs a formatted NAND" theory using the repo's own docs
and the real reference generator. New, firmer conclusions:

- **BUILD.md is explicit: `*_new.page` files are "incomplete write captures, not
  a replayable NAND overlay"; writable persistence "remains planned work."** So
  the working N45AP NAND is the BASE seed (`nand_n45ap.zip`) that boots with a
  CLEAN `FTL_Open` — NOT a kernel-formatted state. The blog part-1 "no
  persistence" note is consistent: the generator seed is meant to boot directly.
- **Our `build-m68ap-nand.py` port is FAITHFUL.** Compiled the real
  `generate_nand.c` (it1g, FIL_ID→M68AP) and diffed its 136 532-page output
  against ours: only 10 pages differ — 8 BBT pages (expected: our production
  0xFF BBT vs the reference zero BBT) and 2 GPT pages (minor field diffs). The
  FTL context, VFL context, mapping, and all data/spare pages are byte-identical.
  (The data-block-spare bug was the last real port divergence and is now fixed.)
- **The real remaining conflict is the BBT, and it is M68AP-specific:**
  - Booting the reference generator's ZERO-BBT output on M68AP fails at iBoot:
    `_LoadVFLCxt(line:768) fail bank 0` → `VFL_Open failed`. So M68AP
    iBoot-204.3.14's Whimory GENUINELY needs the production (0xFF) BBT — its VFL
    is stricter than N45AP iBoot's, which accepts the zero BBT.
  - But WITH the production BBT, iBoot passes and the KERNEL's `AppleNANDFTL`
    `FTL_Open` rejects the (reference-identical) FTL context → `_FTLRestore` →
    `_ScanForFreeBlk` fails. N45AP's kernel does a clean `FTL_Open` on the same
    context shape. So the M68AP kernelcache's `AppleNANDFTL` is also stricter
    than N45AP's, OR the production BBT it required for iBoot is what its own
    FTL_Open then rejects.

**BBT ISOLATION TEST DONE — the BBT is RULED OUT as the kernel-FTL cause.**
Patched M68AP iBoot's `_LoadVFLCxt` bitmap check to accept a zero BBT: the scan
at `0x18016214` only reads a block if its bit is set in the BBT-derived
searchable bitmap (`tst r2,r3; beq skip` at `0x18016220`/`0x18016222`); NOP-ing
the `beq` (VA `0x18016222`, file `0x16222`: `08 d0` → `00 bf`) makes it scan
every block and find the VFL context on block 35 regardless of the BBT. Result
with the pure ZERO-BBT reference seed + that patch:
- iBoot `VFL_Open [OK]` (no "fail bank") — the patch works, the zero-BBT seed
  reaches the kernel.
- **The kernel FTL still fails IDENTICALLY:** `_ScanForFreeBlk(0xF35) failed`,
  `wDataBlkCnt=0xF20 wFreeBlkCnt=0x15`, `Still waiting for root device` — exactly
  the same as with the production BBT.

**Conclusion: the production BBT is needed only for M68AP iBoot; it does NOT
affect the kernel FTL.** The kernel wall is entirely in the M68AP kernelcache's
`AppleNANDFTL`: its `FTL_Open` rejects the (byte-identical to N45AP) FTL context
and falls into `_FTLRestore`, where the N45AP kernel does a CLEAN `FTL_Open`. So
the difference is the KERNEL DRIVER, not the NAND format, not the BBT, not our
generator (proven faithful).

**CRITICAL REFRAME — `_FTLRestore` and DFU/restore are BOTH dead ends; only a
clean `FTL_Open` can work.** The emulator's NAND write model is write-ONLY:
`hw/arm/ipod_touch_nand.c` writes modified pages to `%d_new.page` (line 275) but
the READ path NEVER opens `_new.page` — it reads only `nand.pack` or the base
`%d.page` (or an empty buffer). BUILD.md confirms: `_new.page` are "incomplete
write captures, not a replayable overlay"; writable persistence "remains planned
work." Therefore:
- **N45AP MUST do a clean `FTL_Open` on the raw generator seed** — if it needed a
  restore/format, those writes would be discarded (never read back) and it would
  fail on EVERY boot. So the iPod boots the seed directly; it does NOT format,
  restore, or use DFU. (The 1237 `_new.page` files in the installed N45AP NAND
  are later, ignored write captures — not the source of its bootability.)
- **Chasing M68AP's `_FTLRestore` to succeed is pointless** — even if it
  completed, the rebuilt context could not persist. Likewise a DFU/`asr` restore
  would not stick. Both are dead ends UNTIL a real NAND write/erase/persistence
  model exists (the "planned work").

**So the viable route under the current NAND model is to make the M68AP kernel's
AppleNANDFTL clean-open the seed, exactly as N45AP's kernel does.** The remaining
scope is `FTL_Open` and the NAND/VFL inputs it consumes; it is not yet proven to
be one missing static field. The 2026-07-22 analysis above subsequently located
the function and proved that its `dwVersion/dwVersionNot` pair already matches.
Trace context selection and table loading next. (The VFL zero-BBT patch was
diagnostic only — not landed; M68AP iBoot legitimately needs the production BBT
for its own VFL_Open.)

## Session log — 2026-07-21 (real root FS DECRYPTED; kernel FTL needs a formatted NAND — SUPERSEDED above)

Followed the strategic correction: got the authoritative sources instead of
hand-crafting. Two results — one solved, one precisely diagnosed.

**SOLVED: the real root filesystem is decrypted (the true gate).**
`scripts/decrypt-m68ap-rootfs.sh` does the full pipeline:
- VFDecrypt key for `022-3894-4.dmg` (1.1.4/4A102/iPhone1,1) from The iPhone Wiki
  ("Little Bear 4A102"): `d0a0c0977bd4b6350b256d6650ec9eca419b6f961f593e74b7e5b93e010b698ca6cca1fe`.
  (The GID key does NOT open it — it is vfdecrypt/encrcdsa, not an 8900 container.)
- Compile the standard `vfdecrypt` (openssl); decrypt → a UDIF(zlib) DMG;
  `hdiutil convert` → raw APM disk; slice out the HFS+ volume (`HX`, block 4096,
  68246 blocks = 266 MB). Output `filesystem-m68ap-readonly.img` is a real,
  bootable iPhone OS 1.1.4 root filesystem (with the kernelcache already inside).
  Apple-derived output is never committed.

**DIAGNOSED (the wall to SpringBoard): the kernel FTL needs a FULLY-FORMATTED
NAND, which the generator alone does not produce.** With a full NAND generated
from the real 266 MB root FS (`build-m68ap-nand.py --hfs`, 136 532 pages), the
M68AP kernel still fails: `AppleNANDFTL` `FTL_Open` fails → `_FTLRestore` →
`_ScanForFreeBlk(0xF35) failed` → `Still waiting for root device`. Evidence,
narrowing it precisely:
- **N45AP does a CLEAN `FTL_Open [OK]`** (never restores); M68AP always falls
  into `_FTLRestore`. So M68AP's FTL *context* is rejected by `FTL_Open`.
- **It is NOT filesystem size** (same failure with 16 MB and 266 MB filesystems).
- **The released N45AP NAND has ~19 FTL-context pages in bank0/block 201; the
  generator (and our port) produce only ~3.** The extra context is
  KERNEL-WRITTEN: `generate_nand.c` emits a *seed* NAND that the kernel formats
  on a successful first boot (NAND writes persist to `*_new.page`), and the
  released N45AP NAND is that post-format state.
- **M68AP's first-boot `_FTLRestore` cannot format the seed.** A NAND-model fix
  to return erased pages as 0xFF (real NAND; `hw/arm/ipod_touch_nand.c:141`
  returns 0x00 today) removed the `unidentified spare` misclassification but
  `_ScanForFreeBlk` still failed — so the data blocks also lack the per-block
  FTL spare (logical-block-number/type) that a real format writes. (That model
  change was reverted: it did not unblock M68AP and N45AP's full SpringBoard
  boot depends on the current behavior; land it only with full
  `ipod-acceptance-test.py` validation.)

**Realistic path to SpringBoard (next session):** produce a FORMATTED M68AP NAND,
not a seed. Options: (a) make the kernel's first-boot `_FTLRestore` succeed on
the seed and persist the kernel-written context; or (b) the DFU/USB restore path
(iBSS/iBEC + restore ramdisk + `asr` formats the NAND) — the larger, more
faithful project the design doc flagged as secondary.

**Update — (a) pushed hard, still blocked; two real fixes found, insufficient.**
Two seed-vs-format divergences were identified and corrected, but the kernel's
`_FTLRestore` still fails, so (a) needs deep AppleNANDFTL RE (or (b)):
- **Data-block spare bug FIXED (landed in `build-m68ap-nand.py`):**
  `valid_ftl_spare()` wrote `0x00FF00FF` at spare+8, which set BOTH
  `VFLSpare.cStatusMark` (spare[8]) and `eccMarker` (spare[10]) to 0xFF. The
  reference `generate_nand.c` and the kernel-formatted N45AP data blocks set ONLY
  `eccMarker` (spare[8]=0x00, spare[10]=0xFF). Now matches byte-for-byte. This
  is a genuine correctness fix regardless.
- **Erased-page 0xFF (NAND model) reverted:** `hw/arm/ipod_touch_nand.c:141`
  returns erased pages as 0x00+spare[0xA]=0xFF; real NAND is all-0xFF. Setting
  0xFF removed the `unidentified spare` errors in one config but did NOT make
  `_ScanForFreeBlk` succeed, and it touches shared code N45AP's full boot relies
  on, so it was reverted (land only with `ipod-acceptance-test.py` validation).
- **Even both together fail:** `_FTLRestore OK!` (with `lost part of the EC/RC
  Table` warnings) but then `FTL_Open failed` / `_ScanForFreeBlk(0xF35) failed`.
  So the free-block pool build needs more than erased-0xFF + correct data spare
  — likely the production-BBT interpretation (M68AP uses a 0xFF BBT for iBoot;
  N45AP uses zero) and/or EC/RC (erase-count/read-count) tables the kernel
  format writes. This is genuinely deep: the format path is NOT demonstrated by
  the N45AP example (N45AP ships a pre-formatted NAND and does a CLEAN FTL_Open,
  never exercising `_FTLRestore` in the emulator). Next: either RE the kernel's
  `_ScanForFreeBlk`/BBT handling from the kernelcache's AppleNANDFTL, or pivot to
  the DFU/`asr` restore path (which is what legitimately *formats* a NAND).

## STRATEGIC CORRECTION — 2026-07-21 (the NAND approach diverged from the iPod path)

The M68AP NAND has been hand-crafted (copy N45AP's static FTL context + place a
minimal 16 MB HFS with just the kernelcache). This is NOT how the iPod NAND was
made and it is a dead-end for SpringBoard. Proven by comparing the two:

- **N45AP kernel does a clean `FTL_Open [OK]`** (its NAND is internally
  consistent — the FTL context matches the full 272 MB filesystem, ~133 data
  blocks/bank, built by the upstream devos50 `qemu-ios-generate-nand`).
- **M68AP kernel `FTL_Open` FAILS → `_FTLRestore` → fails** (`_ScanForFreeBlk`
  finds only 21 free blocks; `wDataBlkCnt=0xF20`). Our copied context describes
  blocks up to 334 but our data stops at block 210. The FTL-context page
  (`bank0/25728.page`) is byte-identical to N45AP's; the inconsistency is that
  it references a filesystem that isn't there. iBoot's lenient FTL tolerates
  this (kernelcache loads); the kernel's strict FTL does not.

**Right path (follow the iPod literally, don't hand-craft):**
1. Get the authoritative base sources ONLINE: the devos50
   `qemu-ios-generate-nand` generator (it1g/it2g branches) and the **vfdecrypt
   key** for the iPhone1,1 1.1.4 root DMG `022-3894-4.dmg`.
2. Decrypt the real root filesystem with that key.
3. Run/port the generator to emit a CONSISTENT NAND from the real root FS
   (M68AP signature/BBT), so the context matches the data. Then the kernel
   `FTL_Open` succeeds and root mounts.

The decrypted root FS (vfdecrypt) is the TRUE gate to SpringBoard; the minimal
HFS was only ever good for the kernel-banner milestone. Do not keep hand-crafting
the NAND metadata. See the memory note `use-the-reference-generator-not-a-static-copy`.

## Session log — 2026-07-21 (faithful signing PROVEN; exposes a timer-consistency bug)

Replaced the secure-boot patch approach on the `dtre` image with real signing,
to confirm the honest path works. **It does: an UNPATCHED iBoot accepts the
signed `dtre`** — it now logs `image 0x…: bdev 0x… type dtre offset 0x10800 len
0x8be8` (never printed for the unsigned image), i.e. the signature verification
passes. This validates the whole faithful-signing direction.

**The IMG2 signature scheme (reverse-engineered + verified).** For each NOR
image, `image_load`'s validator (`0x18008478`) + worker signed-path
(`0x180089ba`) require FOUR things; the crypto is `SHA1` (hardware engine at
`0x38000000`, which the port implements) plus an `AES` step (`0x18001790`) keyed
by the baked-in constant at VA `0x18020200` (`41705d11…`), IV at `0x18020210`:
1. **flags2 `+0x1c` bit 1 set** (the "signed" bit) — plus bit 24, bit 30 clear.
   Survives iBoot's RAM normalisation (RAM `+0x1c` reads `0x01000002`).
2. **`+0x20` payload hash** (0x40 bytes) — the worker memcmps the computed value
   here (`0x18008a0a`).
3. **`+0x3e0` header signature** (0x20 bytes) = `AES(SHA1(header[0:0x3e0]))` —
   the validator memcmps here (`0x180084f8`). Depends on `+0x20`, so compute it
   LAST.
4. **`+0x64` CRC32** recomputed (covers `+0x1c` and `+0x20`).
Method that works (per image): set `+0x1c` (bit1+bit24) and fix CRC; capture the
worker's expected `+0x20` via lldb at `0x18008a0a`; write `+0x20`, fix CRC;
capture the validator's expected `+0x3e0` via lldb at `0x180084f8`; write it.
All fields are outside/independent enough that the captured values stay valid.
The captured dtre values are in the scratchpad; a clean implementation should
reproduce the SHA1+AES offline (both keys/engines are available) rather than
lldb-capture per image.

**Blocker to a fully-unpatched boot (2 remaining tasks):**
- (a) **Sign all 7 NOR images**, not just `dtre` (same method).
- (b) **A timer-consistency emulation bug** the signed path exposes. With the
  signed `dtre`, iBoot processes/logs the image EARLY and, right after
  `power supply type firewire`, spins in its 64-bit tick read at VA
  `0x180034bc`: it reads TICKSLOW (`0x3e200080`), TICKSHIGH (`0x3e200084`),
  re-reads TICKSLOW, and loops (`bne 0x180034b6`) while the two low reads
  differ. `hw/arm/ipod_touch_timer.c:87-94` **recalculates the counter from
  host time on EVERY read**, so consecutive TICKSLOW reads never match →
  infinite spin. On real hardware the counter is slow relative to 4
  instructions so they match. **Do NOT casually change this timer** — the
  project's sleep/wake feature depends on it (finding #83); a fix must latch or
  stabilise the value across a tight read window WITHOUT regressing sleep/wake,
  and be tested against `scripts/ipod-acceptance-test.py`.

**So: the faithful fix is proven viable but not yet complete.** The iBoot patch
(`scripts/patch-m68ap-iboot.py`) remains the working default that boots the
kernel; signing is the honest replacement once (a) and (b) are done.

## Session log — 2026-07-21 (M68AP DARWIN KERNEL BOOTS; DT + secure boot + UART CTS solved)

Cleared the device-tree wall and everything through the kernel handoff. Three
fixes, in the order the boot hits them:

1. **NOR IMG2 validator** (`build-m68ap-nor.py promote_loadable`, faithful):
   normalise each NOR image's flags2 (`+0x1c`) to bit 24 set / bit 30 clear and
   recompute the `+0x64` CRC to match iBoot's RAM-normalised header. Detail in
   the DT-reverse-engineering log below.
2. **Secure-boot bypass** (`scripts/patch-m68ap-iboot.py`, a SHORTCUT — see the
   "how N45AP does it" note below): this RELEASE iBoot-204.3.14 never sets
   security-config `0x18022fa0` bit 4 (seeded 0x002c0000 at `0x18005a28`), so it
   strictly rejects unsigned images and refuses to LOAD the (found, validated)
   `dtre`. One 2-byte Thumb edit at the unsigned-image decider (VA `0x18005984`,
   file `0x5990`: `movs r0,#0` → `movs r0,#1`) makes it accept unsigned images.
   Applied to a staged iBoot copy; patched firmware is never committed. With
   it, iBoot loads the DT and reaches `gBootArgs.commandLine = [...]`.
3. **UART CTS** (`hw/char/exynos4210_uart.c`, faithful): after `gBootArgs`,
   m68ap iBoot does a flow-controlled write to the **baseband UART1** and spins
   in `uart_write` (VA `0x18003c9e`) polling UMSTAT (`0x3cc0401c` = UART1+0x1c)
   for CTS. The exynos UART model returned UMSTAT=0 (CTS clear) → infinite
   spin. Report CTS asserted (bit 0) — the correct default for an emulated UART
   with no modem. N45AP never polls UMSTAT, so it is unaffected.

**Result (real time, `--icount-shift -1`):**
`Darwin Kernel Version 9.0.0d1: ... xnu-933.0.0.211 RELEASE_ARM_S5L8900XRB`,
then `config(...): starting on M68AP`, `AppleARMPE::start(M68AP)`, IOKit
registers `cpu0` / `vram@F400000` / `arm-io@3C000000` / `buttons` / `dock` /
`charger` / FairPlay. ~5400 serial lines. Acceptance batch: **m68ap PASS
(deepest=kernel), n45ap PASS (deepest=kernel)** — no regression. Note: run in
REAL TIME; under `-icount shift=3` the kernel does not get enough wall-clock and
the run parks in iBoot's UART loop before the banner.

**Next wall (kernel NAND FTL):** `AppleNANDFTL::_FTLRestore` rejects the
generated NAND (`_ScanForFreeBlk(0xF35) failed`, `wFreeBlkCnt=0x15`, many
`unidentified spare`) → `FTL_Open failed` → `Still waiting for root device`.
The kernel FTL is stricter than iBoot's. This was the then-current diagnosis;
the later clean-open reframe and 2026-07-22 static analysis supersede the
free-block-pool prescription.

**Why N45AP boots UNPATCHED but M68AP needs the patch (proven, not assumed).**
It is NOT that the iPod's iBoot is more permissive — the two are identical here:
- N45AP's secure-boot decider is byte-identical (its `movs r0,#0` reject is
  intact at file `0x5070`; the iPod iBoot is unpatched), and its `security_init`
  is byte-identical too (seeds `0x18022fa0=0x2c0000`, sets bits 28/29, 20, 5 by
  hardware, but NEVER bit 4). So N45AP does not allow unsigned images either.
- The emulator **cryptographically verifies the IMG2 signature.** Proof: flip 32
  bytes of the N45AP `dtre` signature at IMG2 `+0x3e0` (outside the `+0x64`
  header CRC) and the iPod fails with the SAME `failed to load device tree`,
  never reaching Darwin — i.e. break the signature and the iPod hits our wall.
- Therefore N45AP boots because its NOR images are **genuinely, validly signed**
  (flags2 bit 1 set, real hash at `+0x20`/`+0x3e0`) by the upstream devos50
  generator; the emulator checks them and they pass. Our M68AP NOR uses the raw
  IPSW IMG2s, which are unsigned in our pipeline (zero hash), so they fail the
  same real check.
- **So the patch is a genuine shortcut, and the faithful alternative is real:**
  sign the M68AP NOR images the way the generator signs the N45AP ones, and the
  iBoot patch becomes unnecessary. That is the honest path to remove the one
  hack in this bring-up. (Open sub-question: what key/scheme the generator/img2
  signature uses that the emulator's crypto accepts — the plain AES engine's
  GID path is a no-op, but AES-UID and the 8900/GID engine are implemented.)

**Process lesson (how the patch was almost mistaken for the faithful path).**
The secure-boot patch was first justified as "the standard pwnage-equivalent —
what the iPod does too," stated without checking. It was wrong: N45AP boots
UNPATCHED with signed images (proven above). The cheap check — how does the
already-working reference (N45AP) pass this exact wall? — was skipped, even
though N45AP had been used as an A/B oracle for the device-tree walls minutes
earlier. **Rule for this project: when a parallel path already works, verify how
IT passes a shared obstacle before adopting a hack; never justify a shortcut
with "that's how it's normally done" unless it's checked against the actual
artifact.** For a preservation/research emulator, every avoidable firmware patch
is a real cost, so the faithful fix (sign the images) is the target and the
patch is only a temporary unblock.

**Dead ends / techniques this session (don't repeat these):**
- **The "make the emulator report development mode to allow unsigned images"
  idea is a DEAD END — do not re-chase it.** iBoot DOES read a hardware
  security register (`security_init` at `0x18005a28` calls `0x180018e4`, which
  returns bit 4 of CHIPID `0x3e500004` — modeled by `hw/arm/ipod_touch_chipid.c`,
  offset 0x4 returns `CHIP_REVISION<<24`), so a CHIPID/fuse lever *seemed*
  plausible (like the SYSIC epoch fix). But tracing it shows the security-config
  word `0x18022fa0` is seeded to `0x002c0000` and NO code path in this RELEASE
  build ever sets its bit 4 (the allow-unsigned bit the decider `0x18005984`
  checks); the CHIPID bit only influences other config bits (bit 5, etc.). There
  is no hardware lever to accept unsigned images. Hence the iBoot patch (or,
  faithfully, reconstructing the img2 GID signatures) is required — not a CHIPID
  tweak.
- **Locating the UART1 spin:** the boot went silent after `gBootArgs` with no
  serial error. Sampling the parked CPU via the monitor (`info registers` → R15)
  gave PC `0x18003c9e`; disassembling around it showed `uart_write`'s TX/CTS
  poll of `[r4+0x1c]`, and R02 held the polled MMIO address `0x3cc0401c`.
  Decoding that against `include/hw/arm/ipod_touch.h` (`UART1_MEM_BASE
  0x3cc04000`) identified UART1+0x1c = UMSTAT. Always sample the parked PC + the
  MMIO address in registers before assuming a hang; the fix followed directly.
- **Each image-load fix only advanced the failure one gate** (validator CRC →
  validator bit-24 → secure-boot decider → post-`gBootArgs` UART), and every
  `image_load` rejection prints the SAME `failed to load device tree`, so serial
  alone never localised it — register-level lldb bisection at each gate was the
  decisive technique (see the DT-reverse-engineering log's lldb recipe).

## Session log — 2026-07-21 (device-tree load fully reverse-engineered; secure boot is the last gate)

Followed the `load_macho_image: failed to load device tree` wall all the way
down with static disassembly + **live lldb probing** (QEMU `-S -gdb tcp::…`,
hardware breakpoints; only `lldb` is on this host, it drives QEMU's gdbstub).
Each probe advanced the failure deeper, converging on a single remaining gate.

**Full device-tree load chain (all VAs, iBoot-204.3.14 @ base 0x18000000):**
1. `load_macho_image` (`0x1800d544`) loads/validates the kernelcache, then calls
   `dt_load` (`0x1800d060`) at `0x1800e07a`; on `dt_load < 0` it prints
   "failed to load device tree" (`0x1800d676`) and returns −7.
2. `dt_load` (`0x1800d060`): `image_find_by_type('dtre'=0x64747265)`
   (`0x18008376`→walks the image list at head `0x180211a8`, matching
   `descriptor[+8]`); size-check `[desc+4] ≤ 0x100000`; `image_load`
   (`0x18008340`) to dest `0x0bf00000` (globals `0x18023c20`/`0x18023c24`).
   **lltb probe: find returns 0x1802bd48 (FOUND); image_load returns −1.**
3. `image_load` (`0x18008340`→worker `0x180088cc`): checks descriptor magic
   `[desc+0xc]==0x22f5ef0e` (probe: OK), runs the IMG2 validator `0x18008478`,
   then a secure-boot/copy tail.

**IMG2 validator `0x18008478` (was the first real blocker; now cleared):**
iBoot builds a *normalised RAM copy* of the NOR IMG2 header (probe: at
`0x1802b918`) and validates THAT, not the NOR bytes. The validator, on the load
path (arg r3=0), requires: magic `"Img2"`; `crc32(header[0:0x64]) == [hdr+0x64]`
(standard zlib CRC, `0x18007780`); flags2 (`+0x1c`) **bit 24 set**
(`0x180084aa: lsls #7; bpl reject`); epoch (`+0xa`) `== 3`. Two facts nailed by
lldb: iBoot's RAM copy **clears flags2 bit 30** (NOR `0x41000000` → RAM
`0x01000000`) but **copies `+0x64` verbatim** from NOR, so the CRC must be
computed over the bit-30-cleared header. Fix (landed, `build-m68ap-nor.py`
`promote_loadable`): set `+0x1c = (flags2 & ~0x40000000) | 0x01000000` and
recompute `+0x64`. After the fix the validator PASSES (probe: reaches
`0x180084b0` and `0x1800852a`, not the `0x18008596` fail block; RAM `+0x64`
= `0x5f2a73a2` now matches the recomputed CRC).

**Remaining gate — secure boot (`image_load` tail `0x180089aa`+):** with the
validator passing, `image_load` then checks flags2 **bit 1** (`0x180089b0:
lsls #0x1e; bmi`) to choose the signed-hash path vs the unsigned path. Our
`dtre` is unsigned (bit 1 clear; zero hash at IMG2 `+0x3e0`), so it takes the
unsigned path to the decider `0x18005984(1)`, which returns "allowed" ONLY if
**bit 4 (0x10) of the security config word `0x18022fa0`** is set. It is not, so
`image_load` returns −1. The generator-made N45AP `dtre` that loads carries a
real hash at `+0x20`/`+0x3e0` (bit 1 set, signed path); the authentic IPSW
M68AP images do not. **This is a secure-boot-policy problem, not a NAND or IMG2
formatting one** — see the next-session prompt for the three ways forward.

**lldb probe recipe (reusable):** boot with
`-S -gdb tcp::PORT -icount shift=3 -serial file:… -monitor none`; then
`lldb --batch -o "gdb-remote PORT" -o "breakpoint set --hardware --address 0xADDR" -o "process continue" -o "register read …"`.
Wrap in a host `( sleep 90; pkill -9 lldb qemu-system-arm )` watchdog. Key
observation points: `0x1800d0a6` (dt_load post-load r0: ≥0 ⇒ DT loaded),
`0x1800891e` (validator entry; r0 = IMG2 ptr to dump), `0x18008596`
(validator fail), `0x18008a60`/`0x18008a64` (worker fail exits).

**Dead ends / notes:** (1) Setting only bit 24 (keeping bit 30) made the
validator fail the CRC check — the RAM-copy normalisation clears bit 30, so the
CRC must be over the cleared value; must clear bit 30 too. (2) The serial
message "failed to load device tree" is identical for EVERY `image_load`
failure mode, so it cannot localise the fault — register-level lldb was required.
(3) `image_load` runs for multiple NOR images, so breakpoints inside it fire for
non-`dtre` calls; use `0x1800d0a6` (inside `dt_load`, dtre-only) for a
dtre-specific verdict.

## Session log — 2026-07-21 (NAND payload works; kernelcache loads; DT is the wall)

Gave the generated NAND a real filesystem payload and cleared the
`Not HFS+ (signature 0x0000)` wall, advancing the boot through five new stages.

**What was done**
- `scripts/build-m68ap-hfs-payload.sh` (new): builds a minimal case-sensitive
  HFS+ (HFSX, matching the N45AP volume's `HX`/version-5 header) via macOS
  `hdiutil -layout NONE` (filesystem from byte 0, volume header at +0x400),
  sized to a 2048 multiple. It places the IPSW kernelcache at
  `/System/Library/Caches/com.apple.kernelcaches/kernelcache.s5l8900xrb` — the
  exact `$boot-path` iBoot's `fsboot` loads (string in iBoot at VA
  `0x1801a500`). No Apple content committed.
- `scripts/build-m68ap-nand.py --hfs <that dmg>` places the HFS image through
  the FTL logical mapping exactly as the N45AP tree does. Verified byte-for-byte
  that the N45AP installed NAND puts MBR@LBA0 (`sysid 0xEE`, part LBA3, size
  132854), GPT header@LBA1 (`EFI PART`, 1 entry, entsz 0x80), GPT entry@LBA2
  (HFS+ type GUID, lba_start 3), and the HFS+ volume header at page+0x400 of
  LBA3 — the `--hfs` output reproduces this layout.

**Result (verified, `iphone-nand-acceptance.py`, both cases PASS, no
regression)** — M68AP serial now reads:
```
[FTL:MSG] FTL_Open            [OK]
HFSInitPartition: 0x1802e888
Loading kernel cache at 0xb000000...data starts at 0xb000180
done
load_macho_image: failed to load device tree
```
`done` is emitted by the adler32 check inside `load_macho_image`, so the
kernelcache is decrypted (GID key), complzss-decompressed, integrity-verified,
and its Mach-O magic is present in RAM. The N45AP regression in the same batch
still reaches `gBootArgs.commandLine = [...]` and `Darwin Kernel Version`.

**Boot-path RE (iBoot-204.3.14, addresses at VA base `0x18000000`)**
- `load_macho_image = 0x1800d544`. It: validates the kernelcache IMG2
  (`Kernelcache image corrupt/too large/not valid`), checks the `complzss`
  signature (`"comp"`=`0x636f6d70` / `"lzss"`=`0x6c7a7373`), prints
  `Loading kernel cache at %#x...` + `data starts at %p`, LZSS-decompresses
  (`0x1800d3a0`), adler32-checks (`0x180075c0`) → `done`, checks Mach-O magic
  `0xfeedface`, then calls the device-tree loader.
- **Device-tree loader `dt_load = 0x1800d060`** (called at `0x1800e07a`,
  dest global `0x18023c20`→`0x0bf00000`, size global `0x18023c24`): calls
  `image_find_by_type('dtre'=0x64747265)` at `0x18008376`; if NULL → fail; if
  `[img+4] > 0x100000` → fail; else `image_load` (`0x18008340`→`0x180088cc`).
  On `< 0` it clears the globals and returns `-1`, so `load_macho_image` prints
  "failed to load device tree" (`0x1800d676`, returns `-7`) and boot drops to
  recovery. **This is why the fault is a NOR/`dtre` problem, not NAND.**
- `image_load` (`0x180088cc`) checks the descriptor magic
  (`[img+0xc] == 0x22f5ef0e`, or `"Memz"=0x4d656d7a`), then validates via
  `0x18008478` comparing its result to `[descriptor+0x10]`, then copies the
  payload through a function pointer at `[obj+0x1c]`. The exact field
  `0x18008478` validates is the open question for the next session.

**Attempts / dead ends recorded this session**
- *Static disassembly first was slow.* `load_macho_image` and its callees are
  compiler-optimized with reordered basic blocks; several disassembly windows
  landed in literal pools and decoded as garbage. The decisive signal was the
  **empirical N45AP-vs-M68AP serial comparison** (N45AP loads the DT silently
  between `done` and `gBootArgs`; M68AP fails there). Reach for the A/B boot
  before deep RE next time.
- *IMG2 header signature is NOT confirmed as the cause (do not chase it blind).*
  The N45AP NOR `dtre` has a populated hash at +0x20 and a 0x20-byte signature
  at +0x3e0; the authentic M68AP `dtre` (decrypted from the IPSW) has zeros
  there and its metadata at +0x60. But the N45AP NOR image was reprocessed by
  the devos50 generator, and a real M68AP device boots without that block, so
  iBoot-204 cannot strictly require it. Treat the header diff as a lead to
  verify against `0x18008478`, not a proven root cause.
- *Root filesystem is a separate, key-blocked track.* `022-3894-4.dmg`
  (`SystemRestoreImages`→`User`, 123 MB) is `encrcdsa` (vfdecrypt), not an 8900
  container, so the GID key does not open it and no offline key is available.
  The kernelcache-only HFS+ is deliberately minimal: it reaches kernel *load*,
  not a mountable root. Do not block the kernel-banner milestone on the root FS.

## Session log — 2026-07-21 (Data Abort SOLVED; full WMR init green)

Executed exactly the planned experiment: applied
`scripts/iphone-data-abort-hook.patch`, rebuilt, reproduced under `-icount`.
The first-Data-Abort capture gave `lr=0x1801605f` (Thumb) — the `memmove`
caller is the loop at **`0x18015fa0`**, and Capstone disassembly decoded it:

- It allocates a page buffer, then scans blocks from the top of the bank
  downward (bounds from geometry `[0x18025530]`: start `blocksPerBank-1`,
  span `blocksPerBank/10`), reading the first pages of each block.
- Each read page is `memcmp`'d against the 16-byte literal at `0x18020710`:
  **`"DEVICEINFOBBT\0\0\0"`** — this is the stored bad-block-table loader.
- On match: `memmove(dst, page+0x38, *(u32 *)(page+0x34))` — i.e. **+0x34 is
  the BBT byte count, +0x38 the bitmap**. A first presence-check pass calls it
  with `dst=NULL` (no copy); the abort happened on the second, real call.
- Our generated `bank*/524160.page` was `DEVICEINFOBBT` + 0xFF fill for the
  whole page, so the count read `0xFFFFFFFF` and the copy walked to
  `0x18100000` (end of iBoot RAM) → Data Abort. The captured live count
  `0xffe6121d` is `0xFFFFFFFF` minus the ~1.7 MB already copied. The N45AP
  page is marker + zeros (count 0 → no-op copy), which is why N45AP never
  faulted.
- This also retroactively explains the "BBT fill size doesn't change the
  fault" dead end: both the 512-byte and full-page 0xFF experiments still
  0xFF-filled the header area including +0x34.

**Fix (landed):** `build_bbt_page()` in `scripts/build-m68ap-nand.py` now
writes count `0x200` at +0x34 (4096 blocks/bank ÷ 8) and 0xFFs only the
0x200-byte bitmap at +0x38 (all blocks good), zeros elsewhere.
`scripts/test-build-m68ap-nand.py` asserts the new shape.

**Verified:** `iphone-nand-acceptance.py` (clean binary, hook reverted):
M68AP case PASS with `full_wmr_init: true`, deepest gate `ftl_open`, serial
shows `VFL_Open [OK]`, `FTL_Open [OK]`, then `HFSInitPartition` →
`Not HFS+ (signature 0x0000)` → recovery prompt (`]`). N45AP regression in
the same batch still reaches the Darwin kernel banner. The fixed NAND is
installed into the app bundle via `install-iphone-firmware.py` (new tree hash
`da27b620…`), and the bundle-default harness run passes.

**Next frontier:** NAND payload — GPT/partition pages, decrypted HFS+ root
filesystem, kernelcache placement (`--hfs` path of the constructor, so far
unexercised), then `bootx` to the kernel banner.

## Session log — 2026-07-21 (NAND signature SOLVED; constructor + tests landed)

Root-caused and eliminated the `no signature or no production format` blocker:

1. **The blocker was ONE 4-byte constant.** M68AP iBoot-204.3.14 `WMR_Init`
   (Thumb @ VA 0x180164a0) reads `bank0/page0` word0 and compares it to the FIL
   "AND driver" signature **`0x43303033` ("300C")**. N45AP iBoot compares to
   **`0x43303032` ("200C")** (constants at file 0x165b0 / 0x15eb0; the whole
   Whimory section is shifted +0x700 between the two builds but the logic is
   byte-identical). The observed `read only version (1, 0)` meant version=1 (OK)
   but signature-flag=0. There is NO `NANDDRIVERSIGN` string in this iBoot —
   M68AP uses the simple FIL-id-at-page-0 scheme like it1g, NOT the N72AP
   `0x43313131` signature-page scheme.
2. **`scripts/build-m68ap-nand.py`** (new): faithful Python reimplementation of
   the it1g Whimory metadata structures (S5L8900 geometry: 8 banks, 2048+64,
   128 pages/block), parameterised on the signature. Verified it reproduces the
   installed N45AP metadata **byte-for-byte** (all 37 metadata pages; the three
   handoff fingerprints match) when run with `--signature n45ap --bbt zero`.
   With `--signature m68ap` it flips only `bank0/0.page` word0 to `0x43303033`
   and 0xFF-fills the BBT. Emits a JSON provenance sidecar. HFS payload optional
   (not needed for WMR init).
3. **Production BBT is required for M68AP.** With the it1g zero-filled BBT,
   VFL_Open's context scan (`_LoadVFLCxt`) finds nothing and fails at line 768
   (`fail bank 0`). The final N72AP generator 0xFF-fills the BBT ("all blocks
   good"); doing the same lets M68AP's Whimory2_1 VFL_Init build a searchable
   bitmap and VFL_Open then discovers the context. N45AP iBoot does not need
   this (accepts zero-fill) — so it is an M68AP-specific production choice.
4. **Milestone reached, verified by `scripts/iphone-nand-acceptance.py`** (new,
   board-aware, staged copies, hard Python watchdog timeout, machine-readable
   JSON, runs the N45AP iPod boot in the same batch): booting `-M iPhone-2G`
   with the real m68ap iBoot + synthetic m68ap NOR + generated m68ap NAND now
   prints `Apple NAND Driver (AND) 0x43303033`, `FIL/BUF/VFL/FTL_Init [OK]`, and
   **no** `no signature or no production format` / `read only version`. The
   N45AP regression still reaches `Darwin Kernel Version`.
5. **Structural tests**: `scripts/test-build-m68ap-nand.py` validates the
   generated metadata by bytes/structure (N45AP fingerprints, M68AP signature
   word, production BBT fill, VFL spare `[8]=0`/`[9]=0x80`, awInfoBlk@0x7A2,
   geometry) with no Apple payloads in the repo.

**Next failure (precise, corrected twice):** with signature + production BBT,
VFL_Open discovers the context, then iBoot takes a **Data Abort** while
executing `memmove`. A full disassembly diff first overturned the "production
VFL body" hypothesis:

- **The entire Whimory VFL/FTL code is byte-identical between the N45AP and
  M68AP iBoot-204 builds** (M68AP `0x14900-0x18000` vs N45AP shifted −0x700);
  every difference is relocation noise (BL/BLX offset high bytes, relocated
  literal-pool pointers, device strings). There is **no new field check, no new
  constant** in the validator (`0x18016120`), VFL_Open (`0x18016194`), the
  checksum funcs (`0x18015810`/`0x180157e0`), FTL_Open (`0x18015068`), or
  WMR_Init (`0x180164a0`). So the two builds require *identical* context bytes,
  and the it1g body is NOT the problem.
- `0x18017cb4` is the ARM `ldrb r3, [r1], #1` byte-copy instruction inside
  `memmove` at `0x18017bac`. The two indirect calls in `_LoadVFLCxt` dispatch
  through a **statically initialised** vtable (`[0x18025570]` set at
  `0x18015a54` to `{0x18015864, 0x18015810}`), so the original VFL callback
  corruption theory is ruled out. The direct caller of this particular
  oversized `memmove` is still unknown.
- **VFL context layout confirmed (it1g, NOT it2g):** spare `[8]==0` /
  `[9]==0x80`, `dwCxtAge` at spare `[0..3]`; data `awInfoBlk[4]` at **0x7A2**
  (literal cited at both iBoots). Optional production trailer (verified present
  in the checksum code, but N45AP accepts zeros so it is not what blocks us):
  `dwVersion`@0x7F4, `dwCheckSum`@0x7F8 `= Σ words[0..509] + 0xAABBCCDD`,
  `dwXorSum`@0x7FC `= ⊕ words[0..509] ^ 0xAABBCCDD` (510 LE u32 over bytes
  0x000–0x7F7; const at `0x1801580c`).

### Runtime diagnosis — corrected exception chain (2026-07-21)

The previous session first called this a garbage-length copy, then incorrectly
overturned that result as a bad indirect branch. The decisive evidence is the
ordered `-d int` exception log, not the final banked register state:

```text
Exception return from AArch32 irq to svc PC 0x18017cb0
Taking exception 4 [Data Abort] on CPU 0
...with DFSR 0x8 DFAR 0x18100000
Taking exception 3 [Prefetch Abort] on CPU 0
...with IFSR 0x8 IFAR 0x10
Taking exception 3 [Prefetch Abort] on CPU 0
...with IFSR 0x8 IFAR 0xc
```

- **The original exception is a Data Abort.** `0x18017cb4` is the executing
  instruction (`ldrb r3, [r1], #1`), not IFAR. `DFAR=0x18100000` equals the
  live source pointer. The active arguments are `r0=0x180fc9e0`,
  `r1=0x18100000`, `r2=0xffe6121d`; these are real `memmove` state, and the
  near-4-GiB count has walked the source to the end of the iBoot RAM mapping.
- **The prefetch aborts are secondary.** A Data Abort sets abort LR to
  `fault_pc + 8 = 0x18017cbc` and vectors to `0x10`. That vector is unmapped
  under the active MMU, so its fetch aborts; vector `0x0c` is also unmapped and
  repeats forever. Sampling only the parked CPU or IFAR after this cascade led
  to the false "fetch of `0x18017cb4`" conclusion.
- **Why the entry breakpoint misled us:** the earlier hand-rolled debugger did
  not observe the `memmove` entry, but the architectural exception LR and the
  ordered exception trace prove execution reached its byte loop normally.
  Treat the missed breakpoint as a debugger/tooling failure, not control-flow
  evidence.
- **Deterministic repro:** `-icount shift=3` reaches `FTL_Init [OK]` and then
  the same Data Abort. Host watchdog duration is not itself a guest-time
  guarantee: an 8-second sample sometimes caught the normal timer helper at
  `0x180034b8`, while a 20–25-second bound reliably caught the abort.
- **NOR isolation completed:** M68AP iBoot plus the N45AP NOR reaches the same
  post-`FTL_Init` boundary and fault. NOR/DeviceTree contents are not the
  variable. The mixed NOR adds expected epoch/image messages but does not alter
  the failure.
- iBoot Whimory code remains byte-identical to N45AP (true section delta
  **0x704**), NAND/ECC/FMI behavior is shared, and BBT fill-size changes do not
  affect the fault. This still does not prove every runtime input is correct;
  it says the next task is to find who supplied `r2=0xffe6121d`, not to redesign
  the VFL context or NAND signature.

**Reusable tooling added:** `scripts/iphone-nand-acceptance.py` now defaults to
`-icount shift=3`, stages firmware, saves a stopped monitor register/stack
snapshot, and has an opt-in `--interrupt-log`. Keep exception runs short because
the nested vector abort produces millions of repetitive lines. The temporary
CPU hook was removed from `target/arm/helper.c`; use the ready-to-apply
`scripts/iphone-data-abort-hook.patch` when the pre-mode-switch SVC LR/SP is
needed: run `git apply scripts/iphone-data-abort-hook.patch`, rebuild and
capture one repro, then run `git apply -R scripts/iphone-data-abort-hook.patch`.
`git apply --check scripts/iphone-data-abort-hook.patch` verifies that the hook
still matches the current CPU source before changing it.

**Key addresses:** `memmove=0x18017bac`; faulting byte load `0x18017cb4`;
WMR_Init `0x180164a0`; post-`FTL_Init` print return `0x18016508`; VFL_Open
`0x18016194`; geometry `[0x18025530]`.

## Firmware layout & parity (iPod ⇄ iPhone)

Firmware lives in the app bundle, one dir per board, same file names:

```
<App>/Contents/Resources/
  ipod_files/     bootrom_s5l8900  iboot_204_n45ap.bin  nor_n45ap.bin  nand/
  iphone_files/   bootrom_s5l8900  iboot_204_m68ap.bin  nor_m68ap.bin  nand/  firmware-provenance.json
```

This is the layout `scripts/install-ipod-app-engine.sh` already expects (its
`ipod-touch` profile → `ipod_files`, `iphone-2g` profile → `iphone_files`).
Populate `iphone_files/` reproducibly from a user-supplied IPSW:

```
python3 scripts/extract-m68ap-images.py <IPSW>/.../all_flash.m68ap.production OUT
python3 scripts/build-m68ap-nor.py  --template <n45ap NOR> --containers OUT/nor-containers --out OUT/nor_m68ap.bin
python3 scripts/build-m68ap-nand.py --out OUT/nand-m68ap --signature m68ap
python3 scripts/install-iphone-firmware.py --from OUT   # assembles + installs + re-signs
```

`scripts/iphone-nand-acceptance.py` defaults to these bundle dirs (no paths
needed), exactly as it uses `ipod_files/` for the N45AP regression. Apple-derived
firmware is never committed (AGENTS.md); it lives only in the bundle, like the
N45AP set. Each `iphone_files/` carries a `firmware-provenance.json` (hashes +
the NAND constructor manifest).

## Session log — 2026-07-21 (merge onto wifi line + step-1 fixes landed)

Work done on branch `ipod_touch_1g` (the wifi/HTTPS line), merging in
`iphone_2g`:

1. **Merge**: `iphone_2g` (machine type, baseband/ALS/Zephyr1 stubs, m68ap
   tooling) merged into the branch carrying MV8686 Wi-Fi + DNS + HTTP/HTTPS
   bridge work. Zero file overlap between the two lines — clean merge. One
   QEMU 11.0.2 binary (`build-ipod11/qemu-system-arm`) now registers both
   `iPod-Touch` and `iPhone-2G`, sharing the whole networking stack.
2. **Board-aware SYSIC epoch (landed)**: `POWER_ID` bits [31:24] now come from
   `IPodTouchSYSICState.power_epoch`, set at machine init: N45AP=2, M68AP=3.
   A new machine option `epoch=` overrides it (see 4).
3. **Watchdog reset semantics (landed, M68AP only)**: `WATCHDOG_MEM_BASE`
   is a real MMIO region on M68AP; writing 0x100000 requests a guest reset.
   N45AP keeps the historical inert-RAM backing (shipped working config).
   *Evidence*: booting n45ap iBoot on `-M iPhone-2G` (default epoch 3) now
   produces a clean panic→reset loop — 1223 QMP RESET events in 20 s — where
   it previously hung silently at `0x18001e3c` forever.
4. **`epoch=` machine option**: `-M iPhone-2G,epoch=2` boots cross-board
   firmware (n45ap images on the M68AP board). `scripts/iphone-smoke-test.py`
   uses it; without it the epoch/watchdog behavior of (3) is the expected
   result, which is itself the regression check for these fixes.
5. **Regressions run**:
   - `-M iPod-Touch` with the merged binary boots to SpringBoard
     (iBoot-204 → Darwin → FTL → AppleMRVL Wi-Fi → mDNSResponder →
     SpringBoard). N45AP unaffected.
   - `scripts/iphone-smoke-test.py` (now with `epoch=2`): all checks pass —
     both machines listed, Darwin boots, AppleISL29003 probes, Zephyr1-mode
     mismatch as expected, no panics.

**Dead-end recorded**: after the epoch fix, the smoke test's original
n45ap-on-M68AP boot broke *by design* (n45ap iBoot requires epoch 2). Do not
"fix" this by reverting the board default — the `epoch=` override exists for
exactly this synthetic combination.

## Session log — 2026-07-21 (real m68ap iBoot boots; NOR solved; NAND is the wall)

Booted the **real extracted m68ap iBoot** (`iboot_204_m68ap.bin`) on the
merged binary. With the epoch fix landed, no debugger override is needed:
iBoot reaches its banner (`BUILD_TAG: iBoot-204.3.14`), FTL, and NAND probe
automatically.

**Synthetic NOR — SOLVED.** With the N45AP NOR, iBoot logged 7×
`Ignoring image with mismatching security epoch` (the N45AP images are epoch
2; this iBoot wants 3). Fix:
- `scripts/extract-m68ap-images.py` now also emits the full decrypted IMG2
  *containers* (0x400 header + payload, epoch 3 intact) into
  `<out>/nor-containers/`, named by source stem.
- `scripts/build-m68ap-nor.py` rewrites the NOR image store (0x10400..) with
  those containers in N45AP order (dtre, batC, logo, nsrv, batl, batL, recm),
  0x40-aligned, preserving the N45AP SysCfg at 0xFC000.
- Result: booting m68ap iBoot with `nor_m68ap.bin` logs **0** epoch
  mismatches (was 7). All M68AP NOR images accepted, DeviceTree included.

**IMG2 epoch field pinned down**: header offset **+0xa** is `uint16
security_epoch` (N45AP=2, M68AP=3). Confirmed by diffing the two DeviceTree
headers. This is the same value the SYSIC `power_epoch` fix reports.

**Gotcha (recorded)**: the two NOR battery images use IMG2 4CCs that differ
only by case — `batl` (batterylow0) vs `batL` (batterylow1). Keying container
files by 4CC collides on a case-insensitive filesystem (macOS default: batL
silently overwrote batl, so both came out 0xedd2). The extractor now keys
container files by source stem instead.

**Remaining wall — the NAND format.** With the m68ap iBoot **and** the synthetic
m68ap NOR, the boot now fails at exactly one place — the N45AP NAND:
```
[FTL:MSG] FTL_Init            [OK]
[WMR:ERR] read only version (1, 0)
[WMR:ERR] no signature or no production format
NAND failed initialisation
... root filesystem mount failed ... Entering recovery mode
```
The Whimory low level initializes (FIL/BUF/VFL/FTL all `[OK]`) because it is
the same SoC/controller, but the higher WMR layer rejects the N45AP NAND's
signature/production format. So the ordered blocker list is now down to one
format-construction task:

### The NAND, precisely

- An IPSW does not contain raw physical NAND pages, but that does **not** mean
  there is no synthesis path. The original qemu-ios projects construct the
  physical page tree and metadata around an IPSW-derived HFS image.
- For N45AP, the public generator emits eight banks of 2048-byte data plus
  64-byte spare pages. It writes FIL `0x43303032`, identical synthetic BBTs,
  VFL contexts, FTL context/mapping pages, GPT, and the HFS payload. The
  upstream author's 2022 instructions explicitly say the released NAND is
  generated from the IPSW root filesystem.
- The bundled N45AP artifact has exact generator fingerprints. On a staged
  copy, the following SHA-256 values match freshly generated metadata pages:

  | Page | SHA-256 |
  |---|---|
  | `bank0/0.page` | `c5dacd1ade5322b1c36507be39e873a387414308cb64c2f9dac4eef26740c006` |
  | `bank0/4480.page` (also bank 1) | `5a0157e626602bea19d797571b245809694a28b4e7e9268b6d08df066c19ee67` |
  | `bank0..7/524160.page` | `6984b58fc2345586f86ab3d64d098a1ffdb6a214556af4574ee439aa22d9bfb0` |

  Therefore the earlier “real dump / not synthesized” claim was incorrect.
  This fingerprint does not prove the origin of every mutable filesystem page;
  keep provenance manifests for future artifacts.
- For N72AP, qemu-ios commit [`1300c08302`](https://github.com/devos50/qemu-ios/commit/1300c08302e6c5f5d26664ced2a9336e2c5947f9)
  temporarily patched iBoot/kernel FTL reads to a host block device. Commit
  [`5e9f53bfd8`](https://github.com/devos50/qemu-ios/commit/5e9f53bfd8ab3f2969138672daa3605eb7f406ef)
  removed that bypass, and the final port reads generated physical pages. Its generator adds a
  `NANDDRIVERSIGN` page, WMR/VFL production fields, mapping pages, BBT, GPT,
  and HFS data. This is the closest precedent for the M68AP rejection.
- `scripts/pack-ipod-nand.py` still only compacts an existing page tree. The
  M68AP constructor is now implemented and reference-diffed; the remaining
  blocker is the M68AP kernel's clean `FTL_Open`, not physical-page construction.

### Route decision

Use the iPod ports' final, proven design: generate an M68AP sparse physical
page tree from a user-supplied IPSW, then boot it through the existing NAND
controller model. A physical iPhone1,1 dump is an optional oracle, not a
dependency. Guest FTL-read patching is diagnostic-only because upstream
removed that transitional bypass before the final 2G solution.

Modeling S5L8900 DFU/USB far enough to run Apple's real restore is feasible in
principle, but it is a substantially larger fidelity project: USB EP0/DFU
state, iBSS/iBEC transfers, recovery protocol, ramdisk boot, host orchestration,
NAND erase/write/persistence, and `asr` behavior all have to work together. It
is not required to solve the current metadata blocker and is now a secondary
track after first boot.

Historical primary sources:

- [1G NAND generation walkthrough](https://devos50.github.io/blog/2022/ipod-touch-qemu-pt2/#manually-generating-the-nand-image)
- [`qemu-ios-generate-nand`](https://github.com/devos50/qemu-ios-generate-nand), including tags `it1g_nand_filesystem` and `it2g_nand_filesystem`
- [Final iPod Touch 2G runner](https://github.com/devos50/qemu-ios/blob/ipod_touch_2g/RUNNING.md)

---

## What is proven (advances)

### 1. Firmware source
`iPhone1,1_1.1.4_4A102_Restore.ipsw` (build 4A102) is still served by Apple's
CDN and is ~162 MB. Get the URL from the ipsw.me API:

```bash
curl -s "https://api.ipsw.me/v4/device/iPhone1,1?type=ipsw" | \
  python3 -c "import json,sys;[print(f['version'],f['url']) for f in json.load(sys.stdin)['firmwares']]"
```

Unzipping it yields (the pieces that matter):

| File | Role |
|---|---|
| `Firmware/all_flash/all_flash.m68ap.production/iBoot.m68ap.RELEASE.img2` | iBoot, 8900-wrapped |
| `.../LLB.m68ap.RELEASE.img2` | LLB, 8900-wrapped |
| `.../DeviceTree.m68ap.img2` | device tree, 8900-wrapped |
| `.../applelogo.img2`, `batterylow*`, `recoverymode.img2`, `needservice.img2` | boot logos / recovery UI |
| `kernelcache.release.s5l8900xrb` | kernel |
| `022-3894-4.dmg` (123 MB) | root filesystem |
| `022-3896-4.dmg`, `022-3900-4.dmg` (~18 MB each) | restore ramdisk(s) |

The 1.1.4 iBoot is **iBoot-204** — the *same* build the n45ap machine already
runs. (1.0/1A543a would be the museum-accurate target per the feasibility doc,
but 1.1.4 is the pragmatic first boot: same iBoot version, freely archived.)

### 2. Decryption is solved — no key hunt needed
S5L8900 is one SoC shared by iPhone 2G and iPod Touch 1G, so they share the GID
key ("AES key 0x837"). That key is **already hard-coded** in
`hw/arm/ipod_touch_8900_engine.h`:

```
188458A6D15034DFE386F23B61D43774
```

It decrypts the iPhone's 8900 images exactly as it does the iPod's. Container
shape: `0x800` 8900 header, then AES-128-CBC(key=GID, iv=0), then a `0x400`
IMG2 wrapper ("Img2" = `2gmI` LE; 4-char type at +4: `tobi`=iBoot, `llbz`=LLB,
`dtre`=DeviceTree), then the raw payload.

Tooling committed: **`scripts/extract-m68ap-images.py`** (extraction/conversion
only — no firmware committed, per the artifact policy in `AGENTS.md`). It also
retains complete decrypted IMG2 containers for the NOR builder. Run:

```bash
python3 scripts/extract-m68ap-images.py \
    <IPSW>/Firmware/all_flash/all_flash.m68ap.production  <out dir>
# -> iboot_204_m68ap.bin (0x22000)  LLB.m68ap.bin (0xb000)  DeviceTree.m68ap.bin (0x9000)
```

The extracted `iboot_204_m68ap.bin` is **0x22000 bytes with the identical ARM
reset vector** (`0e 00 00 ea …`) to `data/iboot_204_n45ap.bin` — same size, same
layout, different build. Sanity check passes: it contains the strings
`iBoot-204.3.14`, `:: iBoot, Copyright 2007, Apple Inc.`, etc.

### 3. It boots far enough to diagnose
Loading the m68ap iBoot with the **n45ap** NOR/NAND under `-M iPhone-2G`:

```bash
cd build && ./qemu-system-arm \
  -M iPhone-2G,bootrom=ipod_files/bootrom_s5l8900,iboot=ipod_files/iboot_204_m68ap.bin,nand=ipod_files/nand \
  -serial mon:stdio -cpu max -m 1G -pflash <writable copy of nor_n45ap.bin> -display none
```

iBoot executes at `0x18000000`, runs the standard reset handler (walks the CPU
through svc→irq→fiq→abt→und→svc setting per-mode stacks, PC `0x1800009c`..`d0`),
then **hangs with PC parked at `0x18001e3c`**. (The `qemu-system-arm` binary must
be rebuilt with `ninja` — a stale build will report "unsupported machine type"
for `iPhone-2G`.)

For comparison, the n45ap iBoot on the same command boots fully (iBoot banner +
`[FTL:MSG] Apple NAND Driver`, ~1500 lines of serial). So the harness is fine;
the m68ap iBoot is the difference.

### 4. The exact panic and post-fix path are proven

A GDB breakpoint on the panic entry at `0x18005420` captured:

```
r0 = 0x1801ac38  -> "miu_init"
r1 = 0x1801ac44  -> "miu_init: Epoch Mismatch\n"
lr = 0x180028c7
```

The caller at `0x180028b0` reads `SYSIC_MEM_BASE + 0x44` (`POWER_ID`), shifts
the result right by 24, and requires `3`. The current SYSIC model returns
`2 << 24` unconditionally and even retains a commented `3 << 24` value marked
"for older iboots" in `hw/arm/ipod_touch_sysic.c`.

Overriding only that guest register value to `0x03000000` produced serial:

```
SysCfg: version 0x00010001 with 4 entries using 200 of 8192 bytes
merlot_init() -- Universal code version 11-13-07
Project/Driver: M68/NSC-Merlot
:: BUILD_TAG: iBoot-204.3.14
[FTL:MSG] FTL_Init                    [OK]
[WMR:ERR] read only version (1, 0)
[WMR:ERR] no signature or no production format
NAND failed initialisation
Entering recovery mode, starting command prompt
```

This proves that the N45AP NOR was not the cause of the early panic. It also
separates the next failure cleanly: the NAND controller and FTL initialize,
then the M68AP iBoot rejects the N45AP NAND's WMR/production content.

---

## Root cause of the hang (diagnosed)

`0x18001e3c` is inside iBoot's **reboot/reset routine** at `0x18001e28`
(Thumb):

```
18001e28  push {r7,lr};  cmp r0,#0;  bne 1e34;  bl 0x18004a00   ; flush, only if r0==0
18001e34  ldr r3,[pc,#8] ; r3 = 0x3E300000  (WATCHDOG_MEM_BASE)
18001e36  movs r2,#0x80; lsls r2,#0xd  ; r2 = 0x100000
18001e3a  str r2,[r3]    ; poke the watchdog reset register
18001e3c  b .            ; spin, waiting for the SoC to reset   <-- PC parks here
```

`0x3E300000` is `WATCHDOG_MEM_BASE` (`include/hw/arm/ipod_touch.h:106`), which
the machine backs with **inert `allocate_ram`** (`hw/arm/ipod_touch.c:386`) — no
reset semantics. So the write does nothing and iBoot spins forever instead of
rebooting.

The caller is a **panic handler**: at `0x18005440` it loads the format string
`panic (%s): ` (literal at file offset `0x5454` → VA `0x1801b504`) and then
calls `reboot(1)`. So **iBoot panicked in early platform init** — before any
serial output appeared (the panic string never reached the UART; UART/putchar is
apparently not up yet on this path, or is routed differently).

**Why it panics:** `POWER_ID` at `SYSIC_MEM_BASE + 0x44` reports epoch 2, but
this M68AP iBoot requires epoch 3 during `miu_init()`. This is a pre-existing
hard-coded N45AP assumption in the shared SYSIC model, exposed by running the
older M68AP platform initialization path. The fix must be board-specific so the
working N45AP behavior remains epoch 2.

---

## NOR layout facts (for the rebuild)

`data/nor_n45ap.bin` is a raw 1 MiB CFI NOR (`hw/block/pflash_cfi02.c`, mapped in
`hw/arm/ipod_touch.c:398-412`). **QEMU does not parse it** — the guest reads it
directly. Layout:

- `0x00000`–`0x10000`: zero in the shipped image (LLB region; the emulator does
  **not** execute a NOR LLB — iBoot is pre-staged raw to `IBOOT_BASE=0x18000000`
  by the machine, see `hw/arm/ipod_touch.c:371-376`, reloaded on reset at
  `:175-180`).
- `0x10400`+: **IMG2 image store**, each entry a `2gmI` header (0x400 bytes) then
  data. In n45ap: `dtre`@0x10400 (len 0x7d28), `batC`@0x18940, `logo`@0x29680,
  `nsrv`@0x2bbc0, `batl`@0x30900, `batL`@0x3de40, `recm`@0x4d380. **No iBoot/LLB
  image is in this store.**
- `0xfc000`: **syscfg / nvram** — `SysCfg version 0x00010001, 4 entries`
  (`nvram`, `common` with `boot-args=…`, etc.).

The N45AP syscfg is accepted after the SYSIC epoch override, so syscfg is not
the early-boot blocker. N45AP IMG2 entries are logged as `Ignoring image with
mismatching security epoch`; `scripts/build-m68ap-nor.py` now replaces them
with retained M68AP containers, including the DeviceTree.

Do not reuse the N45AP IMG2 header around an M68AP body. The guest, not QEMU,
parses the NOR and checks header metadata including its security epoch. The
extractor and NOR builder now preserve that complete 0x400-byte header; keep
this as a regression invariant.

---

## Next plan (in order)

1. **Freeze the proven baselines.**
   - Keep the current M68AP iBoot + NOR trace as the negative fixture: it must
     reach `FTL_Init [OK]` and fail only at WMR production validation.
   - Add fixture tests for the public N45AP generator metadata and its logical
     page-to-bank/page mapping. Tests should validate bytes and structure, not
     require Apple payloads in the repository.
   - Treat the external generator as a format reference. Its repository has no
     explicit license in the checked history, so do not copy its source
     verbatim without resolving that; reimplement the documented structures
     and behavior with attribution.

2. **Implement `scripts/build-m68ap-nand.py`.**
   - Input: a user-supplied iPhone1,1 IPSW or extracted root HFS image plus a
     provenance manifest. Output: a new sparse `bank0..bank7/*.page` tree;
     never modify an installed or source NAND.
   - Start with the working S5L8900/N45AP geometry: eight banks, 2048 data + 64
     spare bytes, 128 pages per block, sparse erased pages, and the established
     virtual/logical-to-physical mapping.
   - Generate FIL, BBT, VFL contexts/copies, FTL context and mapping tables,
     valid data-page spares, GPT/partition pages, HFS payload placement, and
     the kernelcache location expected by M68AP iBoot.
   - Add the production-format ideas proven by the final N72AP generator:
     signature page, explicit VFL metadata version/vendor format, context ages
     and production markers. Determine M68AP's exact signature constant,
     WMR version and field offsets by tracing/disassembling iBoot-204.3.14;
     do not assume N72AP's `0x43313131` is identical.
   - Emit JSON containing source hashes, output geometry, populated pages,
     metadata versions, partition offsets, and constructor revision. Optionally
     run `scripts/pack-ipod-nand.py` only after the sparse tree validates.

3. **Add one scripted M68AP NAND acceptance case.**
   - Extend the existing board-aware boot harness rather than doing manual
     tap-by-tap testing. Always stage NAND and NOR copies.
   - Phase gates: iBoot banner; FIL/BUF/VFL/FTL success; no `WMR:ERR`; kernel
     banner; root mount; launchd; SpringBoard. Emit machine-readable status,
     serial offsets, and a screenshot on success or failure.
   - The first milestone is deliberately narrow: replace `no signature or no
     production format` with a successful WMR init. Then fix partition/
     kernelcache placement using the next observed failure.
   - Run the existing iPod acceptance test in the same regression batch.

4. **Firmware-specific bring-up** once iBoot/kernel run: the m68ap paths the
   branch already stubs go live — Zephyr1 multitouch, ISL29003 ALS, S-Gold2
   baseband (`hw/arm/ipod_touch_multitouch.c`, `_isl29003.c`, `_baseband.c`,
   gated on `board_id == BOARD_ID_M68AP` in `ipod_touch_machine_init`). Expect
   iterative unimplemented-register fixes from the `-d unimp` log. See the
   "Path to a full iPhone OS 1 boot" section of `IPHONE_2G.md`.

5. **Restore fidelity later, independently.** Once generated NAND boots,
   improve erase/write/persistence and only then evaluate real DFU/restore as
   an end-to-end validation path. Do not block first boot on USB restore.

---

## Dead ends / gotchas (don't repeat these)

- **No offline key hunt needed.** Do not go looking up per-image AES keys on the
  iPhone Wiki — the GID key in `ipod_touch_8900_engine.h` already decrypts
  everything for this SoC. (`theiphonewiki.com/wiki/Firmware_Keys/1.1.4_(iPhone)`
  returns 404 anyway.)
- **The IMG2 header is 0x400, not 0x800.** After 8900 decryption the payload is
  an IMG2 container; strip `0x400` to reach raw ARM. (0x22400 decrypted − 0x400
  = 0x22000 = the n45ap iBoot size, which is the confirmation.)
- **Rebuild before testing.** The checked-in `build/qemu-system-arm` can be stale
  and silently lack `-M iPhone-2G`; run `ninja` first and confirm with
  `./qemu-system-arm -M help | grep -i iphone`.
- **A silent hang is a panic, not a QEMU stall.** Zero serial output does not
  mean "nothing ran" — here iBoot ran, panicked, and is spinning in the reboot
  routine. Always sample PC via the monitor (`info registers`, R15) before
  concluding it hung; the watchdog-write-then-spin at `0x18001e3c` is the tell.
- **The N45AP NOR is not the early-panic cause.** The confirmed cause is SYSIC
  epoch 2 versus the M68AP-required epoch 3. With that read overridden, the
  same NOR reaches the banner and recovery prompt.
- **Do not reuse an N45AP IMG2 header for M68AP data.** iBoot checks its
  security epoch and ignores the N45AP entries. Preserve the decrypted M68AP
  header/container in the extraction pipeline.
- **`pack-ipod-nand.py` does not create a NAND.** It only compacts an existing
  `bank0..bank7/*.page` tree. Use `build-m68ap-nand.py` (now built); do not
  confuse packing with construction.
- **The post-`FTL_Init` abort is NOT a NAND-format / VFL-body problem.** The
  iBoot code is byte-identical to N45AP (true section delta **0x704**, not
  0x700 — a 4-byte off-by-one made FIL *look* different once; it isn't). N45AP
  iBoot boots on the same `-M iPhone-2G` machine. Don't re-derive a "production
  VFL context body"; the it1g layout is what M68AP reads (awInfoBlk@0x7A2).
- **Do not dismiss the `memmove` arguments as leftovers.** The ordered
  exception trace proves this is an ordinary Data Abort while the byte-copy
  loop executes: `r1=0x18100000` is DFAR and `r2=0xffe6121d` is the bad active
  count. The earlier entry-breakpoint miss was not evidence of an indirect
  branch. Chase the caller that supplied the count.
- **Do not call the primary fault a Prefetch Abort.** The first exception is a
  Data Abort at `0x18017cb4`; the prefetch aborts only occur because the active
  MMU does not map exception vectors `0x10` and `0x0c`.
- **gdb SOFTWARE breakpoints don't work in iBoot.** `0x18000000` is a read-only
  region; `Z0` silently fails there. Use HARDWARE breakpoints (`Z1`). This cost
  a full session.
- **Don't single-step without `-icount`.** `QEMU_CLOCK_VIRTUAL` tracks wall
  time, so single-stepping makes the guest timer fly and moves the fault earlier
  (before `FTL_Init`) — an artifact. Use `-icount shift=3` for a deterministic
  repro that matches the fast path.
- **BBT fill size is not the cause** — *resolved*: both fill sizes faulted
  because both 0xFF-filled the DEVICEINFOBBT header including the count field
  at +0x34. The real layout is count@+0x34 / bitmap@+0x38 (see the 2026-07-21
  Data Abort session log). The production bitmap fill is still required
  (the it1g zero-fill fails VFL_Open's context scan at line 768).
- **Do not repeat the “device dump only” conclusion.** The 1G generator and
  current bundled metadata hashes disprove it, while the final 2G port proves
  a production-format sparse tree can also be generated.
- **Do not revive the 2G FTL bypass as the product path.** It was a temporary
  bring-up hack removed by `5e9f53bfd8`; physical-page generation is the
  durable design.
- **Bound every boot test with a hard timeout** (an untimed boot wait once wedged
  a session for two hours — see the note in `IPHONE_2G.md`).

## Artifacts

- `scripts/extract-m68ap-images.py` — committed, reproducible decryptor.
- `scripts/build-m68ap-nor.py` — synthetic M68AP NOR builder.
- `scripts/build-m68ap-nand.py` — M68AP NAND constructor (signature `0x43303033`,
  production BBT, it1g Whimory metadata; reproduces N45AP metadata byte-for-byte
  with `--signature n45ap --bbt zero`). Emits a JSON provenance sidecar.
- `scripts/test-build-m68ap-nand.py` — structural fixture tests (no Apple
  payloads): N45AP fingerprints, M68AP signature/BBT, VFL spare, geometry.
- `scripts/iphone-nand-acceptance.py` — board-aware M68AP NAND boot acceptance
  (staged copies, hard timeout, explicit root/launchd/SpringBoard JSON gates) +
  N45AP regression in the same batch.
- `scripts/m68ap-ftl-trace.py` and `contrib/plugins/m68ap-ftl-trace.c` — bounded
  storage/filesystem and startup observation, including NAND identification,
  HFS mount results, and explicitly labelled diagnostic startup adjustments.
- `scripts/compare-s5l8900-startup.py` — ordered semantic comparison of N45AP
  and M68AP serial startup events and parity milestones; output is JSON.
- `scripts/analyze-m68ap-ftl-open.py` — in-memory 8900 decrypt + `complzss`
  verification + Mach-O/ARM literal analysis for the stripped AppleNANDFTL
  `FTL_Open`/`_FTLRestore` call chain; emits JSON and optionally checks a physical
  FTL metadata page.
- `scripts/test-analyze-m68ap-ftl-open.py` — fixture-free LZSS, Mach-O mapping,
  and FTL metadata-field tests; no Apple artifact required.
- `scripts/iphone-smoke-test.py` — N45AP-firmware board-divergence regression;
  it is not a real M68AP kernel test.
- The IPSW, decrypted images, generated M68AP NAND, and any physical comparison
  dump stay uncommitted. The next repository work is the paired N45AP/M68AP
  `IOIpodUSBDevice::start` platform-function observation described above.
- Every generated NAND must carry a sidecar provenance manifest with IPSW
  device/build and hash, extracted HFS/kernelcache hashes, constructor commit,
  output geometry, and declared guest-file modifications.
- The upstream projects and long-lived public releases are strong technical
  precedent. They are not a blanket legal determination; repository policy is
  to accept user-supplied inputs and not distribute Apple payloads or
  device-unique data.
