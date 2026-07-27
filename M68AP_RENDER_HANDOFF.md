# M68AP (iPhone 2G) — session handoff: render SOLVED, home screen reached

> **Artifact paths moved (2026-07-27).** Commands quoted below use the old
> layout — `m68ap-artifacts/stage/` (which was 1.1.4), `stage-1.0/`,
> `extracted/`. Every build now lives in `m68ap-artifacts/builds/<BUILD>/` with
> version-neutral filenames, and every tool takes an explicit `--build`. The
> quoted commands are kept as the dated record of what was run; to re-run them
> today, translate the paths using
> [`M68AP_BUILD_LAYOUT.md`](M68AP_BUILD_LAYOUT.md) — usually
> `--build <BUILD>` replaces the path arguments entirely.


**Date:** 2026-07-25 · **Branch:** `ipod_touch_1g` · **Head at handoff:** `d863470867`+

This is a focused handoff. The wall it was written for (the black screen) is
SOLVED; §0 carries the engineering tasks that remain. `IPHONE_2G_BRINGUP_HANDOFF.md`
remains the long-form working log (every run, every trace); this file is the
short path back into the problem: what is true, what is ruled out, what tools
exist, and what to try next.

---

> **Starting a new session? Read `NEXT_SESSION_HANDOFF.md` first** — it carries
> the two live problems (T6 idle CPU, T7b the iPhone not waking from P+H) with
> their reproductions, what is already ruled out, and the ground rules.

## 0. OPEN ENGINEERING TASKS (carried forward — do not lose these)

