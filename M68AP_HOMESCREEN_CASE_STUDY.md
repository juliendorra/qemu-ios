# How M68AP got to the home screen — the full path, including the wrong turns

**Date:** 2026-07-25, extended 2026-07-26 · **Branch:** `ipod_touch_1g`

Cases in this file: **1** the black screen (render/compositing/activation),
**2** slide-to-unlock dying after a sleep/wake (T7), **3** the iPhone app
pegging a host core (T6), **4** the iPhone not waking from Home (T7b).

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

**Verification of the derived window (both boards, 2026-07-25).** The runtime
derivation independently reproduces each board's constant from the guest's own
announcement, with no mismatch and no "never read" warning:

```
n45ap-control: [TVOUT-WA] derived 0x08a25960 from the guest (swap device VA 0xc0a25800 + 0x160) - matches the board default
m68ap-refreg:  [TVOUT-WA] derived 0x089c8560 from the guest (swap device VA 0xc09c8400 + 0x160) - matches the board default
```

Reproducing devos50's 2022 constant exactly, from the guest, is what makes the
mechanism trustworthy rather than a coincidence — and it means the iPod is not
"patched to match the iPhone": both boards now use one address-free mechanism.

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


---

# Case 2 — slide-to-unlock died after the first sleep/wake (T7)

**Reported:** power, home, slide-to-unlock works; do it again and the slide is
ignored. **Fixed 2026-07-25.** Root cause: the multitouch model had **no SPI
transaction framing**, so one short transfer desynced it permanently.

## The real bug, in one paragraph

`ipod_touch_multitouch.c` tracked a command with `cur_cmd` / `buf_ind` /
`buf_size` and reset them **only** when the guest clocked exactly `buf_size`
bytes. Real drivers do not always do that: a status poll asks for 2 bytes, and
a `0xEB` frame poll is abandoned as soon as the length field reads zero. Every
such short transfer left the device stuck mid-command. The next command byte
was then consumed as *data*, and every byte after it was misread as a new
command — a permanent desync. Touch kept being delivered to the model, and the
guest simply never received a frame it could parse.

## The fix

The SPI controller supplies the boundary the peripheral was missing:
`R_RXCNT` is the number of bytes the driver asked for, and the transfer is
over when it reaches 0. Measured: a 16-byte read is four runs of the 8-byte
FIFO, completing exactly when `RXCNT` hits 0.

    [SPI2] run: tx=0 rxcnt 16 -> 16
    [SPI2] run: tx=8 rxcnt 16 -> 8
    [SPI2] run: tx=0 rxcnt  8 -> 8
    [SPI2] run: tx=8 rxcnt  8 -> 0  (complete)

`apple_spi_run()` now calls `ipod_touch_multitouch_transaction_end()` when a
receive transaction completes, which drops any half-consumed command while
preserving state that legitimately spans transactions (`frame_data_pending`,
the firmware-upload flags). Verified: 4/4 unlock cycles pass, where cycle 2
previously failed every time; M68AP still reaches its home screen.

## Dead ends and false paths, in order

1. **"It's the readiness gate."** Plausible: the gate clears on sleep. Killed
   by the model's own log — the touch WAS delivered (`[TOUCH] mouse DOWN`).
2. **"My first probe run proves the old code was better."** It did not: with
   `-display none` the gate is evaluated in `gfx_update`, which never runs, so
   the run refused all 39 touches. **A test artifact that would have "proved"
   a false regression.** Fixed by evaluating readiness on the LCD refresh
   timer — a real model bug, since touch should not depend on a host window.
3. **`--warmup` screendumps** to arm the gate on an unmodified old binary:
   does not work, screendumps do not drive `gfx_update` often enough. Kept in
   the probe, documented, so it is not reinvented.
