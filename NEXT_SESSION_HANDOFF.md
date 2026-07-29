# Next session — start here

**Date:** 2026-07-26 · **Branch:** `ipod_touch_1g`

> **2026-07-27 — the active thread is the browser/WebAssembly port.**
> Start at [`BROWSER_WASM_HANDOFF.md`](BROWSER_WASM_HANDOFF.md): iPhone OS 1.0
> and 1.1.4 are packaged and measured, the Emscripten toolchain is installed,
> and the next step is finishing the wasm build of QEMU. The M68AP build system
> was reorganised the same day — every firmware now lives in
> `m68ap-artifacts/builds/<BUILD>/` and every tool takes an explicit `--build`
> ([`M68AP_BUILD_LAYOUT.md`](M68AP_BUILD_LAYOUT.md)), so commands quoted below
> need their paths translated.

> **2026-07-28 — MBX has its own handoff now.** The PowerVR MBX stub is what
> the TVOut swap-device window and the `LK_ENABLE_MBX2D=0` guest plist edit
> both stand on (tasks T1/T2). What is known, what is already ruled out, how it
> interacts with the browser port, and where to start are collected in
> [`MBX_HANDOFF.md`](MBX_HANDOFF.md). Touch across all three app bundles is
> fixed and written up in
> [`TOUCH_INVESTIGATION.md`](TOUCH_INVESTIGATION.md).

> **2026-07-28 — OPEN: on iPhone OS 1.0, Home and Power do nothing while an
> app is frontmost.** They work on SpringBoard (P sleeps, H wakes to the lock
> screen, slide-to-unlock works). Open an app and both go dead. Only surfaced
> now because touch on 1.0 was broken until today, so nobody had ever opened an
> app there; whether 1.1.4 shares it is UNVERIFIED.
>
> **UPDATE 2026-07-28 (2) — the "all three bundles" claim was WRONG, and so
> was the harness that produced it.** Re-measured with `scripts/app-button-probe.py`:
>
> | board | open app | touch in app | HOME returns | POWER sleeps |
> |---|---|---|---|---|
> | iPod Touch (N45AP) | PASS | PASS | **PASS** | **PASS** |
> | iPhone OS 1.1.4 | PASS | PASS | **PASS** | **PASS** |
> | iPhone OS 1.0 | PASS | PASS | **FAIL** | **FAIL** |
>
> So it IS 1.0-specific, exactly as first reported. The earlier "all three"
> result came from pressing buttons with QMP `send-key`; with that, POWER does
> not sleep the device even from SpringBoard, where it demonstrably works. A
> held press — key down, 150 ms, key up, the form `lock-unlock-probe.py` has
> always used — makes the iPod and 1.1.4 pass every step.
>
> **What is ruled out for 1.0, measured (`IT_SYSIC_TRACE=1`):** the interrupt
> path is not the problem. Home raises the right group and bit (group 1 bit 8 =
> IRQ 0x28, the M68AP menu IRQ), the guest reads INTSTAT, reads INTLEVEL, ACKs,
> and re-reads — and the WORKING iPod produces a byte-identical conversation
> (its own group 1 bit 14 = IRQ 0x2E). Delivery, servicing and acknowledgement
> are all fine on both. INTLEVEL always reading 0 is NOT the discriminator: the
> iPod reads it too and still works.
>
> So the divergence is above the interrupt controller — in what iPhone OS 1.0's
> kernel/SpringBoard does with an already-acknowledged button event while an
> app is frontmost. Next: PC-sample 1.0 during the press
> (`scripts/m68ap-freeze-probe.py`), and read 1.0's SpringBoard menu-button path
> — its kexts keep full C++ symbols, the trick that cracked the multitouch
> protocol (see TOUCH_INVESTIGATION.md).
>
> Reproduce: `scripts/app-button-probe.py --board m68ap-10 --logs /tmp/x`

The sections below remain the record of the 2026-07-26 session.

Both problems the previous handoff carried are addressed in this session:

* **T6 — the iPhone app pegged a host core: SOLVED.** The generated `/var`
  was missing the OS's own directory skeleton. Idle CPU 98% → **6–10%**, and
  the guest log now contains **zero** SQLite errors (it had 15 592 AddressBook
  lines per boot).
* **T7b — the iPhone would not wake (P sleeps, H does nothing): FIXED in the
  model.** The key handler used the iPod's Home pin/IRQ on both boards.

```bash
open -a "/Applications/iPhone 2G (iOS 1.1.4).app"     # the iPhone
open -a "/Applications/iPod Touch.app"    # the reference
```

---

## T6 — what it actually was

**Not** storage, not SQLite locking, not a missing database. `/var` is a
separate HFS partition we generate ourselves, and it shipped a **hand-written
8-directory list** (`MINIMAL_DIRS` in `build-m68ap-var.py`). A real device's
`/var` is laid down by the restore ramdisk from a template that the **root
filesystem itself carries at `/private/var`** — 73 entries with their real
modes:

```
tmp (1777)  run  preferences  logs  log  db/{dyld,timezone}  Keychains
vm  msgs  empty  mobile/{Library,Media}  root/Library ...
```

Without them every daemon that writes to /var failed;
`com.apple.AddressBook` (`ABDatabaseDoctor`) failed `CREATE TABLE` with
SQLITE_BUSY and retried ~250×/s forever, which is what burned the core.

