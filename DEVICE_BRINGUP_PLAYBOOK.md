# Device bring-up playbook: trace-driven, externalized, parallel

A general method for making a guest driver happy with an emulated device
when the device's protocol is unknown or half-known. It was proven on WiFi
(SDIO/Marvell mailbox) and is now tooled end-to-end for the baseband
(S-Gold2 on uart1); most of its steps apply to any device the guest talks
to. The concrete baseband implementation of every step lives in
`scripts/sgold2d.py`, `scripts/baseband-lab.py`, `scripts/baseband-rules/`.

The core inversion: **stop guessing what the device should do; record what
the driver actually asks, answer it, and let the driver tell you what's
missing.** ("Respond until the driver is satisfied.")

## The six steps

### 1. Trace at the boundary before hypothesizing

Capture every byte/register access in BOTH directions, timestamped, from
the running guest — not from datasheets, not from what the driver "should"
do. Traces are ground truth; datasheet-first emulation is how the WiFi
work accumulated its long dead-ends file (`WIFI_SDIO_DEADENDS.md`).

Baseband: `sgold2d.py` logs `raw.log` (hexdump), `lines.log`
(conversation), `events.jsonl` (machine-diffable). In-QEMU stub:
`IT_BASEBAND_TRACE`. LCD: `IT_LCD_TRACE`. SDIO: `IPOD_SDIO_TRACE`.

The first baseband capture instantly falsified the standing theory: the
"deep init handshake" was three iBoot-era commands, and the kernel driver
stalls while sending *nothing* — no amount of response-guessing could have
found that.

### 2. Move the device brain OUT of the compiled emulator while iterating

An iteration must cost seconds, not a rebuild. For any chardev-backed
device (all five UARTs — baseband, bluetooth on uart3), QEMU already
supports this with zero C changes: point the machine's chardev at
`-serial unix:<sock>,server=on,wait=off` and implement the device in a
script on the other end. The guest cannot tell the difference — verified
byte-identical for the baseband (Python `stub` == C `builtin`, same 1617
serial lines, same stall).

Make the external brain's behavior *data*, not code: an ordered
match→response ruleset, hot-reloaded on mtime change, so behavior changes
mid-boot without a restart. Keep support for timed **unsolicited** sends —
drivers sometimes wait for device-initiated traffic (the kernel-era
AppleBaseband apparently does).

Non-chardev devices (SDIO, SPI, I2C, MMIO blocks) need a one-time C-side
tap (trace + optionally a socket bridge); everything else in this playbook
still applies to them.

### 3. Make every run self-judging and bounded

A human (or LLM) watching a serial log is the slowest component. Encode
the verdicts: scan the serial log for marker regexes and terminate the
instance the moment a decisive state is reached —
`springboard_reached` / `baseband_retry_loop` / `stalled` / `timeout` in
`baseband-lab.py`; pass/fail events in `ipod-https-acceptance.py`;
`iphone-nand-acceptance.py` for NAND. On the terminal verdict,
auto-collect the diagnostics you always end up wanting anyway: PC samples
(deadlock vs crawl), framebuffer non-black % from RAM (never trust a
black `screendump` — see the scanout finding), serial tail, and the
device-conversation summary.

### 4. Run hypotheses as a parallel matrix, never serially

One instance per hypothesis, launched together: N hypotheses cost one
boot's wall time (~2–8 min here), and identical staging removes
run-to-run drift from the comparison. `baseband-lab.py --rules a.json
b.json none builtin` is the template; its `matrix.json` + printed table
is the deliverable.

### 5. Always include two controls

- a **known-good baseline** (baseband: `none`) — catches environment
  regressions and separates real effects from timing artifacts;
- a **current-behavior replica** (baseband: `stub`) — proves the
  externalized path is faithful before you credit any difference to your
  change.

This rule has already paid twice: the `stabilize-root-domain` retain
turned out to be an observer-plugin artifact (weeks of confound), and the
lab's very first run showed "silent socket" panicking at
`IOIpodUSBDevice::start` — same timing-sensitive region, flagged as
artifact-suspect *immediately* because the controls made it comparable.

### 6. Consolidate: bake the winning behavior back in