4. **"The driver sends 0xEC, so implement 0xEC."** The whole command sequence
   after the failure (`0xEC`, `0xED`, `0xE4`, `0xE1`) was **fictional** — an
   artifact of the desync. Implementing `0xEC` as an interrupt-data read
   changed nothing and was reverted. *Lesson: in a byte-stream model, an
   unknown command is not a harmless no-op; it fabricates plausible evidence.*
5. **"Frame transactions on chip-select."** Textbook-correct and completely
   dead here: the guest never drives CS through `R_PIN` — **0 edges** over a
   full boot (`IT_SPI_CS_TRACE`). Reverted.
6. **"It's a regression from the iPhone 2G work."** Measured and refuted: the
   pre-iPhone baseline (`151e64d305`) fails identically. Also A/B-tested the
   most suspicious commit of that era (`151e64d305`'s early return in the wake
   path, via `IT_LCD_LEGACY_WAKE`) — same failure either way.

## Tests and tools this produced

| Tool | Use |
|---|---|
| `scripts/lock-unlock-probe.py` | Drives power → home → slide N times over QMP, classifies the screen, and reports the first failing cycle with the model's own `[LCD]`/`[TOUCH]` lines. This is the regression test for T7. |
| `IT_MT_TRACE=2` | Byte-level SPI logging, armed from the first touch. It is what revealed `cmd 0xeb 2/16` — the model mid-transaction — and exposed the fictional command sequence. |
| `IT_SPI_BURST_TRACE=1` | Per-run TX count and `RXCNT` before/after: how the controller frames transfers. |
| `IT_SPI_CS_TRACE=1` | Chip-select edges (used to prove there are none). |

## Lessons

* **A model that consumes a byte stream needs the protocol's framing.** Byte
  counts alone are not framing: they assume the peer always finishes what it
  started, and real drivers abort early.
* **Unknown input must fail loudly, not plausibly.** The unknown-command path
  silently produced a valid-looking command stream and sent two investigations
  down invented protocol details.
* **Check whether your test can even observe the thing you are testing.**
  Two of the six dead ends above were measurement artifacts, not behaviour.


## Test fidelity ladder (added after the T7 false pass)

A fix passed the harness and regressed the product, so record what each rung
of the ladder actually proves:

| configuration | what it exercises | verdict on T7's fix |
|---|---|---|
| repo binary, `-display none` | **not the product**: QEMU never calls `gfx_update`, so the LCD readiness path never runs | INVALID — this rung "passed" a change that broke the app |
| repo binary + VNC refresh client | display refresh as in the app; synthetic input | reproduces the bug; fix verified |
| **shipped bundle** (`--app`) + VNC | the bundle's own engine, firmware, bridges, launcher | fix verified 3/3 |
| **shipped bundle + Cocoa display** | the real display path a user gets | fix verified 3/3, 234+ frames consumed |
| shipped bundle + Cocoa + real mouse/keys | a human's event stream | requires desktop control |

Rule of thumb this session earned: **if the harness cannot observe the thing
under test without changing it, the harness is wrong** — do not change the
model to make the test work.


# Case 3 — the iPhone app pegged a host core (T6)

**Reported:** `iPhone 2G.app` burns ~98% of a host core sitting on its home
screen; `iPod Touch.app` idles at 11–15%. **Fixed 2026-07-26.** Root cause:
the `/var` partition we generate was missing the directory skeleton the OS
expects — a skeleton the root filesystem itself ships as a template.

## The real bug, in one paragraph