| # | Task | Why it matters | State |
|---|---|---|---|
| T1 | **Model MBX swap completion / the TVOut SDO IRQ** so the guest driver clears the swap-device field itself, and delete the workaround window entirely. The machine already wires `S5L8900_TVOUT_SDO_IRQ`; the MBX region is currently a do-nothing stub with **no IRQ connected at all**. | Removes the last address-dependent hack in the display path, on both boards. | **TODO** — the honest fix |
| T2 | **Model MBX 2D** so `LK_ENABLE_MBX2D=0` is no longer needed. LayerKit otherwise tight-polls `c03b9698` (register-read accessor in `com.apple.driver.AppleMBX`). | Today we force software compositing via a guest plist edit — a shortcut, and the iPod ships the same one. | **TODO** |
| T3 | ~~Make the TVOut workaround self-locating and self-verifying~~ | A build-specific magic address that failed **silently** cost this project the entire render investigation. | **DONE** (2026-07-25) — derived from the guest's own `AppleMBX: Added swap device` line, with a mismatch report and a "window never read" warning at SpringBoard start |
| T7 | ~~N45AP: slide-to-unlock dies after a sleep/wake cycle~~ | The multitouch model had no SPI transaction framing: `cur_cmd`/`buf_ind` reset only when the guest clocked exactly `buf_size` bytes, so any short transfer (a status poll, or a `0xEB` frame poll abandoned once the length reads zero) left the device stuck mid-command forever. Framed on `R_RXCNT` reaching 0. | **DONE** (2026-07-26) — verified with a display client attached: framing off → cycles 2+ fail with **0 frames consumed**; framing on → 4/4 unlock, 47-49 frames each. `IT_SPI_FRAMING=0` disables it for A/B. |
| T8 | ~~Fix the test harness~~ | Headless runs never call `gfx_update`, so the readiness logic under test never executed — and I had compensated by changing the model, validating a configuration nobody runs. | **DONE** (2026-07-26) — `lock-unlock-probe.py` now attaches a minimal VNC/RFB client so QEMU refreshes as in the app, asserts **frame consumption at the model boundary** (a pixel-only verdict once scored `unlocked` with 0 frames consumed), and takes `--slide-steps/--slide-dwell/--slide-hold` to sweep gesture timing. |
| | **NOT a regression from the iPhone 2G work — measured.** The pre-iPhone baseline (`151e64d305`, the parent of the first M68AP commit) fails identically: cycle 1 `unlocked`, cycles 2-3 `stuck-locked`, touches delivered. Method: check out that commit's `ipod_touch_lcd.c` / `_lcd_panel.c` / `ipod_touch_lcd.h` into the current tree and backport ONLY the testability change (evaluate readiness on the LCD timer instead of inside `gfx_update`) — without it the old code refuses every touch under `-display none` (0 delivered, 39 refused) and the comparison is meaningless. Also falsified as the cause: `151e64d305` itself (A/B via `IT_LCD_LEGACY_WAKE=1`, same failure either way). | | |
| T6 | ~~The ~98% idle CPU~~ **SOLVED 2026-07-26 — the /var partition was missing the OS's own directory skeleton.** The root filesystem carries the authoritative template at `/private/var` (73 entries with real modes: `tmp` 1777, `run`, `preferences`, `logs`, `db/timezone`, `Keychains`, `vm`, `mobile/Media`, ...) and on a real device the restore ramdisk lays it onto the data partition. We shipped a hand-written 8-directory list instead, so every daemon that writes to /var failed; `com.apple.AddressBook` failed `CREATE TABLE` with SQLITE_BUSY and retried ~250×/s forever. `build-m68ap-var.py --template-from <root image>` copies the template (wired into the product recipe). **Measured on the app's own artifacts: home screen renders, ZERO SQLite errors in the whole guest log (was 15 592 AddressBook lines/boot), idle CPU 6–10% — better than the iPod's 11–15%.** What was NOT the cause, all measured: seeding the database (the guest does read it — proved by corrupting its SQLite magic through a page override and watching SpringBoard report `SQLITE_CORRUPT` with the path), a second `ABDatabaseDoctor` (there is only ever pid 16), the db living under the wrong `$HOME` (adding a copy at `/var/root/...` changed nothing), and the case-sensitivity of the volume (both ours and the real device are HFSX). | Restores Contacts, lets settings persist, drops idle CPU from ~98% to 6–10%. | **DONE** |
| T6-old | **(superseded, kept for the method)** The ~98% idle CPU / guest state that does not persist. MUCH of the earlier diagnosis was WRONG and is corrected here. Writes DO work: the guest issues ADM `0x500` page writes, ~100-144 pages land per boot, and 5 of them are **SQLite journal pages** — so the filesystem really is writing. Two real model bugs were found and fixed on the way: (1) the write path stored the spare left over from the previous READ, so every written page carried another page's FTL metadata (now taken from the guest at `data3_sec_addr`); (2) written pages were unreadable because the model writes `<page>_new.page` and reads `<page>.page` — `IT_NAND_WRITABLE=1` makes writes land where reads look and shadow the pack. With ALL of that, `com.apple.AddressBook` still fails: it opens an empty database and `CREATE TABLE` returns **SQLITE_BUSY ("database is locked")**, ~250×/s. So the blocker is no longer "writes are discarded" — it is that SQLite cannot complete a transaction. Next diagnostics, in order: (a) is more than one `ABDatabaseDoctor` running (launchd OnDemand + MachService) so they lock each other out? (b) does `fcntl` locking work on this HFS volume in the guest? (c) does any OTHER file the guest creates survive a reboot — a direct persistence test independent of SQLite. Note the reference: the iPod mounts its root **read-write** as a single partition and logs **zero** AddressBook errors. Tools: `IT_NAND_WRITABLE`, `IT_NAND_TRACE`, `IT_NAND_RB` (read-back hits), `IT_NAND_WATCH`. | Restores Contacts, lets settings persist, removes a class of retry loops, and drops idle CPU from ~98% to ~9% (measured with the daemon absent). | **OPEN** |
| T7b | ~~M68AP does not wake: P then H does nothing~~ **FIXED 2026-07-26.** The key handler used the **iPod's** home pin `GPIO_BUTTON_HOME 0x1606` / IRQ `0x2E` for both boards; M68AP's device tree puts `button_menu` on **GPIO 0x1600** and never lists `0x2E`. Power (`hold`, `0x1605`) is shared, which is why P still slept it. Now board-aware (`ipod_touch_home_pin()` / `ipod_touch_home_irq()`). The IRQ is a **derivation, not a measurement**: N45AP fixes the rule `IRQ = 0x28 + (pin & 0xf)` (0x1605→0x2D, 0x1606→0x2E), and applying it to M68AP's five pins reproduces its DT interrupt SET exactly (`0x2d 0x28 0x29 0x2a 0x2b`; 0x2C absent because pin 0x1604 is unused) → menu = 0x1600 / **0x28**. `IT_M68AP_HOME_IRQ` overrides it without a rebuild. Verified with `lock-unlock-probe.py --board m68ap --cycles 2`: 2/2 `unlocked` (sleep → H wakes to the lock screen at 42% non-black → slide → home at 69%). | The iPhone can be woken again. | **DONE** |
| T5 | **Drive the M68AP button pins from reset** instead of at the kernel banner, and understand what iBoot does with them. iBoot samples the port at t=0.087 s (pc=0x180024ba) to pick a boot mode; presenting released volume buttons that early panics the kernel (reproduced 3/3), so the idle levels are installed when the kernel banner appears. | The current placement is a shortcut around an unexplained iBoot path. | **TODO** |
| T4 | Re-run any experiment whose verdict predates the screen classifier | Everything before 2026-07-25 was judged by logs on a black screen; several conclusions were only about *rendering*, not about *which screen*. | partially done |

## 1. Status in one paragraph

**RESOLVED 2026-07-25 — M68AP REACHES THE HOME SCREEN.** iPhone OS 1.1.4 boots
to the SpringBoard **home screen** on `-M iPhone-2G`: Phone/Mail/Safari/iPod
dock, app icons, first-run "Edit Home Screen" tip. Three independent fixes were
needed, and each hid the next:

1. **Render** — the TVOut swap-device workaround window was hard-coded to the
   *iPod's* kernel heap address; the iPhone kernel puts the object elsewhere,
   so SpringBoard waited forever after `attach(AppleH1TVOut)`. Now derived at
   runtime from the kernel's own announcement (§6.1, task T3).