The external brain is scaffolding, not the product. When the driver is
satisfied, port the final ruleset (by then a small fixed protocol) into
the C device so the machine is self-contained, and verify the port
byte-for-byte against the recorded traces (`IT_BASEBAND_TRACE` vs the
daemon's `raw.log`). This is how WiFi ended: trace-driven discovery,
then a real in-emulator implementation that associates and gets an IP.

## The iteration loop, compressed

```
edit/fork ruleset  ->  baseband-lab.py --rules <candidates> none builtin
                   ->  read matrix.json + each summary.json
                   ->  "unmatched commands" = what to answer next
                   ->  repeat
```

Minutes per cycle, no rebuilds, no manual watching.

## Seven rules added by the M68AP home-screen case (2026-07-25)

Full narrative in `M68AP_HOMESCREEN_CASE_STUDY.md`. These are the generalisable
parts — three stacked faults, and about half the effort spent on hypotheses
that were wrong for interesting reasons.

1. **Every shortcut that depends on a build-specific address must announce
   when it is not being used.** The 2022 TVOut workaround was a magic physical
   address correct for one kernel; on a second board it silently punched a
   zero-reading hole in the kernel heap and hung elsewhere. No error, no log —
   which is why it cost an entire investigation. A one-line "this window was
   never read" warning would have ended it in minutes. **Silence is the bug,
   not the shortcut.**
2. **Prefer a value the guest announces over a value you hard-code.** The
   kernel *prints* the object address the workaround needs
   (`AppleMBX: Added swap device: … id: c09c8400`). Deriving it at runtime (see
   the console tap, `include/hw/arm/ipod_touch_console_tap.h`) removed the
   per-board table and works on kernels nobody has booted yet.
3. **A negative result is only valid for the state the system was in.** Three
   correct measurements (`m68ap-mbx`, `m68ap-mbx-root`, `m68ap-prune`) became
   void conclusions, because an earlier fault stopped execution before their
   variable could matter. **When a blocker falls, re-run the negatives taken
   under it** — and mark them as provisional when you record them.
4. **Judge the property you actually care about, not its proxy.** "Non-black
   %" answered "did anything render" but could not answer "which screen" — the
   activation screen is *more* lit (41%) than the home screen (29%). Progress
   only became measurable once the lab classified the screen
   (`classify_screen()`; home = 10.7% colorful / 81% dock vs 1.8% for setup
   screens). Ask what verdict would change your next action, then measure that.
5. **Read the working reference before theorising about your own artifacts.**
   Every correct answer came from N45AP: the SpringBoard plist diff, the board
   capability table, and finally its real `data_ark.plist` — 19 keys where ours
   had 7, CFBooleans where we wrote CFNumbers. Reaching it needed one small
   tool change (`extract-hfs-from-nand.py --partition`), which is a much better
   investment than another round of guesses.
6. **Error messages are evidence, not truth.** SpringBoard's
   `"...but it wasn't a string"` fires for a CFNumber *and* for a genuine
   CFString; it actually wants a CFBoolean. Two experiments were spent
   trusting the wording. Believe accepted-vs-rejected behaviour.
7. **Expect stacked faults.** Fault 1 masked fault 2 masked fault 3, so early
   experiments legitimately produced "no change". If a well-founded hypothesis
   shows no effect, ask whether the system even reaches the code it concerns
   before discarding it.

## Two rules added by the boot-logo case (2026-07-27)

Narrative in `M68AP_RENDER_HANDOFF.md` §4 and `IPHONE_2G_BRINGUP_HANDOFF.md`.
Rule 5 above says *read the working reference*. These are its limits.

8. **A working reference proves the outcome, not the mechanism.** The iPod
   showed the Apple logo throughout its boot, so "the display path is correct
   on N45AP" looked settled. It was not: **both** boards' iBoot draw the logo
   into display window 2, which our LCD model never scanned out. The iPod
   merely recovered — its kernel adopts iBoot's framebuffer into window 1 at
   ~13 s, early enough that the logo looks continuous. The reference was
   carrying the same defect and hiding it. Before concluding "this is
   board-specific", trace the reference doing the thing, not just succeeding
   at it — and expect a fix aimed at the broken board to improve the reference
   too. (Here it did: the iPod now shows the logo *earlier*.)
9. **Know which of your traces are edge-triggered.** `IT_LCD_TRACE` logs a
   framebuffer base only when it *changes*, so a register the guest never
   writes yields no line — indistinguishable from a quiet, working path. An
   empty edge-trace is not evidence of absence. Pair every "when did it
   change" trace with a "what was written" one (`IT_FB_TRACE`: every access,
   throttled), and reach for the level-triggered one first when the question
   is about configuration rather than timing.

## One rule added by the provenance case (2026-07-27)

10. **Derive a record from the artifact, never from the inputs — and make
    "unknown" a value you can write down.** The bundle firmware manifest was
    built from the arguments handed to the installer, and the primary packaging
    path did not call the installer at all. Result: a shipped bundle whose
    manifest had a well-formed hash for every file and was wrong about all of
    them, and which described a patched guest image as stock firmware. It never
    announced itself precisely *because* it was well-formed — nothing compares
    a recorded hash to the file beside it, so correct and rotten looked
    identical. Two habits follow. Compute the record from what is on disk at
    the moment you write it, so it cannot outlive its subject. And when a piece
    of provenance is unavailable, record `MISSING` rather than omitting the
    field: an absent key silently reads as "nothing to report", which is the
    one meaning it must never have.

## Retrospective: where this would have saved time

- **WiFi SDIO** (the proof case, pre-tooling): every hypothesis was a C
  edit + rebuild + reboot + eyeball loop; the dead-ends doc is 152 lines
  long partly because cycles were expensive enough that theories were
  batched instead of tested one-per-minute.
- **The observer-artifact retain**: a standing "always run the no-plugin
  control" rule would have exposed it the first day, not weeks in.
- **The UART Tx-storm / freeze hunts**: PC-sampling + marker verdicts were
  reinvented per-investigation (`m68ap-freeze-probe.py`, `fb-snapshot.py`,
  ad-hoc QMP loops); step 3 makes them a standard attachment of every run.
- **Black-screen misdiagnosis**: "SpringBoard never renders" survived for
  a long time because one-shot `screendump` was trusted; step 3's
  RAM-framebuffer dump is now part of every terminal verdict.

## Applying it to the next candidates

- **Bluetooth (uart3, BlueCore)**: identical wiring to the baseband — a
  chardev socket, a `bluecored` ruleset, the same lab (add a uart3 socket
  option to `baseband-lab.py`).
- **Kernel-era AppleBaseband stall**: the trace says it is NOT a uart1
  request/response problem — investigate GPIO/host-wake lines (openiboot
  `hardware/radio.h`: BB_ON 0x1807, RADIO_ON 0x1507) and unsolicited
  traffic; if the fix is a GPIO, it lands in C directly (step 2 does not
  apply, steps 1/3/4/5 still do).
- **Any new sensor/codec**: start at step 1; do not write the C stub
  first.
