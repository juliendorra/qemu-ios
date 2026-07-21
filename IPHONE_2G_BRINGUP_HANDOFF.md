# iPhone 2G (M68AP) bring-up — session handoff

This is a working log for booting **iPhone OS 1.x** on the `-M iPhone-2G`
machine. It records the path taken, what is proven, the dead ends, and the
next concrete steps, so the next person (or LLM) can continue without redoing
the investigation. Read `IPHONE_2G.md` first for the board design; this file is
the live bring-up state.

## Goal

Boot a real iPhone OS 1.x image to SpringBoard on `-M iPhone-2G`, reusing the
S5L8900 emulation that already boots the iPod Touch 1G (`-M iPod-Touch`).

## Current state (one line)

The iPhone-2G machine is merged onto the Wi-Fi/HTTPS line (one QEMU 11 binary
registers both machines with the full MV8686/DNS/HTTPS stack). SYSIC epoch,
watchdog, M68AP extraction, and synthetic NOR are landed and verified. **The
`no signature or no production format` blocker is SOLVED**: `scripts/build-m68ap-nand.py`
emits a NAND whose FIL signature (`0x43303033`) M68AP iBoot accepts, and the
production BBT lets VFL_Open discover the context. The current failure has moved
one gate deeper: VFL_Open prefetch-aborts while interpreting the VFL context
body (the it1g body layout needs M68AP production field values). WMR init is not
yet fully green (no `VFL_Open [OK]`/`FTL_Open [OK]`).

## Session log — 2026-07-21 (NAND signature SOLVED; constructor + tests landed)

Root-caused and eliminated the `no signature or no production format` blocker:

1. **The blocker was ONE 4-byte constant.** M68AP iBoot-204.3.14 `WMR_Init`
   (Thumb @ VA 0x180164a0) reads `bank0/page0` word0 and compares it to the FIL
   "AND driver" signature **`0x43303033` ("300C")**. N45AP iBoot compares to
   **`0x43303032` ("200C")** (constants at file 0x165b0 / 0x15eb0; the whole
   Whimory section is shifted +0x700 between the two builds but the logic is
   byte-identical). The observed `read only version (1, 0)` meant version=1 (OK)
   but signature-flag=0. There is NO `NANDDRIVERSIGN` string in this iBoot —
   M68AP uses the simple FIL-id-at-page-0 scheme like it1g, NOT the N72AP
   `0x43313131` signature-page scheme.
2. **`scripts/build-m68ap-nand.py`** (new): faithful Python reimplementation of
   the it1g Whimory metadata structures (S5L8900 geometry: 8 banks, 2048+64,
   128 pages/block), parameterised on the signature. Verified it reproduces the
   installed N45AP metadata **byte-for-byte** (all 37 metadata pages; the three
   handoff fingerprints match) when run with `--signature n45ap --bbt zero`.
   With `--signature m68ap` it flips only `bank0/0.page` word0 to `0x43303033`
   and 0xFF-fills the BBT. Emits a JSON provenance sidecar. HFS payload optional
   (not needed for WMR init).