2. **Compositing** — LayerKit drives the MBX 2D path against our do-nothing
   MBX stub, so SpringBoard has to run with `LK_ENABLE_MBX2D=0`, exactly as
   devos50's iPod image does (task T2).
3. **Setup** — the synthesised data ark was the wrong SHAPE. Read off the
   iPod's real ark (`extract-hfs-from-nand.py`, single-partition NAND → the
   ark is in the ROOT image): the real device stores **CFBooleans** where we
   wrote CFNumbers, and carries keys we never wrote at all (international
   language/locale, SIM status, timezone, iTunes/registration flags). With the
   reference shape SpringBoard finally *accepts* EverRegistered
   (`previously registered: [0], state is 0`), and with it **True** the device
   goes to the home screen. Profile: `hacktivate-m68ap.py --profile
   reference-reg`; lab variant `m68ap-refreg`.

Note the type lesson: SpringBoard's `"...but it wasn't a string"` complaint is
**misleading** — it fires for a CFNumber *and* for a real CFString; what it
wants is a CFBoolean.

### How the setup gate was found (superseded steps, kept as method)

Three hypotheses were tested and eliminated before the reference told us the
answer — each is worth *not* re-chasing:

* **Telephony was chrome, not the gate.** `GraphicsServices` owns the
  capability table (exports `GSSystemGetCapability`; knows
  `telephony`/`unifiedIPod`/`camera`/…), sourced from
  `SpringBoard.app/<board>.plist`. **Both** firmwares ship **both** board
  profiles — the iPod's own 1.1.4 image contains `M68AP.plist` with
  `telephony: True` — so it is Apple's runtime board table, not a per-device
  build. Dropping the key (`caps=notel`) removes every bit of phone UI
  (carrier label, lock glyph, emergency slider) and stops the telephony-gated
  EverRegistered check — **and the device still sat on connect-to-iTunes**,
  with a SpringBoard log line-for-line identical to the rendering iPod's.
* **`/var` is not the gate** — a minimal skeleton changed nothing
  (`m68ap-notel-var`, identical 8.0% non-black).
* **EverRegistered's type is not "string"** — supplying a real CFString still
  produced `wasn't a string: <CFString>{contents = "YES"}`. The message lies;
  the consumer wants a CFBoolean, which only the reference revealed.

Which frame paints also depends on the baseband, all pre-home states:
`IT_M68AP_NO_BASEBAND=1` → "Searching…"/connect-to-iTunes (~41% non-black);
`IT_BASEBAND_H5=1` → "No Service / Repair Needed" (~52%); with the reference
ark → **home screen** (~70%, `colorful 8.6 / dock 100`). Baseband registration
remains shelved (`2032b7995e`) and is now decoupled from reaching the UI.

## 2. What is verified working

| Thing | Evidence |
|---|---|
| M68AP boots to SpringBoard with the baseband stub attached | `SpringBoard[15]` in serial; no `IT_M68AP_NO_BASEBAND` needed since `5ed12270c3` |
| S5L8900 UART interrupt semantics | 4 fixes in `hw/char/exynos4210_uart.c` (`5ed12270c3`); fixed the ISR storm that wedged the boot |
| Baseband transport identified end-to-end | AT → `at+xtransportmode` → **H5/BCSP three-wire UART**; link establishment responder works (`da137a1949`, `d818fe74f6`) |
| WiFi driver parity with N45AP | byte-identical driver-ready state; `mv8686` is board-agnostic (`2f7bc5226b`) |
| Activation | `[Activated], state is 2`, EverRegistered cleared (`5efddc1954`, `0d7e2d35e0`) |
| N45AP not regressed | boots, `[Activated]`, renders ~29–47% non-black |

### How activation is achieved (and how it differs from the iPod)

* **Data ark** — a *binary* `data_ark.plist` at `/var/root/Library/Lockdown/`
  carrying `com.apple.mobile.lockdown_cache-ActivationState = "Activated"` and
  `-SBLockdownEverRegisteredKey`. One file, served by lockdownd to **every**
  consumer (SpringBoard, CommCenter, Preferences). This is the iPod-shaped part.
* **Plus one binary patch** in `lockdownd` (rename its `"Unactivated"` CFString
  to `"Activated"`), because `determine_activation_state` re-validates at boot
  against an **activation record** and flips the cached state back otherwise.
* **Measured**: no data-only ark survives that re-validation — `minimal`,
  `factory` (`FactoryActivated`), `unactsvc` (`AllowUnactivatedService`) and
  `all` each reach `[Activated]` and each log the flip (`0660a8822d`). So the
  single lockdownd patch is required.
