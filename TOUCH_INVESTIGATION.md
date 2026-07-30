# Touch input across the three app bundles (2026-07-27)

**Symptom as reported:** tapping does nothing in `iPod Touch.app` and in
`iPhone 2G (iOS 1.0).app`. Only `iPhone 2G (iOS 1.1.4).app` responds to touch.

**Verdict: two unrelated causes, one per board.** The three bundles ship the
*same* `qemu-system-arm` and the *same* launcher (verified by sha1) — they
differ only in `Resources/s5l8900-profile` and the firmware images, so the
difference is entirely in how each guest driver talks to the multitouch model.

Both were measured headlessly with `-display none` + QMP `input-send-event`,
comparing framebuffer pmemsave dumps before/after a tap. The decisive counter
in the log is `[MT] frame consumed` (`IT_MT_TRACE=1`): it is the only signal
that means "the guest actually took this touch". Byte-level evidence came from
`IT_MT_TRACE=2` plus `IT_SPI_BURST_TRACE=1`.

---

## iPod Touch (N45AP, Zephyr 2) — a regression from the T7 SPI framing fix

The Z2 driver reads one frame in **two SPI transactions**:

| transaction | RXCNT | guest TX header | what it wants |
|---|---|---|---|
| 1 | 16 | `ea 01 00 …` | the 16-byte frame-*length* packet |
| 2 | 59 | `ea 01 01 …` | the 59-byte frame *payload* |

`16 + 59 = 75 = sizeof(MTFrame)` — one logical `0xEA` reply split in half.

The model serves `0xEA` as a single 75-byte stream and, worse, calls
`ipod_touch_multitouch_consume_frame()` at *command start*. Since
commit `16866d4d9f` ("T7 fixed for real: SPI transaction framing"),
`apple_spi_run()` calls `ipod_touch_multitouch_transaction_end()` when
`R_RXCNT` hits 0, which resets `cur_cmd`/`buf_ind`. So:

* transaction 1 delivers the length packet **and frees the frame**;
* framing then resets the command state;
* transaction 2 is parsed as a *brand-new* `0xEA`, finds `next_frame == NULL`,
  and returns 59 zero bytes.

Every touch is delivered to the driver and then thrown away. Observed exactly:

```
[MT] Z2 cmd 0xea (n=1)
[MT] frame consumed (event 3)
[MT] transaction ended mid-command 0xea at 16/75 - resetting
[MT] Z2 cmd 0xea (n=2)
[MT] transaction ended mid-command 0xea at 59/75 - resetting
```

Before the framing fix this worked *by accident*: with no transaction
boundary, transaction 2's leading `0xEA` was absorbed as a stream byte and the
model kept serving `out_buffer[16…74]` — the right 59 bytes.

**Proof.** Same bundle, same taps, only the escape hatch differs:

```
framing on   tap (222,145) diff=0.0%    atn=20 consumed=20
framing off  tap (222,145) diff=99.62%  atn=20 consumed=20   → Settings opens
             tap (160,437) diff=62.16%                       → Safari settings
```

```bash
IT_SPI_FRAMING=0 "/Applications/iPod Touch.app/Contents/MacOS/iPod Touch"
```

### Would wiring up chip-select help? No.

`apple_spi_update_cs()` carries a long-standing TODO, and the framing comment
claimed the guest "never drives it (0 edges measured)". **That claim is wrong**
(`IT_SPI_CS_TRACE=1`, added 2026-07-27): the iPod driver writes `R_PIN` once
per transaction — 84 times on SPI2 in a two-minute boot.

But it only ever *asserts*: every write is `0x00000000`, never the deassert. So
CS yields a start-of-transaction marker, not a textbook low/high edge pair —
functionally the same information `R_RXCNT` already gives. And across an `0xEA`
frame read the guest re-asserts **between the two halves**:

```
[SPI2] CS write #83 -> low
[MT] Z2 cmd 0xea (n=1)                                  ← 16-byte length part
[MT] transaction ended mid-command 0xea at 16/75 - resetting
[SPI2] CS write #84 -> low
[MT] Z2 cmd 0xea (n=2)                                  ← 59-byte payload part
```

So real CS framing would draw the boundary in exactly the same places and break
the split read identically. Worth doing for fidelity; it is not a fix, and it
does not remove the need for the change below.

### FIXED (2026-07-27)

Turning framing off is *not* the fix — that reintroduces T7 (touch dies after
the first sleep/wake). `0xEA` is now modelled as the two-part transfer it is,
in `hw/arm/ipod_touch_multitouch.c`:

1. the `0xEA` command-start branch no longer consumes the frame;
2. `transaction_end()` recognises the one legitimate short read — `cur_cmd ==
   0xEA`, `buf_size == sizeof(MTFrame)`, `buf_ind == sizeof(MTFrameLengthPacket)`
   — and sets `frame_data_pending`, leaving the frame queued;
3. the next transaction takes the existing `frame_data_pending` payload path
   (the one `0xEB` already used), which serves the 59 payload bytes and
   consumes the frame there;
4. a driver that clocks all 75 bytes in one transaction still works: the frame
   is consumed in the `buf_ind == buf_size` completion block.

The part byte the guest sends (`in_buffer[2]`, `0x00` = length, `0x01` =
payload) corroborates the sequence but is not needed to drive it — the
handover is carried by `frame_data_pending`, so a driver that omits the byte
still works.

Results, framing left ON throughout:

```
[MT] Z2 cmd 0xea (n=1)
[MT] transaction ended mid-command 0xea at 16/75 - resetting
[MT] 0xEA length packet delivered; payload owed
[MT] frame consumed (event 3)          ← on the payload transaction

tap (222,145) diff=99.62%   → Settings opens
tap (160,437) diff=62.16%   → Safari settings
scripts/lock-unlock-probe.py --board n45ap --cycles 3  → 3/3 unlocked (T7 holds)
```

---

## iPhone 2G on iPhone OS 1.0 (M68AP, Zephyr 1) — an unimplemented command

Not a regression: **iPhone OS 1.0's Zephyr 1 driver uses a different
frame-fetch command than 1.1.4's, and the model does not implement it.**

1.1.4, on every ATN edge (works):

```
[MT] ATN edge (group 5 bit 3)
[MT] Z1 cmd 0x64   (frame-length poll, alternating 0x64/0x65)
[MT] Z1 cmd 0x68   (frame read)
[MT] frame consumed (event 3)
```

1.0, on the ATN edge (dead):

```
[SPI2] run: tx=0 rxcnt 8 -> 8
[MT] Z1 cmd 0x46 ×5           ← guest TX: 46 46 46 46 46
[MT] Z1 cmd 0xff ×3           ← the model's own AGD dummy bytes
[SPI2] run: tx=3 rxcnt 3 -> 0  (complete)
```

The transaction is **8 bytes: five guest bytes of `0x46`, then 3 dummy reads**.
`0x46` is not in the Z1 command table, so `z1_transfer()` takes the `default:`
arm, sets `buf_size = 1` and answers `0x00` — and because an unknown command is
one byte long, each following byte is re-read as another "command". The driver
sees an all-zero reply, concludes the controller is wedged, and **re-uploads
the whole Zephyr firmware** (`0xC2` bootloader packets) — 6 times in one probe
run, 150 `0x46` transactions, zero frames consumed.

Because `ipod_touch_multitouch_inform_frame_ready()` only fires when
`next_frame` is empty, the first unconsumed frame also blocks every later ATN
edge: after the very first touch of a session, no further ATN is ever raised.
Touch is dead for good, which matches "not working at all".

### FIXED (2026-07-28) — `0x46` was never a command

The kext settles it: `AppleMultitouchSPI.kext` in the 1A543a root filesystem
(`m68ap-artifacts/builds/1A543a/root.img`) still has full C++ symbols, so the
frame path reads directly. `interruptOccurred()` → `readOneFrameOfData()`,
which does **two command-less reads**:

1. `deviceGetResultLength(unsigned short *out, unsigned char)` — transmits 5
   bytes and reads 8. The 5 transmitted bytes come from
   `memset(txbuf, this->errCounter, 5)` at +0x2654, which is why they are all
   identical and why the value drifts: **`0x46` is a retry counter, not an
   opcode.** MOSI is ignored by the controller here; only MISO matters.
2. `deviceReadResultData(len + 1, 0)` — reads the frame itself.

So the model's whole framing was wrong for 1.0: it treated the first byte as a
command, hit the `default:` arm (`buf_size = 1`), and every following byte was
re-read as another "command".

**The two reply layouts are NOT the same**, which is the part that costs a
debugging round if you assume otherwise. Both are 0xAA-framed, but:

| | length reply | frame reply |
|---|---|---|
| 1.1.4 (`0x64`/`0x65`, `0x68`) | len at `[4][5]`, ck at `[6][7]` | `AA` + payload + ck |
| 1.0 (command-less) | **len at `[1][2]`, ck at `[3][4]`** | `AA` + payload + ck (same) |

1.0's parse, at +0x2750: `ldrb rx[1]; ldrb rx[2]; orr r12, r3, r2 lsl #8` for
the length; `add r1, r2, r3` versus the `orr` of `rx[3]`/`rx[4]` for the
checksum. It also rejects a length above the max packet size it learned from
the interface-version reply. Only the first five bytes of the 8-byte
transaction are parsed.

