# Bringing up iPhone OS 1.0 on M68AP: what it actually took

Companion to [`IPHONE_OS_1X_VERSIONS.md`](IPHONE_OS_1X_VERSIONS.md) (which holds
the measurements) and in the style of
[`M68AP_HOMESCREEN_CASE_STUDY.md`](M68AP_HOMESCREEN_CASE_STUDY.md): this file
records the *process* — the wrong turns, the rabbit holes, what got me out of
each, and the fixes that actually landed. Written 2026-07-27.

Outcome: **1.0 (1A543a) and 1.0.2 (1C28) both reach the SpringBoard home
screen**, joining 1.1.1 and 1.1.4.

## The one-sentence lesson

Every real blocker was an **unimplemented corner of hardware that iPhone OS
1.1.x never touches** — not a firmware difference, not a bad NAND image. The
emulator is a partial reimplementation, and 1.0 walks paths that had been hollow
for years.

## The six fixes

| # | Fix | Why it was invisible until now |
|---|---|---|
| 1 | NAND **ECC engine** data path (`0x38F00000`) | 1.1.x moves pages with the ADM DMA engine and never uses it, so the block was a stub that raised an IRQ and copied nothing |
| 2 | ECC **region selector** — `SETUP` bits[1:0] = sectors−1; 4 = main page, 1 = spare | once it copied *something* it copied the page for both transfers, leaving `spare[8]/spare[9]` zero, so `_LoadVFLCxt` never recognised its context page |
| 3 | **Uncached aliases** — bit 31 of a physical address | 1.1.x never addresses memory that way |
| 4 | iBoot-159 **hardcodes −1 for unsigned flash images** *before* consulting the security config | iBoot-204 checks the config, so the existing relaxation was written for the wrong decision point |
| 5 | **PMU on i2c0**, not i2c1 | 1.1.x survives the 0xFF bus by believing it is on external power; 1.0 stalls in `IOIpodUSBDevice` |
| 6 | **ADM command layout is per-firmware-blob** — `-14` keeps its page at base+0x444, `-17` at +0x244 | the layout belongs to the DSP firmware the kernel uploads, not to the silicon |

Plus, guest-side: the lockdownd activation patch, `LK_ENABLE_MBX2D=0`, and
`MBCS1` = USB-present (without which 1.0 idle-sleeps before the frame is scanned
out — A/B: **45.4 % vs 0.58 %** screenout).

## The wrong turns, in order

### Claims I asserted without measuring

- **"Both image extractors die on 1.0."** False — `extract-m68ap-images.py`
  already branched on the 8900 format byte. I inferred a code fact from one
  tool's traceback instead of reading the source.
- **"1.1.x wraps the kernelcache in IMG2."** False, and it came from
  `extract-kernelcache.py`'s own **docstring**. No 1.x build does. I had already
  added a profile field encoding the difference before checking; it was removed.
  *Docstrings are not measurements.*
- **"iBoot-159's reads never reach the ADM, so no NAND content can fix it."**
  Committed as a finding. Wrong: it doesn't use the ADM **and** its reads work.
  I had measured one fact (no ADM traffic) and shipped a second (reads fail)
  that I had not. This is the single most expensive class of error in this log —
  it recurred three times.

### Hypotheses tested and killed (each cost a run or more)

- **The 1.0 NAND signature is wrong** — the natural first guess. Killed by
  disassembly: iBoot-159 loads `0x43303030` literally at `0x1801606c`, exactly
  what the generator writes. Two boots were spent before disassembling; that
  should have been step one.
- **VFL context `dwVersion` is unset** and **the signature page needs extra
  words** — both killed the same cheap way: read the **real** N45AP NAND out of
  the shipping iPod bundle. It has zeros in exactly those fields and boots fine.
- **`FMCSTAT` bit 0** (the model leaves it clear) — swept `0x1fff`,
  `0xffffffff`, `0x3`; no effect.
- **Spare bytes on the signature page** — tried `spare[0xA]=0xFF`, all-`0xFF`,
  `spare[9]=0x80`. No effect. The spare *was* the problem, but on a different
  page and because it was never delivered at all.
- **Force the image validator to report "trusted"** (`movs r5,#4` → `#1`) —
  confirmed under the debugger that r5 became 1, and the device tree still
  failed. The rejection happens *earlier*, on the unsigned path. A fix aimed one
  layer above the actual decision.