* **Why the iPod doesn't need it**: N45AP's NAND is a device dump carrying a
  genuine Apple-signed, device-bound activation record
  (`activation_records/pod_record.plist`: AccountToken, AccountTokenCertificate,
  AccountTokenSignature, DeviceCertificate, FairPlayKeyData) — devos50 copied it
  from real hardware. It cannot be forged, will not transfer between devices,
  needs owned iPhone 1,1 hardware to dump, and the repo's artifact policy bars
  shipping it. Hence the synthesised route. **Do not re-litigate this without
  new information** (e.g. a user-supplied iPhone 1,1 record).

## 3. The open problem, precisely

SpringBoard emits exactly two lines and stops:

```
SpringBoard[15]: lockdown says the device is: [Activated], state is 2
SpringBoard[15]: lockdown had a value for EverRegistered but it wasn't a string: <CFNumber …>
```

Then:
* **no kernel framebuffer base is ever programmed** — the only LCD window base
  seen is `0x0fe00000` (iBoot's boot-logo buffer). N45AP programs `0x0f400000`
  **and** `0x0f496000`.
* all three framebuffers stay **0.0% non-black**; N45AP reaches ~29–47%.
* the guest PC sits in the kernel **wait-for-interrupt idle loop**
  (`0xc005a9cc`: `mcr p15,0,r4,c7,c0,4`) on 8/8 samples in the fully-quiescent
  variants → the system has nothing to run: **SpringBoard is blocked on an event
  that never arrives**, not spinning and not crashed.
* the last IOKit activity before the wedge is
  `IOMobileFramebufferUserClient::attach(AppleH1CLCD)` and
  `IOCoreSurfaceRootUserClient::attach(IOCoreSurfaceRoot)`.

**Caveat that has already misled once:** the string `Configuring SpringBoard`
does **not exist** in M68AP's 1.1.4 SpringBoard binary (it is an iPod-firmware
string). So "M68AP never reaches phase `configuring`" proves nothing on its
own; the `phase` column in the lab is only meaningful up to `coresurface` for
M68AP. Judge by **LCD bases** and **framebuffer content**, not by that phase.

## 4. Ruled out — with the measurement that did it