The frame reply needs no new code: 1.0 asks for `len + 1` = 55 bytes and
`deviceReadResultData` hands `rx + 1, len - 3` to `handleFrame()` — exactly the
55-byte, 52-payload packet the `0x68` branch already builds.

Implementation in `z1_transfer()`: a new internal `MT_Z1_CMD_UNSOLICITED`
state, entered only when the leading byte is not a known Z1 command **and** a
frame is actually in flight. That guard keeps 1.1.4 (which pads with zeroes and
always leads with a real command) on its existing path, and leaves genuinely
unknown commands reaching the `default:` arm. `z1_frame_len_sent` carries the
handover between the two transactions.

Results:

```
1.0    tap (277,258) diff=97.03%  atn=20 consumed=20   → Settings opens
1.1.4  tap (180,325) diff=68.78%  atn=20 consumed=20   → modal dismissed
       tap  (47,68)  diff=96.92%  atn=20 consumed=20   → SMS opens
iPod   tap (222,145) diff=99.62%  atn=20 consumed=20   → Settings opens
       lock-unlock-probe.py --cycles 3 → 3/3 unlocked (T7 holds)
```

---

## The 1.1.4 "Edit Home Screen" modal — NOT a bug

**It is expected behaviour, and roughly 40 minutes went into treating it as a
defect before that was pointed out.** The `iphone-2g` launcher clones the
pristine NAND on every launch and deletes it on exit, so the guest's storage is
effectively read-only. A first-run alert that a real device would show once and
record therefore reappears on every launch, by construction. Nothing is broken.

The only thing it costs is that it *looks* like dead touch: a modal covers the
icons, so taps do nothing until Dismiss is tapped. That is worth knowing when
triaging a "touch stopped working" report — check the screenshot first.

**Do not hack it out of the image.** The alert is part of what distinguishes
1.1.4 from 1.0 — 1.0 has no home-screen rearranging and no such alert — and
seeing it on first boot is historically accurate behaviour worth preserving.
The bundles show what these firmwares actually did. Decided 2026-07-28.

For the record, since it will look like a loose end otherwise, these were the
attempts and none of them landed in a shipped image:

Every launch of the 1.1.4 bundle comes up with SpringBoard's `REORDER_INFO`
alert covering the home screen, so taps on icons do nothing until Dismiss is
tapped. It reads exactly like broken touch and is worth ruling out first.

What was established:

* The gating defaults exist and are named `SBDidShowReorderText`,
  `SBDidShowReorderAlert` and `SBReorderCount` (strings in
  `SpringBoard.app/SpringBoard`, alert text under `REORDER_INFO_TITLE` /
  `REORDER_INFO_BODY` in `English.lproj/SpringBoard.strings`).
* Dismissing it in the guest and keeping the NAND does not carry over. The
  guest *does* write — 349 `*_new.page` files appeared — but the next boot
  shows the alert again. (That is the separate NAND write-persistence thread,
  not something to fix from here.)
* Pre-seeding `/var/mobile/Library/Preferences/com.apple.springboard.plist`
  with the two booleans — injected with `extract-hfs-from-nand.py` +
  `overlay-hfs-into-nand.py`, 12288 page overrides written — did **not**
  suppress it, so either those are not the gate for this particular alert or
  the injection did not land where SpringBoard reads.

The plist injection was built in a **throwaway** NAND tree — a symlink to the
bundle's `nand.pack` plus empty bank dirs — so no shipped pack was ever
modified (`nand.pack` still hashes to the value its extract manifest recorded).
The throwaway tree is deleted. There is no hardcoded dismissal anywhere.

Nothing here is worth pursuing. The workaround, if you want past the alert, is
one tap on Dismiss.

### Never point QEMU at a bundle's shipped NAND

Probing the M68AP bundles by invoking `qemu-system-arm` directly with
`nand=<bundle>/Contents/Resources/iphone_files/nand` **bypasses the launcher's
per-launch clone**, so the guest's page writes land in the shipped image:
`bank<N>/<page>_new.page` files that then become part of every later launch's
starting state. 560 such pages were written across the two iPhone bundles this
way before it was noticed, and removed afterwards (`nand.pack` itself is only
ever read, so it stayed byte-identical).

Either drive the bundle through its launcher, or clone the NAND first
(`cp -Rc`) and point QEMU at the clone. To check a bundle is clean:

```bash
find "/Applications/iPhone 2G (iOS 1.1.4).app" -name "*_new.page" | wc -l
```

The iPod bundle is different by design: it is not staged, its NAND is patched
in place, and guest writes there are normal.

**Correction (2026-07-28) — do NOT blindly delete override pages from an
iPhone bundle.** A bundle produced by the CURRENT
`scripts/package-iphone-app.sh` is self-contained: the activation patch and
data ark are baked into `nand.pack`, so zero override pages is correct and it
boots to an activated SpringBoard. An OLDER bundle may not be — the 1.1.4
bundle here carried 84 pages written by some earlier in-place boot, and those
pages held state its pack lacked. Deleting them left a bundle that hung at
early kernel 3/3, and a single in-place re-boot regenerated pages that let it
boot but only as far as the **activation screen** ("Connect to iTunes").

So: leave them alone unless you know the bundle came from the current
pipeline, and if you do remove them, verify the bundle still reaches an
activated home screen. The repair is a repackage, which regenerates the pack
with activation included:

```bash
scripts/package-iphone-app.sh --firmware 4A102
```

**The lesson**, and the reason this is written down: a read-only NAND makes
"state that should persist doesn't" the *default*, not a symptom. Anything in
that class should be classified as expected before any measurement is taken.

---

Related: the display path's remaining hacks, and why the same kext-symbols
approach that cracked this should work there, are in
[`MBX_HANDOFF.md`](MBX_HANDOFF.md).

## OPEN (2026-07-29): icon row 1 on iPhone OS 1.0 never registers a tap

Found from the browser port, but **this is a device-model or guest-mapping
question, not a browser one** — nothing in the wasm input bridge is implicated,
because the LCD's own handler logs the touch and the coordinates it logs are
correct.

**Symptom.** On 1.0's SpringBoard home screen, taps on the TOP icon row (Text,
Calendar, Photos, Camera) never launch anything. Taps on row 2 and row 3 launch
normally.

**Evidence.** Ten attempts across four runs, spanning every press duration
tried, all on the top row; three successes on row 2 and one on row 3:

| target (panel y) | `fy` the model computes | holds tried (ms) | launches |
| --- | --- | --- | --- |
| **67** (row 1) | **0.860** | 30, 60, 120, 250, 280, 320, 360, 400, 500, 800 | **0 / 10** |
| 157 (row 2) | 0.673 | 450, 500, 500 | 3 / 3 |
| 157 (row 2) | 0.673 | 30 | 0 / 1 |
| 249 (row 3) | 0.481 | ~700 | 1 / 1 |

**What is ruled out.**

- *Bad coordinates from the host side.* A row-brightness profile of a native
  home-screen framebuffer (`pmemsave` of `0x0f400000`) puts icon row 1 at rows
  40–94, centre ~67, row 2 at 132–186 and row 3 at 220–276. The tap coordinate
  is dead centre on the row-1 icons.
- *A broken host→guest mapping.* `ipod_touch_lcd_mouse_event` logs
  `fy = 1 - y/2^15`, and the round trip is exact in the cases that WORK:
  panel y=249 → `fy=0.481` → `(1-0.481)*480 = 249`, and that tap launched
  Settings. The same arithmetic gives 67 for `fy=0.860`.
- *The readiness gate.* Every attempt above is after
  `[LCD] Touch input ready` and the model logs `[TOUCH] mouse DOWN`/`mouse UP`
  for each one, so the LCD handler accepted them.
- *Press duration.* Row 1 fails at 800 ms as readily as at 30 ms.

**What is NOT yet known:** whether the guest ever *consumes* the frame. The
decisive counter is `[MT] frame consumed` under `IT_MT_TRACE=1` (see the header
of this file) and it was not enabled for these runs. **That is the next step**,
and it splits the question cleanly:

- frame consumed → the model delivered it and the guest/SpringBoard rejected the
  position, pointing at the sensor-region descriptor or the coordinate scaling
  inside `get_frame()`;
- frame not consumed → the model queued a frame the driver never took, and the
  ATN/queue path is where to look.

### First `IT_MT_TRACE=1` run (2026-07-29): the native harness takes NO touch

Run with `scripts/touch-probe.py` (which gained `--build`, `--hold`, repeatable
`--tap` and per-tap `[MT] frame consumed` counting for this), on 1.0, three taps
in one boot with a writable NAND clone and `IT_MT_TRACE=1`:

| tap | verdict | changed | frames consumed | ATN edges |
| --- | --- | --- | --- | --- |
| 45,67 (row 1) | `frame-never-consumed` | 0.00% | **0** | **1** |
| 200,437 (dock) | `frame-never-consumed` | 0.00% | 0 | **0** |
| 45,157 (row 2) | `frame-never-consumed` | 0.00% | 0 | **0** |

`[LCD] Touch input ready` had fired and every tap was accepted by the LCD
handler, so this is not the readiness gate.