- **Force the plain-FMC fallback** — both `AppleS5L8900XADMFMC` and
  `AppleS5L8900XFMC` probe the same nub, and the plain driver would use the
  direct register path that already works. Making the ADM never report ready
  does **not** make IOKit fall back: it retries forever (45,568 serial lines in
  300 s, re-uploading the firmware each time). To revisit, the driver must
  decline the *match*, not fail its *start*.

### Rabbit holes, and how I got out

**Fictional disassembly.** I hand-disassembled iBoot with capstone from
addresses I chose. Thumb is variable-length: start off a real boundary and the
decode silently desynchronises and emits plausible instructions that do not
exist. Symptom: breakpoints that never fired while their neighbours did, and
"impossible" control flow (landing on a failure exit without hitting the
instruction immediately before it). I burned several runs before noticing.

*Escape:* **`-d in_asm -D <file>`.** QEMU dumps every translation block it
decodes — real entry addresses and exact bytes. Disassembling those is correctly
aligned by construction, and the set of blocks *is* the executed path. The
failing branch fell out in one pass. **Use this before hand-disassembling
anything in this bootloader.**

**A debugger that silently did nothing.** `Z0` + `c` against QEMU's gdbstub
appeared to work and the guest booted to completion regardless. QEMU's stub
needs **`-S`** — a client attaching to a free-running guest never stops it. Two
runs lost. `lldb -b` also hangs against the bare stub; a ~60-line GDB-remote
client in Python is more predictable. Breakpoint at the **comparison**, not at
the error message: the message is many frames from the decision.

**A half-fix that looked like a dead end.** I found the guest's buffer at
`0x98031258` was unmapped and mapped `0x98000000` → SDRAM. Nothing changed, so
I recorded it as a dead end. It was actually the *right idea with the wrong
target*: bit 31 selects the uncached view, and that buffer is the uncached alias
of `0x18031258` in **iBoot RAM**, not SDRAM. A change that "does nothing" can be
a correct fix pointed at the wrong address.

**Guessing an offset instead of deriving it.** For ADM firmware-14 I guessed
"same fields, shifted base" at `data2+0x081c`. It half-worked — commands decoded,
the boot went 1216 → 1835 lines — then read garbage (`nSig 0x5f005043`) and
panicked. Off by 8 bytes. Reverted, because a guess that converts a clean wait
into a panic on corrupt data is worse than the wait and hides the real layout.

**Misreading a field as a counter.** `data2+0x0c68` incrementing 1, 2, 3 looked
like a per-kick sequence number. It is the **page number**, stored big-endian —
`0x0007f435 → 0x0007f436` — as the kernel scans pages. Reading it as a counter
cost the better part of an investigation; the giveaway was in the bytes all
along.

## What actually worked

- **Discriminating experiments over more hypotheses.** The question "is the NAND
  wrong or is the read path wrong?" was settled in one run by feeding iBoot-159
  the *1.1.1* NAND tree. Same failure ⇒ NAND content ruled out entirely. Reach
  for the experiment that separates two hypotheses before generating a third.
- **Diff, don't guess.** `IT_ADM_DIFF` snapshots the ADM sections on every
  engine kick and reports what changed. Whatever the guest wrote before kicking
  *is* the command, wherever it lives.
- **Run the diff against the known-good build.** Firmware-17's baseline is what
  pinned firmware-14: the same `0x500/0x300/0x300/0x100` table sits at
  base+0x10 in both, which located -14's base; and -17's page field at
  `data2+0x1348` holding `0x0007ff80` (524160, the BBT page) is what revealed
  that -14's page lives 0x200 further out. **A working reference is a
  measurement instrument.**
- **Read the real device's data** to kill hypotheses about metadata (the iPod
  bundle's own NAND).
- **A/B every fix.** The iBoot patch reproduces 4A102 byte-identically; the
  charge-wait patch produces a byte-identical N45AP image; `MBCS1` was kept only
  because removing it drops the screenout from 45.4 % to 0.58 %.

## Process notes

- Committing an unverified secondary conclusion ("…and therefore reads fail")
  cost more than any single technical mistake. Commit what was measured; mark
  inference as inference.
- A cleanup with `pkill -f qemu-system-arm` killed two QEMU processes that
  belonged to other work. Match on your own PIDs.
- `fb-snapshot.py` stages a full NAND copy per run (~800 MB). Five of them filled
  the volume to 100 %. Delete the log directory after reading the numbers.