| Hypothesis | Verdict | Evidence |
|---|---|---|
| Device is unactivated | **not the blocker** | held `[Activated]` (0 flips) + EverRegistered cleared → still no render (`c2c6c91756`) |
| Baseband/SIM interference | **not the blocker** | identical wedge with `IT_M68AP_NO_BASEBAND=1` and with the H5 stub (`cc705f1357`) |
| Empty `/var` (no `/var/mobile` for SpringBoard's `mobile` uid 501) | **not the blocker** | minimal skeleton boots identically (launchd 13 / configd 45 / SpringBoard 2) and still does not render; configd still says `no preferences` because it wants preference *files* (`f5a9c414dc`) |
| Display model is board-specific | **no** | LCD/CoreSurface code is shared; N45AP renders through the same path |
| Full `/var` skeleton would help | **harmful** | 56 dirs + `chmod 1777` → launchd never starts at all (0/0/0 lines at a 700 s cap) |
| Zephyr1 multitouch never signals "ready" | **not the blocker** | `IT_MT_TRACE` (2026-07-25): the Z1 bootloader + raw main-firmware upload completes and verifies on M68AP — `firmware_loaded=1` on both boards. Control: `IT_FORCE_MT_Z2=1` (model answers Z2 semantics) breaks boot far *earlier* (phase=kernel, 0 SpringBoard lines), confirming the guest really speaks Z1 |
| Kernel display-controller programming diverges | **no** | `IT_FB_TRACE` (2026-07-25): both boards write the *identical* CLCD register set (only the gamma-ramp values differ, iPhone vs iPod panel calibration). The divergence is in userland compositing, not the kernel LCD driver |
| iBoot's display programming diverges between boards (raised 2026-07-27 by the missing boot logo) | **no** | `IT_FB_TRACE`: **both** boards' iBoot program window 2 only (0x070..0x080 = SFN/base/size/hspan/qlen) and write WNDCON once. Identical. See "the boot logo" below |

### The boot logo — SOLVED 2026-07-27 (two stacked faults, again)

The Apple logo never appeared during an M68AP boot, on **every** iPhone OS 1.x
build; it flashed only at the very end as SpringBoard took over. Two independent
causes, the second of which the iPod was **masking**:

1. **NOR side.** iBoot walks the IMG2 image store by `next_header =
   this_header + (u32 at +0x18) * 0x40`. IPSW containers ship `0xFFFFFFFF`
   there and `scripts/build-m68ap-nor.py` never filled it in, so enumeration
   stopped after `dtre` (1 `image 0x...` line vs the iPod's 7) and iBoot could
   not reach the `logo` entry. Full derivation and dead-ends in
   `IPHONE_2G_BRINGUP_HANDOFF.md`.
2. **Emulator side.** `lcd_refresh()` scanned out **window 1** (0x58..0x68)
   unconditionally. iBoot draws into **window 2** (0x70..0x80) — on both boards
   — and window 1 is the *kernel's*. The iPod looked correct only because its
   kernel adopts iBoot's 0x0fe00000 into window 1 at ~13 s, so the logo appears
   continuous. `lcd_scanout_base()` now falls back to window 2 while window 1
   is unprogrammed; window 1 still wins the moment it is written, so the OS-era
   behaviour is bit-identical to before.

Measured after both (`--boot-wait 4 --samples 7`): screenout 2.173% from t=4 s
on N45AP, M68AP 1.1.4 **and** M68AP 1.0; N45AP still reaching the home screen
at ~13 s exactly as before.

Two things seen while verifying that these fixes did **not** cause, both open:
an intermittent `IOIpodUSBDevice::start` panic on 1.1.4 (the next identical run
was clean — this is the known host-contention race in §7), and M68AP's kernel
framebuffers still fully black 110 s in with SpringBoard already running.

## 5. Tools built this session (all committed, all reusable)

```bash
# Parallel, self-judging boot matrix. Hypotheses live in the VARIANTS table.
python3 scripts/springboard-lab.py --logs /tmp/sblab \
    --variants n45ap-control m68ap-full m68ap-varmin \
    --diff m68ap-full=n45ap-control
#   verdicts: rendered | wedged (static + all PCs idle) | crawling | panicked | timeout
#   evidence: PC histogram, LCD bases, FB non-black %, phase, SpringBoard lines, driver tail
#   --diff A=B: normalised full-log set-diff from the SpringBoard phase on

# Emulator-side display/touch tracing (2026-07-25; the lab sets all three):
#   IT_FB_TRACE=1    every LCD MMIO access (throttled) + panel SPI bytes
#   IT_MT_TRACE=1    multitouch dialogue + firmware-upload transitions
#   IT_FORCE_MT_Z2=1 answer an M68AP guest in Zephyr2 semantics (breaks boot;
#                    that breakage is itself the proof the guest speaks Z1)
# Evidence lands in matrix.json under "trace" and in the TRACE DIFF section.
# New variants: m68ap-z2, m68ap-mbx (LK_ENABLE_MBX2D=0 in the SpringBoard
# plist, replicating the fix devos50's N45AP image ships), m68ap-mbx-root
# (same + drop UserName=mobile), m68ap-prune[-hw|-svc] (launch-daemon set
# reduced toward N45AP's 7-daemon rendering set).

# Baseband matrix (same shape, uart1 rulesets)
python3 scripts/baseband-lab.py --logs /tmp/bblab --rules <rules...> none builtin

# Activation
python3 scripts/hacktivate-m68ap.py build-dataark --out ark.plist [--profile minimal|factory|unactsvc|all]
python3 scripts/hacktivate-m68ap.py patch --root-hfs <root.img> --out <patched.img>   # the lockdownd rename
python3 scripts/hacktivate-m68ap.py analyse|disasm --lockdownd <binary>               # ARM RE helpers

# Guest filesystem
python3 scripts/inject-guest-file.py --image <hfs> --src <file> --dest /root/... [--root-owned] [--verify]
python3 scripts/build-m68ap-var.py --out data-var.img [--data-ark ark.plist] [--full]  # --full is HARMFUL, opt-in

# Firmware RE
python3 scripts/extract-kernelcache.py <8900 kernelcache> -o kc.raw   # GID AES + complzss → raw ARM Mach-O

# Disk hygiene (imported by the labs; also a CLI)
python3 scripts/lab_workspace.py --free /tmp --size DIR --prune DIR

# Boot logo / NOR image store (2026-07-27)
# What can iBoot REACH in a NOR (walking +0x18) vs what is merely PRESENT?
# The gap between the two is the diagnosis; --check makes it a gate.
python3 scripts/nor-image-store.py <nor.bin> [--check --expect 7] [--reference <n45ap nor>] [--json]
python3 scripts/test-nor-image-store.py          # fixture tests, no Apple payloads
# Is the logo actually on the panel early? Defaults to the INSTALLED bundle,
# because the NOR half of the fix lives in the bundle's firmware.
python3 scripts/verify-boot-logo.py --app "/Applications/iPhone 2G (iOS 1.1.4).app"
python3 scripts/verify-boot-logo.py --board m68ap --build 4A102
# Partial bundle firmware update (reuses the installed iBoot/NAND, keeps epoch)
python3 scripts/install-iphone-firmware.py --app <bundle> --nor <nor.bin> --keep-existing
```

**Full reproduction of the current best M68AP state:**

```bash
python3 scripts/hacktivate-m68ap.py build-dataark --out /tmp/ark.plist
python3 scripts/build-m68ap-var.py --out /tmp/data-var.img --data-ark /tmp/ark.plist
python3 scripts/hacktivate-m68ap.py patch \
    --root-hfs m68ap-artifacts/stage/filesystem-m68ap-readonly.img --out /tmp/root-patched.img
python3 scripts/build-m68ap-nand.py --out /tmp/nand --signature m68ap --active-banks 4 \
    --bbt production --hfs /tmp/root-patched.img --data-hfs /tmp/data-var.img \
    --device iPhone1,1 --ipsw-build 4A102
IT_LCD_TRACE=1 IT_M68AP_NO_BASEBAND=1 build-ipod11/qemu-system-arm \
    -M "iPhone-2G,bootrom=m68ap-artifacts/appdbg/bootrom_s5l8900,\
iboot=m68ap-artifacts/stage/iboot_204_m68ap_sbpatch.bin,nand=/tmp/nand" \
    -m 1G -pflash m68ap-artifacts/stage/nor_m68ap.bin \
    -L "/Applications/iPod Touch.app/Contents/Resources/pc-bios" \
    -display none -serial file:/tmp/serial.log -qmp unix:/tmp/q.sock,server,nowait
```

Or just: `python3 scripts/springboard-lab.py --logs /tmp/x --variants m68ap-full`.

## 6. Next investigation avenues (ranked; updated 2026-07-25)

1. **The MBX/LayerKit lead (current best).** Measured 2026-07-25:
   * `com.apple.SpringBoard.plist` differs between the firmwares. N45AP (the
     devos50 image that RENDERS) carries `EnvironmentVariables:
     LK_ENABLE_MBX2D = "0"` — LayerKit's PowerVR-MBX 2D compositing OFF, i.e.
     software rendering. M68AP's stock 1.1.4 plist has no such override (and
     runs SpringBoard as `UserName mobile`, where N45AP runs it as root).
   * Only N45AP ever logs `AppleMBXUserClient::attach(AppleMBXDevice)`.
   * The emulator's MBX is a do-nothing MMIO stub (`s5l8900_mbx_read/write` in
     `hw/arm/ipod_touch.c`) — an MBX-compositing SpringBoard blocks silently
     on a GPU that never completes anything. This matches the wedge exactly,
     and explains why the shared LCD path renders for one board only.
   * **Measured — `m68ap-mbx` (env var alone) does NOT render.** Same wedge
     signature (phase=coresurface, only the iBoot base, all PCs idle). The
     mutation itself is verified end-to-end: a replayed edit reads back
     `EnvironmentVariables = {LK_ENABLE_MBX2D: "0"}` from a fresh mount, and
     M68AP's LayerKit *does* contain the `LK_ENABLE_MBX2D` getenv string (and
     "Failed to initialized MBX2D driver"), so the knob is honored by the
     iPhone build. Interpretation shift: N45AP's `AppleMBXUserClient::attach`
     is probably a *consequence* of SpringBoard getting further, not the
     cause; the block is likely BEFORE LayerKit compositing begins.
   * **Measured — `m68ap-mbx-root` and `m68ap-prune` do NOT render either.**
     Same signature both times (phase=coresurface, iBoot base only, idle).
     So the SpringBoard plist env, the `mobile`-vs-root user AND the
     thirteen extra launch daemons are all eliminated.
   * **The live thread — the TV-Out user client.** Serial ordering
     (first-matrix evidence, `/tmp/sblab-mt`): N45AP's SpringBoard does
     `attach(AppleH1TVOut)` → `attach(IOCoreSurfaceRoot)` →
     **`detach(AppleH1TVOut)`** → `attach(AppleH1CLCD)` → renders, and only
     *later* logs `Couldn't get IAP TV out settings`. M68AP's SpringBoard
     does `attach(AppleH1TVOut)` → `attach(IOCoreSurfaceRoot)` → **silence
     forever** (no TVOut detach, no second CLCD attach, no IAP line). The
     kernel side is identical on both boards (AppleMBX registers both
     CLCD+TVOut swap devices; AppleH1TVOut::start completes in ~0.5 s). So
     the iPhone SpringBoard build blocks inside its TVOut framebuffer
     interaction — and the emulator's TVOut is a RAM-backed stub
     (`hw/arm/ipod_touch_tvout.c`) whose `SDO_IRQ` never fires. TVOut MMIO
     tracing now rides on `IT_FB_TRACE` (`[TVOUT{1,2}]` lines) to show what
     the driver programs before the silent wait.
   * **DECODED — the upstream "TVOut workaround" is the mechanism, and it is
     iPod-specific.** devos50 hit this same hang on the iPod and "got past
     TVOut" (`f59f20f60e`, 2022) by overlaying a **4-byte always-zero MMIO
     window at phys `0x8a25960`** — which decodes as kernel VA `0xc0a25960`
     = N45AP's `AppleMBX: Added swap device: AppleH1TVOut id: c0a25800`
     **+ 0x160**: one field of the TVOut swap-device object, force-read-as-
     zero so the teardown proceeds. The M68AP kernel build allocates that
     object at `c09c8400` (verified byte-stable across four boots and
     different root-image variants), so its field sits at phys `0x89c8560`
     and the iPod's window misses it. **Fix VERIFIED**: with the per-board
     window (`TVOUT_WORKAROUND_M68AP_MEM_BASE 0x89c8560`, board-gated in
     `ipod_touch.c`), M68AP's kernel polls the new window (20 `[TVOUT-WA]`
     reads) and the healthy sequence appears for the first time —
     `detach(AppleH1TVOut)` → re-`attach(AppleH1CLCD)`, SpringBoard advances
     (8 log lines: BT session retries, then LayerKit activity). Clean
     version, recorded: model MBX swap completion / the TVOut SDO IRQ
     instead of zeroing a heap field.
   * **The NEXT wall after TVOut, measured:** SpringBoard's LayerKit then
     drives the **MBX 2D path** and the kernel ends in a tight poll —
     all 8 PC samples at `c03b9698`, which disassembles to the register-read
     accessor (`ldr r0,[r0,r1]; bx lr`) inside **com.apple.driver.AppleMBX**
     (kext identified by walking `__PRELINK` back to its Mach-O header). Our
     MBX is a do-nothing stub, so the polled status never flips; serial dies
     with user-client terminate storms and two `LKLayer ... bogus layer size
     (0.0, 0.0)` lines. This is exactly why devos50's iPod image carries
     BOTH fixes: the TVOut window AND `LK_ENABLE_MBX2D=0`. The earlier
     `m68ap-mbx` negative is VOID — it wedged at TVOut before the MBX could
     matter. Decisive combo (TVOut window + `sb_env=mbx2d`) running at
     update time.
