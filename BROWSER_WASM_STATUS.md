# Browser/WebAssembly port — live status

The dated, working state of the browser port. The design of record is
[`BROWSER_WASM_IMPLEMENTATION_PLAN.md`](BROWSER_WASM_IMPLEMENTATION_PLAN.md);
this file records what is actually built and proven.

> **Starting a session? Read [`BROWSER_WASM_HANDOFF.md`](BROWSER_WASM_HANDOFF.md)**
> — the ordered next steps, their acceptance criteria, and the traps.

**Target:** iPhone 2G (M68AP) running **iPhone OS 1.1.4**, in a browser: fast to
start (chunked assets) and running at **real-time speed**. Then 1.0, once its
button problems are fixed elsewhere, followed by 1.0.2 and 1.1.1 as a version
picker. Assets are prepared offline here and self-hosted. iPod touch (N45AP)
comes after.

---

## Session — 2026-07-29 (Session A): making it visible and interactive

Parallel session A (`BROWSER_WASM_SESSION_A.md`): paint the framebuffer, take
input, skip the boot.

**A1 and A2 are done. iPhone OS 1.0 reaches its home screen in a browser and
launches an app when you tap its icon.** A3 was measured rather than built: a
`migrate file:` snapshot restores guest RAM byte-for-byte and no device state
at all, so it is a worthwhile session of its own rather than a step in this one.

### It works: the home screen renders in a browser and takes touch

A cold JIT boot of iPhone OS 1.0 (`1A543a`, whole 215.6 MiB pack staged into
MEMFS, `-icount shift=1`), measured by the page itself. **Measure headless with
throttling disabled** — the first column shows what a watched tab costs:

| landmark | watched tab | headless #1 | headless #2 |
| --- | --- | --- | --- |
| first pixels published and painted | 6.7 s | **1.0 s** | 1.3 s |
| Darwin kernel | 755.0 s | **138.0 s** | 169.0 s |
| BSD root | 815.8 s | **150.0 s** | 188.0 s |
| launchd | 935.0 s | **170.0 s** | 212.0 s |
| **SpringBoard home screen** (45.6% non-black) | 1206.3 s | **268.4 s** | 326.7 s |

**A browser reaches the iPhone OS 1.0 home screen in 270-330 s.** The tab figure
was inflated ~4x purely by throttling; do not quote it.

The two headless runs used a warm and a fresh Chrome profile respectively, but
**the difference between them is run-to-run variance, not cache**: the assets
come from localhost and staging differed by 0.3 s. Two runs 22% apart is the
honest spread, so quote a range, not a figure.

The panel goes to 0% non-black at ~585 s: the guest auto-locks in the browser
exactly as it does natively. A late sample of a healthy run reads as black.

Then, in the page:

- **tapping the Settings icon launched Settings** — `[TOUCH] mouse DOWN at
  (0.856, 0.481)`, which is exactly the panel coordinate clicked (274/320 =
  0.856), followed by the app's own `IOMobileFramebufferUserClient::attach`
  and a fully rendered settings list at 99.9% non-black;
- **Power reached the guest and was serviced**: `[BTN] keycode=25 ... [PMU]
  ONKEY pressed ... nIRQ assert`, then `keycode=153 ... ONKEY released`, then
  the kernel reading and clearing INT1/INT2. keycodes 25/153 are exactly
  `ipod_touch_input_event`'s Power mapping.

**How to reproduce it, and the two bugs that stood in the way.** The page now
posts its landmarks to `scripts/wasm/serve.py --results` (the endpoint Session B
added for `web/bench-b/`) and mirrors them to `console.log`, so a run is driven
headlessly rather than watched:

```sh
scripts/wasm/serve.py --port 8013 --results /tmp/run.json &
"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
  --user-data-dir=/tmp/prof --headless=new --enable-logging=stderr --v=0 \
  --disable-background-timer-throttling \
  --disable-backgrounding-occluded-windows \
  --disable-renderer-backgrounding \
  --disable-features=CalculateNativeWinOcclusion \
  http://localhost:8013/public/jit-boot/
```

What this page contributes that `bench-b` cannot: **SpringBoard is never
announced on serial, so "home screen" can only be established from the
framebuffer** — and only a page that paints can see it. `bench-b`'s landmarks
stop at launchd.

Getting there surfaced **a real race in the paint loop, which the interactive
run had merely got lucky on**:

> `preRun` runs BEFORE the runtime is initialised, so capturing `moduleRef`
> there does not mean it may be called. Emscripten's pre-init exports are
> assert stubs that abort with *"native function `wasm_display_info_addr`
> called before runtime initialization"* — **and they are functions**, so the
> `typeof fn === 'function'` guard sailed straight past them and killed the
> emulator on the first animation frame.

Interactively the first frame happened to land after init and everything
worked. Headless, with the assets staged faster, the frame came first and the
run died ~2 s in, on every frame, while the server showed a perfectly normal
asset fetch. Painting now waits on an `onRuntimeInitialized` flag.

The second bug was mine and mundane — `report()` sent `failure` where the
variable is `failed`, a ReferenceError that would have suppressed every POST on
its own. `node --check` does not catch that; a heartbeat log does. **Do not
swallow errors in a headless page's reporting path**: the original
`.catch(() => {})` left a run looking dead with no way to ask why.

Then, in the page:

- **tapping the Settings icon launched Settings** — `[TOUCH] mouse DOWN at
  (0.856, 0.481)`, which is exactly the panel coordinate clicked (274/320 =
  0.856), followed by the app's own `IOMobileFramebufferUserClient::attach`
  and a fully rendered settings list at 99.9% non-black;
- **Power reached the guest and was serviced**: `[BTN] keycode=25 ... [PMU]
  ONKEY pressed ... nIRQ assert`, then `keycode=153 ... ONKEY released`, then
  the kernel reading and clearing INT1/INT2. keycodes 25/153 are exactly
  `ipod_touch_input_event`'s Power mapping.

**Caveat: 1206 s is an upper bound, not a measurement.** The log contains a
284 s stretch with no new compiles at all, which was first put down to the two
native QEMU probes running alongside. **That attribution was probably wrong.**
Session B hit the identical symptom independently the same afternoon —
"compiled=352 for minutes" — and found the cause: **a browser throttles a
backgrounded or occluded page**, and the freeze is indistinguishable from a
hang. This page was in a pane that was not frontmost for most of the run.