`com.apple.AddressBook` (`ABDatabaseDoctor`) found no tables in its database,
tried to create them, and `CREATE TABLE` returned **SQLITE_BUSY** — "error 5
creating properties table: database is locked" — about 250 times a second,
forever. The daemon is not the bug. Our `/var` (disk0s2, an HFS partition we
build ourselves because we do not run Apple's restore) contained a
**hand-written 8-directory list**. A real device's `/var` is laid down by the
restore ramdisk from a template the root filesystem carries at
`/private/var`: 73 entries with their real modes — `tmp` (1777), `run`,
`preferences`, `logs`, `log`, `db/{dyld,timezone}`, `Keychains`, `vm`, `msgs`,
`empty`, `mobile/{Library,Media}`, `root/Library`. Nothing that needed to
write to /var could.

## The fix

`build-m68ap-var.py --template-from <root image>` copies that template in with
`ditto` (modes survive; a `mkdir` loop does not preserve 1777), and the product
recipe passes the root image. One line of configuration, but it had to be
*found*.

Measured on the app's own artifacts, before → after:

| | before | after |
|---|---|---|
| idle CPU | ~98% | **6–10%** (iPod: 11–15%) |
| `database is locked` in the guest log | 2 057 in the first 90 s | **0** |
| `no such table` | 15 592 AddressBook lines/boot | **0** |
| home screen | renders | renders (69.6% non-black) |
| guest clock | `Jan 1 00:00` | real date — `/var/db/timezone` exists now |

## Dead ends and false paths, in order

1. **"The guest's writes never reach the NAND."** The inherited diagnosis, and
   already half-corrected by the previous session: writes DO happen. Pursuing
   it further with `IT_NAND_WRITABLE=1` + `IT_NAND_RB=1` produced **zero
   read-back hits**, which looks damning and means nothing — a page just
   written sits in the guest's buffer cache, so it is never re-read. *A
   measurement that cannot distinguish its hypothesis from the null case.*
2. **"Then the written pages land at the wrong addresses."** 131 of 132 page
   writes landed in the boot partition's *physical* range rather than /var's.
   Also meaningless: the FTL allocates fresh physical pages from its own free
   pool and remaps, so physical addresses say nothing about which logical
   partition was written. Dropped.
3. **"The seeded database never made it into the shipped image."** It did —
   118 784 bytes, page size 4096, schema format 1, 20 tables including
   `ABPerson` and `_SqliteDatabaseProperties`, `integrity_check` ok. Getting
   at it required fixing `extract-hfs-from-nand.py` twice (below).
4. **"The guest cannot READ the seeded database."** Killed decisively: the
   file's SQLite magic was corrupted **through a NAND page override** (2 KB
   written into `bank3/60532.page`) and SpringBoard immediately reported
   `SQLITE_CORRUPT encountered while accessing
   /var/mobile/Library/AddressBook/AddressBook.sqlitedb, exiting`. The bytes
   reach SQLite; the daemon was never failing to read.
5. **"Two `ABDatabaseDoctor`s are locking each other out"** (the job is
   `OnDemand` with a `MachService`, so it is plausible). Every AddressBook
   line in the log carries the same pid — `16/com.apple.AddressBook`. One
   instance.
6. **"It is looking under the wrong `$HOME`."** Strong-looking evidence: the
   *real iPod device dump* keeps its AddressBook under `/var/root`, not
   `/var/mobile`. But that iPod runs 1.1, whose daemon plist has no
   `UserName` (so, root); the 1.1.4 plist says `UserName=mobile`. A second
   copy placed at `/var/root/Library/AddressBook/` changed nothing.
7. **"The volume is case-sensitive and the real device's is not."** Both are
   HFSX. Dead.
8. **"SQLite 3.1.3 cannot read a page-size-4096 database."** The guest really
   does ship SQLite 3.1.3 (2005), so this was worth a look — but the *real
   device's own* AddressBook database is page size 4096 too. Dead without
   spending a single boot on it.

The turn came from asking a different question: not "why does SQLite fail?"
but **"what does a real device's /var contain?"** — and the answer was sitting
inside the root filesystem we already ship.

## Tools this produced