**The fix** — `scripts/build-m68ap-var.py --template-from <root image>`,
wired into the product recipe (`build-m68ap-homescreen-nand.py`). Measured on
the app's own artifacts: home screen renders (69.6% non-black on all three FB
bases), **0** `database is locked`, **0** `no such table`, idle CPU 6–10%
(the iPod idles at 11–15%). The guest even gets a sane clock now, because
`/var/db/timezone` exists.

**Ruled out along the way — do not re-chase** (each measured this session):

| Hypothesis | Verdict |
|---|---|
| The guest cannot READ the seeded database | **False.** Corrupting the file's SQLite magic *through a NAND page override* made SpringBoard report `SQLITE_CORRUPT encountered while accessing /var/mobile/Library/AddressBook/AddressBook.sqlitedb`. The bytes reach SQLite. |
| Two `ABDatabaseDoctor` instances locking each other out | **False.** Only ever pid 16 in the log. |
| The database is under the wrong `$HOME` (the real iPod keeps its under `/var/root`, because on 1.1 the daemon runs as root; on 1.1.4 it runs as `mobile`) | **False.** Adding a second copy at `/var/root/Library/AddressBook/` changed nothing. |
| The volume's case-sensitivity | **False.** Ours and the real device are both HFSX. |
| SQLite page size (guest ships 3.1.3) | **False.** The real device's own database is page-size 4096, same as ours. |

## T7b — what it was

`hw/arm/ipod_touch.c` used `GPIO_BUTTON_HOME` (0x1606) and
`GPIO_BUTTON_HOME_IRQ` (0x2E) for **both** boards. M68AP's device tree puts
`button_menu` on **0x1600** and never lists 0x2E. Power/`hold` is 0x1605 on
both boards, which is exactly why P worked and H did not.

The IRQ pairing is a **derivation, not a measurement**: N45AP fixes the rule
`IRQ = 0x28 + (pin & 0xf)` (0x1605→0x2D, 0x1606→0x2E), and applying it to
M68AP's five pins reproduces its device tree's interrupt SET exactly
(`0x2d 0x28 0x29 0x2a 0x2b`, with 0x2C absent because pin 0x1604 is unused).
So Home/menu = **0x1600 / IRQ 0x28**.

Corroborated by a second field: `interrupts` is five `(irq, trigger)` pairs
(`0x2d 7  0x28 7  0x29 5  0x2a 5  0x2b 7`), and under this pairing trigger 7
falls on every pin whose GPIO flags are 0x100 and trigger 5 on every pin whose
flags are 0x000 — a perfect split, whereas pairing by the DT's stored property
order would need `7,7,5,7,5`. Still inference, not an observed acknowledge:
`IT_M68AP_HOME_IRQ=<n>` overrides it without a rebuild, and
`IT_GPIO_TRACE=stderr` during a Home press is how to settle it outright.

---

## Tools added this session

* **`scripts/overlay-hfs-into-nand.py`** — drop an HFS partition image into a
  NAND tree as `bank<N>/<page>.page` overrides (spare copied from the pack).
  Changing `/var` and booting is now ~1 second + a boot instead of a full NAND
  rebuild. This is what made T6 tractable; use it for any "what if /var looked
  like this?" question.

  ```bash
  scripts/extract-hfs-from-nand.py "<bundle>/…/nand" /tmp/var.img \
      --active-banks 4 --partition data          # get the pristine partition
  # …edit /tmp/var.img…
  scripts/overlay-hfs-into-nand.py --nand /tmp/nand-test --image /tmp/var.img \
      --pack "<bundle>/…/nand/nand.pack" --partition data --active-banks 4
  S5L8900_STAGE_NAND=0 S5L8900_NAND=/tmp/nand-test IT_NAND_WRITABLE=1 \
      S5L8900_DEBUG=1 "/Applications/iPhone 2G (iOS 1.1.4).app/Contents/MacOS/iPod Touch"
  ```

* **`scripts/extract-hfs-from-nand.py` — two real bugs fixed.** It addressed
  pack entries as if `active_banks` were always 8, so on a generated 4-bank
  M68AP NAND it read the wrong pages and died with "invalid HFS volume
  signature"; and it refused any pack with holes, which every generated
  (sparse) NAND has. Holes now read as zeros, exactly as the QEMU model reads
  them. Both partitions of the shipped iPhone NAND now extract.

* `S5L8900_DEBUG=1` on either bundle gives serial on stdout — the cheapest
  window into the guest, and how all of the above was measured.

* **`scripts/nor-image-store.py` + `scripts/verify-boot-logo.py`** (2026-07-27,
  from the boot-logo fix). The first walks a NOR's IMG2 store the way iBoot
  does and reports **reachable** separately from **present** — a store can hold
  all seven images and expose only one. The second boots and asserts the logo
  is lit early, defaulting to the installed bundle. Both are gates
  (`--check`, non-zero exit), and `verify-boot-logo.py` was confirmed to FAIL
  on the pre-fix NOR, so neither is a check that cannot fail.

  ```bash
  python3 scripts/nor-image-store.py <nor.bin> --check --expect 7
  python3 scripts/verify-boot-logo.py --app "/Applications/iPhone 2G (iOS 1.1.4).app"
  python3 scripts/test-nor-image-store.py     # fixture tests, no Apple payloads
  ```

  Run both after regenerating any NOR or touching `ipod_touch_lcd.c`.

* **`install-iphone-firmware.py --keep-existing`** — partial firmware update:
  reuses the installed iBoot/NAND for anything not passed, so a NOR-only swap
  is one command. It also now carries the `epoch` file across, which it
  previously dropped (silently wedging a 1.0 or 1.1.1 bundle in iBoot).

## Still open