Re-measure with `scripts/wasm/bench-run.py` (Session B's, commit `67782c9d12`),
which launches Chrome with the three throttles disabled. Do not quote 1206 s as
a browser boot time.

### A snapshot LOADS AND PAINTS in the browser in 5.4 s — but does not keep running

**Proven, seen on screen:** `?resume=1` fetches a natively-produced 56.9 MiB
migration stream, stages it into MEMFS, boots with `-incoming file:/fw/state`,
and the page shows the **iPhone OS 1.0 home screen at 5.4 s** — first pixels and
the >40%-non-black landmark both at 5.4 s, 45.5% non-black — against ~250 s for
a cold chunked boot. The resume itself completed at **2.4 s**.

**Not yet usable:** the restored machine does not keep executing. Measured from
the page, without blocking the main thread:

```
run_state = 0        (not running, not paused, not INMIGRATE)
guest_ms delta = 0   over 55 s of wall clock
```

And it is **racy**: the first attempt painted the home screen, later reloads of
the same stream painted nothing at all. So what works today is "the stream loads
and the restored frame is presented", not "the machine resumes".

#### Three things this needed, all of which are findings in their own right

**1. `-incoming` leaves the machine PAUSED, and the page has no monitor.** QEMU
records the source's runstate in the stream and only auto-starts when it says
"running". `ui/wasm.c` therefore exports `wasm_request_resume()`, serviced from
the drain timer on the emulator thread under the BQL. It waits for
`RUN_STATE_PAUSED`, which is exactly the "the incoming stream has finished
loading" edge, since the runstate is `INMIGRATE` until then.

**2. Live migration is IMPOSSIBLE on this machine.** The obvious way to get a
stream recorded as "running" is to migrate a live guest. It aborts:

```
Assertion failed: (block == qemu_get_ram_block(end - 1)),
  tlb_reset_dirty_range_all, physmem.c:872
```

The machine maps main RAM **twice** — `RAM_MEM_BASE` and an uncached alias at
`RAM_MEM_BASE | UNCACHED_MEM_BIT` — so a dirty range spans what that assertion
insists is one block. Two truncated state files were produced before the
assertion was read; the failure surfaces on restore as
`check_section_footer: Read section footer failed`, which points nowhere near the
cause. **Only stop-and-copy works**, hence `--stop-first`.

*A wrong diagnosis worth recording:* the first truncation was attributed to a
session interrupt killing the probe mid-transfer. That was wrong — the assertion
was doing it, and a rerun with no interruption failed identically.

**3. A migration stream is only portable between hosts whose DEVICE SETS match.**
The first browser attempt failed with

```
load of migration failed: Unknown section or instance 'slirp' 0
```

The native build links slirp and registers a `slirp` savevm section; the wasm
build has no slirp at all. Fixed by generating with `-net none` so both sides
agree. **Expect more of these** as either build's device set drifts; the error
names the section, so each one is cheap to fix once looked at.

#### What must be snapshotted, and why it matters more than it sounds

A snapshot of a guest parked in "awaiting Power/Home" restores to a **black
panel** even though it restores correctly: the guest is in WFI with almost no
timers armed, so under `-icount` virtual time has nothing to advance towards.
Natively that recovers, because native runs ~50x faster and gets 30 s of guest
time in the probe's settle window; the browser gets under one second of guest
time in the same wall clock and simply sits there.

Snapshot a **live home screen** instead (`--boot-wait 300`, before the ~260 s
auto-lock, and check the `before` sample really is ~45%). That is also what you
would ship: resuming to a locked device is not an instant boot.

#### Next steps, in order

1. **Make `wasm_run_state()` return the raw runstate index.** "0" currently means
   "none of the three I check for", which is not diagnosable. This is the one
   thing blocking progress.
2. **Find out whether the resume request races the load.** `wasm_request_resume()`
   is called from `onRuntimeInitialized`, which fires during module init — before
   the incoming stream has loaded. The wait on `RUN_STATE_PAUSED` is supposed to
   handle that, but the run-to-run inconsistency suggests it does not always.
3. **Check which timers are armed after a restore.** Only the LCD's refresh timer
   and pl192 migrate; every other device's timer is whatever `realize()` armed.
   If nothing periodic is armed, a halted vCPU plus `-icount` is a permanent
   stall, which is exactly the signature observed.

### A3 started: the LCD now migrates, and a snapshot RESTORES a live panel

First increment of the snapshot work, and the measurement that motivated it is
now inverted:

| | before | with the LCD's VMStateDescription |
| --- | --- | --- |
| scanout after restore | **0.003%** (dead panel) | **45.393%** (live home screen) |
| RAM `0x0f400000` after restore | 59.03% | 59.043% |
| state file | 57.0 MiB | 23.9 MiB |

`hw/arm/ipod_touch_lcd.c` gains a `VMStateDescription` covering the register
block, the panel/input flags and — deliberately — the **refresh timer**, because
a restored machine whose refresh timer never fires again looks exactly like the
dead panel this exercise is about. `post_load` forces `invalidate`, since
`fbsection` is a mapping cached from the last scanout base and dirty tracking
would otherwise report nothing to repaint.

The restored guest is genuinely executing, not showing a frozen frame: 73 KB of
serial, `IOMobileFramebufferUserClient::attach(AppleH1CLCD)`, and a Home press
delivered and processed (`[BTN] keycode=35`/`163`).

#### The restored machine is INTERACTIVE — a tap on a snapshot launches an app

The live panel only proves the LCD came back. `snapshot-probe.py --tap-after`
proves the machine did: it taps a coordinate known to work, after the restore.

```
after_tap: 95.993% (was 45.117%)  interactive=True
```

45.1% (home screen) → 96.0% (an app, filling the panel). That single tap
exercises everything a snapshot has to bring back but a framebuffer cannot show:
the multitouch device, the SPI path, the ATN GPIO in sysic, the interrupt
controller's masks, and the timers driving all of it.

**So the native snapshot path is complete for the instant-boot use case:** boot
once (~400 s), `migrate file:` into **23.9 MiB**, restore, and the machine comes
back live, executing and touch-responsive. 23.9 MiB is the same order as the
18.57 MiB the chunked loader already streams for a cold boot, so the delivery
cost of a snapshot is roughly one extra cold boot's worth of bytes — in exchange
for skipping ~250 s of it.

`hw/intc/pl192.c` also gained a `VMStateDescription` (its class_init had
`//dc->vmsd` and a "TODO save VM"). The priority stack is included, not just the
registers: pl192 pushes the pre-empted priority on every vector read and pops it
on EOI, so a snapshot taken with an interrupt in service restores mid-nesting and
dropping `stack_i` would unbalance the next pop. `post_load` re-runs
`pl192_update()`, because the CPU's own IRQ input is otherwise left low while the
VIC believes it is driving it.

**Honest attribution:** the VIC's vmstate is *defensive*, not proven necessary. A
reset VIC has `intenable == 0`, which would mean no interrupt is ever delivered
again — but the LCD-only run already showed the guest executing and servicing a
Home press, so the guest evidently reprograms enough of it. Nothing here A/B's
pl192 on its own, and the interactive result above was measured with both
devices migrated.

**What is left for the browser** is delivery and plumbing, not device state:
restore from `-incoming file:` inside MEMFS with the state file fetched like any
other asset. That is the next step, and it is the last piece of "full boots from
scratch AND instant resume".

#### Trap: the resume serial log REPLAYS the whole boot, so it looks like a reboot

This nearly cost the conclusion. `serial-resume.log` **begins with the Darwin
kernel banner and 1500 lines of early-boot output**, which reads as "the machine
rebooted instead of resuming" — and a reboot would invalidate everything.

It did not reboot. The guest's kernel message buffer lives in RAM, and the
restored kernel re-emits it to the UART. Two checks settle it in seconds:

- the resume log's first 40 lines are **byte-identical** to the boot log's — a
  replay, not a fresh boot;
- the resume log **ends** with post-home-screen activity (framebuffer clients
  attaching), where a fresh boot would end in early init.

**Read the TAIL of a resume log, not the head.** And confirm with
`query-status`, which reported `paused / running: false` — exactly what
`-incoming file:` should give before `cont`.

### Picking up an engine fix, and making that cheap (2026-07-30)

The 1.0 in-app button work landed a device-model fix (`e79971b5e9`: MBX register
`0x12C` now returns `0x140`, setting the bit `AppleMBX` spins on). Integrating it
into the browser port was one incremental rebuild —
`IT_WASM_MEMORY64_FULL=1 scripts/wasm/build-qemu.sh`, ~30 s, no reconfigure,
because it is a plain source change.

**Verified in the browser, and it reproduces the NATIVE result exactly.**
`?sweep=home` drives the whole round trip from the page: launch an app, press
Home, look for SpringBoard.

| | native (`app-button-probe.py`) | browser (`?sweep=home`) |
| --- | --- | --- |
| open an app | PASS 97.1% | PASS — 45.6% → **94.7%** |
| Home returns | **FAIL 0.00%** | **FAIL** — stays at 96.3% |

So the browser is faithful: **no browser-specific regression, and the remaining
failure is the one `IN_APP_BUTTON_INVESTIGATION.md` already attributes to the MBX
gap (T1) — display/compositing, not the event path.** The browser also
independently confirms that doc's "the guest is idle rather than spinning": the
heartbeat shows `guestRatio` swinging to 0.24–0.69, which is the signature of a
HALTED cpu (icount warps virtual time forward when idle), with the frame counter
frozen. A spinning guest would show a low, steady ratio.

#### The rebuild was easy; the DEPLOY was not, and that is now fixed

Rebuilding was always one command. **Deploying was a manual copy of
`qemu-system-arm.{js,wasm}` into the page directory**, done by hand five times in
one session — and a stale copy silently gives you old-engine results, which is
precisely the class of mistake this port keeps paying for (see the wrong NAND at
the canonical path).

Session B had already solved it and `web/.gitignore` already documented the
intent, so the fix was to adopt the existing convention rather than invent one:
**the viewer's artifacts are now symlinks**, exactly as `web/bench-b/`'s are.

```
web/public/jit-boot/qemu-system-arm.js   -> ../../../build-wasm/qemu-system-arm.js
web/public/jit-boot/qemu-system-arm.wasm -> ../../../build-wasm/qemu-system-arm.wasm
web/public/jit-boot/bootrom              -> ../../../m68ap-artifacts/shared/bootrom_s5l8900
web/public/jit-boot/iboot.bin            -> ../../../m68ap-artifacts/builds/1A543a/iboot-sb.bin
web/public/jit-boot/nor.bin              -> ../../../m68ap-artifacts/builds/1A543a/nor.bin
```

A rebuild is now live in the page immediately, with no copy step and no way to
serve a stale engine. The `.gitignore` rules already cover these paths.

`web/public/jit-boot/nand.pack` (216 MiB) stays only because `web/bench-b/`
fetches it; the viewer no longer reads it.

**Still not automatic, and worth knowing:** a change to
`configs/meson/emscripten.txt` needs `--configure`, because meson reads a cross
file's `[built-in options]` only at configure time. A plain rebuild is a silent
no-op for those flags.

### Chunked delivery merged into the viewer — 18.57 MiB cold, 0 warm

The last join between the two parallel sessions: the page that *paints* was
still staging the whole 215 MiB pack, while the page that *streamed chunks* did
not paint. Session B's brief for this is
[`BROWSER_WASM_CHUNKED_IN_THE_VIEWER.md`](BROWSER_WASM_CHUNKED_IN_THE_VIEWER.md);
nothing in B's files needed changing.

Measured in the viewer, headless with throttles off, verified **by bytes at the
server** (`/__chunk-stats`) rather than by what the page believed:

| | cold cache | warm cache | whole pack (best prior) |
| --- | --- | --- | --- |
| download | **18.57 MiB** | **0 bytes** | 215.6 MiB |
| chunk requests | 504 | 0 (1137/1137 cache hits) | — |
| first pixels | 0.8 s | 0.5 s | 1.0 s |
| kernel | 156 s | 148 s | 138 s |
| launchd | 195 s | 186 s | 170 s |
| **home screen** (45.6% non-black) | **249.5 s** | **241.0 s** | 268 s |

**Chunked is not a trade-off here — it is faster AND 11.6x smaller.** 18.57 MiB
matches B's independently measured figure exactly, which is the cross-check that
matters: two different pages, same seam, same bytes.

Four things the brief was right to spell out, all load-bearing:

- **The service worker must be CONTROLLING the page before the Module exists**,
  or the first chunk requests bypass the cache.
- **The PAGE owns the fetch worker**, not the emulator's thread — a nested
  dedicated worker is serviced through its parent's context, and that parent is
  the thread blocked in `Atomics.wait`.
- **`overlay=ram` in `<nand>/nand-tune` is mandatory.** Without an overlay the
  boot stalls after launchd for ever, and the file-backed writable mode is
  *worse* than useless under `-sPROXY_TO_PTHREAD`: every MEMFS syscall is
  proxied to the main thread, so the per-read `stat()` becomes a cross-thread
  round trip.
- **Await the prefetch.** A demand-faulted chunk costs the guest a full round
  trip while it blocks.

**One bug, mine, and it is the kind worth naming:** the page reported
`chunks=0.00MiB` while the server had shipped 18.5 MiB, because `chunkStats()`
returns `{hits, misses, bytesFromNetwork, bytesServed}` and the page read a
`bytes` field that does not exist — silently, via `??`. Two lessons: an optional
-chaining default will happily report zero forever, and **`bytesFromNetwork` is
the field that answers "what did this visit cost"** — `bytesServed` counts cache
hits, so a warm boot reported through it looks like it downloaded the lot.

The whole-pack path is gone from the viewer, but `web/public/jit-boot/nand.pack`
must stay on disk: `web/bench-b/` still fetches it.

### Guest time is far slower than wall time, and that breaks normal taps

The first real click on an icon did nothing, and the log said why:

```
[1515.8s] [TOUCH] mouse DOWN at (0.856, 0.481)
[1515.8s] [TOUCH] mouse UP   at (0.856, 0.481)
```

**Same guest timestamp.** A ~100 ms wall-clock click is, at these speeds, a
single guest instant: the drain timer dispatches the down and the up
back-to-back with no guest execution in between, so SpringBoard never observes
a finger. An 800 ms hold produced ~1.5 s of *guest* separation and launched the
app immediately.

The page therefore floors every release at `MIN_PRESS_MS` (800 ms) — for taps
and for the Home/Power buttons alike. **This is a browser-speed workaround, not
a fidelity question, and it should shrink as Session B's work lands.** The value
is conservative rather than measured; the correction below explains why, and it
is worth reading before trusting any number in this area.

#### Sizing the hold: six runs, three conclusions, two of them wrong

Kept in full because the two wrong conclusions were each *reasonable on the
evidence available*, and the thing that finally separated them was not another
run — it was cross-tabulating the runs already done on a variable nobody was
tracking.

| # | ladder (ms) | gate wait | outcome |
| --- | --- | --- | --- |
| A | 2–256 **guest** ms, ascending | fixed 10 s | **INVALID** — panel auto-locked from rung 4; conversions nonsense |
| B | 30, 60, 120, 250, 500 | fixed 10 s | rung 5 (500 ms @ y=157) launched |
| C | 280, 320, 360, 400, 450 | fixed 10 s | rung 5 (450 ms @ y=157) launched |
| D | 800 via the real pointer path @ y=67 | after gate | **failed** |
| E | 30, 60, 120, 250, 500 | after gate message | rung 5 (500 ms @ y=157) launched |
| F | **500, 250, 120, 60, 30** (descending) | after gate message | **all failed**, including 30 ms @ y=157 |

**Wrong conclusion 1 — "the threshold is ~450 ms, derived by sweep."** B and C
agreed on ~450 ms, so it was written up as measured. But they also agreed on
something else: both succeeded at rung *index* 5.

**Wrong conclusion 2 — "it is a clock, not a threshold."** Three runs (B, C, E)
succeeding at index 5 with holds of 500, 450 and 500 looked conclusive, and
there was a real mechanism to hang it on (see the gate below). Run F killed it:
run the ladder **descending** and rung 5 is 30 ms — and it failed. Elapsed time
cannot explain that.

**The right conclusion came from re-tabulating every run by TARGET, not hold:**

| target | holds tried | launches |
| --- | --- | --- |
| **y=67** (icon row 1) | 30, 60, 120, 250, 280, 320, 360, 400, 500, 800 | **0 / 10** |
| y=157 (row 2) | 450, 500, 500 | **3 / 3** |
| y=157 (row 2) | 30 | 0 / 1 |
| y=249 (row 3) | ~700 (interactive) | 1 / 1 |

Two independent effects, and the sweep confounded them:

1. **Icon row 1 never registers, at any hold** — 10 attempts across 4 runs.
   This is a device-model finding, not a browser one; it is written up in
   [`TOUCH_INVESTIGATION.md`](TOUCH_INVESTIGATION.md).
2. **The hold does matter** where taps work at all: y=157 launches at 450/500 ms
   (3/3) and not at 30 ms (0/1). **The threshold is somewhere in (30, 450] ms**
   — a wide bracket, and that is all the data supports.

Every ascending ladder put rungs 1–4 on y=67 and rung 5 on y=157, so "rung 5
wins" was never a clock: **rung 5 was simply the first rung aimed somewhere
that works.** Run D's 800 ms "failure" is the same artifact — it was aimed at
y=67.

**The methodological error is the transferable part.** Each rung deliberately
used a *different icon*, so that a hold too short to register left SpringBoard
untouched and failures stayed free and repeatable. That was a good idea for
isolation and a bad one for attribution: it varied **two** things per rung.
Vary one; if the design forces two, cross-tabulate before concluding.

`MIN_PRESS_MS` is **800 ms** — above the (30, 450] bracket, with margin for the
twofold run-to-run spread in engine speed. Now genuinely supported, but still
not a measured threshold: narrowing it needs a ladder that holds the target
fixed at a position known to work.

#### The gate is real, even though it did not explain the sweep

Found while chasing wrong conclusion 2, and worth keeping regardless:
`lcd_update_input_ready()` refuses **all** touch until it has seen
`2 * LCD_REFRESH_RATE_FREQUENCY` frames of a stable OS image — **two seconds of
GUEST time**, which at a ratio of ~0.02 is ~100 s of wall clock. Waiting a fixed
wall time before tapping is therefore a coin flip.

The page now mirrors `[LCD] Touch input ready` and every `[TOUCH]` verdict to the
console and blocks the sweep on that message. **Do this even though the gate was
not the confound** — it removes a genuine source of false failures, and the
`[TOUCH]` mirror is what makes a refused touch distinguishable from an
unregistered one at all.

#### Two dead ends in the sweep harness itself

**Do not express the hold in GUEST milliseconds.** Run A swept guest holds and
converted through `guestRatio`, producing 4 ms guest → 10 ms wall alongside
256 ms guest → 4113 ms wall. **With `-icount` QEMU warps virtual time forward
whenever the CPU idles**, and an idle home screen is exactly where the sweep
runs. The ratio is worth reporting; it is not worth steering by. The constant is
a wall-clock quantity — sweep it in wall time.

**The ladder must finish inside the auto-lock window.** Run A used eight rungs
at 45 s each; the guest locked after roughly 260 s of idle and the last five
rungs tapped a dead panel, every one reading as a clean failure. The sweep now
aborts and reports `invalid` as soon as the panel drops below 5% non-black.

#### A browser boot can wedge outright

The first attempt at run F never reached the ladder: **`guestRatio` went to
0.0000** — guest virtual time not advancing at all — about 1170 s in, having
passed BSD root but never launchd. A second attempt booted cleanly in 403 s. So
this is intermittent, and the 15 s heartbeat is what distinguishes it from
"still working"; without that the page just sits there.

### The real-time ratio, and why it cannot be trusted at idle

`ui/wasm.c` now publishes `QEMU_CLOCK_VIRTUAL` in milliseconds alongside the
display geometry, and the page divides its delta by the wall-clock delta. This
is the metric the handoff asks for: **guest seconds per wall second**, where 1.0
would mean the guest experiences time as the hardware did. With `-icount`, boot
DURATION and guest SPEED are different quantities, and a boot time alone cannot
tell a fast engine from a throttled tab.

Measured on 1.0: **0.015–0.022 while booting** (the CPU is genuinely busy), i.e.
the guest runs at roughly **2% of real time**, ~50× slower than the hardware.

**The idle figure is not comparable and must not be quoted.** At the home screen
the sampler reports 0.036 and swings far higher, because QEMU warps virtual time
forward when all CPUs are halted — the guest is not running faster, it is
skipping. Only a busy-CPU sample means anything. Published outside the seqlock
as a single `u32` store, so polling it does not force a repaint.

### Home from inside an app does nothing on 1.0 — and that is NOT this code

The one unmet acceptance criterion. It is the **already-diagnosed 1.0 event
ROUTING bug** (commit `5f019eac7f`, measured natively on this branch the same
day): on 1.0 the handler runs when SpringBoard is frontmost and stops running
when an app is, while 1.1.4 works from inside an app. The GPIO pin, the IRQ,
INTEN, the interrupt-controller ACK, `AppleM68Buttons` and SpringBoard's
handlers were all exonerated there by measurement.

The browser evidence agrees and adds nothing new: the button path is proven to
reach the guest and be serviced (the Power trace above), so nothing in the
wasm input bridge is implicated. **Do not re-investigate this from the browser
side.**

### A3 — a snapshot restores RAM perfectly and NO device state

`scripts/wasm/snapshot-probe.py` boots 1.0 natively, samples, migrates to a
file, restores in a fresh process and samples again — by framebuffer, because
SpringBoard never announces itself.

`migrate file:` works mechanically: `status: completed`, 475 ms, a **57.0 MiB**
state file for 128 MiB of guest RAM (23.9 MiB on a second, less-dirty run).

And the result is exactly the failure the zero-VMState survey predicted:

| | before | after restore |
| --- | --- | --- |
| **scanout** | **45.4%** | **0.003%** |
| RAM `0x0fe00000` | 59.03% | 59.03% |
| RAM `0x0f400000` | 59.03% | 59.03% |
| RAM `0x0f496000` | 59.041% | 59.041% |

**Guest RAM comes back byte-identical — the rendered home screen survives — and
the scanout is dead**, because `w1_framebuffer_base` came back as zero. Not one
of the 26 `hw/arm/ipod_touch*.c` device models defines a `VMStateDescription`,
so migration saves RAM and the CPU and nothing else: no LCD window bases, no
PMU, no VIC, no FTL controller, no multitouch.

**Verdict: A3 is worth doing and is NOT a quick win.** The prize is real —
24-57 MiB of state replaces a ~20 minute browser boot, which is the same order
as the 18.6 MiB chunked first-boot working set already measured for 1.0. But it
needs VMState descriptors written for the device models, which is device-model
work of its own and overlaps files Session B holds. It is a session, not a step.

Two notes for whoever takes it:

- **Start with the LCD.** The scanout base is provably lost and provably
  sufficient to make a restore look completely dead. The VIC is the next
  suspect (a lost mask means no interrupts ever fire again).
- **Snapshot an AWAKE machine.** Left alone the guest auto-locks and the PMU
  powers the panel off, so a long `--boot-wait` measures a sleeping device at
  ~0% non-black and the comparison becomes meaningless. `--no-wake` turns off
  the Home press that avoids this. Note that on a device already parked in
  "awaiting Power/Home", a QMP `send-key h` did **not** wake it within 6 s.

### The display seam as committed could never have worked

`ui/wasm.c` published `Module.qemuDisplay` from an `EM_JS` body. Under
`-sPROXY_TO_PTHREAD=1` — which `configs/meson/emscripten.txt` sets — `main()`
runs on a *worker*, and an `EM_JS` body executed there sees **that worker's**
`Module`, a different JavaScript object from the page's. Nothing assigned to it
is visible to the page, ever.

It had never been exercised: the committed `jit-boot` page ran `-display none`,
so the backend was registered-but-unused and the defect was invisible.

Replaced with a seam that does not care which thread is asking:

- a static `WasmDisplayInfo` struct in the wasm heap, all `u32` fields so JS can
  read it out of `HEAPU32` with no layout guesswork (the surface pointer is
  split into two words because a wasm64 pointer does not fit in one);
- `EMSCRIPTEN_KEEPALIVE uint32_t wasm_display_info_addr(void)` returning its
  address. **Exported wasm functions are callable from any thread holding the
  instance, and every thread addresses the same shared memory** — which is the
  property `EM_JS` lacks.

`seq` is a **seqlock** (odd while the emulator thread is mid-update), not a bare
counter. This is not fastidiousness: a torn *pointer* read is not a torn frame,
it is an arbitrary index into `HEAPU8`.

**Generalise this.** Anything that has to reach the page from the emulator
thread has the same problem, and the same answer: put it in memory and export
an accessor. `EM_JS` is only safe for code that runs on the thread that owns the
`Module` you mean.

### The heap views are not exported — and touching one kills the emulator

First run with `-display wasm` died 2.2 s in:

```
Aborted('HEAPU32' was not exported. add it to EXPORTED_RUNTIME_METHODS)
```

`Module.HEAPU8`, `HEAPU32`, `wasmMemory` and `wasmExports` are all **absent, and
absent in the worst way**: the property is a stub that calls `abort()`, tearing
down the runtime and taking the emulator with it. A missing display feature
killed a boot.

Two fixes, both needed:

1. `configs/meson/emscripten.txt` now exports `HEAPU8,HEAPU32` alongside
   `addFunction,removeFunction,TTY,FS`.
2. **The page reaches every `Module` property through a try/catch** and switches
   painting off after the first failure. Presentation is a nice-to-have; it must
   never be able to end a run.

**Trap worth its own line: meson reads a cross file's `[built-in options]` only
at CONFIGURE time.** Editing `emscripten.txt` and rebuilding is a silent no-op —
ninja's own regenerate does not re-read them, and the link succeeds with the old
flags. It needs `build-qemu.sh --configure`.

### Pixel format: `x8r8g8b8`, read from the source rather than diagnosed

`draw_line32_32()` in `hw/arm/ipod_touch_lcd.c` stores
`rgb_to_pixel32(r, g, b)` = `(r<<16)|(g<<8)|b` as a native `u32`, so memory
holds **B, G, R, X** while `ImageData` wants **R, G, B, A**. The page does one
`u32` shuffle per pixel:

```js
dst = 0xff000000 | ((p & 0xff) << 16) | (p & 0xff00) | ((p >>> 16) & 0xff);
```

The handoff predicted "if the panel comes out blue, this is why". It never came
out blue, because the conversion was written from the source before the first
run. Recorded because the cheap move (read the drawing function) beat the
expensive one (recognise the symptom).

### Input: an SPSC ring drained by a QEMU timer

QEMU's input queue belongs to the emulator thread and expects the BQL, so
nothing may call into `ui/input.c` from JS. The exported entry points
(`wasm_input_touch`, `wasm_input_button`) **only write a slot** in a
single-producer/single-consumer ring; a 15 ms `QEMU_CLOCK_REALTIME` timer on the
emulator thread drains it and dispatches.

- **`dpy_refresh` was the tempting free drain point and was rejected.** It
  already runs on the right thread at roughly the right rate, but QEMU throttles
  the display refresh interval when a console looks idle — input latency would
  then depend on how much the guest happens to be drawing.
- The page speaks **buttons** (`home`/`power`), not QKeyCodes, so the
  `Q_KEY_CODE_H` / `Q_KEY_CODE_P` mapping stays in C where the enum lives.
- Touch is sent in the panel's own 320x480 coordinates and
  `qemu_input_queue_abs` rescales; the **multitouch model is what flips Y**
  (`fy = 1 - y/2^15`), exactly as for a native display, so the page must not
  pre-flip.

### The damage rectangle needed an ack to mean anything

As written the union only ever grew: `wasm_gfx_update` accumulated into one
rectangle with nothing to say when accumulation could restart, so within a
second it was permanently full-screen. The struct now carries an `ack` field —
the only field written by the page — holding the seq it last painted. The page
repaints in full and ignores the rectangle, but a partial-blit consumer can now
exist.

### The shared working tree is a live hazard between the two sessions

Both sessions edit one checkout, and B's in-flight work broke A's build twice
(first `no member named 'pack_entry_count'`, then undefined `it_nand_pack_*`
symbols) — unavoidable, and cheap to wait out.

**What was not cheap:** B editing `hw/arm/meson.build` made ninja auto-regenerate,
and *that* enabled curl, zstd and libssh for a WebAssembly build — the disaster
this repo documents as "never run meson/ninja by hand", reached **without anyone
running meson by hand**.

Root cause, and it is a real bug in our tooling:

> `build-qemu.sh` exported `PKG_CONFIG_PATH`, which only **prepends** to
> pkg-config's search path. The initial configure was clean only because
> `emconfigure` sets `PKG_CONFIG_LIBDIR`, which **replaces** it. Ninja's
> `meson --internal regenerate` escapes `emconfigure` entirely, so pkg-config
> fell back to its built-in path and found `/opt/homebrew`.

`build-qemu.sh` now exports `PKG_CONFIG_LIBDIR` itself. Verified: a regenerate
after the fix reports `libcurl found: NO`, `libssh found: NO`, `libzstd found:
NO`.

**This retires "never run meson/ninja by hand" as sufficient advice.** Ninja
runs meson on its own, whenever any `meson.build` changes — which, with two
sessions in one tree, is constantly.

---

## Session — 2026-07-29 (Session B): fast and small

Parallel session B (`BROWSER_WASM_SESSION_B.md`): JIT tuning, the pack-access
seam, and chunked delivery. Builds in `build-wasm-b/`, serves on port 8011, and
does not touch `ui/wasm.c` or `web/public/*/index.html`.

### The JIT threshold is now tunable at RUN TIME, so a sweep is three page loads

`INSTANTIATE_NUM` was a `#define`, which made every point in a sweep a full
wasm rebuild (~15 min) before a ~15 min boot. It is now `jit_instantiate_num`,
resolved once in `init_wasm()` from, in order:

1. `IT_WASM_INSTANTIATE_NUM` — native and Node;
2. `instantiate=<n>` in **`/fw/jit-tune`** — the browser.

The second exists because Emscripten's `ENV` object is not in
`EXPORTED_RUNTIME_METHODS`, so a page **cannot set an environment variable** —
but it can write a file into MEMFS before startup. `max_instances=<n>` rides
along in the same file and is clamped to the compile-time `MAX_INSTANCES`,
which still sizes a static array. Defaults are unchanged (100 / 48000), and the
resolved values are printed once: `[JIT] tuning: instantiate=… max_instances=…`.

`web/bench-b/index.html` turns those into URL parameters
(`?instantiate=50&max=48000`), alongside the landmark table and the `[JIT]`
counters.

### The pack-access seam (B2) — landed, with golden fixtures

`nand_read_packed_page()` used to binary-search a `GMappedFile` and `memcpy`
straight out of it. That lookup now lives in **`hw/arm/ipod_touch_nand_pack.c`**
behind `it_nand_pack_record(pack, vpn)`, with two sources:

| source | opened from | used by |
| --- | --- | --- |
| mapped | `nand.pack` (whole file) | native — still the oracle |
| chunked | `nand.pack.idx` (header + index) + a chunk fetcher | the browser |

The module deliberately has **no device-model dependencies** (osdep + glib), so
`tests/unit/test-nand-pack.c` links it directly. Four tests, all passing:
mapped output matches committed SHA-256 digests; chunked and mapped agree
byte-for-byte on every page and both return NULL on every absent page; a page
whose chunk is not resident reads as absent rather than as neighbouring data;
and malformed packs (short, bad magic, wrong version, **unsorted index**) are
rejected at open rather than misread at lookup.

The fixture is generated by `scripts/wasm/make-pack-fixture.py`
(`--check` verifies it is current) into `tests/data/nand-pack/`: 24 pages,
50,804 B, real 2,112-byte stride.

**Two traps found writing it**, both worth keeping:

- The absent-page list has to be *derived*, not guessed. VPN = `page * 8 +
  bank`, so a fixture holding page 0 of banks 0/3/7 occupies VPNs 0, 3 and 7 —
  the first hand-written "absent" list named pages the pack actually carried.
- A chunk source that returned a pointer into the same mapping would make the
  comparison vacuous, so the test's cache **copies** each chunk into its own
  buffer and asserts the two pointers differ.

**Native is unchanged and still reaches the home screen**: after the refactor,
`fb-snapshot.py --board m68ap --build 1A543a` reports `kernel_0x0f496000` at
**59.02% non-black** — the documented figure for 1.0 is 59.04%.

### The JIT sweep: the threshold is not what costs 10 minutes — the CAP is

Five pack-mode boots, one at a time, `-icount shift=1`, times in seconds from
page load (iPhone OS 1.0's iBoot-159 prints nothing, so the first landmark is
the kernel):

| threshold | kernel | BSD root | launchd | compiled | recompiled | evicted | live |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 50 | 116 | 127 | **147** | 31,888 | 0 | 0 | 31,888 |
| 100 (run 1) | 107 | 118 | **692** | 138,992 | 5,218 | 96,000 | 42,992 |
| 100 (run 2) | 114 | 125 | **736** | 127,920 | 4,923 | 96,000 | 31,920 |
| 100 (run 3) | 115 | 125 | **143** | 23,376 | 0 | 0 | 23,376 |
| 300 | 114 | 126 | **141** | 13,744 | 0 | 0 | 13,744 |

**Every run reaches BSD root within 118–127 s.** The 5× spread is entirely in
the launchd phase, and it separates cleanly on one thing: whether the run
saturated `MAX_INSTANCES`. The slow runs hit 48,000 live instances, evicted
96,000 and recompiled ~5,000; the fast runs never came within half the cap.

**The threshold value does not predict which regime a run enters.** Two runs at
100 went slow, a third at the same setting did not, and 50 and 300 — on either
side of it — were both fast. So the earlier standing advice ("treat the two
constants as a pair") holds, but the useful form of it is sharper:

> **The tuning goal is not a number, it is staying off the cap.** A run that
> saturates pays ~10 minutes for it; a run that does not is insensitive to the
> threshold across a 6× range.

This is also a correction to how the previous session read its own data: at
threshold 100 it recorded kernel 524.6 s / BSD root 584.7 s / launchd 791.0 s
and attributed the improvement to the threshold. With the current build the
same setting reaches the kernel in **107–115 s** — nearly 5× faster — so those
figures measured something else as well (the build has moved a long way since).

### The adaptive threshold works, and it is the fix

`adaptive=1` scales the compile threshold by cap pressure: base while under
50% of `MAX_INSTANCES`, ×4 to 80%, ×16 beyond. Eager compilation for a boot's
long cold tail, but the last slots go only to genuinely hot blocks.

| run | kernel | BSD root | launchd | live at launchd | evicted |
| --- | --- | --- | --- | --- | --- |
| static 100 | 115 | 125 | 143 | 23,376 | 0 |
| **adaptive, base 100** | 114 | 124 | **143** | 24,144 (50% of cap) | **0** |

Identical boot time, and the counters show the mechanism working: the threshold
had escalated to 400 by the end, and the run stopped at exactly the pressure
step. It cannot enter the saturation regime that cost the two slow runs above,
because approaching the cap is precisely what makes it stop compiling marginal
blocks.

The knob is off by default and lives in the same `/fw/jit-tune` file
(`?adaptive=1` in the bench page). Two adaptive runs: launchd at **143 s** and
**146 s**.

**It delays saturation rather than abolishing it.** The second adaptive run was
left going past launchd and did reach 48,000 live instances at 437 s — but with
the threshold escalated to **1600**, and still **zero evictions and zero
recompiles**, which is the whole difference from the churn regime. A long
session will still arrive at the cap eventually, so the reclaim path (or a
post-boot snapshot that skips the boot's cold tail entirely) remains the real
answer for app use.

### Chunked delivery (B3) — the pieces

- **`scripts/wasm/chunk-pack.py`** writes the `ipod-nand-chunks-v1` asset set:
  `nand.pack.idx`, `chunk-hashes.bin` (32 B per chunk, in chunk order),
  `chunk-config.txt` (what the *emulator* reads — two integers and a string
  need no JSON parser inside a device model), `chunk-manifest.json` (what the
  *loader* reads), and `chunks/<sha256>` stored **already Brotli-compressed**.
- **Chunks are served with `Content-Encoding: br`**, so the browser
  decompresses them in the network stack and **no Brotli decoder is linked into
  the emulator**. `scripts/wasm/serve.py` does this; a CDN does it natively.
- **`hw/arm/ipod_touch_nand_chunks.c`** (EMSCRIPTEN-only) fetches a chunk with a
  **synchronous `XMLHttpRequest`** — legal on a worker thread, which is where
  `-sPROXY_TO_PTHREAD` puts the emulator — into a 64-slot LRU (~8 MiB at 62
  pages/chunk). The emulator never awaits, so QEMU's MMIO path is untouched.
- **`web/sw.js`** intercepts those requests, answers them from Cache Storage,
  prefetches the recorded boot order, and counts bytes.
- **Selection needs no configuration**: a chunked NAND directory has no
  `nand.pack` at all, so the presence of `chunk-config.txt` +
  `chunk-hashes.bin` is what selects the browser path. A native tree can never
  take it by accident.
- `serve.py` also answers **`/__chunk-stats`**, so "a cold boot downloads
  18.6 MiB, a warm boot downloads nothing" is measured by the *server* rather
  than by the page under test.

### Measured: a cold boot downloads 18.52 MiB, a warm boot downloads NOTHING

The 1.0 asset set (`scripts/wasm/chunk-pack.py`, 62 pages/chunk, brotli q11):

| | |
| --- | --- |
| pack | 215.2 MiB, 106,858 pages |
| chunks | 1,724, of which **1,320 unique — 23.4% deduplicated** |
| stored, whole set | 68.8 MiB (32.0%) |
| **prefetch (boot working set)** | **512 chunks = 18.5 MiB** |
| index, always downloaded | 0.41 MiB |

Both browser boots, byte counts from the **server**:

| run | chunk requests | on the wire | staged into MEMFS |
| --- | --- | --- | --- |
| cold | 503 | **18.52 MiB** | 1.7 MiB (index + config + firmware) |
| warm | **0** | **0.00 MiB** | 1.7 MiB |

Against 216.8 MiB staged by the whole-pack page — **a 12× reduction on first
visit and everything on the second.** The service worker reported 512 cache
hits and 0 misses on the warm run.

**The guest really is reading through the chunks**, not from a stashed pack:
the cold run reached the kernel and BSD root, and the emulator's own counter
reports `fetched=320 hits=11095 resident<=64 bytes=39.9 MiB` — a **97% hit rate
in the 64-slot LRU**, and 39.9 MiB of records consumed for 18.52 MiB
downloaded, which is the read amplification the chunk size trades away.

### 1.1.4 does it too — both shipping versions boot in a browser

Same pipeline, same defaults, iPhone OS **1.1.4** (`4A102`) beside **1.0**
(`1A543a`), cold cache, chunked:

| | 1.0 (`1A543a`) | 1.1.4 (`4A102`) |
| --- | --- | --- |
| pack | 215.2 MiB, 106,858 pages | 299.7 MiB, 148,812 pages |
| chunks (62 pages) | 1,724, 1,320 unique (23.4% dedup) | 2,401, 1,998 unique (16.8% dedup) |
| stored, whole set | 68.8 MiB (32.0%) | 102.3 MiB (34.1%) |
| index, always downloaded | 0.41 MiB | 0.57 MiB |
| cold boot touches | 20,507 pages, 512 chunks | 25,136 pages, 600 chunks |
| first pixels | 12 s | 16 s |
| iBoot banner | never printed (iBoot-159) | 31 s |
| kernel | 156 s | 110 s |
| BSD root | 169 s | 130 s |
| launchd | — | 149 s |
| **home screen** | **252 s** | **296 s** |
| non-black, settled | 45.5% (59.04% native) | **69.4%** (73.96% native) |
| **downloaded, cold** | **18.57 MiB** | **20.97 MiB** |
| **warm boot** | 0 requests, **0 bytes** | 2 requests, **71 KiB** |
| home screen, warm | — | **196 s** (100 s faster: no prefetch in the way) |
| guest time / wall | 32.5 s / 315 s | 15.5 s / 359 s |

1.1.4's warm run pulling **two** chunks rather than none is not a cache miss to
chase: a boot does not touch exactly the same page set twice, so a couple of
chunks outside the recorded prefetch list is the expected shape. 71 KiB against
299.7 MiB.

**1.1.4's own iBoot prints a banner and 1.0's does not**, which is worth
remembering when a 1.0 run looks silent for two minutes — it is supposed to.

The one thing that had to change for 1.1.4 was **not** in the browser at all:

> `scripts/fb-snapshot.py` never passed `-icount`, and 1.1.4 needs it. The
> first native verification boot panicked in `IOIpodUSBDevice::start` and the
> USB wrangler (`caller 0xC00638CC`) and rendered **nothing** — kernel
> framebuffer 0.0%, which reads exactly like "this NAND is broken". With
> `--icount 1` the same NAND reports **73.96% non-black** and zero panics.

That is the third independent confirmation of the icount rule (browser, the
shipped app's intermittent Apple-logo hang, and now the verification tool), and
the trap it sets is the worst kind: the tool that exists to tell you whether a
NAND is good was quietly answering a different question.

The per-build **security epoch** travels with the build in the bench page
(`1A543a` → 0, `4A102` → 3): getting it wrong wedges iBoot with an empty serial
log that looks like a hang.

### THE HOME SCREEN, IN A BROWSER, FROM CHUNKED ASSETS

A cold browser boot of iPhone OS 1.0 now reaches the **SpringBoard home
screen**, with the NAND arriving as 130,944-byte chunks over the network:

| landmark | wall clock |
| --- | --- |
| first pixels | 12 s |
| Darwin kernel | 156 s |
| BSD root | 169 s |
| **home screen** | **252 s** |

Verified the way this repo always verifies it — by pixels, not by serial:
**45.5% non-black** in the kernel framebuffer after settling, against 59.04%
natively and ~1.6% for the Apple logo. 504 chunk requests, **18.57 MiB** on the
wire, for a 215.6 MiB pack.

The emulator's own chunk cache reports `fetched=768 hits=23310` — a **97% hit
rate** in the 64-slot LRU, 95.8 MiB of records consumed for 18.6 MiB
downloaded, which is the read amplification the chunk size trades away.

**Speed, honestly:** 32.5 s of guest virtual time in 315 s of wall clock, so
the guest runs at **~10% of real time** at `-icount shift=1`. That is the number
the real-time goal has to move, and it is measured now rather than inferred.

**This run is what the defaults now do.** The adaptive threshold was switched on
by default on the strength of it — the same boot, same build, differing only in
that setting:

| threshold | home screen | evicted | recompiled |
| --- | --- | --- | --- |
| static 100 | 290 s | 72,000 | 4,557 |
| **adaptive (default)** | **252 s** | **0** | **0** |

`adaptive=0` in `/fw/jit-tune` pins the old behaviour. Session A's page writes
no tune file, so it gets the adaptive default too — measured better in
everything tried, and it cannot enter the eviction-churn regime that cost two
runs ten minutes each.

### Two things the full boot needed, and one that had to be measured to believe

**1. A copy-on-write overlay — in RAM, not in MEMFS.** A read-only NAND never
reaches SpringBoard: daemons that must create state spin for ever. The
file-backed writable mode cannot serve the browser either, and the reason is
worth remembering: under `-sPROXY_TO_PTHREAD` **every MEMFS syscall is proxied
to the main thread**, so the `stat()` this model does before each page read
becomes a cross-thread round trip. Measured, chunked, otherwise identical:

| NAND mode | outcome |
| --- | --- |
| read-only | stalls after launchd (documented, and still true) |
| file-backed writable in MEMFS | **stalls outright** — 816 blocks compiled in 181 s, no landmarks |
| `overlay=ram` | kernel 118 s, BSD root 129 s, launchd 144 s |

`overlay=ram` in `<nand>/nand-tune` keeps written pages in a hash table
(2,112 B each, a few tens of MB across a boot) that the read path prefers over
the pack. It is W6's core in its smallest working form — session-lifetime, not
persistent — and it also makes the `bank0..bank7` directories stop being
load-bearing for a packed NAND.

**2. A framebuffer probe inside the emulator**
(`hw/arm/ipod_touch_fb_probe.c`). SpringBoard announces nothing, so the home
screen has to be seen. The probe samples the same three bases
`scripts/fb-snapshot.py` does, on a QEMU timer — i.e. on the emulator's own
thread, because `cpu_physical_memory_read()` from the page's thread would be an
unlocked access from outside QEMU's world — and publishes the percentages under
a seqlock for the page to read.

**3. Do not verify through the display backend.** The obvious approach —
`-display wasm` and count pixels in the surface — **changes what it measures**:

| | kernel banner | launchd |
| --- | --- | --- |
| `-display wasm` | 276 s | not reached in 1,000 s |
| `-display none` + probe | 118 s | 144 s |

Painting is Session A's business and belongs in their page; a measurement run
should not pay for it.

### SOLVED: the chunked boot runs in Chrome — futex + a PAGE-OWNED worker

The synchronous-read blocker described below is fixed. The emulator now writes a request
into a mailbox in its own heap and sleeps on it with `emscripten_futex_wait`;
**`web/chunk-fetch-worker.js`, created by the page**, watches that mailbox with
`Atomics.waitAsync`, fetches the chunk, writes it straight into the wasm heap
and wakes the emulator. Nothing goes through `postMessage`, and the emulator
still never awaits — it blocks — so QEMU's MMIO path is untouched.

Measured in **standalone Chrome 149**, which is where every earlier attempt
failed:

| run | kernel | BSD root | chunk requests | on the wire |
| --- | --- | --- | --- | --- |
| chunked, demand only (no prefetch, no SW) | 113 | 124 | 63 | 4.2 MB |
| chunked, warm (SW cache) | 116 | 128 | **0** | **0** |
| whole-pack, for comparison | 114 | 125 | — | 216.8 MiB staged |

**Chunked delivery costs nothing in boot time** — the landmarks match the
whole-pack run within noise — and a boot that only demand-faults reaches BSD
root having pulled **4.2 MB**.

**Two arrangements were tried and rejected first, and both failed quietly:**

| arrangement | what happens |
| --- | --- |
| synchronous XHR on the emulator's thread | `NetworkError: Failed to execute 'send'`, request never leaves the browser |
| `emscripten_fetch(SYNCHRONOUS)` | returns **zero bytes, no error** — its backend is that same XHR |
| the emulator's thread creating the fetch worker itself | fetch never completes; 30 s timeout |

That last one is the subtle one and it cost the most: **a nested dedicated
worker is serviced through its parent's context**, and this parent spends its
life blocked in `Atomics.wait`, so its own fetcher can never run. The page has
to own the worker.

Getting the mailbox address to the page needs an **exported function**
(`it_nand_chunk_mailbox_addr`, `EMSCRIPTEN_KEEPALIVE`), for exactly the reason
Session A found with the display: an `EM_JS` body runs on the calling thread and
sees *that* thread's `Module`, which under `-sPROXY_TO_PTHREAD` is never the
page's.

### The sync-read hunt, in order — every attempt and what its failure LOOKED like

Chunked delivery turns on one requirement: the emulator must read a chunk
synchronously from inside QEMU's MMIO path. Five arrangements were tried before
one worked, and the reason this took a whole evening is that **four of the five
failed in a way that did not point at itself**.

| # | attempt | how it failed | what it looked like |
| --- | --- | --- | --- |
| 1 | synchronous `XMLHttpRequest` on the emulator's thread | `NetworkError` on `send`, request never leaves the browser | "chunked delivery is broken" |
| 2 | the same, with `Content-Encoding: br` removed (`serve.py --no-brotli-header`) | identical `NetworkError` | ruled the encoding out |
| 3 | the same, service worker bypassed (`?sw=0`) | identical `NetworkError` | ruled the worker out |
| 4 | `emscripten_fetch(EMSCRIPTEN_FETCH_SYNCHRONOUS)` | **zero bytes, no error at all** — its backend is that same XHR | "the fetch succeeded and the chunk is empty" |
| 5 | the emulator's thread creating the fetch worker itself | fetch never completes; 30 s timeout | "the worker is broken" |
| 6 | **the page creating the fetch worker, futex handshake** | works | — |

The cause of 1-4 is one thing: **`-sEXPORT_ES6` makes Emscripten's pthread
workers MODULE workers, and Chrome does not support synchronous XHR there.** The
cause of 5 is a different thing: **a nested dedicated worker is serviced through
its parent's context**, and this parent spends its life blocked in
`Atomics.wait`.

Three smaller defects were found on the way, each of which would have been hard
to guess and trivial to see with the right probe:

- **`console.error` from a pthread worker reaches nothing the page can read.**
  Its parent is another worker, whose console goes nowhere either. A whole
  debugging round was spent on a bare `-1` before the fetcher started returning
  distinct codes *and* copying the exception text into a caller buffer.
- **A worker created from a `blob:` URL cannot resolve a root-relative script
  path**: `new Worker('/chunk-fetch-worker.js')` throws
  `SyntaxError: '/chunk-fetch-worker.js' is not a valid URL`. Resolve against
  `self.location.origin` — Emscripten's pthread workers are blob workers.
- **`TextDecoder` refuses a view onto a `SharedArrayBuffer`** ("The provided
  ArrayBufferView value must not be shared"), so reading the URL out of the
  wasm heap needs `.slice()` (a copy), not `.subarray()`.

### Measurement hygiene: three separate ways a run lied

None of these were emulator bugs, and each cost more than the bug it hid.

| what looked wrong | what was actually wrong |
| --- | --- |
| JIT counters frozen mid-boot, "the emulator hung" | the tab was **hidden**, and browsers throttle hidden pages |
| no reports at all from a cold run, "the service worker freezes the page" | `keepalive`/`sendBeacon` share a **64 KiB in-flight quota**; the reports queued behind the chunk fetches, filled it, and were rejected into a swallowed `.catch` |
| a chunked run reporting `first pixels` and a `nonBlackPct` it does not measure | a **server left behind by a killed run** still owned the port, so the results file belonged to the previous run — and Session A's page posts to the same endpoint |
| a 1.1.4 run "ending" at 14 s, and a warm run at 120 s | the Chrome binary **handed off to a browser process and exited**; the runner believed it and killed a boot that was still going |

Fixes, in the same order: `scripts/wasm/bench-run.py` disables the three
throttles; periodic reports use a plain `fetch()` and **count their failures**;
`serve.py --results-label` accepts only posts carrying the current run's label,
`bench-run.py` refuses to start on a busy port, and a Chrome exit is only
believed when the page has ALSO stopped reporting for 60 s.

### The tool that found it: `web/bench-b/worker-selftest.html`

Three bugs in this protocol were each costing a 5-minute wasm rebuild plus two
minutes of booting to reach the first NAND read. The self-test reproduces the
whole handshake — shared `WebAssembly.Memory`, a requester worker blocking in
`Atomics.wait`, the real `chunk-fetch-worker.js` — **in about a second**, and
`?nested=1` switches between the two arrangements:

```
nested:      Atomics.wait -> timed-out after 10017 ms; state=1 status=0
page-owned:  Atomics.wait -> ok after 4 ms; state=2 status=116   PASS
```

It also caught the bug that would have been hardest to guess: **`TextDecoder`
refuses a view onto a `SharedArrayBuffer`** ("The provided ArrayBufferView
value must not be shared"), so reading the URL out of the wasm heap needs
`.slice()` (a copy) rather than `.subarray()`.

**Build the cheap reproduction first.** Every one of these bugs was a
three-message protocol defect that a page could exercise in a second.

### The "service worker freezes the page on a cold run" was never a freeze

The page was fine and the emulator was fine. **The reports were being thrown
away by a quota.**

`fetch(..., { keepalive: true })` and `navigator.sendBeacon()` share a **64 KiB
cap on IN-FLIGHT bytes per origin**. On a cold run every report queued behind
the service worker's hundreds of chunk fetches, the quota filled, and each
later report was rejected immediately — into a `.catch(() => {})` that said
nothing. Warm runs and `?sw=0` runs never filled the quota, which is exactly
why the symptom looked like "the service worker freezes the page".

Fixed by using a plain `fetch()` for periodic reports and **counting the
failures** (`reportFailures` / `lastReportError` are now in every report), so a
dropped report can never again read as a stopped emulator.

Two rules out of it, both general:

- **Do not use `keepalive`/`sendBeacon` for periodic telemetry.** They exist for
  the unload path, where their quota is the point. For anything sent while the
  page is alive they convert congestion into silence.
- **Never swallow a telemetry failure.** The `.catch(() => {})` is what turned a
  five-minute diagnosis into a multi-run hunt for a phantom main-thread stall.

Same build, same profile, cold then warm, in standalone Chrome:

| run | kernel | BSD root | chunk requests | on the wire | dropped reports |
| --- | --- | --- | --- | --- | --- |
| **cold** (prefetch + SW) | 113 s | 125 s | 503 | **18.52 MiB** | 0 |
| **warm** | 102 s | 114 s | **0** | **0** | 0 |

That is the B3 acceptance criterion met end to end in the target browser: a
cold boot downloads 18.52 MiB against a 215.6 MiB pack, a warm boot downloads
nothing, and the boot is no slower than staging the whole pack.

### The original diagnosis (kept: it is what the fix is built on)

**Chrome 149 will not do a SYNCHRONOUS network read here**

The chunked design turns on the emulator reading a chunk synchronously from
inside QEMU's MMIO path. That works in the in-app browser and **fails in
Chrome 149**, which is the browser that matters:

```
[NANDCHUNK] fetch failed: chunk 1723 (…/chunks/4242821e…) -> -4
    NetworkError: Failed to execute 'send' on 'XMLHttpRequest':
    Failed to load 'http://localhost:8013/chunked/1A543a/chunks/4242821e…'
```

**The request never reaches the server** — the server's own counter stays at
zero — so this is a client-side refusal, not a transport error. Two candidate
causes were tested and eliminated rather than assumed:

| suspected | test | result |
| --- | --- | --- |
| the `Content-Encoding: br` response | `serve.py --no-brotli-header` | identical NetworkError |
| the service worker in the path | `?sw=0`, worker never registered | identical NetworkError |

What remains is that `-sEXPORT_ES6=1` makes Emscripten's pthread workers
**module workers**, where Chrome does not support synchronous XHR.

**`emscripten_fetch(EMSCRIPTEN_FETCH_SYNCHRONOUS)` does not rescue it** — its
backend is that same XHR, and it fails *silently*, returning a zero-byte
result. It was still worth adding (`-sFETCH`, and the fetcher now tries XHR
then falls back), because it keeps the one configuration that works.

**The fix is the Atomics.wait design, and it is the next piece of work:** the
emulator thread blocks on a futex in shared memory while a **classic**
(non-module) worker performs an ordinary async `fetch()` and writes the chunk
into the wasm heap. The emulator still never awaits, which is the property the
whole design needs; only the mechanism changes.

Getting the diagnosis at all needed one piece of instrumentation worth keeping:
**`console.error` from a pthread worker reaches nothing the page can read**, so
the fetcher returns distinct negative codes *and* copies the exception text
into a caller buffer. A whole debugging round was spent on a bare `-1`.

**And a second, separate freeze** — with the service worker in the path, a
standalone Chrome stops running the page's timers seconds into the run, before
the emulator has touched the NAND at all, while chunk requests keep arriving at
the server. Not diagnosed. It does not reproduce with `?sw=0`, and it does not
reproduce in the in-app browser.

### Note for Session A: `configs/meson/emscripten.txt` gained `-sFETCH`

The flag cannot arrive through `--extra-ldflags` — this file overrides
`LDFLAGS`, the same trap that swallowed `-lnodefs.js` — so the shared cross
file had to change. **Cross-file options are read at CONFIGURE time**, so
picking it up needs `build-qemu.sh --configure`; a plain rebuild silently keeps
the old link line.

### Two harness traps, both of which produced convincing wrong readings

- **A hidden tab is throttled, and it looks exactly like a hang.** The first
  browser run appeared to stall at `compiled=352` for minutes. It was
  backgrounded. `scripts/wasm/bench-run.py` exists because of this.
- **`--headless=new` deadlocks the chunked boot.** The emulator's synchronous
  XHR, intercepted by the service worker, never completes in headless Chrome:
  the prefetch stops a few chunks short and the page stops reporting entirely.
  The identical run **in a real browser window works** — kernel and BSD root
  reached, 11,095 cache hits. Use `--headed` for chunked runs; headless is fine
  for pack-mode JIT sweeps.
- Related, and it cost a wrong reading: a killed run leaves its `serve.py`
  behind, the next run's bind loses silently, and **its results are written to
  the previous run's file**. `bench-run.py` now refuses to start on a busy
  port.

### Costs and habits worth knowing before the next session

- **A native verification boot costs ~300 MiB of scratch.** `fb-snapshot.py`
  clones the NAND into `<logs>/stage`, and with `IT_NAND_WRITABLE=1` the clone
  stops being a near-free APFS clone and becomes real written pages. Three of
  them plus the chunker took the volume down to **290 MiB free** mid-run. Delete
  `<logs>/stage` when a run is done.
- **`chunk-pack.py` now reuses chunks already written** — the filename IS the
  content hash — so a re-run to add a prefetch list costs a read and a hash
  pass instead of a fresh 45-minute Brotli sweep over a 300 MiB pack.
- **Keep `web/chunked/<BUILD>/`.** 72 MiB (1.0) and 102 MiB (1.1.4), and each
  takes ~35-45 minutes of Brotli q11 to rebuild. Small on disk, expensive in
  time.

### Still owed on the browser side

- **Guest writes have nowhere to go** (W6). In chunked mode the emulator still
  opens `<nand>/bank<N>/<page>_new.page` in MEMFS, so the bank directories must
  exist; the copy-on-write overlay is unimplemented, and without it the boot
  cannot get past the point where daemons must create state.
- **Only the chunks are cached across visits.** The 52 MB `qemu-system-arm.wasm`
  and the 1.7 MiB index are re-fetched every load because the dev server sends
  `Cache-Control: no-store` for everything except chunks. Fine for development,
  and exactly wrong for a warm start.

---

## Session — 2026-07-28 (later): grafting the WebAssembly JIT

TCI is ~13× slower than native and the product needs real-time speed, so the
out-of-tree WebAssembly TCG backend moved from "option" to "requirement". This
records how it was adopted, including the wrong turns.

### False start: "the JIT is on an old QEMU, so grafting means crossing the refactor"

The first assessment looked at `ktock/qemu-wasm`'s **default** branch, found
QEMU **8.2.0** with the pre-rename TCG layout (`tcg/arm`, `tcg/i386`,
`tcg/mips`), and concluded that adopting it meant porting a backend across the
QEMU 10.x TCG rework — days of expert work, possibly justifying a rewrite.

**That was wrong, and cheap to have checked.** The repository has 22 branches.
Enumerating them and reading each `VERSION` takes one command:

| branch | QEMU | backend |
| --- | --- | --- |
| `master` | 8.2.0 | `tcg/wasm32` |
| `dev-e` | 9.2.92 | `tcg/wasm32` |
| `bench64` | 10.2.50 | none (benchmark branch) |
| **`wasm64-tcg-b`** | **10.2.50** | **`tcg/wasm64`** |

`wasm64-tcg-b` is based on the development line that *became* 11.0, and its
`tcg/` layout is **identical** to ours (`aarch64 loongarch64 mips64 ppc64
riscv64 s390x sparc64 tci x86_64`). The backend is therefore written against
essentially our interface.

**Lesson: check every branch and its `VERSION` before judging a port's cost.**
Judging an upstream by its default branch nearly turned a copy job into a
rewrite.

### The right path: copy 9 files, then fix what upstream guards on TCI

The backend is `tcg/wasm64/` plus `tcg/wasm64.{c,h}` — 9 files, ~118 KB,
~4,000 lines. Everything else it needs was **already upstream in 11.0.2**:
`os-wasm.c`, `coroutine-wasm.c`, the `wasm64` cpu in `configure`, and the
emscripten cross file's `ALLOW_TABLE_GROWTH` / `addFunction` exports, which
exist for precisely this backend.

The integration work was five hooks, all the same shape — upstream guards
things on `CONFIG_TCG_INTERPRETER`, and the wasm backend needs the same
treatment because it behaves like TCI in those respects:

1. `meson.build` — delete upstream's `error('WebAssembly host requires
   --enable-tcg-interpreter')`. That check exists only because upstream has no
   wasm backend.
2. `tcg/meson.build` — build `wasm64.c` and link libffi on an emscripten host.
3. `include/tcg/helper-info.h` — libffi declarations and the `ffi_cif` field.
   Without it: `use of undeclared identifier 'ffi_cif'`.
4. `tcg/tcg.c` — four guards (`typecode_to_ffi`, the ffi layout macros, both
   `tcg_qemu_tb_exec` sites).
5. `include/tcg/tcg.h` — `tcg_qemu_tb_exec` must be declared a **function**.
   Like TCI, the backend supplies its own dispatcher rather than a pointer to a
   generated host prologue, so the pointer declaration collided with it.

### The one real integration defect: `WASM64_MEMORY64_2`

First run threw:

```
WebAssembly.Module(): BufferSource argument is empty
```

The backend's `EM_JS` glue encodes pointers and table indices differently in
Emscripten's 32-bit-address-limit mode, selected by `WASM64_MEMORY64_2`. Their
tree sets it from a meson option; our 11.0.2 handles the limit in `configure`
(`--wasm64-32bit-address-limit` → `-sMEMORY64=2`) and never passed the define.
`configure` now defines it whenever `-sMEMORY64=2` is in effect.

**That define was necessary but NOT the cause — the error was unchanged with it
applied.** Recorded rather than quietly deleted, because the wrong diagnosis is
instructive: it was inferred by reading the macro and reported as settled before
a rebuild confirmed it.

**Memory mode was not the cause either.** `-sMEMORY64=1` (the mode the backend
is developed against) fails identically. Testing that needed a browser, because
full wasm64 requires **Node v23** and this host has 22.22.3 — see "Testing
wasm64" below.

### The real cause: `tcg_out_tb_end` does not exist in QEMU 11.0.2

The backend assembles its module in `tcg_out_tb_end()` — that function writes
`h->wasm_ptr` and `h->wasm_size` into the TB header. `tcg_out_tb_end` is a
backend hook **added by the patch series**: in 10.2.50 every backend defines it
(a no-op returning 0 for the native ones), `tcg.c` forward-declares it and calls
it after relocations are resolved.

QEMU 11.0.2 has `tcg_out_tb_start` but **no `tcg_out_tb_end` anywhere** — not in
`tcg.c`, not in any backend. So the wasm backend's generator was dead code:
never called, `wasm_size` never written, and `WebAssembly.Module()` handed an
empty view. Exactly the "structural mismatch" candidate, and findable in one
grep once the right question was asked.

**Fix:** declare and call the hook in `tcg/tcg.c`, guarded by `EMSCRIPTEN`.
Upstream's series instead adds a no-op to all nine backends; scoping it to the
WebAssembly host keeps native builds — the correctness oracle — bit-identical.

**Result:** the empty-buffer error is gone. Generated modules compile,
instantiate and execute — and then trapped one layer deeper with
`RuntimeError: unreachable` raised *inside* a generated module
(`wasm://wasm/<hash>`).

### The second missing hook: `tcg_out_label_cb`

Same class of bug, found by asking the same question. The WebAssembly backend
cannot branch to an arbitrary address, so it compiles a TB into

```
loop { if (BLOCK_IDX <= 0) {…} if (BLOCK_IDX <= 1) {…} … }
unreachable          ← only reached if the loop falls through
```

and branches by assigning `BLOCK_IDX`. **`tcg_out_label_cb` is what opens a new
block at each label and records the label → block mapping.** In 10.2.50,
`tcg_out_label()` calls it; in 11.0.2 it does not exist. So no blocks were
created, branches selected an index no block matched, the body ran to
completion, and control fell out of the loop into the guard.

Rather than wait for the next crash, the remaining hooks were enumerated in one
pass — extract every `static … tcg_out_*` declaration from both trees and diff:

```sh
git show FETCH_HEAD:tcg/tcg.c | grep -oE "^static [a-z0-9_ ]+\**tcg_out_[a-z0-9_]+" …
```

`tcg_out_label_cb` was the only one left. **Two hooks total, both invisible at
compile and link time**, because a hook that is never called is just an unused
static function.

**Lesson for any future backend graft: diff the hook surface first.** A TCG
backend integrates through a set of `tcg_out_*` callbacks; a series that adds
new ones will link cleanly and fail at runtime in ways that look like codegen
bugs.

With both hooks wired, the emulator runs past the trap and drives the machine
model (`[PMU] RESUME_STATUS read`), i.e. the JIT is compiling and executing
translation blocks for real.

### Measured: the JIT is at least 13.6× faster than TCI

Same page (`jit-smoke` vs `tci-smoke`), same artifacts, same `-icount shift=1`,
same browser, **each run solo** — time to `[LCD] Merlot panel woke from sleep`:

| engine | first Merlot landmark |
| --- | --- |
| **WebAssembly JIT** | **31.4 s** (second at 206.4 s) |
| TCI | **not reached by 428 s** |

So **≥13.6×**, and the true figure is larger because TCI had still not arrived
when the run was stopped. That is the order-of-magnitude the real-time goal
needs, and it matches Infinite Mac's independent finding that qemu-wasm beats
hand-ported C emulators.

**Two methodology errors were made getting this number, both caught:**

1. **Comparing across icount settings.** The JIT run sitting at 206 s with no
   iBoot banner looked like a slowdown against "TCI reached the banner at
   12.7 s" — but that TCI figure was from a run with **no icount**, where the
   guest's delay loops finish almost instantly because virtual time tracks wall
   clock. At `shift=1` each 1 ms delay costs a full 500,000 instructions, and
   iBoot is mostly waits. Different amounts of work; not comparable.
2. **Running both engines at once.** The first A/B had the two pages in two
   tabs competing for CPU, which halves each. Re-run one at a time.

Before speculating further on the first, the obvious mechanism was checked and
ruled out: icount does **not** push the backend back onto TCI. The compile
decision in `wasm64.c` is a per-TB execution counter (`INSTANTIATE_NUM` 1500)
with no icount interaction.

**Caveat on what this measures.** Neither run reached the iBoot banner: the page
is iBoot-only and `shift=1` makes its delay loops expensive. This is a
landmark-to-landmark engine comparison, not a boot time. A boot-time number
needs the NAND and a faster clock setting, and should be taken once the JIT
completes a full boot.

### The display backend, and two QAPI traps

`ui/wasm.c` adds `-display wasm`. It does **not** draw: the emulator runs on a
pthread while the canvas lives on the main thread, so the backend publishes
`Module.qemuDisplay = {width, height, stride, ptr, generation, damage}` — `ptr`
being an address inside the wasm heap — and bumps `generation` on damage. The
page reads those pixels straight out of `HEAPU8`.

That split is the point: wasm memory is SharedArrayBuffer-backed, so the main
thread reads what the emulator thread wrote with **no copy and no postMessage
per frame**, and presentation policy stays in the page.

Two build errors worth knowing before adding a display type, both fatal rather
than advisory:

- every QAPI enum value needs a doc comment (`value 'wasm' lacks documentation`);
- those comments are limited to **70 characters** per line.

`error_report()` also needs `qemu/error-report.h`, which `ui/console.h` does not
pull in.

### Tuning the JIT: what was measured, what failed

Getting the backend to build was not the end of it. Once it ran, the browser
boot was still slow, and four experiments were needed to find out why. The
instrumented counters (`compiled / recompiled / evicted / live`, in
`tcg/wasm64.c`, reported every 16 compiles) are what made each of these a
measurement rather than a guess — **add them back first if this is ever
revisited.**

**1. Instance thrashing — hypothesis, disproved.** The obvious suspicion for
"mysteriously slow JIT" is the browser's cap on live WebAssembly instances,
with the backend evicting and recompiling in a loop. The counters said no:

```
[ 4.8s] compiled=176 recompiled=0 evicted=0 live=176/12000
[22.0s] compiled=208 recompiled=0 evicted=0 live=208/12000   ← and then nothing
```

Zero evictions, zero recompiles, **1.7% of the cap in use**.

**2. The real cause: the compile threshold.** `INSTANTIATE_NUM` was upstream's
1500 — a TB must execute 1500 times before it is compiled. That suits a
long-running Linux guest; a **boot** is thousands of moderately-warm blocks and
few very hot ones, so only 208 qualified and essentially the whole boot ran on
the forked TCI interpreter, the engine already measured at ~13× slower than
native.

Lowered to **100**, and the boot went from never reaching the kernel to:

| landmark | threshold 1500 | threshold 100 |
| --- | --- | --- |
| kernel banner | never (silent past 500 s) | **524.6 s** |
| BSD root | never | **584.7 s** |
| launchd | never | **791.0 s** (with the cap raised) |

This also qualifies the earlier "≥13.6× faster than TCI" figure: that was taken
on a smoke test dominated by iBoot polling loops, which *do* cross 1500 quickly.
It describes hot loops, not boot code.

**3. Second-chance (CLOCK) eviction — tried, worse, REVERTED (427cfb972e).**
FIFO eviction drops the oldest half of the ring, which discards by *age*: the
kernel and libc blocks hot since early boot are the oldest, while one-shot
driver-matching code compiled seconds ago survives. A reference bit per instance
should have fixed that. It did the opposite:

| policy | at first eviction | recompile rate |
| --- | --- | --- |
| FIFO | compiled=19840 recompiled=4399 | 22% |
| CLOCK | compiled=20688 recompiled=8483 | **41%** |

Most likely because skipping hot entries frees far less per sweep than FIFO's
unconditional half, so the cap is re-hit almost immediately — more sweeps, more
total evictions (12513 vs 12000), more recompiles. **That run also crashed**
with `RuntimeError: memory access out of bounds`, exactly as `live` reached the
cap, inside the rewritten eviction path; FIFO never crashed. The fault was not
found by inspection, which is a reason to keep it reverted rather than a reason
to trust it.

**4. The cap itself — the actual fix.** Hitting `MAX_INSTANCES` is far worse
than it sounds: `can_add_instance()` then returns false, so the JIT **stops
compiling entirely** and every newly-hot block runs interpreted, while reclaim
waits on a JS `FinalizationRegistry` that may not run for a long time. The
threshold-100 run saturated 12000 during driver matching and then made no
further progress for ~500 s. Raised to **48000** — it is a heuristic guard
against a browser limit, not the limit itself, so this trades resident memory
for keeping compilation alive.

**Standing advice:** treat `INSTANTIATE_NUM` and `MAX_INSTANCES` as a pair.
Lowering the threshold without headroom in the cap just moves the stall. And
note the workload split — a **boot** wants eager compilation (long cold tail),
while **app use** is a small working set hammered repeatedly and tolerates a
high threshold. One static value cannot be right for both; an adaptive
threshold, or a post-boot snapshot that skips the boot phase entirely, is the
real answer.

### Testing wasm64: use a browser, not Node

`-sMEMORY64=1` requires **Node v23** ("This emscripten-generated code requires
node v23.0.0"); browsers have supported memory64 for a while. `web/public/jit-smoke/`
is a self-contained page that boots **iBoot only, no NAND** — enough to cross
the backend's 1500-execution compile threshold in ~2 s — and reports PASS /
FAIL / INCONCLUSIVE. Serve it with `scripts/wasm/serve.py` (the COOP/COEP
headers are required) and open `/public/jit-smoke/`.

Chrome plus Playwright/Patchright are available on this host, so the same page
is the basis for an automated headless check; that is the right long-term
harness for wasm64 work regardless of the Node version.

### Two self-inflicted build failures worth not repeating

**Never regenerate a cross build outside its toolchain environment.** When
ninja reported a stale `build.ninja`, running `meson --internal regenerate` by
hand from inside `build-wasm` re-probed dependencies without
`PKG_CONFIG_PATH` pointing at the wasm sysroot. Meson found **host Homebrew**
libraries and enabled curl, zstd and libssh for a WebAssembly build:

```
-I/opt/homebrew/opt/zstd/include -I/opt/homebrew/Cellar/libssh/0.11.3/include
../block/curl.c:35:10: fatal error: 'curl/curl.h' file not found
```

`scripts/wasm/build-qemu.sh --configure` exists so that environment is always
set; use it. A correct reconfigure reports `Run-time dependency libcurl found:
NO (tried pkgconfig)`.

**A QEMU build directory symlinks `scripts/`.** Leaving the shell inside
`build-wasm` and running `./scripts/wasm/build-qemu.sh` still *resolves* — but
the script computes the repo root from its own path and concludes the root is
`build-wasm`, then reports `native toolchain missing; run
scripts/wasm/setup-toolchain.sh` even though the toolchain is present and
intact. A misleading error with an unrelated remedy; always invoke it from the
repository root.

---

## Session — 2026-07-28: QEMU RUNS IN WEBASSEMBLY

**`build-wasm/qemu-system-arm.wasm` exists (53 MB) and boots iPhone OS 1.1.4's
iBoot and Darwin kernel.** It then panics in a driver-matching race, and TCI is
~13× slower than native. Both are quantified below.

### What it took

QEMU 11.0.2's Emscripten support accepted our configure line unchanged — no
patches. Four things had to be fixed:

1. **Wrong ninja target.** Emscripten names it `qemu-system-arm.js`; asking for
   `qemu-system-arm` fails.
2. **OpenSSL — the real blocker.** Four device files included `<openssl/aes.h>`
   or `<openssl/sha.h>`, and OpenSSL does not cross-compile to wasm64. Three
   uses were dead code; the two live ones now use what QEMU already ships —
   glib's `GChecksum` for SHA1, `crypto/aes.h` plus a new
   `include/hw/arm/ipod_touch_aes_cbc.h` for AES-CBC (QEMU has the cipher but
   no CBC wrapper). The helper mirrors OpenSSL's `CRYPTO_cbc128_*` exactly,
   including the trailing partial block and the in-place IV update. **This is a
   portability fix, not a browser hack: the native build no longer needs
   `-lcrypto` for these devices either.**
3. **The 8900 engine's AES code lives in its header**, so grepping the `.c`
   showed nothing and the first edit removed a declaration it needed. The native
   build caught it immediately.
4. **`-lnodefs.js` never reaches the link** — QEMU's `configs/meson/emscripten.txt`
   overrides `LDFLAGS`, and even via `--extra-ldflags` the emitted module still
   stubs NODEFS out. Unresolved; MEMFS is used instead, which is what the
   browser needs anyway. Staging all 301.5 MiB into MEMFS takes **0.7 s**.

### Measured: TCI is ~13× slower than native

Same firmware (4A102), same host, same artifacts:

| landmark | native | wasm (TCI) | ratio |
| --- | --- | --- | --- |
| iBoot-204.3.14 banner | 2.0 s | 12.7 s | 6.4× |
| Darwin kernel version | 7.3 s | 84.7 s | 11.6× |
| `USBWrangler::start starting` | 7.8 s | 103.9 s | 13.3× |
| `AppleS5L8900XADM::attach` | 7.8 s | 105.1 s | 13.5× |

**This is the go/no-go answer for TCI, and it is not encouraging.** A native
boot to the home screen is on the order of 20-30 s, so 13× puts a browser cold
boot in the several-minute range before any of the asset-loading work counts.
The out-of-tree qemu-wasm JIT is therefore looking like a requirement rather
than an option — consistent with Infinite Mac measuring qemu-wasm *faster* than
hand-ported C emulators. Re-check whether the backend has merged upstream
before committing to carrying the patch set.

### Guest-visible time must be decoupled: run with `-icount`

Without it the guest sees its own driver `start()` calls taking **16944 ms,
22818 ms, 19703 ms** — because `QEMU_CLOCK_VIRTUAL` follows wall clock while
TCI runs the guest 13× slower. The kernel then takes timeout paths that no real
device ever takes, and panicked with a null dereference in
`AppleS5L8900XUSBWrangler` right after the USB PHY registered.

With `-icount shift=3` the same drivers report **539 / 513 / 544 ms** and that
panic is gone. `scripts/wasm/boot-test.mjs` takes `IT_ICOUNT=<shift>`.

**The browser build should always run with icount.** TCI will always be far
slower than native, so every timeout-sensitive driver is exposed without it.
This is the repo's own standing advice (AGENTS.md, "Determinism — use
`-icount`") arriving in a new context.

*Correction to an earlier claim in this session:* the wrangler's
`phyRegistered` event was described as something "native never produces". That
was measured against a native log that was itself booting the wrong NAND (see
below) and never reached the relevant stage. Against a correct boot, native
produces `phyRegistered` too. The divergence was never the event — only the
panic.

### Both panics were the clock, and `shift=1` clears them

With icount **and** the verified home-screen NAND, the boot now gets much
further — through the USB PHY, the network stack, the LCD — and then:

```
IOIpodUSBDevice::start
panic(cpu 0 caller 0xC012D963):
```

Native runs the same driver at the same point and simply continues
(`Registering: ../usb-device/AppleS5L8900XIpodHAL/IOIpodUSBDevice`), reaching
the home screen at 74.3% non-black. The panic message is empty, which is
unusual and worth chasing.

**Resolved by using a faithful clock rate.** The panic point tracks the virtual
clock, which is how we know these are guest timeouts rather than bad values out
of the device models:

| icount | ns per instruction | outcome |
| --- | --- | --- |
| none | wall clock | USB wrangler null-deref (`caller 0xC00638CC`) |
| `shift=5` | 32 (~31 MIPS) | same null-deref |
| `shift=3` | 8 (~125 MIPS) | passes it; panics in `IOIpodUSBDevice::start` |
| **`shift=1`** | **2 (~500 MHz)** | **both panics gone** |

`shift=1` printed `Registering: ../usb-device/AppleS5L8900XIpodHAL/IOIpodUSBDevice`
— the same line native prints — and continued. **No device-model change was
needed.**

Note the direction is counter-intuitive: a HIGHER shift means more virtual
nanoseconds per instruction, i.e. a guest that believes it is running on a
SLOWER CPU. The real S5L8900 is 412 MHz ≈ 2.4 ns/instruction, so `shift=1` is
the faithful setting; `shift=3` already presents a machine ~3× slower than the
hardware iPhone OS was written for.

The cost is wall clock: at `shift=1` the guest executes 4× more instructions per
virtual millisecond than at `shift=3`. Under TCI that is slow. **Another
argument that the JIT is required** — a ~10× faster engine makes the faithful
clock rate affordable.

### The trap that cost this session two wrong conclusions

`m68ap-artifacts/builds/4A102/nand` was **not** the home-screen NAND. The
migration filed `stage/nand-m68ap-fresh` there; it is a full, valid tree built
from an *unpatched* root, so it boots and renders **nothing** (kernel
framebuffer 0.0%). Every wasm boot test today used it, and the native
comparison run used it too — which is how `phyRegistered` looked wasm-only.

A wrong NAND at the canonical path is worse than a missing one: missing fails
loudly, wrong boots to a black screen. Fixed three ways:

- the tree was renamed to `nand-prepack-not-product`, so the canonical path is
  now empty and fails loudly until the recipe regenerates it;
- `build-m68ap-nand.py` gained `--recipe`, and the product recipe stamps
  `"recipe": "home-screen"` into `nand-provenance.json`, so the question is
  answerable from the tree itself;
- the migration script records why that mapping is wrong.

### Every failure so far has had a cause OUTSIDE the wasm build

Worth stating plainly, because it changes how the next failure should be
triaged. Four separate symptoms all looked like "the WebAssembly port is
broken", and none of them were:

| symptom | actual cause |
| --- | --- |
| `AppleS5L8900XUSBWrangler` null dereference | wall-clock virtual time → guest timeout path |
| `IOIpodUSBDevice::start` panic | the same, at a different threshold |
| black framebuffer, 0% non-black | a NAND built from an unpatched root at the canonical path |
| emulator `abort()` shortly after `BSD root` | the **test harness** had not created the NAND bank directories |

**No defect has yet been found in the device model, in QEMU's Emscripten
support, or in TCI's correctness.** The only code change the port has required
is removing OpenSSL. Triage the next wasm-only symptom as environment, clock,
or harness before suspecting the port.

### Dead end: the harness abort after `BSD root`

`nand_flush_buffered_page()` opens `<nand>/bank<N>/<page>_new.page` for WRITING
on every guest page write, and calls `hw_error()` — which aborts the entire
emulator — when that open fails. `boot-test.mjs` created `/fw/nand` but not the
eight bank directories, so the first guest write after the root filesystem
mounted killed the run. `fb-snapshot.py` and `ipod-app-launcher.sh` both create
them; the harness now does too.

**This is a hard requirement for W6, not just a harness fix.** In a browser
there is no filesystem to write to at all, so that path must be replaced by the
copy-on-write overlay. Leaving it as-is means the first guest write aborts the
emulator in the page.

### Dead end: `-lnodefs.js` / NODEFS

Two attempts (LDFLAGS environment, then `--extra-ldflags`) both left the
emitted module stubbing NODEFS out with "no longer included by default". QEMU's
`configs/meson/emscripten.txt` sets its own link arguments; the flag reaches
`config-meson.cross` but not the effective link. Abandoned: MEMFS is what the
browser needs anyway, and staging 301.5 MiB into it costs 0.7 s.

### Native-build improvements this work produced

The browser port paid for itself in the native tree three times over:

- **OpenSSL is no longer needed by the S5L8900 device models.** SHA1 goes
  through glib (already a hard QEMU dependency) and AES through QEMU's own
  `crypto/aes.h` plus `include/hw/arm/ipod_touch_aes_cbc.h`. One less external
  dependency for every build, native included.
- **`fb-snapshot.py` can verify again.** Its QMP socket lived in `/tmp`, which
  is swept during long runs; the socket vanished mid-boot and the failure
  surfaced as a bare `FileNotFoundError` that read like a guest hang. Now
  `/var/tmp`, and it waits for the socket with an explanatory error. This had
  been silently breaking home-screen verification for every board.
- **NAND provenance can now answer "is this the product NAND?"**
  `build-m68ap-nand.py --recipe` records the recipe, and the home-screen recipe
  stamps `"recipe": "home-screen"`. Before this, a tree built from an unpatched
  root was indistinguishable from the real one until you booted it and saw a
  black screen.

`-icount` also stopped being advice and became a measured requirement — see
AGENTS.md, which now points here for the worked example.

---

## Session — 2026-07-27: delivery decided, assets measured

- **Delivery model settled**: prepared offline, self-hosted, chunked and
  compressed. No in-browser conversion of original artifacts — the M68AP NAND is
  constructed from an IPSW by this repo's pipeline and has no downloadable
  equivalent.
- **Measured the shipping 1.1.4 pack** (300.3 MiB, 148,812 × 2,112 B pages) with
  `scripts/wasm/measure-pack.py`: per-chunk **brotli q11 = 31.4%** → 94.2 MiB
  (lzma 30.9%, zlib 36.5%). No chunk is uniform, but a **full scan found 197 of
  1,201 chunks are duplicates (16.4%)**, one repeating 198 times.

  *Correction to an earlier figure in this session:* a first pass reported ~6%
  duplicates. That sample was taken at raw 256 KiB **byte** offsets, which
  straddle the header/index boundary and are misaligned to the 2,112-byte page
  stride, so almost no two windows could match. Measured at the real chunk
  definition — page-aligned, fixed page count — dedup is 16.4%. Only the
  page-aligned number is meaningful.

- **Measured what a cold boot actually touches — the number the whole delivery
  design turns on.** Added `IT_NAND_TRACE_PAGES` to `hw/arm/ipod_touch_nand.c`
  (records every page fetch as a little-endian u32 VPN, in access order) and
  `scripts/wasm/analyze-nand-trace.py` to reduce a trace against a pack. From a
  boot verified to reach the home screen (kernel framebuffer 73.95% non-black at
  420 s):

  | | |
  | --- | --- |
  | page fetches | 31,384 |
  | distinct pages touched | **25,030 = 16.8% of the pack** |
  | distinct chunks (124 pages) | 351 of 1,201 = 29.2% |
  | **first-boot download, chunked + brotli** | **24.3 MiB** |
  | whole pack + brotli | 94.2 MiB |
  | whole pack raw | 300.3 MiB |

  **Lazy loading wins decisively — ~4× better than a compressed whole-pack
  download.** Chunk size trades read amplification against request count:
  124 pages → 24.3 MiB, 62 → 20.9 MiB, 32 → 18.5 MiB, 16 → 17.0 MiB. **62 pages
  (128 KiB) is now the recommended size**, chosen from our own data rather than
  inherited from Infinite Mac's 256 KiB.

- **Two traps found while taking that measurement**, both worth knowing before
  anyone repeats it:
  - A **read-only NAND never reaches SpringBoard**. Without
    `IT_NAND_WRITABLE=1` writes are silently discarded, and daemons that must
    create state spin forever — the boot stalls after launchd with no error. Use
    a throwaway APFS clone (`cp -Rc`, near-zero space) plus that flag.
  - **SpringBoard does not announce itself on serial.** Grepping the log for
    "SpringBoard" finds nothing even on a fully successful boot. Verify with
    `scripts/fb-snapshot.py`, which reports the kernel framebuffer's non-black
    percentage; scanout itself reads ~0% because the panel sleeps, which is the
    already-documented "black screen is scanout, not SpringBoard" artifact.
- **Researched Infinite Mac's approach** and adopted it: content-addressed
  fixed-size chunks, per-chunk Brotli, service-worker interception keeping the
  emulator's reads synchronous, prefetch of the boot working set, per-chunk
  residency. Its measured bar is boot screen in 1 s, booted in 3 s, cold cache.
- **Established what "the JIT" is, and what our engine already has.** The JIT is
  [qemu-wasm](https://github.com/ktock/qemu-wasm)'s WebAssembly TCG backend:
  each translation block becomes one Wasm module executed via the browser's
  `WebAssembly.Module`/`Instance` APIs, and it is *hybrid* — a forked TCI
  interprets everything and only blocks run ~1000 times get compiled, because
  compilation is costly and browsers cap live Wasm instances.

  Upstreaming is half done and **we have the half that landed**: Emscripten host
  support plus TCI for 32-bit guests merged in QEMU 10.1, which is why 11.0.2
  builds for the browser at all. The backend is still out of tree — verified
  2026-07-27 that **QEMU master has no `tcg/wasm*` directory**, and our own
  `tcg/` has none either. Our guest is 32-bit ARM (the upstreamed case) and the
  v2 backend series targets wasm64 (how we configure), so adoption would be
  well-aligned if it becomes necessary.

  *Correction:* our build links libffi and passes `ASYNCIFY_IMPORTS=ffi_call_js`,
  which looks like JIT plumbing already present. It is not — that is TCI's own
  helper-call path (`ffi_call`, `tcg/tci.c:366`). No part of the JIT is in tree.

- **Found supporting evidence for the JIT fallback**: Infinite Mac benchmarked
  qemu-wasm at 8 s on an MD5 workload against DingusPPC's 13 s and PearPC's 18 s
  — a JIT-equipped QEMU beats hand-ported emulators. Says nothing about TCI,
  which is what we currently have.
- **Corrected a stale assumption**: all four 1.x builds now reach the home
  screen natively (`IPHONE_OS_1X_VERSIONS.md`, 2026-07-26), including 1.0. The
  1.0-first plan is therefore feasible; earlier notes saying 1.0 was blocked on
  the ADM are out of date.

**The packaging gap for a 1.0-first release is closed** (2026-07-27). Every
build now uses one layout and an explicit `--build` — see
[`M68AP_BUILD_LAYOUT.md`](M68AP_BUILD_LAYOUT.md). The **iPhone OS 1.0 pack
exists and boots to the home screen**:

| | 1.0 (`1A543a`) | 1.1.4 (`4A102`) |
| --- | --- | --- |
| pack | **215.6 MiB, 106,858 pages** | 300.3 MiB, 148,812 pages |
| brotli, whole pack | **63.8 MiB (29.6%)** | 94.2 MiB (31.4%) |
| cold boot touches | **20,397 pages = 19.1%** | 25,030 pages = 16.8% |
| **first boot, chunked (62 pages) + brotli** | **18.6 MiB** | 20.9 MiB |
| home screen verified | 59.04% non-black | 73.95% non-black |

1.0 is both the first target and the **cheapest**: 18.6 MiB to first boot
against a 215.6 MiB raw pack — a 12× reduction, and a third of what a
compressed whole-pack download would cost.

**Measurements still owed:** the cold-boot working set for 1.0.2 and 1.1.1
(1.0 and 1.1.4 are done), and how much chunk content the versions share
(`measure-pack.py --cross`, which needs the other packs built first).

### Reproducing the measurements

```sh
# whole-pack compression and dedup
scripts/wasm/measure-pack.py <nand.pack>            # sampled, fast
scripts/wasm/measure-pack.py <nand.pack> --full     # exact, slow
scripts/wasm/measure-pack.py a.pack b.pack --cross  # sharing between versions

# cold-boot working set: trace a VERIFIED home-screen boot, then reduce it
cp -Rc m68ap-artifacts/builds/<BUILD>/nand /tmp/nand-clone   # APFS clone, ~0 B
IT_NAND_WRITABLE=1 IT_NAND_TRACE_PAGES=/tmp/boot.trace \
    python3 scripts/fb-snapshot.py --board m68ap --build <BUILD> \
    --boot-wait 420 --nand-m68ap /tmp/nand-clone --logs /tmp/fb
# require kernel_0x0f400000 nonzero_pct to be high before trusting the trace
scripts/wasm/analyze-nand-trace.py /tmp/boot.trace <nand.pack> \
    --pages-per-chunk 62 --prefetch prefetch.json
```

---

## Session — 2026-07-26: toolchain and tools

### What changed since the plan was written

The plan's two largest prerequisites are already done, which removes most of its
Phase 1:

- **The forward port happened.** This tree is QEMU **11.0.2**, not 6.2.50
  (`QEMU_11_PORT.md`).
- **Emscripten support is in-tree and upstream.** QEMU 11.0.2 already carries
  `host_os == 'emscripten'`, `--cpu=wasm64`, `util/coroutine-wasm.c`,
  `os-wasm.c`, `configs/meson/emscripten.txt`, and a container recipe at
  `tests/docker/dockerfiles/emsdk-wasm64-cross.docker`. No out-of-tree patch set
  is needed to *build*.
- **The packed NAND exists.** `scripts/pack-ipod-nand.py` and the reader in
  `hw/arm/ipod_touch_nand.c` already implement `IPODNAND` v1 (mmap'd, sorted
  index, binary search), and the packaged apps ship one. The plan's "specify and
  implement the pack" work is done for the read path. The copy-on-write overlay
  is still unimplemented.

### Proven this session

- **Docker is not required.** Emscripten **4.0.10** installs and runs natively on
  this arm64 macOS host (`emcc --version` verified). The container is retained
  only as the reproducible/CI path.
- **The host's Python is too old for emsdk** (Command Line Tools ships 3.9.6;
  emsdk requires ≥ 3.10). Rather than changing the host's Python — the native
  QEMU build depends on it — `setup-toolchain.sh` unpacks a pinned standalone
  CPython 3.12.13 into `.wasm-toolchain/`.
- **meson comes from this tree.** QEMU vendors `python/wheels/meson-1.10.0.whl`;
  the toolchain venv installs it offline, so no system meson is needed.
- **zlib 1.3.1, libffi 3.5.2 and pixman 0.44.2 cross-compile to wasm64**
  natively, same versions and flags as the upstream container.
  - **macOS-only correction:** zlib's `configure` detects a Darwin *build* host
    and swaps the archiver for Apple's `libtool`, which rejects emcc's wasm
    objects (`adler32.o is not an object file`). `--uname=Linux` forces the
    generic branch and keeps `AR=emar`. The container never hits this because
    its build host is Linux.

### Blocked: the host disk is full

The volume is at **100% (≈300 MiB free of 228 GiB)**. Two steps died on
`ENOSPC` and are unfinished, not broken:

- **glib 2.84.0** did not install (`target/lib/pkgconfig/glib-2.0.pc` absent);
  its build tree plus the pcre2 subproject needs roughly 1–2 GiB.
- **Asset staging** failed mid-copy of the 300 MiB `nand.pack`; the partial
  `web/public/assets/` was deleted.

Rough space needed to finish: **~1–2 GiB** for glib, **~2–3 GiB** for the QEMU
wasm build tree, **~300 MiB** for the staged asset set. `.wasm-toolchain/` is
currently 1.8 GiB (1.6 GiB of that is the emsdk itself).

### Tools added (`scripts/wasm/`)

| tool | purpose |
| --- | --- |
| `toolchain.env` | every pinned version in one place |
| `setup-toolchain.sh` | standalone Python (if needed) + emsdk 4.0.10 + meson venv |
| `build-deps.sh` | wasm64 zlib/libffi/pixman/glib into `.wasm-toolchain/target` |
| `build-toolchain.sh` | the container alternative (reproducible/CI) |
| `build-qemu.sh` | `arm-softmmu` for wasm64/TCI into `build-wasm/` |
| `stage-assets.py` | asset set + hashed `asset-manifest.json` |
| `serve.py` | COOP/COEP static server with byte ranges and `--check` |

Plus a `web/` shell: capability gate, asset loader (download → verify →
Cache Storage), worker lifecycle, canvas display target, and the input bridge
(pointer with capture, Home/Power, the native `H`/`P` bindings, blur cleanup).

### Decisions taken

- **Native toolchain by default, container for releases.** Nothing about the
  build requires Linux, and routine work should not need a running daemon.
- **wasm64 + TCI.** QEMU 11.0.2's meson refuses a WebAssembly host without
  `--enable-tcg-interpreter`; there is no in-tree WebAssembly TCG backend at
  this version. TCI is the correctness path and the first measurement.
  `--wasm64-32bit-address-limit` keeps the address space at 32 bits, which the
  128 MiB guest never exceeds.
- **Assets stay out of git.** `web/public/assets/` and `web/emulator/` are
  ignored; staging needs explicit paths or one `--from-app` bundle.

### Not yet done / open

- **The wasm QEMU build has never been run to completion.** Expect real work in
  the iPod device code: it uses `fopen`/`g_mapped_file` per artifact, and mmap
  over a ~300 MiB pack in MEMFS is the first memory question to measure.
- **No display bridge.** QEMU is built `-display none`; the plan's display
  listener (surface + dirty rects → `OffscreenCanvas`) is unwritten, so the
  worker's `frame` message has no producer yet.
- **No input path into QEMU.** The frontend emits input events; the worker
  drops them until the bridge exists.
- **The worker's module instantiation is provisional** — written against
  Emscripten's documented shape, to be re-checked against the first real
  `qemu-system-arm.js`.
- **No copy-on-write overlay.** Guest writes have nowhere to go in the browser;
  the base pack must stay immutable.
- **Performance is unmeasured.** If TCI cannot reach an acceptable time to
  SpringBoard, the out-of-tree [qemu-wasm](https://github.com/ktock/qemu-wasm)
  JIT becomes a requirement rather than an optimization. That is the next
  go/no-go signal.
  One lever exists on the guest side: SpringBoard currently composites in
  SOFTWARE (`LK_ENABLE_MBX2D=0`) because the MBX is a stub, so that work is
  guest ARM instructions running through the interpreter. Modelling MBX 2D
  would move it into compiled wasm — a bigger win under TCI than natively.
  Unmeasured; sequencing and how to measure it are in
  [`MBX_HANDOFF.md`](MBX_HANDOFF.md) §4-5. It is NOT a substitute for the
  TCI-vs-JIT decision, and should not be attempted alongside the display
  bridge.
- **`crypto.subtle` has no streaming digest**, so the loader hashes the whole
  300 MiB pack in memory. Acceptable on desktop; revisit for constrained hosts.

### Reproduction

```bash
scripts/wasm/setup-toolchain.sh
scripts/wasm/build-deps.sh
scripts/wasm/build-qemu.sh
scripts/wasm/stage-assets.py --from-app "/Applications/iPhone 2G (iOS 1.1.4).app" \
    --board m68ap --firmware 1.1.4
scripts/wasm/serve.py            # then open http://localhost:8010
```
