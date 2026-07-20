# iPhone 2G (M68AP) bring-up — session handoff

This is a working log for booting **iPhone OS 1.x** on the `-M iPhone-2G`
machine. It records the path taken, what is proven, the dead ends, and the
next concrete steps, so the next person (or LLM) can continue without redoing
the investigation. Read `IPHONE_2G.md` and `IPHONE_2G_OS_1_FEASIBILITY.md`
first for the board design; this file is the live bring-up state.

## Goal

Boot a real iPhone OS 1.x image to SpringBoard on `-M iPhone-2G`, reusing the
S5L8900 emulation that already boots the iPod Touch 1G (`-M iPod-Touch`).

## Current state (one line)

The iPhone-2G machine is merged onto the Wi-Fi/HTTPS line (one QEMU 11 binary
registers both machines with the full MV8686/DNS/HTTPS stack), the SYSIC epoch
and watchdog fixes from "Next plan" step 1 are landed and verified in code, and
the remaining blockers are artifacts: extract the m68ap images from a
user-supplied IPSW, build a synthetic `nor_m68ap.bin`, and (the hard one)
construct an M68AP NAND.

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
only — no firmware or extra keys committed, per the artifact policy in
`IPHONE_2G_OS_1_FEASIBILITY.md`). Run:

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
the early-boot blocker. The N45AP IMG2 entries are nevertheless logged as
`Ignoring image with mismatching security epoch`; a real kernel boot therefore
still needs the M68AP DeviceTree image.

Do not reuse the N45AP IMG2 header around an M68AP body. The guest, not QEMU,
parses the NOR and checks header metadata including its security epoch. The
extractor currently discards the decrypted 0x400-byte M68AP IMG2 headers, so it
must be extended to retain complete decrypted containers before a reliable
synthetic NOR can be built.

---

## Next plan (in order)

1. **Make SYSIC `POWER_ID` board-aware (unblocks iBoot normally).**
   - Return epoch 3 for `BOARD_ID_M68AP` and retain epoch 2 for N45AP.
   - Add a regression test that reaches the M68AP iBoot banner without a
     debugger override and preserves the iPod boot.
   - Give `WATCHDOG_MEM_BASE` real reset semantics so future panics reboot
     instead of silently spinning at `0x18001e3c`.

2. **Retain complete M68AP IMG2 containers and build a synthetic NOR.**
   - Extend `scripts/extract-m68ap-images.py` to emit both raw payloads and the
     decrypted IMG2 container/header needed for NOR construction.
   - Build `nor_m68ap.bin` with the authentic M68AP `dtre` header/body and
     correct alignment. The N45AP syscfg can remain for the first experiment
     because iBoot already accepts it.
   - The direct-iBoot path does not require LLB in NOR.

3. **Supply or construct an M68AP NAND (unblocks the kernel).**
   - A lawful physical NAND dump is the shortest route.
   - Otherwise implement conversion from the IPSW root filesystem and
     kernelcache into the physical eight-bank layout, including 2048-byte data,
     64-byte spare, VFL/FTL/WMR metadata, and production signatures/format.
   - `scripts/pack-ipod-nand.py` is only a packer for an existing page tree; no
     constructor equivalent to the claimed "same approach used for the iPod"
     exists in this repository.

4. **Firmware-specific bring-up** once iBoot/kernel run: the m68ap paths the
   branch already stubs go live — Zephyr1 multitouch, ISL29003 ALS, S-Gold2
   baseband (`hw/arm/ipod_touch_multitouch.c`, `_isl29003.c`, `_baseband.c`,
   gated on `board_id == BOARD_ID_M68AP` in `ipod_touch_machine_init`). Expect
   iterative unimplemented-register fixes from the `-d unimp` log. See the
   "Path to a full iPhone OS 1 boot" section of `IPHONE_2G.md`.

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
  `bank0..bank7/*.page` tree; producing VFL/FTL/WMR metadata from a DMG remains
  unsolved.
- **Bound every boot test with a hard timeout** (an untimed boot wait once wedged
  a session for two hours — see the note in `IPHONE_2G.md`).

## Artifacts

- `scripts/extract-m68ap-images.py` — committed, reproducible decryptor.
- `scripts/iphone-smoke-test.py` — N45AP-firmware board-divergence regression;
  it is not a real M68AP kernel test.
- The IPSW, decrypted images, and the m68ap NAND must be regenerated from a
  user-supplied IPSW/device dump (not committed, per policy). The extractor
  reproduces `iboot_204_m68ap.bin` byte-for-byte, but must still retain wrapped
  IMG2 outputs for NOR construction.
