# M68AP (iPhone 2G) — session handoff: the render blocker

**Date:** 2026-07-25 · **Branch:** `ipod_touch_1g` · **Head at handoff:** `f5a9c414dc`

This is a focused handoff for the *current* wall. `IPHONE_2G_BRINGUP_HANDOFF.md`
remains the long-form working log (every run, every trace); this file is the
short path back into the problem: what is true, what is ruled out, what tools
exist, and what to try next.

---

## 1. Status in one paragraph

M68AP boots iPhone OS 1.1.4 all the way to **SpringBoard**, with the **baseband
attached** and the **WiFi driver up**, and the device reports **`[Activated]`**.
It still shows a **black screen**: SpringBoard runs, logs its two lockdown
lines, and then never programs a kernel framebuffer base — the guest settles
into the kernel idle loop. N45AP (iPod Touch), booted from the same emulator on
the same code paths, renders normally. **The render blocker is not activation,
not the baseband, and not the empty `/var`** — all three were tested and
eliminated. It is currently unidentified.

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

* `IPHONE_2G_BRINGUP_HANDOFF.md` — long-form log: every run, trace and dead end.
* `WIFI_SDIO_NOTES.md` — WiFi milestones A–I (N45AP proven) + M68AP parity note.
* `DEVICE_BRINGUP_PLAYBOOK.md` — the general method (controls in every matrix).
* `IPHONE_2G_OS_1_FEASIBILITY.md` — artifact policy and the activation stance.