2. **Launch-daemon set** — measured: M68AP's root runs the full stock twenty
   daemons; N45AP renders with only seven (AddressBook, CommCenter,
   SpringBoard, configd, mDNSResponder, lockdown, notifyd). If `m68ap-mbx`
   does not render, `m68ap-prune[-hw|-svc]` bisects the thirteen extras
   (BTServer, iapd, usbptpd, coreaudiod touch stubbed hardware).
3. **Disassemble SpringBoard's startup** after the EverRegistered check
   (`_hasEverRegistered` at `0xb3ec4` / `0xbdcc4`; log string `0xa392c`) to
   see what it calls next — now specifically to confirm the LayerKit/MBX
   surface-creation path and the `LK_ENABLE_MBX2D` getenv.
4. **Preference *files*, not directories** — configd wants files. Seeding a
   minimal `com.apple.SystemConfiguration.plist` / language + locale
   preferences might unstick the userland chain (`lockdown:
   _load_international_settings: Could not load languages list` is still
   present). Use `inject-guest-file.py`; this is unproven but cheap.
5. **Bisect the harmful full `/var` skeleton** (`--full`) — understanding *why*
   launchd dies with 56 dirs + `chmod` may itself explain what early userland
   is sensitive to.

## 7. Traps and gotchas (learned the hard way)