* **T1/T2** — model the MBX (swap completion + 2D) and drop
  `LK_ENABLE_MBX2D=0`.
* ~~**The M68AP kernel framebuffers stay black for a long time.**~~
  **NOT AN ISSUE — I measured the wrong artifact (corrected 2026-07-27).**
  The observation was real (110 s in, `0x0f400000` and `0x0f496000` both 0.0%
  with SpringBoard running) but it was taken against
  `m68ap-artifacts/builds/<BUILD>/nand`, the **plain** NAND, whose own
  provenance reads `"root and data HFS+ partitions placed; GPT/MBR
  synthesised"`. The three things M68AP needs to render at all — the lockdownd
  activation patch, `LK_ENABLE_MBX2D=0`, and the reference-shaped data ark —
  are applied by `build-m68ap-homescreen-nand.py`, which only
  `package-iphone-app.sh` runs. The plain NAND was never going to render.

  On the bundle, sampled every 8 s: logo at t=10 and t=18, and by **t=26 s**
  both kernel buffers are at ~74% with the screen at 69.75%. Clean handoff.

  **The general trap:** `builds/<BUILD>/nand` and the bundle's NAND are not the
  same artifact, and only the latter is expected to render. Judge anything
  about rendering on the bundle — which is ground rule 1 above, and this is
  what ignoring it looks like.
* **Bundles predate the boot-logo fix.** Both the NOR builder change and the
  LCD window change are needed; the installed `/Applications/iPhone 2G*.app`
  bundles carry neither, and their `nor.bin` must be regenerated (not just the
  engine replaced). The N45AP bundle also benefits — the iPod now shows the
  logo from ~4 s instead of ~13 s.
* **T5** — drive the M68AP button pins from reset instead of at the kernel
  banner.
* ~~Repackage the iPhone bundle~~ **done** — `/Applications/iPhone 2G (iOS 1.1.4).app`
  carries both fixes (engine + regenerated NAND) and passes
  `lock-unlock-probe.py --app` 2/2 with **47 touch frames consumed per
  cycle**. The iPod bundle was left alone: the engine change is
  board-conditional and nothing in it touches N45AP.

## Ground rules (carried forward, all still true)

1. **Test the packaged bundle, not the repo build.**
2. **Never test headless** — `-display none` means no `gfx_update`.
3. **Assert at the model boundary, not on pixels.**
4. **If the harness cannot observe the thing under test without changing it,
   the harness is wrong.**
5. **Disk fills fast and fatally.** Clean `/tmp/nand-*`, `/tmp/sblab-*`,
   `/tmp/s5l8900-nand.*` (the launcher's per-launch clones leak when a run is
   killed) and detach `hdiutil` mounts. In zsh a non-matching glob aborts the
   whole `rm` line — loop over paths instead.

## Reference documents

* `M68AP_RENDER_HANDOFF.md` — §0 task table (T1–T8), current status.
* `M68AP_HOMESCREEN_CASE_STUDY.md` — how the home screen and the touch bug
  were solved, including every dead end.
* `IOS_1_0_BRINGUP_CASE_STUDY.md` — the 1.0/1.0.2 bring-up as *process*: the
  six fixes, the claims asserted without measuring, the hypotheses killed (the
  NAND signature among them), and the rabbit holes with the escape from each.
  Read it before touching the NAND/FTL path or hand-disassembling iBoot.
* `IPHONE_OS_1X_VERSIONS.md` — the per-build measurement matrix (format,
  epoch, iBoot, FIL signature, root FS) and the open-issue list.
* `IPHONE_2G_BRINGUP_HANDOFF.md` — long-form log.
* `BUILD.md` §2b — one-command packaging for both bundles.

---

## The in-app HOME wedge on iPhone OS 1.0 — localised (2026-07-28, later session)

**Not an ignored button, and not a spin. The guest goes IDLE and never wakes.**
That is a different bug from the one the earlier entry describes, and it
explains why the interrupt-path investigation correctly found nothing wrong.

The user's report is the thing that reframed it: *"I open an app, once H is
pressed it doesn't work, and touch is blocked too."* HOME does not do nothing —
it takes the system somewhere it cannot leave.

### What the sequence actually does

Reproduced with `app-button-probe.py --board m68ap-10` plus
`S5L8900_DEBUG=1 IT_MT_TRACE=1 IT_SYSIC_TRACE=1 IT_LCD_TRACE=1`:

1. **The interrupt is delivered and acknowledged.** SYSIC group 1 bit 8
   (IRQ 0x28): read INTSTAT → read INTLEVEL → ACK → re-read, twice, once for
   press and once for release. Confirms the earlier finding.
2. **The guest reacts, and the app really does close.**
   `IOCoreSurfaceRootUserClient::attach`, then the app's
   `IOMobileFramebufferUserClient::detach` and
   `IOCoreSurfaceRootUserClient::detach`. HOME *works*; the teardown succeeds.
3. **Then everything stops at once.** The LCD window base had been flipping
   0x0f496000 → 0x0fe00000 → 0x0f400000 continuously; the last flip is the line
   immediately before the keypress and there is never another. `[MT] frame
   consumed` stops at the same moment — which is exactly the "touch is blocked
   too" the user saw.

### Where the CPU is

`scripts/home-wedge-probe.py` (new) repeats the sequence and PC-samples over
QMP. 40 samples after the press:

```
  c005a2ec  x32   CPSR 0x600000d3   (SVC, I=1 F=1)
  c0060654  x5    64-bit counter read, hi/lo consistency loop -- timekeeping
  c0060658  x2
  c0435474  x1
```