**This run does not answer the row-1 question — it invalidates itself as a
control**, because y=157 is a coordinate that demonstrably launches an app in the
browser and here it did nothing either. Something in this native configuration
takes no touches at all.

Two things it *does* establish:

- **One lost frame silences the channel.** The first tap raised exactly one ATN
  edge and was never consumed, and the two later taps raised **none** — which is
  what `ipod_touch_multitouch_queue_frame()` does by construction: with
  `next_frame` still occupied, later frames go to `deferred_frame` and no new
  ATN edge is generated. So a driver that misses the first edge stops receiving
  anything, silently.
- **`-icount` is NOT the cause.** It was the prime suspect, because
  `touch-probe.py` had `-icount shift=1` added the same day and touch had worked
  in this tool before. A rerun with icount off behaved identically: 1 ATN edge,
  0 frames consumed, 0.00% change. The suspicion was wrong and is recorded as
  such. `--icount` is now an opt-in flag on the tool, defaulting off.

**Next:** find out why the first frame is never consumed here, since that gates
everything else. Candidates not yet excluded: `IT_M68AP_NO_BASEBAND=1` (which
`touch-probe.py` sets by default), tapping too soon after the gate, and whether
the Z1 firmware upload completed in this boot (`IT_MT_TRACE=1` logs the
transitions). Only once a native tap is consumed at ALL does the row-1 versus
near-the-extremes question become answerable natively.

### touch-probe.py was testing a STOPPED machine (fixed 2026-07-30)

Every `touch-probe.py` result in this file's earlier sections that reports
"not delivered" or "0 frames consumed" should be **discounted**, and the reason
is not subtle once seen: **the probe tapped a machine whose vCPUs were stopped.**

The guest auto-locks after a few minutes idle and the PMU park then calls
`vm_stop()`. With a fixed `--boot-wait` of 300-400 s the probe was routinely
tapping a parked machine, where the LCD's mouse handler cannot run because
nothing runs — so the tap produced no `[TOUCH]` line at all and looked exactly
like a device fault.

Proof it was never the injection: restoring a snapshot of an AWAKE machine and
sending the same QMP events delivers fine, in both shapes —

| pattern | `[TOUCH]` lines |
| --- | --- |
| abs, then btn, as separate `input-send-event` commands | **2** (DOWN + UP) |
| abs + btn in one command | **2** |

so the "separate vs combined command" theory is dead too.

`touch-probe.py` now polls `query-status` and the panel before tapping, presses
Home if the machine is parked, and reports `machine not interactive before the
tap` rather than a touch verdict about a machine that was never running. Same
lesson as `--require-live` in `scripts/wasm/build-snapshot.py`: **check the vCPU
is running before attributing anything to a device.**

### Is the dead zone 1.0-only? BLOCKED on 4A102's NAND, not on touch

The browser viewer takes `?build=` now, so the comparison is one page load. But
1.1.4 in the browser reaches launchd and then renders nothing — 2.2% non-black
(the Apple logo) with `guestRatio` at ~0.5, which under `-icount` means the CPU
is IDLE, not fast.