3. **Production BBT is required for M68AP.** With the it1g zero-filled BBT,
   VFL_Open's context scan (`_LoadVFLCxt`) finds nothing and fails at line 768
   (`fail bank 0`). The final N72AP generator 0xFF-fills the BBT ("all blocks
   good"); doing the same lets M68AP's Whimory2_1 VFL_Init build a searchable
   bitmap and VFL_Open then discovers the context. N45AP iBoot does not need
   this (accepts zero-fill) — so it is an M68AP-specific production choice.
4. **Milestone reached, verified by `scripts/iphone-nand-acceptance.py`** (new,
   board-aware, staged copies, hard Python watchdog timeout, machine-readable
   JSON, runs the N45AP iPod boot in the same batch): booting `-M iPhone-2G`
   with the real m68ap iBoot + synthetic m68ap NOR + generated m68ap NAND now
   prints `Apple NAND Driver (AND) 0x43303033`, `FIL/BUF/VFL/FTL_Init [OK]`, and
   **no** `no signature or no production format` / `read only version`. The
   N45AP regression still reaches `Darwin Kernel Version`.
5. **Structural tests**: `scripts/test-build-m68ap-nand.py` validates the
   generated metadata by bytes/structure (N45AP fingerprints, M68AP signature
   word, production BBT fill, VFL spare `[8]=0`/`[9]=0x80`, awInfoBlk@0x7A2,
   geometry) with no Apple payloads in the repo.

**Next failure (precise, re-diagnosed):** with signature + production BBT,
VFL_Open discovers the context then takes a **Prefetch Abort** (IFAR
`0x18017cb4`, IFSR 0x8). A full disassembly diff overturned the "production VFL
body" hypothesis:

- **The entire Whimory VFL/FTL code is byte-identical between the N45AP and
  M68AP iBoot-204 builds** (M68AP `0x14900-0x18000` vs N45AP shifted −0x700);
  every difference is relocation noise (BL/BLX offset high bytes, relocated
  literal-pool pointers, device strings). There is **no new field check, no new
  constant** in the validator (`0x18016120`), VFL_Open (`0x18016194`), the
  checksum funcs (`0x18015810`/`0x180157e0`), FTL_Open (`0x18015068`), or
  WMR_Init (`0x180164a0`). So the two builds require *identical* context bytes,
  and the it1g body is NOT the problem.
- `0x18017cb4` is **inside the ARM-mode `memcpy` at `0x18017bac`** (its
  byte-copy tail), not a code target. Every memcpy in this path is a **fixed**
  size (8 / 0x800 / 6). A fixed-size memcpy that aborts means a **bad source or
  destination pointer**, i.e. a NAND-geometry / buffer issue, not a rejected
  page. The two indirect calls in `_LoadVFLCxt` dispatch through a **statically
  initialised** vtable (`[0x18025570]` set at `0x18015a54` to
  `{0x18015864, 0x18015810}`), so "call through garbage" is ruled out.
- **VFL context layout confirmed (it1g, NOT it2g):** spare `[8]==0` /
  `[9]==0x80`, `dwCxtAge` at spare `[0..3]`; data `awInfoBlk[4]` at **0x7A2**
  (literal cited at both iBoots). Optional production trailer (verified present
  in the checksum code, but N45AP accepts zeros so it is not what blocks us):
  `dwVersion`@0x7F4, `dwCheckSum`@0x7F8 `= Σ words[0..509] + 0xAABBCCDD`,
  `dwXorSum`@0x7FC `= ⊕ words[0..509] ^ 0xAABBCCDD` (510 LE u32 over bytes
  0x000–0x7F7; const at `0x1801580c`).

**Runtime diagnosis (2026-07-21, corrected after gdbstub + `-icount` work).**
The earlier "garbage-length memcpy" reading was WRONG. Findings that hold:

- **It is a Prefetch Abort where the *fetch* of `0x18017cb4` external-aborts**
  (IFSR=0x8, IFAR=0x18017cb4). `0x18017cb4` is a valid instruction inside the
  ARM `memmove` at `0x18017bac` (readable via `xp`), yet fetching it faults.
  The CPU then loops taking the abort (even fetching vector `0xc` re-aborts).
- **Execution JUMPS into the middle of `memmove`, it does not call it.** A
  hardware breakpoint at the `memmove` entry `0x18017bac` never fires before the
  fault; and a huge count with `|dst-src|=0x3620 < count` would route to the
  backward path `0x18017d0c`, not the forward byte loop at `0x18017cb4`. So
  `r0=0x180fc9e0/r1=0x18100000/r2=0xffe6121d` are **leftover garbage**, not real
  memmove args — this is a **bad indirect branch / corrupted code pointer** to
  `~0x18017cb4`, not a copy with a bad length.
- **Deterministic repro:** `-icount shift=3` reproduces it exactly (reaches
  `FTL_Init [OK]`, then aborts) — use this for any future single-stepping so the
  timer does not distort timing.
- **Timing-sensitive.** Without `-icount`, gdb single-stepping makes
  `QEMU_CLOCK_VIRTUAL` fly (it tracks wall time), so the fault moves *earlier*
  (before `FTL_Init`) — an artifact, not the real path. The real (fast / icount)
  fault is right after `FTL_Init [OK]`, in the WMR/VFL path (`r4=0x18025530` =
  geometry struct at the abort).
- iBoot code is **byte-identical** to N45AP (true section delta **0x704**), which
  boots on the same `-M iPhone-2G` machine. NAND/ECC/FMI emulation is not
  board-conditional and behaves correctly (FMCSTAT@0x48 returns ready; the FIL
  wait `0x18016850` polls `0x38a00048` bit1 and succeeds). BBT fill size is not
  the cause (512-byte vs full-page fill: identical fault). So this is **not a
  `build-m68ap-nand.py` fix** — it is a corrupted-code-pointer bug driven by
  M68AP-build runtime data / the NOR device tree / interrupt timing.

**Tooling notes for next time (learned the hard way):** iBoot at `0x18000000`
is a read-only region, so gdb **software** breakpoints (`Z0`) silently fail
there — use **hardware** breakpoints (`Z1`). `-icount` + `-S -gdb` did not boot
cleanly in a hand-rolled RSP client; try a real cross-`gdb`/`lldb`. **Next
step:** with `-icount shift=3` + hardware breakpoints, single-step from
`0x18016508` (post-`FTL_Init` print) to the branch that targets `0x18017cb4`;
the instruction before the jump (a `bx`/`blx`/`pop {pc}` through a corrupted
value) is the culprit. Alternatively add temporary QEMU instrumentation in the
prefetch-abort path (`arm_cpu_do_interrupt`) to log the pre-abort PC/LR. Also
run the isolation boot: M68AP iBoot + **n45ap NOR** to test the device-tree
variable. Key VAs: `memmove 0x18017bac` (fault fetch `0x18017cb4`), WMR_Init
`0x180164a0`, VFL_Open `0x18016194`, geometry `[0x18025530]`.

## Firmware layout & parity (iPod ⇄ iPhone)

Firmware lives in the app bundle, one dir per board, same file names:

```
<App>/Contents/Resources/
  ipod_files/     bootrom_s5l8900  iboot_204_n45ap.bin  nor_n45ap.bin  nand/
  iphone_files/   bootrom_s5l8900  iboot_204_m68ap.bin  nor_m68ap.bin  nand/  firmware-provenance.json
```

This is the layout `scripts/install-ipod-app-engine.sh` already expects (its
`ipod-touch` profile → `ipod_files`, `iphone-2g` profile → `iphone_files`).
Populate `iphone_files/` reproducibly from a user-supplied IPSW:

```
python3 scripts/extract-m68ap-images.py <IPSW>/.../all_flash.m68ap.production OUT
python3 scripts/build-m68ap-nor.py  --template <n45ap NOR> --containers OUT/nor-containers --out OUT/nor_m68ap.bin
python3 scripts/build-m68ap-nand.py --out OUT/nand-m68ap --signature m68ap
python3 scripts/install-iphone-firmware.py --from OUT   # assembles + installs + re-signs
```

`scripts/iphone-nand-acceptance.py` defaults to these bundle dirs (no paths
needed), exactly as it uses `ipod_files/` for the N45AP regression. Apple-derived
firmware is never committed (AGENTS.md); it lives only in the bundle, like the
N45AP set. Each `iphone_files/` carries a `firmware-provenance.json` (hashes +
the NAND constructor manifest).

## Session log — 2026-07-21 (merge onto wifi line + step-1 fixes landed)

Work done on branch `ipod_touch_1g` (the wifi/HTTPS line), merging in
`iphone_2g`:

1. **Merge**: `iphone_2g` (machine type, baseband/ALS/Zephyr1 stubs, m68ap
   tooling) merged into the branch carrying MV8686 Wi-Fi + DNS + HTTP/HTTPS
   bridge work. Zero file overlap between the two lines — clean merge. One
   QEMU 11.0.2 binary (`build-ipod11/qemu-system-arm`) now registers both
   `iPod-Touch` and `iPhone-2G`, sharing the whole networking stack.
2. **Board-aware SYSIC epoch (landed)**: `POWER_ID` bits [31:24] now come from
   `IPodTouchSYSICState.power_epoch`, set at machine init: N45AP=2, M68AP=3.
   A new machine option `epoch=` overrides it (see 4).
3. **Watchdog reset semantics (landed, M68AP only)**: `WATCHDOG_MEM_BASE`
   is a real MMIO region on M68AP; writing 0x100000 requests a guest reset.
   N45AP keeps the historical inert-RAM backing (shipped working config).
   *Evidence*: booting n45ap iBoot on `-M iPhone-2G` (default epoch 3) now
   produces a clean panic→reset loop — 1223 QMP RESET events in 20 s — where
   it previously hung silently at `0x18001e3c` forever.
4. **`epoch=` machine option**: `-M iPhone-2G,epoch=2` boots cross-board
   firmware (n45ap images on the M68AP board). `scripts/iphone-smoke-test.py`
   uses it; without it the epoch/watchdog behavior of (3) is the expected
   result, which is itself the regression check for these fixes.
5. **Regressions run**:
   - `-M iPod-Touch` with the merged binary boots to SpringBoard
     (iBoot-204 → Darwin → FTL → AppleMRVL Wi-Fi → mDNSResponder →
     SpringBoard). N45AP unaffected.
   - `scripts/iphone-smoke-test.py` (now with `epoch=2`): all checks pass —
     both machines listed, Darwin boots, AppleISL29003 probes, Zephyr1-mode
     mismatch as expected, no panics.

**Dead-end recorded**: after the epoch fix, the smoke test's original
n45ap-on-M68AP boot broke *by design* (n45ap iBoot requires epoch 2). Do not
"fix" this by reverting the board default — the `epoch=` override exists for
exactly this synthetic combination.

## Session log — 2026-07-21 (real m68ap iBoot boots; NOR solved; NAND is the wall)

Booted the **real extracted m68ap iBoot** (`iboot_204_m68ap.bin`) on the
merged binary. With the epoch fix landed, no debugger override is needed:
iBoot reaches its banner (`BUILD_TAG: iBoot-204.3.14`), FTL, and NAND probe
automatically.

**Synthetic NOR — SOLVED.** With the N45AP NOR, iBoot logged 7×
`Ignoring image with mismatching security epoch` (the N45AP images are epoch
2; this iBoot wants 3). Fix:
- `scripts/extract-m68ap-images.py` now also emits the full decrypted IMG2
  *containers* (0x400 header + payload, epoch 3 intact) into
  `<out>/nor-containers/`, named by source stem.
- `scripts/build-m68ap-nor.py` rewrites the NOR image store (0x10400..) with
  those containers in N45AP order (dtre, batC, logo, nsrv, batl, batL, recm),
  0x40-aligned, preserving the N45AP SysCfg at 0xFC000.
- Result: booting m68ap iBoot with `nor_m68ap.bin` logs **0** epoch
  mismatches (was 7). All M68AP NOR images accepted, DeviceTree included.

**IMG2 epoch field pinned down**: header offset **+0xa** is `uint16
security_epoch` (N45AP=2, M68AP=3). Confirmed by diffing the two DeviceTree
headers. This is the same value the SYSIC `power_epoch` fix reports.

**Gotcha (recorded)**: the two NOR battery images use IMG2 4CCs that differ
only by case — `batl` (batterylow0) vs `batL` (batterylow1). Keying container
files by 4CC collides on a case-insensitive filesystem (macOS default: batL
silently overwrote batl, so both came out 0xedd2). The extractor now keys
container files by source stem instead.

**Remaining wall — the NAND format.** With the m68ap iBoot **and** the synthetic
m68ap NOR, the boot now fails at exactly one place — the N45AP NAND:
```
[FTL:MSG] FTL_Init            [OK]
[WMR:ERR] read only version (1, 0)
[WMR:ERR] no signature or no production format
NAND failed initialisation
... root filesystem mount failed ... Entering recovery mode
```
The Whimory low level initializes (FIL/BUF/VFL/FTL all `[OK]`) because it is
the same SoC/controller, but the higher WMR layer rejects the N45AP NAND's
signature/production format. So the ordered blocker list is now down to one
format-construction task:

### The NAND, precisely

- An IPSW does not contain raw physical NAND pages, but that does **not** mean
  there is no synthesis path. The original qemu-ios projects construct the
  physical page tree and metadata around an IPSW-derived HFS image.
- For N45AP, the public generator emits eight banks of 2048-byte data plus
  64-byte spare pages. It writes FIL `0x43303032`, identical synthetic BBTs,
  VFL contexts, FTL context/mapping pages, GPT, and the HFS payload. The
  upstream author's 2022 instructions explicitly say the released NAND is
  generated from the IPSW root filesystem.
- The bundled N45AP artifact has exact generator fingerprints. On a staged
  copy, the following SHA-256 values match freshly generated metadata pages:

  | Page | SHA-256 |
  |---|---|
  | `bank0/0.page` | `c5dacd1ade5322b1c36507be39e873a387414308cb64c2f9dac4eef26740c006` |
  | `bank0/4480.page` (also bank 1) | `5a0157e626602bea19d797571b245809694a28b4e7e9268b6d08df066c19ee67` |
  | `bank0..7/524160.page` | `6984b58fc2345586f86ab3d64d098a1ffdb6a214556af4574ee439aa22d9bfb0` |

  Therefore the earlier “real dump / not synthesized” claim was incorrect.
  This fingerprint does not prove the origin of every mutable filesystem page;
  keep provenance manifests for future artifacts.
- For N72AP, qemu-ios commit [`1300c08302`](https://github.com/devos50/qemu-ios/commit/1300c08302e6c5f5d26664ced2a9336e2c5947f9)
  temporarily patched iBoot/kernel FTL reads to a host block device. Commit
  [`5e9f53bfd8`](https://github.com/devos50/qemu-ios/commit/5e9f53bfd8ab3f2969138672daa3605eb7f406ef)
  removed that bypass, and the final port reads generated physical pages. Its generator adds a
  `NANDDRIVERSIGN` page, WMR/VFL production fields, mapping pages, BBT, GPT,
  and HFS data. This is the closest precedent for the M68AP rejection.
- `scripts/pack-ipod-nand.py` still only compacts an existing page tree. The
  missing repository component is a constructor equivalent to the public iPod
  generators, specialized for M68AP.

### Route decision

Use the iPod ports' final, proven design: generate an M68AP sparse physical
page tree from a user-supplied IPSW, then boot it through the existing NAND
controller model. A physical iPhone1,1 dump is an optional oracle, not a
dependency. Guest FTL-read patching is diagnostic-only because upstream
removed that transitional bypass before the final 2G solution.

Modeling S5L8900 DFU/USB far enough to run Apple's real restore is feasible in
principle, but it is a substantially larger fidelity project: USB EP0/DFU
state, iBSS/iBEC transfers, recovery protocol, ramdisk boot, host orchestration,
NAND erase/write/persistence, and `asr` behavior all have to work together. It
is not required to solve the current metadata blocker and is now a secondary
track after first boot.

Historical primary sources:

- [1G NAND generation walkthrough](https://devos50.github.io/blog/2022/ipod-touch-qemu-pt2/#manually-generating-the-nand-image)
- [`qemu-ios-generate-nand`](https://github.com/devos50/qemu-ios-generate-nand), including tags `it1g_nand_filesystem` and `it2g_nand_filesystem`
- [Final iPod Touch 2G runner](https://github.com/devos50/qemu-ios/blob/ipod_touch_2g/RUNNING.md)

---

## What is proven (advances)

### 1. Firmware source
`iPhone1,1_1.1.4_4A102_Restore.ipsw` (build 4A102) is still served by Apple's
CDN and is ~162 MB. Get the URL from the ipsw.me API:

```bash
curl -s "https://api.ipsw.me/v4/device/iPhone1,1?type=ipsw" | \
  python3 -c "import json,sys;[print(f['version'],f['url']) for f in json.load(sys.stdin)['firmwares']]"
```

Unzipping it yields (the pieces that matter):

| File | Role |
|---|---|
| `Firmware/all_flash/all_flash.m68ap.production/iBoot.m68ap.RELEASE.img2` | iBoot, 8900-wrapped |
| `.../LLB.m68ap.RELEASE.img2` | LLB, 8900-wrapped |
| `.../DeviceTree.m68ap.img2` | device tree, 8900-wrapped |
| `.../applelogo.img2`, `batterylow*`, `recoverymode.img2`, `needservice.img2` | boot logos / recovery UI |
| `kernelcache.release.s5l8900xrb` | kernel |
| `022-3894-4.dmg` (123 MB) | root filesystem |
| `022-3896-4.dmg`, `022-3900-4.dmg` (~18 MB each) | restore ramdisk(s) |

The 1.1.4 iBoot is **iBoot-204** — the *same* build the n45ap machine already
runs. (1.0/1A543a would be the museum-accurate target per the feasibility doc,
but 1.1.4 is the pragmatic first boot: same iBoot version, freely archived.)

### 2. Decryption is solved — no key hunt needed
S5L8900 is one SoC shared by iPhone 2G and iPod Touch 1G, so they share the GID
key ("AES key 0x837"). That key is **already hard-coded** in
`hw/arm/ipod_touch_8900_engine.h`:

```
188458A6D15034DFE386F23B61D43774
```

It decrypts the iPhone's 8900 images exactly as it does the iPod's. Container
shape: `0x800` 8900 header, then AES-128-CBC(key=GID, iv=0), then a `0x400`
IMG2 wrapper ("Img2" = `2gmI` LE; 4-char type at +4: `tobi`=iBoot, `llbz`=LLB,
`dtre`=DeviceTree), then the raw payload.

Tooling committed: **`scripts/extract-m68ap-images.py`** (extraction/conversion
only — no firmware committed, per the artifact policy in `AGENTS.md`). It also
retains complete decrypted IMG2 containers for the NOR builder. Run:

```bash
python3 scripts/extract-m68ap-images.py \
    <IPSW>/Firmware/all_flash/all_flash.m68ap.production  <out dir>
# -> iboot_204_m68ap.bin (0x22000)  LLB.m68ap.bin (0xb000)  DeviceTree.m68ap.bin (0x9000)
```

The extracted `iboot_204_m68ap.bin` is **0x22000 bytes with the identical ARM
reset vector** (`0e 00 00 ea …`) to `data/iboot_204_n45ap.bin` — same size, same
layout, different build. Sanity check passes: it contains the strings
`iBoot-204.3.14`, `:: iBoot, Copyright 2007, Apple Inc.`, etc.

### 3. It boots far enough to diagnose
Loading the m68ap iBoot with the **n45ap** NOR/NAND under `-M iPhone-2G`:

```bash
cd build && ./qemu-system-arm \
  -M iPhone-2G,bootrom=ipod_files/bootrom_s5l8900,iboot=ipod_files/iboot_204_m68ap.bin,nand=ipod_files/nand \
  -serial mon:stdio -cpu max -m 1G -pflash <writable copy of nor_n45ap.bin> -display none
```

iBoot executes at `0x18000000`, runs the standard reset handler (walks the CPU
through svc→irq→fiq→abt→und→svc setting per-mode stacks, PC `0x1800009c`..`d0`),
then **hangs with PC parked at `0x18001e3c`**. (The `qemu-system-arm` binary must
be rebuilt with `ninja` — a stale build will report "unsupported machine type"
for `iPhone-2G`.)

For comparison, the n45ap iBoot on the same command boots fully (iBoot banner +
`[FTL:MSG] Apple NAND Driver`, ~1500 lines of serial). So the harness is fine;
the m68ap iBoot is the difference.

### 4. The exact panic and post-fix path are proven

A GDB breakpoint on the panic entry at `0x18005420` captured:

```
r0 = 0x1801ac38  -> "miu_init"
r1 = 0x1801ac44  -> "miu_init: Epoch Mismatch\n"
lr = 0x180028c7
```

The caller at `0x180028b0` reads `SYSIC_MEM_BASE + 0x44` (`POWER_ID`), shifts
the result right by 24, and requires `3`. The current SYSIC model returns
`2 << 24` unconditionally and even retains a commented `3 << 24` value marked
"for older iboots" in `hw/arm/ipod_touch_sysic.c`.

Overriding only that guest register value to `0x03000000` produced serial:

```
SysCfg: version 0x00010001 with 4 entries using 200 of 8192 bytes
merlot_init() -- Universal code version 11-13-07
Project/Driver: M68/NSC-Merlot
:: BUILD_TAG: iBoot-204.3.14
[FTL:MSG] FTL_Init                    [OK]
[WMR:ERR] read only version (1, 0)
[WMR:ERR] no signature or no production format
NAND failed initialisation
Entering recovery mode, starting command prompt
```

This proves that the N45AP NOR was not the cause of the early panic. It also
separates the next failure cleanly: the NAND controller and FTL initialize,
then the M68AP iBoot rejects the N45AP NAND's WMR/production content.

---

## Root cause of the hang (diagnosed)

`0x18001e3c` is inside iBoot's **reboot/reset routine** at `0x18001e28`
(Thumb):

```
18001e28  push {r7,lr};  cmp r0,#0;  bne 1e34;  bl 0x18004a00   ; flush, only if r0==0
18001e34  ldr r3,[pc,#8] ; r3 = 0x3E300000  (WATCHDOG_MEM_BASE)
18001e36  movs r2,#0x80; lsls r2,#0xd  ; r2 = 0x100000
18001e3a  str r2,[r3]    ; poke the watchdog reset register
18001e3c  b .            ; spin, waiting for the SoC to reset   <-- PC parks here
```

`0x3E300000` is `WATCHDOG_MEM_BASE` (`include/hw/arm/ipod_touch.h:106`), which
the machine backs with **inert `allocate_ram`** (`hw/arm/ipod_touch.c:386`) — no
reset semantics. So the write does nothing and iBoot spins forever instead of
rebooting.

The caller is a **panic handler**: at `0x18005440` it loads the format string
`panic (%s): ` (literal at file offset `0x5454` → VA `0x1801b504`) and then
calls `reboot(1)`. So **iBoot panicked in early platform init** — before any
serial output appeared (the panic string never reached the UART; UART/putchar is
apparently not up yet on this path, or is routed differently).

**Why it panics:** `POWER_ID` at `SYSIC_MEM_BASE + 0x44` reports epoch 2, but
this M68AP iBoot requires epoch 3 during `miu_init()`. This is a pre-existing
hard-coded N45AP assumption in the shared SYSIC model, exposed by running the
older M68AP platform initialization path. The fix must be board-specific so the
working N45AP behavior remains epoch 2.

---

## NOR layout facts (for the rebuild)

`data/nor_n45ap.bin` is a raw 1 MiB CFI NOR (`hw/block/pflash_cfi02.c`, mapped in
`hw/arm/ipod_touch.c:398-412`). **QEMU does not parse it** — the guest reads it
directly. Layout:

- `0x00000`–`0x10000`: zero in the shipped image (LLB region; the emulator does
  **not** execute a NOR LLB — iBoot is pre-staged raw to `IBOOT_BASE=0x18000000`
  by the machine, see `hw/arm/ipod_touch.c:371-376`, reloaded on reset at
  `:175-180`).
- `0x10400`+: **IMG2 image store**, each entry a `2gmI` header (0x400 bytes) then
  data. In n45ap: `dtre`@0x10400 (len 0x7d28), `batC`@0x18940, `logo`@0x29680,
  `nsrv`@0x2bbc0, `batl`@0x30900, `batL`@0x3de40, `recm`@0x4d380. **No iBoot/LLB
  image is in this store.**
- `0xfc000`: **syscfg / nvram** — `SysCfg version 0x00010001, 4 entries`
  (`nvram`, `common` with `boot-args=…`, etc.).

The N45AP syscfg is accepted after the SYSIC epoch override, so syscfg is not
the early-boot blocker. N45AP IMG2 entries are logged as `Ignoring image with
mismatching security epoch`; `scripts/build-m68ap-nor.py` now replaces them
with retained M68AP containers, including the DeviceTree.

Do not reuse the N45AP IMG2 header around an M68AP body. The guest, not QEMU,
parses the NOR and checks header metadata including its security epoch. The
extractor and NOR builder now preserve that complete 0x400-byte header; keep
this as a regression invariant.

---

## Next plan (in order)

1. **Freeze the proven baselines.**
   - Keep the current M68AP iBoot + NOR trace as the negative fixture: it must
     reach `FTL_Init [OK]` and fail only at WMR production validation.
   - Add fixture tests for the public N45AP generator metadata and its logical
     page-to-bank/page mapping. Tests should validate bytes and structure, not
     require Apple payloads in the repository.
   - Treat the external generator as a format reference. Its repository has no
     explicit license in the checked history, so do not copy its source
     verbatim without resolving that; reimplement the documented structures
     and behavior with attribution.

2. **Implement `scripts/build-m68ap-nand.py`.**
   - Input: a user-supplied iPhone1,1 IPSW or extracted root HFS image plus a
     provenance manifest. Output: a new sparse `bank0..bank7/*.page` tree;
     never modify an installed or source NAND.
   - Start with the working S5L8900/N45AP geometry: eight banks, 2048 data + 64
     spare bytes, 128 pages per block, sparse erased pages, and the established
     virtual/logical-to-physical mapping.
   - Generate FIL, BBT, VFL contexts/copies, FTL context and mapping tables,
     valid data-page spares, GPT/partition pages, HFS payload placement, and
     the kernelcache location expected by M68AP iBoot.
   - Add the production-format ideas proven by the final N72AP generator:
     signature page, explicit VFL metadata version/vendor format, context ages
     and production markers. Determine M68AP's exact signature constant,
     WMR version and field offsets by tracing/disassembling iBoot-204.3.14;
     do not assume N72AP's `0x43313131` is identical.
   - Emit JSON containing source hashes, output geometry, populated pages,
     metadata versions, partition offsets, and constructor revision. Optionally
     run `scripts/pack-ipod-nand.py` only after the sparse tree validates.

3. **Add one scripted M68AP NAND acceptance case.**
   - Extend the existing board-aware boot harness rather than doing manual
     tap-by-tap testing. Always stage NAND and NOR copies.
   - Phase gates: iBoot banner; FIL/BUF/VFL/FTL success; no `WMR:ERR`; kernel
     banner; root mount; launchd; SpringBoard. Emit machine-readable status,
     serial offsets, and a screenshot on success or failure.
   - The first milestone is deliberately narrow: replace `no signature or no
     production format` with a successful WMR init. Then fix partition/
     kernelcache placement using the next observed failure.
   - Run the existing iPod acceptance test in the same regression batch.

4. **Firmware-specific bring-up** once iBoot/kernel run: the m68ap paths the
   branch already stubs go live — Zephyr1 multitouch, ISL29003 ALS, S-Gold2
   baseband (`hw/arm/ipod_touch_multitouch.c`, `_isl29003.c`, `_baseband.c`,
   gated on `board_id == BOARD_ID_M68AP` in `ipod_touch_machine_init`). Expect
   iterative unimplemented-register fixes from the `-d unimp` log. See the
   "Path to a full iPhone OS 1 boot" section of `IPHONE_2G.md`.

5. **Restore fidelity later, independently.** Once generated NAND boots,
   improve erase/write/persistence and only then evaluate real DFU/restore as
   an end-to-end validation path. Do not block first boot on USB restore.

---

## Dead ends / gotchas (don't repeat these)

- **No offline key hunt needed.** Do not go looking up per-image AES keys on the
  iPhone Wiki — the GID key in `ipod_touch_8900_engine.h` already decrypts
  everything for this SoC. (`theiphonewiki.com/wiki/Firmware_Keys/1.1.4_(iPhone)`
  returns 404 anyway.)
- **The IMG2 header is 0x400, not 0x800.** After 8900 decryption the payload is
  an IMG2 container; strip `0x400` to reach raw ARM. (0x22400 decrypted − 0x400
  = 0x22000 = the n45ap iBoot size, which is the confirmation.)
- **Rebuild before testing.** The checked-in `build/qemu-system-arm` can be stale
  and silently lack `-M iPhone-2G`; run `ninja` first and confirm with
  `./qemu-system-arm -M help | grep -i iphone`.
- **A silent hang is a panic, not a QEMU stall.** Zero serial output does not
  mean "nothing ran" — here iBoot ran, panicked, and is spinning in the reboot
  routine. Always sample PC via the monitor (`info registers`, R15) before
  concluding it hung; the watchdog-write-then-spin at `0x18001e3c` is the tell.
- **The N45AP NOR is not the early-panic cause.** The confirmed cause is SYSIC
  epoch 2 versus the M68AP-required epoch 3. With that read overridden, the
  same NOR reaches the banner and recovery prompt.
- **Do not reuse an N45AP IMG2 header for M68AP data.** iBoot checks its
  security epoch and ignores the N45AP entries. Preserve the decrypted M68AP
  header/container in the extraction pipeline.
- **`pack-ipod-nand.py` does not create a NAND.** It only compacts an existing
  `bank0..bank7/*.page` tree. Use the planned M68AP constructor; do not confuse
  packing with construction.
- **Do not repeat the “device dump only” conclusion.** The 1G generator and
  current bundled metadata hashes disprove it, while the final 2G port proves
  a production-format sparse tree can also be generated.
- **Do not revive the 2G FTL bypass as the product path.** It was a temporary
  bring-up hack removed by `5e9f53bfd8`; physical-page generation is the
  durable design.
- **Bound every boot test with a hard timeout** (an untimed boot wait once wedged
  a session for two hours — see the note in `IPHONE_2G.md`).

## Artifacts

- `scripts/extract-m68ap-images.py` — committed, reproducible decryptor.
- `scripts/build-m68ap-nor.py` — synthetic M68AP NOR builder.
- `scripts/build-m68ap-nand.py` — M68AP NAND constructor (signature `0x43303033`,
  production BBT, it1g Whimory metadata; reproduces N45AP metadata byte-for-byte
  with `--signature n45ap --bbt zero`). Emits a JSON provenance sidecar.
- `scripts/test-build-m68ap-nand.py` — structural fixture tests (no Apple
  payloads): N45AP fingerprints, M68AP signature/BBT, VFL spare, geometry.
- `scripts/iphone-nand-acceptance.py` — board-aware M68AP NAND boot acceptance
  (staged copies, hard timeout, JSON phase gates) + N45AP regression in the same
  batch.
- `scripts/iphone-smoke-test.py` — N45AP-firmware board-divergence regression;
  it is not a real M68AP kernel test.
- The IPSW, decrypted images, generated M68AP NAND, and any physical comparison
  dump stay uncommitted. The extractor and NOR builder are complete; the next
  repository artifact is the constructor plus structural/boot tests.
- Every generated NAND must carry a sidecar provenance manifest with IPSW
  device/build and hash, extracted HFS/kernelcache hashes, constructor commit,
  output geometry, and declared guest-file modifications.
- The upstream projects and long-lived public releases are strong technical
  precedent. They are not a blanket legal determination; repository policy is
  to accept user-supplied inputs and not distribute Apple payloads or
  device-unique data.