0xc005a2ec is inside the kernel **idle** path, not SpringBoard:

```
  c005a2e4  mcr  p15, #0, r4, c7, c10, #4    DSB
  c005a2e8  mcr  p15, #0, r4, c7, c0,  #4    WAIT FOR INTERRUPT
  c005a2ec  subs r3, r3, #1                  <-- 80% of samples
  c005a2f0  bne  #0xc005a2ec                 bounded delay, r3 starts at 0x4b0
```

So the system is parked at WFI with interrupts and FIQs masked, cycling a short
delay. Nothing is runnable. This is a **missing wakeup**, not a deadlock and not
a livelock: SpringBoard tore the app down, went idle, and no event ever arrives
to make it repaint the home screen.

### The lead worth taking next

What should wake it is the obvious question, and the strongest candidate is a
**display/vsync interrupt that our LCD model stops delivering** once the app's
framebuffer client detaches. The evidence that points there is the coincidence
in (3): base flips and touch consumption stop on the same line, and both are
downstream of the compositor being scheduled. Check whether the LCD raises a
periodic IRQ at all, whether it stops at the detach, and compare against 1.1.4
and the iPod, which pass this step.

Ruled out already, so do not re-do them: interrupt delivery/ACK (measured
byte-identical to the iPod), the button model itself, and — from the same
session — the device-tree radio properties, which cause a *different* touch
failure by a different mechanism (see TOUCH_INVESTIGATION.md).

**Harness rule, learned expensively.** `touch-probe.py` runs `-display none`,
under which QEMU never calls `gfx_update` and every touch is refused; it reports
`no-response` for known-good bundles. Use `app-button-probe.py` or
`home-wedge-probe.py`, which attach a display client. A negative from a harness
that cannot produce a positive is not evidence.

### The HOME wedge: what the mask means (and a WRONG root cause, retracted)

`IT_FB_TRACE=1` now also reports the vsync state once a second
(`refresh_timer_tick`), because the MMIO trace suppresses a register after its
8th access and that hid exactly the question that mattered. After the HOME
press, every second:

```
[FB] vsync t=41s status=0x00000001 mask=0x00003f00 irq=0 acked_since_last=NO
```

* `int_status` bit 0 is set — the model's refresh timer is alive and still
  raising a frame interrupt every tick. The timer is free-running and re-arms
  unconditionally, so "the vsync stopped" was the wrong hypothesis.
* `int_mask` is **0x3f00 — bit 0 CLEAR**. The gate is
  `qemu_set_irq(irq, (int_status & int_mask) != 0)`, so `0x1 & 0x3f00 == 0` and
  **the line is never asserted**. `irq=0` every single tick.
* Earlier in the same boot the guest had `mask = 0x3f01`, bit 0 set, which is
  why the frame interrupt was delivered and the compositor ran.

So during the app teardown iPhone OS 1.0 narrows the LCD interrupt mask from
0x3f01 to 0x3f00, and from that moment our frame interrupt is masked out. The
guest goes idle waiting for a frame interrupt that the model is signalling on a
bit the guest is not listening to.

**The likely model bug: `s->int_status |= 1` in `refresh_timer_tick` is the
wrong bit.** A guest that deliberately enables bits 8..13 and disables bit 0,
while still expecting frame callbacks, is telling us the real frame/vsync
source is one of 8..13 and bit 0 is something else. Bit 0 happens to be enabled
during early boot, which is why every result to date looked correct.