That is the documented signature of a NAND built from an unpatched root, and the
provenance supports it: **`m68ap-artifacts/builds/4A102/nand` does not exist**
(W7a is open); only `nand-prepack-not-product` and `nand-metadata-only` do. The
two candidate 1.1.4 packs are the same SIZE but **different content**
(`8afaebbd…` vs the app bundle's `e4ace192…`), and nothing records which one
`web/chunked/4A102` was built from — neither carries a `recipe` field.

So before re-running the comparison: establish which pack that chunk set came
from, or regenerate the product NAND (W7a). Session B reports 1.1.4 reaching the
home screen in `web/bench-b/`, so their asset path may differ from this one.

### OPEN: the click-to-tap shift, and why part of it is EXPECTED

**Reported symptom (user, observed manually, native):** a click lands up and to
the left of the pointer. On **1.1.4 it is not uniform across the screen**, so a
single constant offset cannot describe it.

**One concrete measurement.** In Calculator on 1.0, the lowest-and-rightmost
click that still registered as `5` was at panel **(157.5, 345)**, while 5's
drawn button is about **x 94..146, y 269..321**. For that to still hit 5 the
guest must have received a point at or inside (146, 321):

    offset >= (-11.5, -24) panel px      i.e. kx <= 0.927, ky <= 0.930

#### Part of the vertical offset is BY DESIGN — do not "fix" it blind

iPhone OS makes tap targets **larger than the drawn control and biased upward**,
to compensate for the finger occluding the target and the contact centroid
sitting below where the user believes they are pointing. Driven by a MOUSE,
which is exact, that compensation appears as a systematic upward error. **So a
vertical up-shift is expected**, and treating all of it as a bug would mean
"fixing" the emulator until it stops matching the hardware.

**Where that compensation lives: in the GUEST, not here.** The Zephyr controller
reports a raw contact centroid; the expansion and upward bias are UIKit /
SpringBoard hit-testing. So its magnitude is not a constant in this tree — it
would be recovered by disassembling UIKit, or inferred from a hit-box map. Our
model's only job is to deliver an accurate contact point.

**The horizontal component is NOT explained by any of that** — finger geometry
is symmetric left/right. That part is ours.

#### The discriminator, and the instrument

A design compensation is roughly CONSTANT across the screen. A scale error GROWS
with distance from the origin. **1.1.4 varying across the screen therefore points
at a scale error, not at compensation** — so mapping the offset at several
positions separates them.

`scripts/calc-touch-map.py` is the instrument, and Calculator is why: every other
touch test here answers a yes/no ("did an app launch") through a settle window
already shown to be unreliable, whereas **pressing a digit puts that digit in the
display** — a precise, per-tap oracle. That turns hit-box edges into a binary
search instead of a guess. Clear with `c` between taps.

**State: stage 1 only.** It restores the snapshot, taps the Calculator icon at
(122, 247) — the measured centre of row 3 / column 2 — and then reports
`not in Calculator (top strip brightness 102)`. The tap path itself is known
good (the same harness launches Settings from (274, 249)), so this is either a
settle that is too short or a display-strip probe aimed at the wrong rows. The
calibration and boundary-search stages are designed but NOT written.

*(Superseded 2026-07-30 — the tool is finished and the map is measured. See
"The touch map, measured" below. The stage-1 failure above did not reproduce:
the tap and the threshold were both fine, so the run that produced it had
simply not launched Calculator. Calculator's top strip reads **149** and the
home screen **102**, so the 120 threshold was correctly placed.)*

### The touch map, measured (2026-07-30) — the shift is VERTICAL and it is a SCALE

`scripts/calc-touch-map.py` is finished and ran end to end on iPhone OS 1.0
(1A543a), 357 taps in one restored-snapshot session, ~15 minutes. It measures,
per button, the four edges of the ACTUAL hit box by binary search, against a
button grid profiled from the real framebuffer.

**Drawn grid, measured off the scanout** (rows profiled inside column 4, columns
profiled inside row 1 — see "the profile has to be taken where the buttons are
light" below):

| | extent |
| --- | --- |
| keypad rows | 127..176, 199..248, 270..318, 342..390, 412..461 |
| keypad columns | 16..66, 95..145, 175..225, 254..302 |

**The map.** `dL`…`dB` are hit-box edge minus drawn edge; positive means the hit
box sits right of / below the drawn edge, i.e. **the guest receives a point left
of / above the cursor**.

```
digit  centre(drawn)    dL    dR    dT    dB  shift x  shift y  slop x  slop y
    7   41.0, 223.5   -     9.0  12.0  30.0      -       21.0     -       9.0
    9  200.0, 223.5 -13.0   6.0  12.0  30.0     -3.5     21.0     9.5     9.0
    1   41.0, 366.0   -     9.0   5.0  22.0      -       13.5     -       8.5
    3  200.0, 366.0 -13.0   6.0   5.0  22.0     -3.5     13.5     9.5     8.5
    5  120.0, 294.0 -11.0   7.0   9.0  27.0     -2.0     18.0     9.0     9.0
```

`shift = (dL+dR)/2` is the hit box's **centre** displacement — the candidate
coordinate error. `slop = (dR-dL)/2` is how much **larger** than drawn the box
is — the guest's own tap-target expansion, which is design and not ours.

**Four independent consistency checks passed**, which is why the numbers are
worth building on:

* buttons in the same row give **identical** vertical edges (7 and 9 both 211 /
  278; 1 and 3 both 347 / 412), and buttons in the same column give identical
  horizontal edges (7 and 1 both R=75; 9 and 3 both 162 / 231);
* every edge measured on the first run reproduced **exactly** on the second;
* digit 5's bottom edge measures **345** — the same value the user reached by
  hand ("the lowest click still registering as 5 was at (157.5, **345**)");
* the drawn boxes agree with the hand-measured ones (5 at x 95..145 y 270..318
  versus "about x 94..146, y 269..321").

#### The verdict: vertical is ours, horizontal is not

`scripts/calc-touch-map-fit.py` fits `shift(C) = slope·C + intercept` per axis:

| axis | measured shifts | slope | swing over the probed span | verdict |
| --- | --- | --- | --- | --- |
| x | 120→−2.0, 200→−3.5 | −0.0188 | −1.5 px over 80 px | **CONSTANT** |
| y | 223.5→+21.0, 294→+18.0, 366→+13.5 | −0.0527 | −7.5 px over 142 px | **SCALE** |

The three vertical points are collinear to **0.6 px**. This is the
discriminator the whole exercise was built around, and it comes out clean:

* **Vertically there is a real scale error, and it is ours.** `1/(1−0.0527) =
  1.0556`: the driver behaves as if the sensor surface were `7306 / 1.0556 =
  6922` tall, not the 7306 the model uses. The error is *largest at the top of
  the panel* and shrinks downward, because the model inverts y
  (`fy = 1 − y/2^15`), so the scale pivots about the BOTTOM of the screen.
* **Horizontally there is no scale to speak of**, and the constant is ~2–3 px in
  the direction *opposite* to the reported symptom (the guest lands slightly
  RIGHT of the cursor). The reported "and to the left" is not a coordinate
  error: it is the ±9 px of hit-box **slop**, which is the guest's own target
  expansion and must not be touched.

**So the single hand measurement was right about the magnitude and wrong about
the split.** It inferred `offset ≥ (−11.5, −24)` from one corner point, but a
corner conflates shift with slop. Decomposed: at digit 5 the bottom edge sits
+27 px below the drawn button = **+18 of real shift plus +9 of slop**, and the
right edge sits +7 px right = **−2 of shift plus +9 of slop**. The horizontal
11.5 px is slop almost entirely.

#### Then 1.1.4 was measured, and it says the opposite

Same instrument, same Calculator (its keypad profiles **byte-identically** on the
two firmwares, so the maps are directly comparable), 326 taps:

```
digit  centre(drawn)    dL    dR    dT    dB  shift x  shift y  slop x  slop y
    7   41.0, 223.5   -     7.0   0.0  22.0      -       11.0     -      11.0
    9  200.0, 223.5 -10.0  12.0   0.0  22.0      1.0     11.0    11.0    11.0
    1   41.0, 366.0   -     7.0   1.0  23.0      -       12.0     -      11.0
    3  200.0, 366.0 -10.0  12.0   1.0  23.0      1.0     12.0    11.0    11.0
    5  120.0, 294.0 -12.0  10.0   1.0  24.0     -1.0     12.5    11.0    11.5
```

| build | x shift | y shift | verdict on y | slop |
| --- | --- | --- | --- | --- |
| 1.0 (1A543a) | −2.0 … −3.5 | 21.0 → 18.0 → 13.5 | **SCALE**, slope −0.0527 | 9 |
| 1.1.4 (4A102) | −1.0 … +1.0 | 11.0 → 12.5 → 12.0 | **CONSTANT**, slope +0.0070 | 11 |

**On 1.1.4 there is no scale error at all.** What is left is a **constant** ~11–12
px up-shift with a symmetric ±11 px slop — which is precisely the signature the
discriminator assigns to *design compensation*, not to a coordinate bug. On 1.0,
the same constant is there and a scale is stacked on top of it.

**This reverses the reported symptom.** It was reported as "not uniform across
the screen on 1.1.4"; measured, **1.1.4 is the uniform one and 1.0 is the one
that varies**. (Caveat on scope: the Calculator keypad only spans y 199…390, so
this says nothing about the status bar or the dock. A scale shows up worst at the
extremes, and over the full 0…480 panel 1.1.4's slope is still only ±3 px.)

**And it kills the tempting fix.** The model's constants are what 1.1.4's driver
already agrees with, on both axes. So the height cannot simply be changed to suit
1.0 — that would flatten 1.0 and introduce into 1.1.4 exactly the error 1.0 has
now. The two drivers derive the sensor→screen mapping differently, and no single
pair of constants satisfies both. That is the real finding, and it is the same
trap as the advertised-scale "fix": a change that is obviously right against one
firmware, measured wrong against another.

#### What 6922 probably is, and how to test it rather than assume it

The number 1.0's driver behaves as if it had is `7306 / 1.0556 = 6922 ± 18`.
Within that, `4602 × 480/320 = 6903` — **the height that would give the internal
surface the panel's own aspect ratio**. That is not arbitrary: the model also
advertises a sensor grid of `MT_SENSOR_ROWS 15 × MT_SENSOR_COLUMNS 10`, whose
ratio is exactly 1.5, while `4602 × 7306` has a ratio of 1.588. So the model
describes its sensor two ways and the two disagree, and **1.0's driver appears to
believe the grid while 1.1.4's believes the surface** — which is exactly the
shape that produces one firmware with a scale error and one without.

That is a hypothesis with a prediction, not a fix, and the last time a
sensor-surface constant "obviously" needed changing the change measured
**wrong**. So it lives behind `IT_MT_SENSOR_SCALE=aspect`, **nothing is changed
by default**, and the prediction is stated in advance and in both directions:

| arm | predicted with `aspect` |
| --- | --- |
| 1.0 | `shift_y` slope collapses to ~0; the residual constant lands near +6 |
| 1.1.4 | slope goes the OTHER way, ~+0.058, ±4 px across the keypad — i.e. broken |

If 1.1.4 does break, the conclusion is not "apply it anyway" — it is that the
model needs to report a self-consistent sensor geometry (grid ratio and surface
ratio agreeing) rather than have one constant tuned to one firmware. Anything
else and the hypothesis is dead and the 1.0 scale error needs a different cause.

**Arm 1 — 1.0 with `aspect`: the prediction held, to the pixel.** 343 taps, same
snapshot, same everything else:

```
digit  centre(drawn)    dL    dR    dT    dB  shift x  shift y  slop x  slop y
    7   41.0, 223.5   -     9.0  -3.0  18.0      -        7.5     -      10.5
    9  200.0, 223.5 -13.0   6.0  -3.0  18.0     -3.5      7.5     9.5    10.5
    1   41.0, 366.0   -     9.0  -3.0  18.0      -        7.5     -      10.5
    3  200.0, 366.0 -13.0   6.0  -3.0  18.0     -3.5      7.5     9.5    10.5
    5  120.0, 294.0 -11.0   7.0  -3.0  19.0     -2.0      8.0     9.0    11.0
```

| | slope | shifts |
| --- | --- | --- |
| 1.0 default | −0.0527 | 21.0, 18.0, 13.5 |
| 1.0 `aspect` | **−0.0000** (max resid 0.4) | **7.5, 8.0, 7.5** |

The vertical scale error is gone; the residual constant is +7.6, against a
predicted +6.4. And the control holds: **every horizontal edge is byte-identical
to the default run** (−13/+6, +9, −11/+7), which is what the change is supposed
to do, since it touches only the height.

So the 1.0 vertical scale error is **real, is ours, and its cause is identified**:
the model's sensor surface height disagrees with what 1.0's driver derives. What
is left on 1.0 after correcting it — a flat +7.6 px up-shift with symmetric
±10.5 slop — is the same shape 1.1.4 shows untouched, i.e. design compensation.

**Arm 2 — 1.1.4 with `aspect`: it breaks, and by the predicted amount.** 310 taps:

```
digit  centre(drawn)    dL    dR    dT    dB  shift x  shift y  slop x  slop y
    7   41.0, 223.5   -     7.0 -17.0  10.0      -       -3.5     -      13.5
    9  200.0, 223.5 -10.0  12.0 -17.0  10.0      1.0     -3.5    11.0    13.5
    1   41.0, 366.0   -     7.0  -8.0  19.0      -        5.5     -      13.5
    3  200.0, 366.0 -10.0  12.0  -8.0  19.0      1.0      5.5    11.0    13.5
    5  120.0, 294.0 -12.0  10.0 -11.0  15.0     -1.0      2.0    11.0    13.0
```

| arm | predicted slope | measured slope | predicted shift @ y 223.5 | measured |
| --- | --- | --- | --- | --- |
| 1.0 `aspect` | ~0 | **−0.0000** | +6.4 | +7.5 |
| 1.1.4 `aspect` | +0.058 | **+0.0631** | −3.5 | **−3.5** |

1.1.4 goes from flat (slope +0.007) to a **9 px swing across the keypad**, in the
opposite direction to 1.0's original error — which is what "the guest was already
using the number you just changed" looks like. Both predictions were written down
before the runs and both came in; horizontal edges are unchanged in both arms.

#### Verdict, and what is deliberately NOT being changed

1. **The vertical part of the reported shift is real and is ours — on 1.0 only.**
   A scale of 1.0556, worth 21 px at the top of the keypad, caused by the model's
   sensor height. `IT_MT_SENSOR_SCALE=aspect` removes it exactly.
2. **The horizontal part is not a coordinate error at all.** Both builds sit
   within ±3.5 px, and 1.1.4 within ±1. What reads as "and to the left" is the
   guest's ±9–11 px of hit-box slop, and a single corner measurement cannot tell
   the two apart — which is how the original `kx ≤ 0.927` estimate came about.
3. **The rest is design compensation and must stay.** After correcting 1.0, both
   builds show a constant up-shift (1.0: +7.6, 1.1.4: +11.5) with a symmetric
   slop. Constant across the panel is the signature of guest hit-testing, not of
   this tree.
4. **The default is NOT changed, and should not be on this evidence.** The flag
   fixes one firmware and breaks the other by a comparable amount. Shipping it
   would trade a 1.0 defect for a 1.1.4 one — the same shape of mistake as the
   advertised-scale "fix", caught this time before it landed rather than after.

### Where the up-shift comes from: `SBFingerProjection`, 3.5 points (2026-07-30)

Disassembled rather than inferred, with `scripts/macho-disasm.py` against the
1.1.4 root filesystem. **The whole sensor→screen path is in userland, not in the
kext**, which is why looking at `AppleMultitouchSPI.kext` alone was never going
to answer it: `handleFrame()` forwards the raw frame to registered user clients
and interprets no coordinates at all.

The chain, end to end:

| stage | binary | what it does |
| --- | --- | --- |
| cache | `AppleMultitouchSPI.kext` `_cacheSensorSurfaceDimensions` | reads report **0xD9**, publishes the two words as IORegistry `Sensor Surface Width` / `Height` |
| bounds | `MultitouchHID.plugin` `MultitouchHIDClass::ResetHMLite` | `MTDeviceGetSensorSurfaceDimensions()`, divides each by **100** (units are hundredths of a mm), and passes them to `MTHMLiteInit` as `gScreenBounds_mm`. If the query fails it keeps a hardcoded default of **{0, 0, 50.0, 75.0} mm** — numerically identical to the 5000 × 7500 this model advertises |
| position | `MultitouchHID.plugin` `_mthm_FilterContactForScreenUI` | `px_x = normX * gScreenSize.width`, `px_y = screenH - normY * screenH` |
| tip offset | same, via `_mthm_ComputeFingerEllipseTipOffset_mm` + `_mthm_mmToPixels` | converts an offset in **mm** to pixels with `screenPx / gScreenBounds_mm` |

**So in the plugin the contact POSITION is `normalised × screen size`, and the
sensor-surface dimensions are used ONLY to scale the tip offset**, at
`320/50 = 480/75 = 6.4 px/mm`. The position arrives already normalised.

The normalisation itself happens upstream, in `MultitouchSupport.framework` —
**see the next section, which found it.**

**The upward projection, with its actual number.** `MultitouchHID`'s own
defaults have `majorAxisGain = 0` and `upwardsAxisOffset_mm = 0`; the values are
pushed in at runtime by **SpringBoard**, which is the only other binary in the
filesystem mentioning `FingerTipVerticalOffset`:

```
SBFingerProjection  (com.apple.springboard)   default 3.5     -> FingerTipVerticalOffset
SBFingerGain        (com.apple.springboard)   default 0.0     -> EllipseTipGain
                    multiplied by 25.4/72 -- the value is in TYPOGRAPHIC POINTS
```

    3.5 pt x 25.4/72 = 1.2347 mm ; 1.2347 mm x 6.4 px/mm = 7.90 px

**7.90 px of deliberate upward shift**, and `1A543a` and `4A102` carry
byte-identical code with the same 3.5 default — so it is the same on both.

**Does it match the measurement? On 1.0, almost exactly.** Once the separate
vertical scale error is corrected, 1.0's residual constant is **+7.5…+8.0 px**
against a predicted **7.90**. 1.1.4 measures **+11.0…+12.5**, about 3.6 px more;
since the projection is identical, that surplus is the guest's own hit-box
asymmetry (1.1.4's slop is 11 px against 1.0's 9), not the multitouch path.

**And no, the iPod work did not remove it.** Nothing in this tree writes
`SBFingerProjection`, `SBFingerGain`, `FingerTipVerticalOffset` or
`EllipseTipGain`, and no NAND recipe patches them. The projection is intact and
authentic on all three bundles.

#### Optionally cancelling it: `IT_MT_TIP_CORRECTION`

Because a mouse is exact and occludes nothing, the projection is a pure error
for a desktop user — so the model can pre-correct it, and now can:

```bash
IT_MT_TIP_CORRECTION=11.5 ...      # panel pixels; contacts are reported LOWER
```

`get_frame()` subtracts it from the normalised y (the sensor origin is at the
BOTTOM, so "lower on screen" is a *decrease*), before the velocity computation
so that stays consistent. **The default is 0 — historically faithful.** It is
deliberately vertical-only: horizontal measured within ±3.5 px on 1.0 and ±1 px
on 1.1.4, so there is nothing there to correct.

Suggested values, from the map: **11.5** for 1.1.4, and for 1.0 either **7.6**
(the projection, leaving the scale error visible) or nothing until the scale
question is settled. Anyone wanting the authentic behaviour — including the fact
that a real finger *needed* that 1.2 mm — should leave it off; the difference
showing is itself the historical detail.

#### The browser page: a checkbox, and the numbers it needs

The web port injects **panel pixels** straight into the model
(`_wasm_input_touch(x, y, down)` in `web/public/jit-boot/index.html`), so the
pre-correction there is a JS-side offset on `y` and needs **no wasm rebuild**
and no env var. `#tipfixbox` does exactly that, **off by default**.

The measured correction is per build and is affine, `shift(y) = a + b·y` in panel
pixels — it is added back to the injected `y`, so a click lands where the pointer
is:

| build | a | b | at panel top → bottom |
| --- | --- | --- | --- |
| 1A543a (1.0) | 32.91 | −0.0527 | +33 px → +8 px |
| 4A102 (1.1.4) | 9.64 | +0.0070 | +10 px → +13 px |

**The two rows are not the same kind of number, and the UI should not pretend
they are.** 4A102's is a constant and is entirely the guest's own behaviour —
`SBFingerProjection` (7.90 px) plus that build's hit-box asymmetry; nothing is
broken. 1A543a's is a **scale**, and only ~7.6 px of it is the finger
projection; the rest is this emulator's own vertical geometry defect, so ticking
the box there *hides* a bug rather than compensating for a design choice. That is
recorded in the `BUILDS` table next to the coefficients.

Deliberately **not** applied to the `?tap=` / `?sweep=` paths: those exist to
measure the raw path, and correcting them would measure the correction.
Horizontal is not corrected at all — measured within ±3.5 px on 1.0 and ±1 px on
1.1.4, and only two columns were probed, so there is nothing there to justify it.

**What we have, and what we do not.** Both builds the page serves are mapped, so
the numbers exist. Two gaps worth stating before anyone trusts the box:

* **The iPod (N45AP, Zephyr 2) has never been mapped.** The page does not serve
  it, but any UI that grows an iPod row needs its own map first — its driver is
  a different one and none of these numbers transfer.
* **The fit is only measured over y ≈ 199…390**, the Calculator keypad. The
  table extrapolates it to the full 0…480 panel, which is untested at the
  status bar and the dock — and for 1.0, where `b` is real, the extrapolation
  is exactly where it is least safe. Probing an app with controls near both
  extremes would close that.

**Verified as far as the browser currently allows**: the page loads with no
console error, the control renders, and the label computes `8–33 px` for 1.0
from the table. **It is not verified end-to-end against a live guest** — the
snapshot resume currently stalls before the panel comes up (runstate never
reaches `running`, 0 frames). That stall is **pre-existing and unrelated**:
checking `index.html` out at HEAD, with no checkbox present, reproduces it
exactly. It is the browser thread another session is working on.

*(Noticed while testing, not fixed, not mine: the page has no
`<meta charset="utf-8">`, so the em dash in the `<h1>` renders as mojibake.)*

**Verified with the same instrument**, 1.0 on the shipped snapshot,
`IT_MT_TIP_CORRECTION=11.5`, 325 taps:

| | shift_y by row (223.5 / 294 / 366) | shift_x | slop |
| --- | --- | --- | --- |
| off | 21.0 / 18.0 / 13.5 | −3.5, −2.0 | 9.0–9.5 |
| 11.5 | **9.5 / 6.0 / 2.0** | −3.5, −2.0 | 8.5–9.5 |

A uniform **−11.5 px at every row**, with the horizontal edges byte-identical
(−13/+6, +9, −11/+7) and the slop unchanged. The knob does exactly one thing,
and the surviving slope is the separate 1.0 scale error — which this correction
deliberately does not touch, because a constant cannot cancel a scale.

### What MultitouchSupport normalises by: the sensor GRID, not the surface

`_mt_FillMTContactDirectFromBinary` in `MultitouchSupport.framework` is the
function that turns a wire contact into an `MTContact`. The position is at wire
offsets **+4 (x) and +6 (y)** — `MTParse_BinaryPathOrImage` copies the contact
field-for-field, byte-swapping only, so wire offsets survive intact — and it is
used three times:

```
_alg_ClipPosToScreenEdge(x, xMin, xMax)          ; clipped to the surface range
[contact+0x40] = x / 100.0                       ; millimetres
[contact+0x1c] = (x - xMin) / (xMax - xMin)      ; NORMALISED  <- what the plugin reads
```

So, end to end:

    normX = (rawX − xMin) / (xMax − xMin)
    normY = (rawY − yMin) / (yMax − yMin)
    px    = normX × screenWidth ,   screenH − normY × screenHeight

**And `xMin/xMax/yMin/yMax` are not the advertised surface dimensions.** They are
four `int16`s at `surface+0x148…0x14e` (1.1.4; `+0x150…0x156` on 1.0), in
hundredths of a millimetre — the same fields `_MTSurface_getBounds_mm` divides
by 100 — and they are built by **`_alg_InitRowColXYConvert`** from the sensor's
**row and column counts**:

* two 65/66-entry `int16` lookup tables are built, one per axis, each entry
  `((i − 1) × pitch × 100) / divisor` via `__divsi3`;
* the range is then `table[count] + margin` down to `table[1] − margin`;
* **X comes from the ROW table and Y from the COLUMN table.**

1.1.4's `_alg_InitZephyrPlatformSpecifics` supplies the per-family constants for
the Zephyr branch (family id `0x41`, `0x42`, `0x50…0x52`):

| field | value | meaning |
| --- | --- | --- |
| `+0x2c` / `+0x30` | 36 / 7 | column pitch → `3600/7` = **5.143 mm** per column |
| `+0x34` / `+0x38` | 56 / 11 | row pitch → `5600/11` = **5.091 mm** per row |
| `+0x24…+0x2a` | 75 each | four edge margins, **0.75 mm** |
| `+0x1c` / `+0x20` | 5000 / 7500 | the advertised surface — stored, but not on this path |

Those pitches are the right order for a 10 × 15 grid over a ~50 × 75 mm sensor,
and `(15−1) × 5.143 + 2 × 0.75 ≈ 73.5 mm` against this model's 73.06 — i.e. the
model's internal constants sit within ~0.6% of what the guest computes, which is
exactly why 1.1.4 lands correctly.

**So the answer to "what does it divide by" is: a range it computes itself, from
the grid the device reports times constants compiled into the framework.** The
advertised `MT_REPORT_SENSOR_DIMENSIONS` never touches the position path.

#### And this is where 1.0 and 1.1.4 diverge

The same two functions exist in 1.0's `MultitouchSupport` and are structurally
identical (all offsets shifted by +8). **But 1.0's
`_alg_InitZephyrPlatformSpecifics` Zephyr branch sets only the 5000 / 7500 pair
and a table pointer — no pitch, no divisor, no margins at all**, and it reads the
row/column counts from different fields (`[fp]` and `[fp+4]` words, versus
1.1.4's bytes at `[[fp+8]+2]` / `[[fp+8]+5]`). 1.1.4 gained the whole per-family
pitch/margin block that 1.0 lacks.

Different constants feeding the same formula is a complete and sufficient
explanation for the measured divergence — 1.1.4 flat, 1.0 carrying a −5.6%
vertical scale — without either build being "wrong".

**Not closed, and stated as such:** the exact numeric range was not reproduced
arithmetically for either build. Doing that needs the table base term
(`~[fp+0x3c]`), the actual row/column values the driver passes, and 1.0's pitch
defaults (set somewhere other than the function that sets 1.1.4's). What is
verified is the formula, the field locations, the table construction, 1.1.4's
constants, and 1.0's *absence* of them.

#### Confirming it: perturb the reported grid

If the reading above is right, changing the grid the model reports **must** move
where taps land. `IT_MT_SENSOR_GRID=<rows>x<cols>` overrides the SENSOR_INFO
(0xD3) reply for exactly that test; the default is the real 15 × 10.

**Result: it moves, and the mechanism is confirmed.** Cold-booted 1.0 with
`IT_MT_SENSOR_GRID=14x10` (one row fewer), everything else identical:

| | 15 × 10 (real) | 14 × 10 |
| --- | --- | --- |
| tap (122, 247) = Calculator's icon centre | launches, every run | **nothing launches** |
| `[TOUCH] mouse DOWN` | 1 | 1 |
| `[MT] frame consumed` (`IT_MT_TRACE=1`) | — | **18** |
| panel after the tap | Calculator | home screen, 45.2% — unchanged |

The driver is **alive and consuming frames**; the touch simply arrives somewhere
other than the icon. That is the positive control the disassembly needed: the
grid the model reports really does set the range the position is normalised by.

**One detail does not fit yet and is left open.** A one-row change should move
the mapping by ~7% if the range were simply proportional to `count − 1`, which
would still land inside a 57 px icon — yet the tap misses entirely. So the
displacement is larger than that proportionality predicts, meaning either the
row/column count feeds the range somewhere else as well, or the table index and
the count are not related the way the loop above suggests. Quantifying it needs
a map run whose Calculator icon position is re-found for the perturbed grid.

**A trap that would have produced a false negative:** the first attempt ran from
the shipped snapshot, which is the fastest harness and the wrong one here. The
driver queries the sensor grid **once, during its startup**, long before the
snapshot was captured, so the restored guest already holds the original
15 × 10 range and no override can reach it. **Any experiment that changes what
the model reports during enumeration has to cold-boot.** Killed and re-run
against a `cp -Rc` clone of the 1A543a NAND, which also keeps the artifact tree
read-only.

#### Dead ends and traps from the disassembly, in the order they were hit

* **The kext is the wrong binary, and it looks like the right one.** The name
  says multitouch, the symbols say `handleFrame`, `_cacheSensorSurfaceDimensions`
  — and it interprets no coordinates whatsoever. `handleFrame()` forwards to
  `sendFrameToRegisteredUserClients()`. Everything of interest is in
  `PlugIns/MultitouchHID.plugin` and `MultitouchSupport.framework`. Start there.
* **`_MTSurface_getBounds_mm`'s `/100` is a red herring.** It is the first
  concrete scale constant you find, it is exactly the "hundredths of a mm" the
  surface dimensions are in, and it is *not* on the contact-position path — it
  converts the sensor GRID descriptor's int16 bounds, which our model answers
  with an empty region descriptor anyway.
* **The plugin's own default parameters say the finger correction does not
  exist.** `gHMTipOffsetParams` is statically initialised with
  `majorAxisGain = 0` and `upwardsAxisOffset_mm = 0`. Reading the initialised
  data and stopping there gives exactly the wrong answer: the real values are
  pushed in at runtime, and the only other binary in the filesystem mentioning
  `FingerTipVerticalOffset` is SpringBoard. **Grep the whole root filesystem for
  the property name before trusting a default.**
* **A wrong intermediate conclusion, kept because it was productive.** Having
  found that `gScreenBounds_mm` = advertised dimensions / 100, the obvious
  reading is "the guest divides positions by the advertised 5000 × 7500", which
  would compress every touch by 8%. The **map says otherwise** (1.1.4 within
  ±1 px), and it was that contradiction — not more reading — that forced finding
  the actual position path, where the coordinate arrives already normalised and
  the surface dimensions only scale the tip offset. Measurement refuted a
  plausible disassembly reading, again.
* **`otool -tV` and Apple's `objdump` print nothing at all** for these `arm_v6`
  Mach-Os on a current host — not an error, just an empty disassembly, which
  reads like a corrupt file. Hence `scripts/macho-disasm.py`.
* **A C string can have more than one `__cfstring` wrapper.** Locating
  `SBFingerProjection` by finding the C string and then the `__cfstring` entry
  pointing at it found a *different* reference site than the real one; the code
  that matters used a second copy. Resolve the literal pool from the code side
  and read back what the `__cfstring` points to, rather than the reverse.

**The real defect underneath is that the model describes its sensor two ways.**
It advertises a 10 × 15 grid (ratio 1.5) and places fingers on a 4602 × 7306
surface (ratio 1.588), and the two firmwares read different ones. A fix has to
make those agree *and* keep 1.1.4's numbers where they are — which means finding
what 1.1.4's driver actually divides by, not tuning a constant until one build
looks right. `AppleMultitouchSPI.kext` in 1A543a still has full C++ symbols
(that is how the `0x46` question was settled), so the 1.0 side is readable
directly; 1.1.4's is the one to disassemble next.

#### Method notes worth keeping

* **The profile has to be taken where the buttons are light.** The digit buttons
  are dark circles carrying a bright glyph, so a column profile across a digit
  row profiles the GLYPH (~30 px), not the button (~51 px) — and a global
  threshold clips the dark rows by 10–15 px, which would have injected exactly
  the kind of systematic error the tool exists to measure. Rows are profiled
  inside column 4 (`÷ × + − =`, light in every row) and columns inside row 1
  (`m+ m− mrc ÷`, light in every column). Both then come out 49–51 px.
* **`-display none` is fine here, contrary to the earlier warning**, because the
  harness screendumps constantly: `screendump` drives `gfx_update`, which is
  what arms the LCD's input-ready gate. A probe that taps without ever taking a
  screenshot is the one that sees every touch refused.
* **Do not use `0` as a probe digit.** A cleared display already reads `0`, so it
  is indistinguishable from "the tap hit nothing".
* **The oracle must not compare bytes exactly.** One pixel at the display's
  bottom bevel flipped mid-run and every later `c` press was then read as "the
  display would not clear", which killed a 12-minute map with two buttons done.
  It also must not use a MEAN difference: the display is a big pale gradient
  carrying one glyph, so `7` and `9` differ by ~5% of *pixels* but almost
  nothing on average, and a mean-based check declared them identical. Count
  changed bytes, and treat "cleared" as a tolerance.
* **The leftmost keypad column cannot be bracketed on the left.** A tap at x=1
  still registers as `7`, twice, in two runs — the column-1 hit box reaches the
  screen edge. Its `shift_x` is therefore unmeasurable and is reported as `-`
  rather than guessed.

#### Which 1.1.4 images the comparison used, and why that question had an answer

*(Overtaken while these runs were in flight: `b7423eb1ac` closed W7a and
`m68ap-artifacts/builds/4A102/nand` now exists, regenerated by
`build-m68ap-homescreen-nand.py` with a structured recipe. It did not exist when
the maps below were measured, so they used the route described here. Re-running
the 1.1.4 map against the product NAND is now the cheaper option and would be a
useful cross-check — its pack is `b8711bd7…`, not the bundle's `e4ace192…`.)*

The provenance worry as it stood ("`m68ap-artifacts/builds/4A102/nand` does not
exist, the two candidate packs differ, nothing records which one
`web/chunked/4A102` came from") was real, and was **sidestepped rather than
solved**: the shipped
`iPhone 2G (iOS 1.1.4).app` carries a NAND with **full recorded provenance** —
`firmware-provenance.json` names the constructor (`build-m68ap-nand.py`
`0bcb211e8b`), the IPSW (`iPhone1,1` build `4A102`), the HFS hashes, and the
five-step home-screen recipe. That is the pack that boots to a home screen, and
`nand.pack` there is `e4ace192…`, one of the two candidates. So the map was run
against a **`cp -Rc` clone** of it, never the bundle itself.

`scripts/wasm/build-snapshot.py` grew `--nand/--nor/--iboot` and `--out-name` for
this, and records the actual image paths plus an `overridden` list in the
snapshot metadata, so an override-built snapshot can never be mistaken for one
built from the product tree. Regenerating 4A102's product NAND (W7a) is still
the right thing to do; it is not a blocker for measuring touch.

**The 1.1.4 snapshot was attempted first, and failed — worth knowing before
trying again.** `build-snapshot.py --build 4A102 --nand <clone>` boots fine and
renders (`[LCD] Retained kernel enabled scanout at 0x0f400000`), then
**`[LCD] Merlot panel entered sleep`** before the 300 s boot-wait is up, and the
producer's wake loop reports `panel at 0.0%, waking ...` twice and refuses to
ship a dark snapshot. **Home does not bring it back**: the `[BTN]` lines show
the keypresses reaching the guest, but a slept Merlot panel comes up on the LOCK
screen, which then wants a slide (`scripts/lock-unlock-probe.py` has the
recipe — drag y=430 from x=45 to x=280). The serial log also fills with repeated
`AppleMultitouchSPI: downloaded 44480 bytes of firmware data` after the sleep.

Rather than teach the snapshot producer to unlock, the maps **cold-boot and
poll**: `calc-touch-map.py` grew `wait_live()`, which proceeds the moment the
panel is live (two consecutive samples ≥40%) instead of after a fixed wait, so
the run starts before the auto-lock rather than racing it. The map then taps
continuously and nothing sleeps. A 1.1.4 cold boot to a live panel took ~420 s
of allowance and came up at 69.8%.

**One caveat on the clone:** pointing QEMU at it means guest writes land in it,
so successive runs do not start from identical state (the bundle's own launcher
clones per launch precisely to avoid this). For a hit-box map that is harmless —
nothing measured depends on stored state — but re-clone before anything that
does.

### Measured home-screen geometry (1.0), and a method for hit-box work

Taken from a column/row brightness profile of the real scanout, so it is not an
assumption. **The bright runs are exactly 57 px — the visual icons ARE the
57x57 tap targets**, which makes the geometry unambiguous:

| | extent |
| --- | --- |
| icon rows | **39..95**, **129..185**, **219..275** (centres 67, 157, 247) |
| icon columns | **23..69**, **94..150**, **170..226**, **246..302** (centres 46, 122, 198, 274) |
| dock row | 392..455 |
| gaps (columns) | 70..93, 151..169, 227..245 |

**Method that works, and is cheap.** Restore the shipped snapshot rather than
booting: an identical machine state every run, interactive in ~20 s instead of
~250 s, so a tap costs ~1.5 min end to end. `scripts/wasm/snapshot-probe.py`
produced the snapshot; the A/B harness is a dozen lines around `-incoming` plus
`input-send-event`.

**Method that does NOT work: tapping icon centres.** Two attempts at the
sensor-scale A/B tapped centres and both arms launched, which discriminates
nothing. Aim at a GAP, or at a target edge.

#### An under-powered probe, recorded so it is not mistaken for a result

Hunting a vertical shift at column 4 with single trials and a 25 s settle:

| tap | result |
| --- | --- |
| (274, 215) — just ABOVE the 219..275 target | 45.1% -> **70.6%** |
| (274, 225) — INSIDE the target | 45.4% -> 45.4% (nothing) |
| (274, 249) — centre | 45.1% -> 99.95% (launched) |

70.6% is neither the home screen nor a settled app — almost certainly a launch
animation caught mid-flight, i.e. 215 DID activate something. But 225 being
inert while 249 launches does not fit any simple shift, and **one trial per
point with a 25 s settle cannot separate that from timing noise** (a launch
animation has been seen to take far longer). Repeat each point several times
with a longer settle before drawing anything from it.

The horizontal boundary probe was better behaved: x=240 did not launch, x=250
did, and the target edge is 245.5 — so **no gross horizontal shift at column 4**.

### The sensor-scale "bug" is NOT a bug — a wrong fix, caught by measurement

**Do not "fix" the mismatch between the advertised and internal sensor
surfaces.** It was tried on 2026-07-30 and measured wrong, in the direction that
would have shifted every touch on all three apps by ~8%.

**The apparent defect.** `MT_REPORT_SENSOR_DIMENSIONS` hands the guest
5000 x 7500, while `get_frame()` places a finger at `fx * 4602, fy * 7306`
(`MT_INTERNAL_SENSOR_SURFACE_*`). Scaling by a surface you do not advertise
reads like an obvious bug, and it is the right *shape* for a reported "touch
shift" — which is on 1.1.4 as well as 1.0, so it lives in this shared model
rather than in one firmware.

**The prediction, and why it was wrong.** If the driver mapped sensor→screen
with the dimensions it was told, every touch would be compressed by 4602/5000
horizontally — displaced ~22 px left at the right-hand column. Making the two
agree would remove that. Both halves of that turned out to be false.

**The measurement.** A/B on ONE binary via `IT_MT_SENSOR_SCALE`, restoring the
SAME snapshot each run so the machine state is identical, tapping x=235 — which
a column profile of the real framebuffer places in a GAP. Icon columns in row 3
are 23..69, 94..150, 170..226, 246..302 (centres 46, 122, 198, 274); with
**57x57 tap targets** the hit boxes are 17.5..74.5, 93.5..150.5, 169.5..226.5
and 245.5..302.5, so 235 falls between the last two.

| scale used | where x=235 should land | result |
| --- | --- | --- |
| internal 4602 (the default) | 235 — in the gap | **nothing launched** |
| advertised 5000 ("the fix") | 255 — inside Settings | **Settings launched** |

**Both are the opposite of the arithmetic prediction**, and they are only
consistent if the driver maps with something very close to the INTERNAL scale:
`235/320*4602 = 3380`, and `3380/4602*320 = 235` (gap, no launch); while the
"fixed" `235/320*5000 = 3672` gives `3672/4602*320 = 255` (on Settings,
launches). Both observations fall out of that, and neither falls out of
"the guest divides by what it was advertised".

So the two constants disagreeing is what makes a tap land where it was aimed.
Reverted; `IT_MT_SENSOR_SCALE=advertised` reproduces the wrong behaviour for
anyone who wants to re-run it.

**The transferable part:** a mismatch that looks indefensible on inspection was
load-bearing, and the check that caught it was an A/B against a *measured*
target — the framebuffer column profile — not against an assumed geometry. Two
earlier attempts at the same test tapped icon CENTRES and both modes launched,
which proved nothing; only a target in a gap discriminated.

**The reported touch shift therefore still has no identified cause.** This was
the leading candidate and it is now excluded.

### Two contaminated results from the same day, discounted

* **The row-1 tap on a restored snapshot** reported `launched=false`, but the
  panel read **0% non-black** at the end: the device had auto-locked during the
  ~2 minute wait, so the tap landed on a sleeping machine. It says nothing.
* **Every `touch-probe.py` result** in the sections above — see the harness fix
  below; the probe was tapping machines whose vCPUs were stopped.

**Worth checking whether it is really "row 1" or "near the extremes".** `fy`
0.860 is close to the top of the range while both working rows sit mid-panel
(0.481–0.673). If the sensor coordinate space is narrower than the screen, the
dock (`fy` ≈ 0.14) should fail too — which would also make the status bar and
the dock unreachable, i.e. a much bigger usability hole than one icon row. One
tap on a dock icon settles it.

**Reproduce it** with the browser boot page, which drives itself:

```sh
scripts/wasm/serve.py --port 8013 --results /tmp/r.json &
# ...launch headless Chrome at /public/jit-boot/?sweep=1&rungs=500,250,120,60,30
# (full command in BROWSER_WASM_STATUS.md); rungs 1-4 land on row 1
```

Natively, `scripts/lock-unlock-probe.py` and QMP `input-send-event` are the
equivalent instruments, and `IT_MT_TRACE=1` should be on for both.

---

## Dead ends and mistakes

Recorded so nobody spends the time twice.

**"`0x46` is an unimplemented command."** That was the first conclusion here,
and it was wrong. It is a retry counter that `deviceGetResultLength()` memsets
into its transmit buffer; the controller ignores MOSI on that read entirely. It
looked like an opcode because the model logs every unrecognised first byte as
a command, and because it repeated exactly five times — which reads like a
handshake and is really just `memset(txbuf, counter, 5)`. Chasing the "what
does command 0x46 mean" framing wasted the first pass at this.

**Assuming the two firmwares share a reply layout.** They share the 0xAA
framing, so reusing the `0x64`/`0x65` length-reply builder for 1.0 looked
obviously right. It is not: the length sits at bytes `[1][2]`, not `[4][5]`.
The failure is quiet and easy to misread — the driver reads length 0, decides
nothing is pending, and simply stops. One transaction, no error, no retry.
`[MT] Z1 unsolicited length read -> 54` printed happily while the guest was
reading a zero.

**Wiring up SPI chip-select.** The `apple_spi_update_cs()` TODO looks like the
principled fix for the framing hack. Measured (`IT_SPI_CS_TRACE=1`) it is not:
the guest re-asserts CS *between* the two halves of the split `0xEA` read, so
real CS framing would draw the boundary in exactly the same place and break it
identically. The comment claiming the guest "never drives it (0 edges
measured)" was also simply wrong — it drives it 84 times per boot on SPI2, and
only ever asserts, never deasserts. Both comments are corrected in the source.

**Measuring with taps that hit nothing.** The first probe runs tapped (160,240)
and (160,437) — blank wallpaper and the gap above the dock — got 0.0 % on every
board, and nearly produced the conclusion "frames are consumed but the guest
ignores them". Tap an actual icon, and confirm with a screenshot rather than a
percentage.

**Letting the device auto-lock mid-probe.** The ~2-minute auto-lock fires right
around when a headless boot reaches the touch gate. Several early runs measured
a sleeping device: `[LCD] Merlot panel entered sleep`, then `[TOUCH] Ignoring
input until display/driver startup is stable`, and the tap never reached the
controller. Tap immediately after the gate arms.

**Reading a modal as broken touch.** 1.1.4 was reported as regressed after the
iPod fix. It was not: the bundle comes up with SpringBoard's *Edit Home Screen*
alert covering the icons, so taps do nothing until Dismiss is tapped. Frames
were being consumed the whole time. Always look at the screenshot before
believing a 0 % diff.

**Running three emulators at once.** On a full disk the 1.1.4 bundle (which
clones its NAND per launch) hung twice at early kernel and produced a log that
looked like a boot failure. Run one at a time when a result matters.

**Treating the Edit Home Screen modal as a bug.** ~40 minutes: a two-boot
persistence experiment, a kext/strings hunt for the gating default, and a
plist injection through the NAND overlay tooling. All of it was answered up
front by "the bundle clones a pristine NAND every launch", which makes
non-persisting first-run state the expected outcome -- and the alert is a real
difference between 1.1.4 and 1.0 that should stay visible anyway. See below.

**Believing a corner measurement about an offset.** One click at (157.5, 345)
still registering as `5` gave `offset ≥ (−11.5, −24)`, which is arithmetically
right and attributes the whole excess to a shift. A corner conflates the shift
with the hit box's own **slop**, and decomposed the horizontal 11.5 px is ~9 px
of slop plus ~2 px of shift *in the other direction*. Measure two opposite edges
before calling anything an offset. (The rest of that measurement held up
exactly: 5's bottom edge really is at 345.)

**Two harness failures that each killed a 12–15 minute map after it had
started.** Both are the same lesson — a test whose oracle is a whole-image
comparison needs a tolerance, and a run that discards partial results pays for
it in wall clock:

* the `c`-clears-the-display check compared the display region **byte for
  byte**, and a single pixel at the display's bottom bevel flipped mid-run, so
  every later clear read as "the display will not clear";
* the fingerprint distinguishability check used a **mean** absolute difference,
  and on a big pale-blue gradient carrying one dark glyph, `7` and `9` differ in
  ~5% of *pixels* but almost nothing on average — it declared two perfectly
  distinguishable fingerprints identical and refused to start.

Both are fixed (count changed bytes; treat "cleared" as a tolerance), and the
map now writes `map.json` after **every edge** and tolerates up to three clear
failures instead of exiting.

**Probing a bundle by pointing QEMU at its shipped NAND.** That skips the
launcher's per-launch clone, so guest writes accumulate in the shipped image
and change what every later launch starts from. 560 stray pages across the two
iPhone bundles before it was caught. See below.

## Reproduction

**There is a harness now** (this said there was not, until 2026-07-30):

```bash
# the hit-box map -- 1.0, from the shipped snapshot, ~15 min
scripts/calc-touch-map.py --build 1A543a \
    --snapshot web/public/jit-boot/snapshots/1A543a/state --logs /tmp/map10

# 1.1.4 -- cold boot; --pre-tap dismisses the Edit Home Screen alert, and the
# Calculator icon sits at (122,237) here, not 1.0's (122,247)
scripts/calc-touch-map.py --build 4A102 --nand <clone>/nand \
    --nor <clone>/nor_m68ap.bin --iboot <clone>/iboot_204_m68ap.bin \
    --boot-wait 420 --pre-tap 180,325 --calc-icon 122,237 --logs /tmp/map114

# constant-vs-scale verdict, one or many maps
scripts/calc-touch-map-fit.py /tmp/map10/map.json /tmp/map114/map.json

# the A/B knobs, neither of them a default
IT_MT_SENSOR_SCALE=aspect     # fixes 1.0's vertical scale, BREAKS 1.1.4's
IT_MT_TIP_CORRECTION=11.5     # cancels the guest's own finger projection

# read Apple's own binaries (mount root.img read-only first)
scripts/macho-disasm.py <binary> --find Sensor
```

Useful extras: `--home-only` stops after the home-screen shot for
reconnaissance, `--geometry-only` after the keypad profile, and `--env K=V`
passes anything else through to QEMU.

For the older, yes/no style probes, the essentials:

* launch a bundle with `-display none -qmp unix:…,server,nowait`;
* wait for `[LCD] Touch input ready` in the log (the model refuses touch until
  a frame has been visibly stable for two seconds);
* tap **before** the ~2-minute auto-lock, or the device sleeps and every
  measurement is confounded (`[LCD] Merlot panel entered sleep`);
* `input-send-event` abs x = `px/320*32768`, abs y = `py/480*32768`;
* read the verdict from `[MT] frame consumed`, and confirm with a `pmemsave`
  diff of `0x0F400000` / `0x0F496000`.

Useful control: the Home key (`send-key h`) changes ~69 % of the iPod's
framebuffer, which proves the UI is alive and isolates the failure to touch.

---

## Regression and revert (2026-07-28): a device-tree edit for Wi-Fi killed 1.0's touch

**Reported by the user after a repackage**, not by a test: "the repackaged iOS
1.0 app has buttons and touch not working". They were right, and it was mine.

**What caused it.** Chasing iPhone OS 1.0's dead Wi-Fi (WIFI_SDIO_NOTES.md), I
filled the device tree's zeroed `tx-calibration` and `local-mac-address`
properties in the NOR, which gets 1.0's `AppleMRVL868x` past its calibration and
MAC checks. It also kills touch. Isolated with
`scripts/app-button-probe.py --board m68ap-10`, one variable at a time, same
NAND and same engine throughout:

| device tree | 1_open_app | 2_touch_in_app |
|---|---|---|
| nothing filled | **PASS** 97.11% | **PASS** 35.45% |
| `tx-calibration` only | **PASS** 97.11% | **PASS** 35.49% |
| `tx-calibration` + **Wi-Fi node's MAC** | FAIL 0.00% | FAIL |
| everything filled *(what shipped)* | FAIL 0.00% | FAIL |

So the calibration half is harmless; the **MAC on the Wi-Fi node** is the whole
regression. Reverted: the fill is now opt-in behind
`build-m68ap-nor.py --fill-radio-properties`, every artifact NOR was restored to
zeros, both bundles were re-installed and re-signed, and the shipped 1.0 app
re-tested **PASS / PASS** on steps 1 and 2.

**Steps 3 and 4 (HOME returns, POWER sleeps) fail in every row above,
including the clean one.** That is the separate, pre-existing in-app button bug
recorded the same day in `09fdcb46f2` — not part of this regression, and not
fixed by the revert.

**Two method failures worth more than the fix.**

1. **I shipped a firmware change without running the touch probe.** The device
   tree is not a Wi-Fi file; it is the file every driver reads. A change there
   needs the whole interaction suite, not the subsystem you were thinking about.
2. **My first A/B "cleared" the change, wrongly.** I ran `touch-probe.py`, got
   `verdict: no-response` for BOTH the filled and reverted NOR, and concluded my
   change was innocent. Both verdicts were harness artifacts: `touch-probe.py`
   runs `-display none`, and under it QEMU never calls `gfx_update` so **every**
   touch is refused — the trap `09fdcb46f2` had documented hours earlier, and
   which `app-button-probe.py` exists to avoid. The tell was there and I missed
   it: the same run said `no-response` for 1.1.4, whose touch is known to work.
   **A negative result from a harness that cannot produce a positive one is not
   evidence.** Sanity-check any interaction probe against a known-good bundle
   before trusting what it says about a suspect one.
