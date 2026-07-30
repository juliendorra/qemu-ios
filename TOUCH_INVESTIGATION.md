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

### A real coordinate mismatch, found 2026-07-30 (not yet shown to be THE cause)

The model **scales touch coordinates by one surface and advertises another**:

```c
#define MT_SENSOR_SURFACE_WIDTH  5000       /* reported to the guest */
#define MT_SENSOR_SURFACE_HEIGHT 7500
#define MT_INTERNAL_SENSOR_SURFACE_HEIGHT (13850 - 7500) * 84 / 73   /* = 7306 */
```

`get_frame()` writes `finger_data.y = fy * MT_INTERNAL_SENSOR_SURFACE_HEIGHT`
(7306), while `MT_REPORT_SENSOR_DIMENSIONS` hands the guest 7500. If the driver
maps sensor→screen with the dimensions it was given, every touch is compressed
by 7306/7500 = **2.6%**, i.e. displaced toward the bottom by an amount that
grows with height:

| panel y | intended | guest sees | error |
| --- | --- | --- | --- |
| 67 (row 1) | 67 | ~78 | **+11 px** |
| 157 (row 2) | 157 | ~165 | +8 px |
| 249 (row 3) | 249 | ~255 | +6 px |
| 437 (dock) | 437 | ~438 | +1 px |

**This is a defect on its own** — scaling by a surface you do not advertise has
no defensible reading — and it is the right shape for a remembered "shift in
touch". **But it does not explain the dead row 1**: the icons are 54 px tall and
an 11 px displacement stays well inside them.

So: fix it, but do not expect it to resurrect row 1, and do not fix it blind —
changing the scale affects every build and every tap, and the working rows are
currently working. Establish a passing tap test first.

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

**Probing a bundle by pointing QEMU at its shipped NAND.** That skips the
launcher's per-launch clone, so guest writes accumulate in the shipped image
and change what every later launch starts from. 560 stray pages across the two
iPhone bundles before it was caught. See below.

## Reproduction

`scripts/` has no harness for this yet; the probes used here live in the
session scratchpad. The essentials:

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