| Tool | Use |
|---|---|
| `scripts/overlay-hfs-into-nand.py` | **The loop that made T6 tractable.** Drops an HFS partition image into a NAND tree as `bank<N>/<page>.page` overrides, spare copied from the pack so FTL metadata is byte-identical. Editing `/var` and booting went from a full NAND rebuild (minutes, ~900 MB of scratch on a disk that is always ~98% full) to about a second. Every hypothesis above was tested through it. |
| `scripts/extract-hfs-from-nand.py` (2 fixes) | It addressed pack entries as if `active_banks` were always 8 — true for the iPod's real dump, false for the generated 4-bank M68AP NAND, where it read the wrong pages and died with "invalid HFS volume signature". It also refused any pack with holes, which every generated (sparse) NAND has. Holes now read as zeros, exactly as the model reads them. |
| `S5L8900_DEBUG=1` | Serial on stdout from either bundle. The cheapest window into the guest, and where every count above came from. |
| `IT_NAND_WATCH=<bank>/<page>` | "Did the guest ever read this file?" — used to place, then discard, hypothesis 4. |

## Lessons

* **When a working reference exists, read it before theorising.** Two full
  hypotheses (`$HOME`, page size) were settled in minutes by looking at the
  real device's own files, and the *fix itself* came from the root filesystem's
  own template.
* **Build the fast loop first.** Four of the eight dead ends were each one boot
  away from being killed; they only got chased because a boot used to cost a
  NAND rebuild.
* **A measurement that cannot fail is not evidence.** "No read-back hits" and
  "writes land in the wrong physical range" both looked like findings and were
  compatible with a perfectly healthy system.
* **A daemon spinning on an error is usually right about its environment.**


# Case 4 — the iPhone would not wake: P slept it, H did nothing (T7b)

**Reported:** on `iPhone 2G.app`, Power sleeps the device and Home does
nothing; the iPod wakes from the same keys. **Fixed 2026-07-26.**

## The real bug, in one paragraph

`ipod_touch_key_event()` used `GPIO_BUTTON_HOME` (0x1606) and
`GPIO_BUTTON_HOME_IRQ` (0x2E) for **both** boards. Those are the *iPod's*
values. M68AP's device tree (`buttons` node, compatible `buttons,m68`) puts
`button_menu` on **0x1600**, and 0x2E is not in its interrupt list at all.
Power (`hold`, 0x1605) is shared between the boards — which is exactly why
one key kept working and the other did not.

## The fix, and the honest part of it

Home is now board-aware (`ipod_touch_home_pin()` / `ipod_touch_home_irq()`).
The **pin is certain** (it is in the device tree). The **IRQ is a derivation**:
N45AP fixes the rule `IRQ = 0x28 + (pin & 0xf)` (0x1605→0x2D, 0x1606→0x2E),
and applying it to M68AP's five button pins reproduces its device tree's
interrupt SET exactly — `0x2d 0x28 0x29 0x2a 0x2b`, with 0x2C absent precisely
because pin 0x1604 is unused. That is strong, but it is inference from a set,
not a measured pairing, so `IT_M68AP_HOME_IRQ=<n>` overrides it without a
rebuild.

## The A/B that proves it

Same NAND, same fixed `/var`, only the engine differing — the pre-fix engine
is the one still inside the shipped bundle, so no rebuild was needed to get a
baseline:

| | cycle 1 | cycle 2 |
|---|---|---|
| pre-fix engine | no `[LCD] Merlot panel woke from sleep` at all; screen frozen at its pre-sleep 63.5% (the pixel classifier called this "unlocked" — a false pass) | `stuck-locked` at 42.2% |
| post-fix engine | wakes: 69.1% home → sleep → H → 42.2% lock screen → slide → 69.1% home | same, 2/2 `unlocked` |

Recorded as a limitation: neither run counted touch frames at the model
boundary, because `lock-unlock-probe.py` only reports them when the **caller**
sets `IT_MT_TRACE=1`. The wake claim rests on the panel's own wake trace and
on the lock screen actually appearing, not on pixels alone.

## Lesson

**Shared hardware hides board divergence.** Power sits on the same pin on both
boards, so the button path looked "tested" for as long as anyone only pressed
P. The device tree for each board is the specification — read it per board,
not once.