* **Host contention flips a race.** Simultaneous boots perturb the guest's
  USB-start window and turn `IOIpodUSBDevice::start` into a panic — it happens
  even to the non-socket `builtin` baseband config. The labs stagger by 30 s;
  keep that. Repeat any `panicked` seat once before believing it.
* **Build artifacts serially.** Four concurrent NAND builds starve the host
  enough to error some builds and stall the boots that do start.
* **Disk fills fast and fatally.** A NAND tree is ~310 MB, a patched root
  ~280 MB. This session filled the volume and no command could run at all.
  All labs now import `lab_workspace.py`: pre-flight `require_free_bytes`,
  `Workspace` disposal (evidence is never deleted), `prune_runs`, and
  leak-proof `attached()` mounts. Cleanup is **on by default**.
* **Format matters at the consumer.** The data ark must be a **binary** plist;
  XML makes lockdownd log "Could not load" — which for a while I misread as an
  HFS-visibility problem. The guest reads macOS-written HFS files fine (the DNS
  restore proves it).
* **Ownership** is only needed for consumers that check it (launchctl skips
  "dubious" non-root plists). lockdownd read a uid-501 data ark happily.
  `inject-guest-file.py --root-owned` covers the strict cases.
* **`Configuring SpringBoard` is an iPod-only string** — see §3.
* **`IT_LCD_TRACE` only fires on a base *change*.** A window the guest never
  writes produces no line at all, and a rewrite of the same value is invisible.
  So an empty trace does not mean "nothing happened" — it can mean "the
  register you care about was never touched", which is exactly how the boot
  logo hid. When the question is *what did the guest program*, use
  `IT_FB_TRACE=1`; keep `IT_LCD_TRACE` for *when did the base flip*.
* **`logs/command.txt` is not shell-safe.** It is `" ".join(argv)`, and the
  artifact paths contain a space (`iPod Touch.app`), so `bash command.txt`
  dies with a truncated `Could not open …`. To re-launch a recorded run with a
  tweak (e.g. `-serial file:/dev/stdout` to interleave guest output into the
  register trace), rebuild the argv list in Python.
* **`fb-snapshot.py --boot-wait` defaults to 180 s — past everything early.**
  The whole iBoot era is over within a few seconds. For anything about the
  boot logo or the pre-kernel display, use `--boot-wait 4 --samples 7`, or
  just `verify-boot-logo.py`.