Next step: identify which of bits 8..13 is the frame interrupt (the LCD
register block at 0x14/0x18 and 1.0's `AppleH1CLCD` are the sources), raise
that bit instead of/in addition to bit 0, and re-run
`app-button-probe.py --board m68ap-10` — steps 3 and 4 should start passing.
Verify 1.1.4 and the iPod, which currently pass step 3, do not regress: they may
simply keep bit 0 enabled and would be unaffected either way.


---

## Boot reliability: the Apple-logo hang is a virtual-time artifact (2026-07-28)

**Symptom:** the iPhone bundles intermittently never get past the Apple logo.
Measured on the shipped 1.1.4 bundle: **1 panic in 3 boots**, at
`IOIpodUSBDevice::start` on `AppleS5L8900XIpodHAL`.

**Cause,** from the parallel wasm work the same evening: without `-icount`,
`QEMU_CLOCK_VIRTUAL` follows wall clock while the guest runs slower than real
hardware, so iPhone OS sees its own driver `start()` calls taking many SECONDS
and takes timeout paths a real device never takes.

**Fix, shipped:** `ipod-app-launcher.sh` now passes `-icount shift=1` for the
`iphone-2g` profile. Measured after: **0 panics in 4 boots**, and the LCD
readiness gate arms. Opt out with `S5L8900_ICOUNT=0`, or set another shift.

N45AP is deliberately left alone. It does not hit this, and its verified
sleep/wake results were all measured in real time; changing its clock model
would invalidate them for no benefit.

**Regression-checked before shipping:** the 1.0 bundle is unchanged with icount
(`1_open_app` PASS 97.10%, `2_touch_in_app` PASS 35.63%, steps 3/4 fail exactly
as before), so this buys boot reliability without disturbing what worked.

### The HOME "freeze" is PRESENTATION, not the guest (2026-07-28, late)

Two measurements settle what H actually does on iPhone OS 1.0.

**1. The guest returns to SpringBoard.** `home-wedge-probe.py` now dumps all
three framebuffer bases after the press. Every one reads 45.56% non-black --
the home screen's signature (the app is 99.1%) -- and rendering
`fb_kern0.raw` to PNG shows the **full home screen, all twelve icons and the
dock**. HOME works. The app tears down and SpringBoard repaints.

**2. The host presenter stops running.** With `IT_FB_TRACE=1`, `lcd_refresh`
(the `gfx_update` callback, i.e. what actually pushes pixels to the window) now
reports how often it runs and whether dirty tracking gave it anything. In one
full probe run:

```
vsync   t= counter reached 37     <- the guest's frame timer, alive throughout
present t= counter reached  1     <- the presenter ran ~60 times, then stopped
```

The guest-side frame timer ticked ~37x longer than the host presenter ran. A
window repainted ~60 times in a multi-minute run is, to a user, frozen. So the
freeze is on the HOST side of the display, not in iPhone OS.

**This also explains the "touch is blocked" half of the report**: touch is very
likely working -- the user is tapping a home screen the window is not showing.

### The probe's screen capture is unsound for this test

`app-button-probe.py`'s `grab()` returns *"the liveliest of the three
framebuffer bases"* -- the one with the most non-black pixels. That silently
fails whenever the NEW screen is DIMMER than stale content left in another
buffer:

* 1.0 step 3 is app (99.1% lit) -> home (45.4%), i.e. dimmer. Any buffer still
  holding the app wins, and the step reports `0.00% changed` even when the
  transition happened.
* 1.1.4's home screen is 69.5% lit; if its app is dimmer than that, `grab`
  keeps returning the home buffer and `1_open_app` reports a false FAIL. That
  is the most likely explanation for 1.1.4 "regressing" at step 1 today, and it
  should be re-checked before anyone bisects engine commits looking for it.

**Fix the measurement before trusting any button verdict.** `grab()` should read
the CURRENT SCANOUT (`w1_framebuffer_base`, falling back to w2 -- the model's
own `lcd_scanout_base()` rule), not a brightness heuristic. Steps also need
longer waits under `-icount`, which slows the guest in wall-clock terms: a run
today had every step land one action late (`4_power_sleeps` "passing" with a
97% change that was actually the app finally opening).

### Where to look next

The presenter is driven by the display backend's refresh, so the question is why
`gfx_update` stops being requested for this console. Check whether it is the
VNC harness (client stops asking) or QEMU's console refresh throttling, then
confirm against the iPod, which passes every step with the same LCD model.

### Errors I shipped in this session, and what each one cost

Recorded because every one of them wasted the user's time, and three of them
were the *same* mistake in different clothes: changing a shipped bundle without
re-verifying the thing I had just changed.

**1. An empty bash array killed the iPod app outright.** The icount change added
`QEMU_ICOUNT=()` and expanded it as `"${QEMU_ICOUNT[@]}"`. macOS ships **bash
3.2**, where expanding an EMPTY array under `set -u` is an "unbound variable"
error. The iPhone profiles fill the array, so they worked and I tested only
those; the iPod, which deliberately gets no icount, took the empty path and died
before QEMU started:

```
/Applications/iPod Touch.app/Contents/MacOS/iPod Touch: line 255: QEMU_ICOUNT[@]: unbound variable
```

Fixed with the 3.2-safe guard `${QEMU_ICOUNT[@]+"${QEMU_ICOUNT[@]}"}`. **Rule:
a launcher change is not tested until all three bundles have been launched.**
The two-second check that would have caught it is the one at the end of this
section.

**2. Re-signing a bundle kills the copy the user is running.** Twice I ran
`install-ipod-app-engine.sh` / `codesign --force` on a bundle while the user had
it open. macOS SIGKILLs a process whose signed binary is replaced underneath it
(`EXC_BAD_ACCESS`, `SIGKILL (Code Signature Invalid)`, `CODESIGNING / Invalid
Page`), so the window vanishes with no dialog and no guest panic. It reads
exactly like an emulator crash, and I first went looking for one. **Announce
before touching `/Applications`, or work on a copy.**

**3. Two confident root causes that were wrong.** "1.0 Wi-Fi SOLVED" (the driver
was still failing further down the same log I had only read the head of), and
"the model raises the wrong LCD interrupt bit" (disassembling `AppleH1CLCD`
showed bit 0 IS the frame interrupt). Both are retracted in place above. **Read
the whole log, and read the guest's code before naming a cause.**

**4. A measurement that could not produce a positive.** `touch-probe.py` runs
`-display none`, under which QEMU never calls `gfx_update` and every touch is
refused. I used it to "clear" a change of having broken touch; it returned
`no-response` for the filled NOR, the reverted NOR, AND for 1.1.4, whose touch
was known to work. The third result was the disproof and I read past it.

**5. A brightness heuristic that hid real transitions.** `app-button-probe.py`'s
`grab()` returned "the liveliest of the three framebuffer bases". Any step where
the new screen is DIMMER than stale content elsewhere reads as `0.00% changed` --
which is 1.0's app->home step exactly. This produced a false "1.1.4 touch
regressed today" that I reported to the user. Now fixed: `grab()` keeps all
three buffers and `changed()` takes the largest per-base difference.

**6. A finding that was an artifact of my own harness.** "The host presenter
stops running" was measured under VNC. On the cocoa path the app actually uses,
`present` and `vsync` counters run 1:1 (`present:8 vsync:8`). Retracted before
it reached a fix. **Measure on the path the user runs.**

### The check that catches most of this

```bash
for a in "iPod Touch" "iPhone 2G (iOS 1.0)" "iPhone 2G (iOS 1.1.4)"; do
  "/Applications/$a.app/Contents/MacOS/iPod Touch" -display none & sleep 12
  pkill -f qemu-system-arm
done
```
Anything that prints a shell error instead of boot output is broken for the user
before QEMU is even involved.

### WITHDRAWN: "the in-app HOME failure is a BOARD problem"

`09fdcb46f2` concluded "Only iPhone OS 1.0 fails". That was measured with the
brightness-heuristic `grab()` (see above), which cannot see a transition to a
dimmer screen. Re-measured with the fixed capture and `IT_PROBE_WAIT=8`:

| board | 1_open_app | 2_touch_in_app | 3_home_returns | 4_power | 5_home_wakes |
|---|---|---|---|---|---|
| iPod (N45AP) | PASS | PASS | **PASS 98.47%** | PASS | PASS |
| iPhone OS 1.0 | PASS 97.11% | PASS 35.95% | **FAIL 0.00%** | FAIL | — |
| iPhone OS 1.1.4 | PASS 25.76% | FAIL 0.32% | **FAIL 0.04%** | PASS | PASS |

**This table was wrong about 1.1.4, and so was the conclusion drawn from it.**
1.1.4 never opened an app in that run: iPhone OS 1.1.4 shows an educational
"Edit Home Screen" modal on FIRST LAUNCH, and every launch here is a first
launch because the launcher clones a pristine NAND each time. The modal sits
over the home screen until dismissed, so the icon tap did nothing, and steps 2-5
measured a dialog. The modal changed 25.76% of pixels, which sailed past step
1's bare `d > 20` verdict.

With the modal dismissed first, **1.1.4 passes all five steps**, including
`3_home_returns` at 97.00% (lit 98.8 -> 47.4). A human doing it by hand always
knew this; the probe did not. `DISMISS` in `app-button-probe.py` now handles it,
and step 1 additionally requires the screen to become APP-LIKE (a lit-fraction
move > 8) so a modal can never satisfy it again.

So the original framing stands: **only iPhone OS 1.0 fails, on the same board
that 1.1.4 passes.**

What is measured about the M68AP HOME press, with an app frontmost:

* The IRQ is raised, read, INTLEVEL-read, ACKed and re-read -- group 1 bit 8
  (IRQ 0x28), press and release, byte-identical in shape to the iPod's.
* The guest then does **nothing**: no LCD register writes, no client teardown,
  no framebuffer change in any of the three buffers, for 64 s.
* The iPod in the same situation starts writing LCD registers (`0x020`, `0x00c`,
  `0x0d8`...) in the very next lines after the keypress.

**Tried and rejected: publishing the pin level in INTLEVEL.** The model never
set `gpio_int_level`, so every guest read "released" even on a press -- a real
infidelity, and the obvious candidate for "IRQ arrives, nothing happens".
Implemented it (mirror `gpio_state` into the level register on press/release)
and it changed nothing: `3_home_returns` still 0.00%. **Reverted** rather than
left in as an unvalidated guess.

Next, given the board framing: the M68AP HOME button is on its own pin and IRQ
(`c187fb682d`, `c50c158e01` -- button_menu, IRQ 0x28, group 1 bit 8, not the
iPod's). The iPod works with the same kernel-visible sequence, so what differs
is what ELSE the M68AP guest expects to see around that pin -- a second GPIO it
polls, a different pin for the same logical button, or a level/edge expectation
the pulse model does not meet. Diff the two boards' GPIO traffic across the
press (`IT_GPIO_TRACE=1`), not their SYSIC traffic, which is already known to
match.


## The definitive HOME-from-an-app comparison (2026-07-29)

Same board, same engine, same harness, modal handled, `IT_PROBE_WAIT=8`. This is
the measurement every earlier conclusion about this bug should have been based
on.

```
1.1.4 (WORKS)                                 1.0 (FAILS)
  keycode=35                                    keycode=35
  rd INTSTAT  group 1 = 0x00000100              rd INTSTAT  group 1 = 0x00000100
  rd INTLEVEL group 1 = 0x00000000              rd INTLEVEL group 1 = 0x00000000
  ACK INTSTAT group 1 = 0x00000100              ACK INTSTAT group 1 = 0x00000100
  rd INTSTAT  group 1 = 0x00000000              rd INTSTAT  group 1 = 0x00000000
  keycode=163  (+ the same four lines)          keycode=163  (+ the same four lines)
  [LCD] w1 base <- 0x0f496000                   -- nothing --
  [LCD] w1 base <- 0x0fe00000                   -- nothing --
  [LCD] w1 base <- 0x0f400000                   -- nothing --
  ...compositor animates back to SpringBoard    ...next event is the probe's POWER press
```

**The kernel-visible interrupt handling is byte-identical**, INTLEVEL=0 included.
1.1.4 then drives the display; 1.0 does nothing and returns to the idle WFI loop
(PC 0xc005a2ec, CPSR 0x600000d3).

Ruled out by measurement, so do not re-do these:

* **The pin.** Both device trees encode `function-button_menu` identically --
  `...4f495047 0016 0000 0001 0000`, i.e. GPIO 0x1600 / IRQ 0x28. Only the
  phandle differs. The model's hardcoded pin is right for both.
* **INTLEVEL.** Publishing the real pin level instead of a constant 0 changed
  nothing (tried, reverted).
* **The interrupt path.** Raised, read, ACKed, re-read -- identical on both, and
  matching the iPod's shape.
* **The display/presenter.** Runs 1:1 with the guest frame timer on the cocoa
  path; the earlier "presenter stops" was a VNC-harness artifact.

So the difference is entirely in what iPhone OS 1.0 does with an
already-acknowledged button while an app is frontmost. The next probe has to see
into that: PC-sample DURING the press rather than after it (the after-state is
just the idle loop), or find where 1.0's kernel posts the button event and check
whether it posts at all.

## PC-sampling across the press: what it showed, and why it cannot decide this

`home-wedge-probe.py` now dismisses the first-launch modal, opens an app, and
samples the PC **across** the press (`--samples`, `--interval`), splitting the
histogram into before/after and listing PCs seen only after.

**The instrument perturbs the thing it measures.** Each sample is a QMP
`human-monitor-command "info registers"`, which stops the vCPU. At 50 Hz over
32 s, **1.1.4 did not return to SpringBoard either** -- its framebuffers still
read 98.94% (the app) at the end, though the same build passes the same step at
97% when left alone. So neither run reached the transition, and the comparison
cannot discriminate. Any future PC sampling here needs a much lower rate, or an
instrument that does not stop the CPU.

With that caveat, the data (2400 samples, press at 1/3, 20 ms apart):

| | 1.1.4 (works unperturbed) | 1.0 (fails) |
| --- | --- | --- |
| dominant PC after press | `c005a9cc` x1574 (idle WFI) | `c005a2ec` x1504 (idle WFI) |
| counter reads | `c0061654/58/5c` | `c0060654/58/5c` |
| distinct USERLAND PCs after press | 3 (`0x30...`) | **8** (`30e980b0`, `30af7150`, `3045fd94`, `30e928fc`, `30e99ce4`, `303f232c`, `303f5152`, `326d9968`) |
| kernel PCs only after press | -- | `c041065c`, `c0435adc`, `c0435474` |

The one thing worth carrying forward: **1.0 is not ignoring the button.** It runs
*more* distinct userland and kernel code after the press than 1.1.4 does. So
"the event never reaches userland" is not supported -- something runs and then
gives up.

**Also dead: the GPIO lead.** With `IT_GPIO_TRACE=stderr`, 1.1.4 makes **zero**
GPIO accesses after the press despite returning to SpringBoard normally. So the
button path does not consult the GPIO data register at all, and diffing GPIO
traffic between the boards cannot explain anything. (The 1.0 half of that run was
invalid -- both boards were given the same VNC port -- but the 1.1.4 result alone
kills the hypothesis.)

**Suggested next instrument**, given that stop-the-world sampling and GPIO
tracing are both ruled out: measure the LATENCY from keypress to the first
`[LCD] w1 base` write on 1.1.4. It is known to be long (the step needs a 64 s
wait to pass). If 1.0's failure is really "much slower" rather than "never", the
question becomes a timer/clock one rather than a button one -- and that is
cheap to test by simply waiting far longer on 1.0 before declaring failure.

### Latency theory: DEAD (2026-07-29)

`home-wedge-probe.py --watch N` presses HOME from inside an app and then polls
the three framebuffers every 15 s at low cost (`pmemsave` is a memory read, not
a stop-the-world register query, so unlike PC sampling it does not starve the
guest).

iPhone OS 1.0, watched for **7 minutes** after the press:

```
  t=   0s  lit [99.2, 99.2, 99.2]   (app on screen)
  t=  32s  lit [ 4.0, 99.1, 99.1]   iBoot's buffer goes dark
  t=  63s  lit [ 4.0, 99.1, 99.1]
  ...
  t= 423s  lit [ 4.0, 99.1, 99.1]   unchanged for the rest of the 7 minutes
```

The two KERNEL buffers hold the app at 99.1% throughout and nothing ever
approaches the home screen's ~45% signature. **1.0 does not return slowly; it
does not return.** (Note the trap this nearly became: a first version of the
watcher stopped at the first big change and would have reported "RETURNED after
60s -- this was LATENCY". The change was one buffer going near-black, not a
return. Require the home-screen signature, not merely movement.)

### Everything now ruled out for the 1.0 in-app HOME failure

Each with the measurement that killed it, so none of these get re-tried:

| hypothesis | how it died |
| --- | --- |
| Wrong GPIO pin / IRQ | Both device trees encode `function-button_menu` identically: GPIO 0x1600, IRQ 0x28. Only the phandle differs. |
| INTLEVEL always reads 0 | Implemented the real pin level; `3_home_returns` still 0.00%. Reverted. |
| Interrupt not delivered/acked | Byte-identical SYSIC sequence to 1.1.4 and the iPod, press and release. |
| Host presenter stops | Runs 1:1 with the guest frame timer on the cocoa path (`present:8 vsync:8`). VNC-only artifact. |
| Guest polls a GPIO we do not drive | 1.1.4 makes ZERO GPIO accesses after the press and returns to SpringBoard fine. |
| It is a BOARD problem | 1.1.4 passes all five steps on the same board once its first-launch modal is dismissed. |
| The event never reaches userland | 1.0 runs EIGHT distinct userland PCs after the press, against 1.1.4's three. |
| It is just slow | 7 minutes, no return. |

**What is left** is what iPhone OS 1.0's own software does with the event, and
the two tractable ways in are: (a) 1A543a's binaries keep full C++ symbols, so
SpringBoard's menu-button path can be read directly the way `AppleMRVL868x` was;
(b) find where 1.0's kernel posts the button HID event and confirm whether it
posts at all -- the PC data says something runs, so the interesting question is
what it decides.

## ANSWERED: the HOME event never reaches SpringBoard on 1.0 (2026-07-29)

`scripts/springboard-button-breakpoint.py` resolves
`-[SpringBoard menuButtonDown:]` / `menuButtonUp:` from the build's own
`SpringBoard` binary (old-ABI `__OBJC` metadata, no symbols needed), sets gdbstub
breakpoints there, opens an app, and presses HOME. iPhone OS 1.x has no ASLR, so
the link-time address is the runtime address.

```
1.0    IMP 0x6ae0 / 0x6bd8   ->  NO HIT in 90 s
1.1.4  IMP 0x78dc / 0x79d4   ->  BREAKPOINT HIT: T05thread:01;
```

**The control is the point.** 1.1.4 hits, so the instrument can produce a
positive, and 1.0's miss is a real negative -- not another harness artifact.

So on 1.0 the chain is: GPIO IRQ raised -> kernel reads INTSTAT/INTLEVEL, ACKs
(byte-identical to 1.1.4) -> **and SpringBoard's handler is never called.** The
break is in the kernel's HID posting path, between acknowledging the interrupt
and delivering a GSEvent to SpringBoard. Everything above it -- the handlers,
their `SBSyncController` gates, the early-return ivar -- is innocent, because it
never runs.

### What the handlers look like (recorded so nobody re-reads them)

Both builds are structurally identical, which is itself evidence the difference
is below them:

```
-[SpringBoard menuButtonUp:]
    ldrsb r3, [self, #0x40]            ; 1.1.4: #0x44
    cmp   r3, #0 ; movne/strbne/popne  ; early-return ivar, swallows the up
    [[SBSyncController sharedInstance] isRestoring]        -> return
    [[SBSyncController sharedInstance] isResetting]        -> return
    [[SBSyncController sharedInstance] isSoftwareUpdating] -> return
    ...
```

### Where to look next

The kernel side of the button path, on 1.0 versus 1.1.4. The IRQ is handled
identically, so the divergence is in what the handler DOES with it -- which
driver claims the interrupt and whether it posts an event. Both kernelcaches
are readable with the technique that worked twice today (`__PRELINK` skew
0xC0027000 for 1A543a; recompute it for 4A102 from its own load commands), and
the strings to anchor on are in the button/HID kext rather than SpringBoard.

## Reading the kernel button path in both kernelcaches (2026-07-29)

Both kernelcaches extract with `scripts/extract-kernelcache.py`. The `__PRELINK`
skew differs per build and must be computed from that build's own load commands
-- **1A543a: 0xC0027000, 4A102: 0xC00A7000**. (Dead end #5's "the kext uses
PC-relative references so a byte search cannot work" was wrong; it had used a
guessed skew. With the right one, every log string has exactly one absolute
literal pointer.)

### The button driver is `AppleM68Buttons`, and it is identical on both

It builds each device-tree property name with `function-button_%s` and looks it
up. The enumeration code around that string is **instruction-for-instruction
identical** between 1.0 (`0xc032792c`) and 1.1.4 (`0xc03ab938`); the only
differences are vtable slot offsets (`0x588/0xa8` vs `0x3c8/0x68`), which is
just the two kernels' vtable layouts. So button registration is not the
difference.

### SpringBoard's handlers are also identical -- and never run on 1.0

`-[SpringBoard menuButtonDown:]` / `menuButtonUp:`, resolved from each build's
own binary via old-ABI `__OBJC` metadata:

```
-[SpringBoard menuButtonUp:]
    ldrsb r3, [self, #0x40]            ; 1.1.4: #0x44
    cmp   r3, #0 ; movne/strbne/popne  ; early-return ivar, swallows the up
    [[SBSyncController sharedInstance] isRestoring]        -> return
    [[SBSyncController sharedInstance] isResetting]        -> return
    [[SBSyncController sharedInstance] isSoftwareUpdating] -> return
```

Structurally the same on both. And they are innocent, because on 1.0 they are
never reached -- see the breakpoint result above (1.1.4 HIT, 1.0 NO HIT, with
the control proving the instrument works).

### New dead end: the interrupt IS enabled on 1.0

Suspicion: the model stores `gpio_int_enabled[]` and **never consults it** --
`ipod_touch_key_event()` says "Always raise the GPIO interrupt for all buttons".
So a guest that had not enabled the menu pin would still get an interrupt, its
GPIO IC driver would ACK an unregistered line and drop it, and nothing further
would happen: exactly the observed shape.

**Measured, and it is not that.** The INTEN writes are the same on both builds,
and both end with group 1 = `0x04003f00`, which includes bit 8 (the menu
button):

```
1.1.4   group1: 0x2000 -> 0x2100 -> 0x2300 -> 0x2700 -> 0x2f00 -> 0x3f00 -> 0x04003f00
1.0     group1: 0x2000 -> 0x2100 -> 0x2300 -> 0x2700 -> 0x2f00 -> 0x04002f00 -> 0x04003f00
```

**Separate fidelity bug, worth fixing on its own merits but NOT this bug:** the
model ignores `gpio_int_enabled` entirely. Nothing today depends on that being
wrong, and fixing it would not change this outcome, since 1.0 enables the pin.

### Where the break must be

Between the GPIO IC driver acknowledging the interrupt (observed, identical on
both) and SpringBoard's handler being called (observed on 1.1.4, absent on 1.0).
That interval is `AppleM68Buttons`' interrupt handler and the HID event posting
above it. Registration and enablement are both ruled out, so the next thing to
read is the handler itself -- anchor on `AppleM68Buttons`' class name and vtable
rather than on `function-button_%s`, which is registration-time only.
