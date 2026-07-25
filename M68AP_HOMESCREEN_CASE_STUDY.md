# How M68AP got to the home screen — the full path, including the wrong turns

**Date:** 2026-07-25 · **Branch:** `ipod_touch_1g`

The iPhone 2G (M68AP) went from *"boots to SpringBoard, black screen forever"*
to *"iPhone OS 1.1.4 home screen"* in one session. Three separate faults were
stacked, each hiding the next, and roughly half the elapsed effort went into
hypotheses that turned out to be wrong. This document records the route
**including the dead ends**, because the dead ends are the expensive part to
rediscover — and because two of them were wrong in instructive ways.

Companion docs: `M68AP_RENDER_HANDOFF.md` (current state + open tasks),
`IPHONE_2G_BRINGUP_HANDOFF.md` (long-form log), `DEVICE_BRINGUP_PLAYBOOK.md`
(the general method, updated from this session's lessons).

---

## 1. The starting symptom

M68AP booted iPhone OS 1.1.4 all the way to SpringBoard, reported
`[Activated]`, had the WiFi driver up — and showed **nothing**. Specifically:

* the only LCD window base ever programmed was `0x0fe00000` (iBoot's boot-logo
  buffer). N45AP programs `0x0f400000` **and** `0x0f496000`;
* all three framebuffers sampled 0.0% non-black;
* the guest PC sat in the kernel wait-for-interrupt idle loop on 8/8 samples;
* the last IOKit activity was
  `IOMobileFramebufferUserClient::attach(AppleH1CLCD)` and
  `IOCoreSurfaceRootUserClient::attach(IOCoreSurfaceRoot)`.

"Idle, not spinning" was the single most useful early fact: it made this a
**blocked wait**, not a crash and not a livelock — so the question was always
*"what event never arrives?"*, never *"where is the bug in the loop?"*.

## 2. The three real faults (in the order the system hits them)

| # | Fault | Fix applied | Class |
|---|---|---|---|
| 1 | The **TVOut swap-device workaround window** was hard-coded to the *iPod's* kernel heap address. On the iPhone kernel the object lives elsewhere, so TVOut teardown never completed and SpringBoard waited forever after `attach(AppleH1TVOut)`. | Window address **derived at runtime** from the kernel's own `AppleMBX: Added swap device: AppleH1TVOut  id: <VA>` announcement, via a read-only console tap. Per-board constants remain only as initial placement. | emulator |
| 2 | **LayerKit composites through the MBX 2D path**, which this emulator only stubs; the kernel tight-polls `c03b9698` (a register-read accessor inside `com.apple.driver.AppleMBX`) forever. | `LK_ENABLE_MBX2D=0` in `com.apple.SpringBoard.plist` — *exactly what devos50's iPod image already ships*. | guest data (shortcut) |
| 3 | The synthesised **data ark had the wrong shape**: CFNumbers where the real device stores CFBooleans, and 12 missing keys (international language/locale, SIM status, timezone, iTunes/registration flags). SpringBoard therefore treated the device as never-set-up and showed connect-to-iTunes. | Ark rebuilt to the **reference shape** read off the iPod's own activated device (`--profile reference` / `reference-reg`). | guest data (authentic) |

Fault 1 masked fault 2, which masked fault 3. That ordering is why several
early experiments produced "no change" — they were testing hypotheses about a
gate the system had not even reached yet.

## 3. Dead ends, in order, with the measurement that killed each

Every one of these was a *reasonable* hypothesis given what was known at the
time. They are listed so nobody re-runs them.

1. **Zephyr1 multitouch never signals "touchscreen ready".** M68AP is the only
   board using the Z1 protocol path (`mt->zephyr1`), and a missing readiness
   event would look exactly like a silent wait.
   **Killed by:** `IT_MT_TRACE` — the Z1 bootloader upload *and* the raw
   main-firmware upload complete and verify; `firmware_loaded=1` on both
   boards. Control: forcing Z2 semantics (`IT_FORCE_MT_Z2=1`) broke the boot
   far *earlier* (phase=kernel, zero SpringBoard lines), proving the guest
   genuinely speaks Z1 and the dialogue is healthy.
2. **The kernel's display-controller programming diverges between boards.**
   **Killed by:** `IT_FB_TRACE` — both boards write the *identical* CLCD
   register set; only the gamma-ramp values differ (panel calibration). The
   divergence had to be in userland.
3. **`LK_ENABLE_MBX2D=0` alone is the fix** (spotted as a real difference
   between the two SpringBoard plists — a genuinely correct observation!).
   **Killed by:** `m68ap-mbx` wedged identically. *This verdict was later
   proven VOID*: it wedged at fault 1, before the MBX could matter. The
   hypothesis was right and the experiment was wrong.
4. **SpringBoard must run as root** (N45AP's plist has no `UserName`, M68AP's
   says `mobile`). **Killed by:** `m68ap-mbx-root` — identical wedge. (Also
   later void for the same reason.)
5. **A launch daemon that M68AP runs and N45AP doesn't is blocking.** M68AP
   runs the stock twenty daemons; the *rendering* iPod runs only seven.
   **Killed by:** `m68ap-prune` (reduced to exactly N45AP's set) — identical
   wedge.
6. **An empty `/var` starves SpringBoard.** **Killed by:** minimal skeleton →
   identical; and a *full* 56-directory skeleton is actively **harmful**
   (launchd never starts at all).
7. **The home screen is gated on telephony registration.** After rendering was
   fixed, the painted frame was the activation screen; with the H5 baseband
   stub it became "No Service / Repair Needed". Very persuasive.
   **Killed by:** the board capability profile — dropping `telephony` from
   `SpringBoard.app/M68AP.plist` removed *every* piece of phone UI and the
   device **still** sat on connect-to-iTunes, with a SpringBoard log
   line-for-line identical to the rendering iPod's. Telephony was chrome on
   top of the real gate.
8. **`EverRegistered` must be a string** — SpringBoard literally logs
   `had a value for EverRegistered but it wasn't a string: <CFNumber 0>`.
   **Killed by:** supplying a genuine CFString → *the same complaint*, now
   printing `<CFString>{contents = "YES"}`. **The log message is simply
   wrong**; the consumer wants a CFBoolean. Two experiments were spent
   trusting an error message.

## 4. What actually cracked each fault

**Fault 1 — decoded by reading the upstream hack instead of the symptom.**
`git log -S TVOUT_WORKAROUND` found `f59f20f60e` ("Got past TVOut", 2022): a
4-byte always-zero MMIO window at physical `0x8a25960`. That decodes as kernel
VA `0xc0a25960` = N45AP's `AppleMBX: Added swap device: AppleH1TVOut id:
c0a25800` **+ 0x160** — one field of the TVOut swap-device object, forced to
read zero so teardown proceeds. The M68AP kernel allocates the same object at
`c09c8400` (byte-stable across four boots and different root images), so its
field is at `0x89c8560`, outside the iPod's window. The mapping
`PA = VA − 0xC0000000 + RAM_MEM_BASE` was confirmed independently on *both*
boards, which is what made the derivation trustworthy.

**Fault 2 — found by diffing the two firmwares' SpringBoard job plists**, and
confirmed by the fact that only N45AP ever logs
`AppleMBXUserClient::attach(AppleMBXDevice)`.

**Fault 3 — found by reading the reference device, not by guessing.** The
breakthrough was realising the iPod's shipped NAND declares **one** partition
(so devos50 flattened `/var` into the root volume), which means its real
`data_ark.plist` was sitting inside an image we could already extract. That
activated device's ark has 19 keys; ours had 7, with the wrong types. Mirroring
its *shape* (names, types, generic values — no Apple-signed material) made
SpringBoard accept the value for the first time:
`lockdown says we've previously registered: [0], state is 0`. Setting
EverRegistered `True` produced the home screen.

## 5. Lessons

1. **A magic address with a silent failure mode is the most expensive kind of
   shortcut.** The TVOut window worked for five years on one board and then
   cost an entire investigation on the second, because a wrong address doesn't
   error — it punches a zero-reading hole in the kernel heap and hangs
   somewhere unrelated. *If a hack depends on a build-specific address, it must
   announce when it is not being used.* One warning line would have ended this
   in minutes.
2. **Prefer values the guest tells you over values you hard-code.** The kernel
   *printed* the address we needed. Deriving it from the guest's own
   announcement removed the per-board table entirely and made the hack
   self-adapting to any future kernel.
3. **When a working parallel path exists, read it before theorising.** Every
   correct answer this session came from the N45AP reference: the plist diff
   (fault 2), the capability table, and the data ark (fault 3). Every wrong
   answer came from reasoning about our own artifacts in isolation.
4. **Error messages are evidence, not truth.** `"wasn't a string"` fires for a
   CFNumber *and* a CFString. Believe the *behaviour* (accepted vs rejected),
   not the wording.
5. **A negative result is only valid for the state the system was in.** Three
   verdicts (`m68ap-mbx`, `m68ap-mbx-root`, `m68ap-prune`) were correct as
   measurements and *void* as conclusions, because an earlier fault stopped
   execution before their variable mattered. **When a blocker is removed,
   re-run the negatives taken under it.**
6. **Judge the thing you actually care about.** For weeks the judge was "did
   anything render" (non-black %). But the activation screen is *more* lit
   (41%) than the home screen (29%) — so the metric could not answer the real
   question. The moment the lab learned to classify *which screen*, progress
   became measurable.
7. **Fix the model, not the consumer, when the model is wrong.** The UART
   interrupt-semantics work earlier in this project and the TVOut window here
   are the same story: a wrong device model produces symptoms far away.
8. **Disk is a first-class resource in this project.** A NAND tree is ~310 MB
   and a root image ~280 MB; two sessions independently filled the volume. The
   space guard refusing to start beats a half-written matrix.

## 6. Tools built or extended this session (all committed, all reusable)

```bash
# Parallel, self-judging boot matrix. Hypotheses live in VARIANTS as data.
python3 scripts/springboard-lab.py --logs /tmp/sblab \
    --variants n45ap-control m68ap-refreg --diff m68ap-refreg=n45ap-control
```

| Tool / knob | What it answers |
|---|---|
| `classify_screen()` in `springboard-lab.py` | **Which** screen rendered (home vs setup vs blank), from raw BGRA. Calibrated: home = 10.7% colorful / 81% dock; setup screens = 1.8% colorful. Reported as a `screen` column + in `matrix.json`. |
| `IT_FB_TRACE=1` | Every LCD MMIO access (throttled per register), the panel SPI bytes, TVOut register traffic, and TVOut-workaround window hits. |
| `IT_MT_TRACE=1` | The multitouch dialogue and firmware-upload state transitions, Z1 and Z2. |
| `IT_FORCE_MT_Z2=1` | Falsifier: answer an M68AP guest in Zephyr2 semantics. Breaking the boot *proves* the guest speaks Z1. |
| `caps=notel/notel-false` (lab knob) | Board capability profile — Apple's own "this is not a phone" switch, as data. |
| `sb_env=mbx2d/mbx2d-root/root` | SpringBoard job-plist mutations (MBX off, run as root). |
| `prune=all13/hw/svc/iap` | Launch-daemon set reduction toward N45AP's rendering set. |
| `hacktivate-m68ap.py --profile reference / reference-reg` | Data ark in the **reference shape** read off a real activated device. |
| `extract-hfs-from-nand.py --partition {boot,data}` | Reconstructs either volume; parses the GPT in the FTL logical space. This is how the reference ark was obtained. |
| Console tap (`ipod_touch_console_tap.h`) | Lets the emulator react to what the guest *announces* — currently deriving the TVOut window address; reusable for any "the kernel prints the address/state we need" case. |
| `fb-snapshot.py` | Pixel truth: dumps all three FB bases from guest RAM and renders PNGs (the scanout can be black while RAM holds a full frame). |
| `lab_workspace.py` | Space guard, disposal, pruning, leak-proof mounts. Refuses to start rather than fill the disk. |

## 7. Traps worth carrying forward

* **`hdiutil` types raw images by file extension** — a working copy named
  `root.img.tmp` fails to attach ("image not recognized"). Name temp copies
  `*.tmp.img`. This silently killed one lab seat.
* **Host contention flips a boot race** — simultaneous boots perturb the
  USB-start window and can turn `IOIpodUSBDevice::start` into a panic. The labs
  stagger by 30 s; **repeat any `panicked` seat once** before believing it.
* **Build artifacts serially**, boot in parallel.
* **The data ark must be a *binary* plist** — XML makes lockdownd log "Could
  not load", which was once misread as an HFS visibility problem.
* **`Configuring SpringBoard` is an iPod-only string** — its absence on M68AP
  proves nothing.
* **Don't copy Apple-signed material.** The reference's activation record and
  StoreIdentityCookie stay on the reference; only key names, types and generic
  values were mirrored.