* **Killing a bundle launch does not kill the emulator.** The bundle entry
  point is a shell script that *runs* qemu and waits rather than `exec`ing it,
  so signalling the process you spawned leaves an orphaned emulator holding
  its per-launch NAND clone (~300 MB) — one per run, until the volume fills.
  This is how `verify-boot-logo.py` filled the disk on 2026-07-27 before it
  was given `start_new_session=True` + `killpg`. Any new harness that launches
  a bundle needs the same, and `pgrep -c` does not exist on macOS, so a
  before/after process count must go through `pgrep … | wc -l` — otherwise the
  leak check silently compares two empty strings and always says "no leak".
* **The bundle launcher is `Contents/MacOS/iPod Touch`**, on the iPhone
  bundles too — the name is retained for compatibility, and
  `scripts/ipod-app-launcher.sh` is what gets installed under it. Launching
  `Contents/MacOS/ipod-app-launcher.sh` fails with a bare "No such file".
* **The bundle manifest used to describe artifacts that were long gone.**
  `package-iphone-app.sh` installs the firmware itself rather than going
  through `install-iphone-firmware.py` — it ships only `nand.pack` plus empty
  bank dirs, for launch speed — so nothing in the packaging path ever rewrote
  `firmware-provenance.json`. A shipped bundle was found carrying **every**
  hash stale (iBoot, NOR, the NAND tree) plus a `nand_provenance` reading
  `"none (metadata-only)"`, i.e. actively asserting that a NAND carrying the
  activation patch and a forced-software-compositing SpringBoard was stock
  firmware. Fixed 2026-07-27: packaging now copies the constructor's
  `nand-provenance.json` next to the pack and ends with
  `install-iphone-firmware.py --refresh-manifest`, which derives the manifest
  from what is actually on disk. When the sidecar is absent the manifest says
  `status: MISSING` rather than omitting the key — "no record" must not read
  as "nothing was modified".
* **`install-iphone-firmware.py` replaces `iphone_files/` wholesale.** Before
  2026-07-27 it also required every input, so a NOR-only update meant either
  passing the bundle's own NAND back to it or editing the file in place — and
  it dropped the `epoch` file, which silently turns a working 1.0 (epoch 0) or
  1.1.1 (epoch 2) bundle into one that wedges in iBoot with an empty serial
  log. `--keep-existing` and epoch carry-over now cover both.
* **hdiutil types raw images by their extension.** A working copy named
  `root.img.tmp` fails to attach with "image not recognized"; name temp
  copies `*.tmp.img`. This silently killed the first `sb_env` lab seat.
* **The disk is contended by parallel sessions.** Another session working in
  `/Applications` consumed ~3 GB mid-run here. The lab now deletes each
  intermediate root image as soon as the next stage has consumed it (peak one
  root image per matrix, not one per recipe), but keep an eye on `df` anyway.

## 8. Commit trail (this session, oldest → newest)

```
31509944eb  baseband: file-driven C-table rules + staggered launches
5ed12270c3  UART: S5L8900 interrupt semantics — SpringBoard with a LIVE baseband
ba1658c6db  baseband: post-xtransportmode transport is SLIP-framed
c21c352aba  baseband: locate the frame-builder target
da137a1949  baseband: transport is H5/BCSP; link responder advances CommCenter
d818fe74f6  baseband: H5 establishes one-way; modem-side muzzle
b568787b9c  baseband: H5 state machine reverse-engineered from the kernelcache
eb31dbec6e  baseband: window field doesn't unmuzzle; CommCenter Active but idle
f97208807d  baseband: post-H5 blocker is CommCenter's "wakeup flags" handshake
2f7bc5226b  WiFi: M68AP parity with N45AP verified
2032b7995e  docs: telephony shelved as a stretch goal (decision record)
c99e12a83d  WiFi/M68AP: UI Safari test blocked by black screen
032a4d00ea  M68AP: black screen proven to be the activation gate
2dfb35dafb  hacktivation: data-ark injection failed (2 cycles); pivot
0d7e2d35e0  hacktivation WORKS via lockdownd string rename → [Activated]
5efddc1954  authentic data-ark hacktivation + reusable injector
c2c6c91756  activation solved; render blocker is SEPARATE
cc705f1357  parallel SpringBoard lab + iPod-vs-us activation rationale
0519c98d82  shared disk-hygiene module used by every lab tool
0660a8822d  measured: no data-only ark holds activation
f5a9c414dc  /var hypothesis tested and DISPROVED
```

## 9. Related documents

* `M68AP_HOMESCREEN_CASE_STUDY.md` — **how this was actually solved**: the
  three stacked faults, all eight dead ends with the measurement that killed
  each, the lessons, and the tool inventory.

* `IPHONE_2G_BRINGUP_HANDOFF.md` — long-form log: every run, trace and dead end.
* `WIFI_SDIO_NOTES.md` — WiFi milestones A–I (N45AP proven) + M68AP parity note.
* `DEVICE_BRINGUP_PLAYBOOK.md` — the general method (controls in every matrix).
* `IPHONE_2G_OS_1_FEASIBILITY.md` — artifact policy and the activation stance.
