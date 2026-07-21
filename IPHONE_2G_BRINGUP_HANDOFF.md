# iPhone 2G (M68AP) bring-up — session handoff

This is a working log for booting **iPhone OS 1.x** on the `-M iPhone-2G`
machine. It records the path taken, what is proven, the dead ends, and the
next concrete steps, so the next person (or LLM) can continue without redoing
the investigation. Read `IPHONE_2G.md` first for the board design; this file is
the live bring-up state.

---

## ► NEXT-SESSION PROMPT (start here)

> **THE M68AP DARWIN KERNEL NOW BOOTS.** With three fixes landed (below), m68ap
> iBoot loads the device tree, hands off, and the real iPhone-2G kernelcache
> runs: `Darwin Kernel Version 9.0.0d1 ... RELEASE_ARM_S5L8900XRB`, the platform
> expert matches as **M68AP**, and IOKit registers cpu0 / vram@F400000 /
> arm-io@3C000000 / buttons / dock / charger / FairPlay. (~5400 serial lines.)
>
> **The next wall is the kernel's own NAND FTL.** After IOKit comes up, the
> `AppleNANDFTL` kext's `_FTLRestore` scans the generated NAND, finds no valid
> free-block pool (`_ScanForFreeBlk(0xF35) failed`, `wDataBlkCnt=0xF20
> wFreeBlkCnt=0x15`, thousands of `found block (#N) with unidentified spare`),
> so `FTL_Open failed`, `AppleNANDFTL::start ... failed`, and the kernel prints
> `Still waiting for root device`. **This is a NAND-generation fidelity task,
> not a hack:** the kernel FTL is stricter than iBoot's (iBoot's own `FTL_Open`
> succeeds on this same NAND). Our tree has only metadata + a minimal 16 MB HFS
> payload; every other block is bare-erased with empty spare, which the kernel
> FTL rejects. Sub-tasks:
> 1. Extend `scripts/build-m68ap-nand.py` to populate a proper FTL free-block
>    pool and per-block spare across all banks so `_FTLRestore` passes — match
>    the mature it1g/it2g generator's FTL context/free-list, not just the WMR
>    metadata pages. This is the same class as the earlier BBT/VFL work.
> 2. Then place the real 1.1.4 root HFS+ (`022-3894-4.dmg`) so `rd=disk0s1`
>    mounts and boot proceeds to launchd/SpringBoard. Still blocked offline: the
>    dmg is `encrcdsa`/vfdecrypt-encrypted (GID key does not open it).
>
> **Verify with** `scripts/iphone-nand-acceptance.py` (its `kernel` JSON gate is
> now reachable). **Use `--icount-shift -1` (real time)** so the kernel gets
> enough wall-clock — the default icount path parks in iBoot's UART loop just
> before the banner. The boot needs the patched iBoot (see fix 2) and the
> validator-normalised NOR (fix 1).
>
> **The three fixes that got from "failed to load device tree" to a live kernel
> (all landed this session):**
> 1. **NOR IMG2 validator** — `build-m68ap-nor.py promote_loadable` normalises
>    each NOR image's flags2 (`+0x1c`) to bit 24 SET / bit 30 CLEAR and
>    recomputes the `+0x64` CRC, matching iBoot's RAM-normalised header. Faithful
>    format fix. (Committed earlier: `e44f44cad6`.)
> 2. **Secure-boot bypass** — `scripts/patch-m68ap-iboot.py`: one 2-byte Thumb
>    edit at the unsigned-image decider (VA `0x18005984`, file `0x5990`
>    `00 20`→`01 20`). This RELEASE iBoot strictly enforces secure boot and our
>    synthetic images are unsigned; this is the standard "pwnage"-equivalent
>    every legacy-iOS emulator relies on. Apply to a STAGED iBoot copy; never
>    commit patched firmware.
> 3. **UART CTS** — `hw/char/exynos4210_uart.c` UMSTAT now reports CTS asserted
>    (bit 0). Without it, m68ap iBoot's flow-controlled baseband write on UART1
>    (`0x3cc0401c`) spins forever right after `gBootArgs`. A genuine UART-model
>    completeness fix; N45AP never polls UMSTAT so it is unaffected.
>
> When M68AP finally boots through SpringBoard, create `iPhone 2G.app` from the
> app scaffolding and the board-aware `s5l8900-profile=iphone-2g` launcher.
>
> ---
>
> **(Historical, now solved) The device-tree secure-boot gate.** The full path
> is reverse-engineered in the 2026-07-21 "device-tree load fully
> reverse-engineered" session log below. Where it stood before the iBoot patch:
> `dt_load` (`0x1800d060`) found the `dtre` descriptor and called `image_load`
> (`0x18008340`→`0x180088cc`); the IMG2 validator (`0x18008478`) was cleared by
> fix 1; the last gate was `image_load` (`0x180089b0`) taking the unsigned path
> to decider `0x18005984`, which requires security-config `0x18022fa0` bit 4 —
> never set in this RELEASE build — hence the secure-boot patch (fix 2). A more
> faithful alternative to fix 2 is to reconstruct Apple's img2 GID signatures so
> the bit-1 signed path passes; larger and separately licensable.

**The former blocker (solved 2026-07-21, kept for the record):** after
`FTL_Init [OK]`, iBoot Data-Aborted inside `memmove` (`DFAR=0x18100000`,
count `r2=0xffe6121d`). Root cause: the constructor's full-page 0xFF
"production BBT" fill corrupted the `DEVICEINFOBBT` page's own length field.
The loader at `0x18015fa0` does `memcmp(page, "DEVICEINFOBBT", 0x10)` then
`memmove(dst, page+0x38, *(u32 *)(page+0x34))` — with the page 0xFF-filled
past the marker, the count was 0xFFFFFFFF and the copy ran off the iBoot RAM
window. Fix (landed): `build-m68ap-nand.py` writes count `0x200` at +0x34
(4096 blocks/bank ÷ 8) and 0xFF only across the 0x200-byte bitmap at +0x38,
zeros elsewhere — the same shape as the N45AP page (whose count is 0).

## Goal

Boot a real iPhone OS 1.x image to SpringBoard on `-M iPhone-2G`, reusing the
S5L8900 emulation that already boots the iPod Touch 1G (`-M iPod-Touch`).

## Current state (one line)

The iPhone-2G machine is merged onto the Wi-Fi/HTTPS line (one QEMU 11 binary
registers both machines). SYSIC epoch, watchdog, M68AP extraction, synthetic
NOR, NAND signature/production BBT/DEVICEINFOBBT fix, **and now the NAND
filesystem payload** are all landed and verified. With a kernelcache-carrying
HFS+ boot partition, m68ap iBoot mounts HFS+, loads/decrypts/decompresses the
kernelcache. **The M68AP Darwin kernel now boots** (three fixes: NOR IMG2
validator normalisation, an iBoot secure-boot bypass patch, and a UART CTS
fix) — `Darwin Kernel Version ... RELEASE_ARM_S5L8900XRB`, platform expert
matches M68AP, IOKit registers. The wall is now the kernel's `AppleNANDFTL`
`_FTLRestore` (no free-block pool in the generated NAND) → `Still waiting for
root device`. Both machines still reach the Darwin kernel in the acceptance
batch (no regression from the shared UART change).

## Session log — 2026-07-21 (M68AP DARWIN KERNEL BOOTS; DT + secure boot + UART CTS solved)

Cleared the device-tree wall and everything through the kernel handoff. Three
fixes, in the order the boot hits them:

1. **NOR IMG2 validator** (`build-m68ap-nor.py promote_loadable`, faithful):
   normalise each NOR image's flags2 (`+0x1c`) to bit 24 set / bit 30 clear and
   recompute the `+0x64` CRC to match iBoot's RAM-normalised header. Detail in
   the DT-reverse-engineering log below.
2. **Secure-boot bypass** (`scripts/patch-m68ap-iboot.py`, standard "pwnage"
   equivalent): this RELEASE iBoot-204.3.14 never sets security-config
   `0x18022fa0` bit 4 (seeded 0x002c0000 at `0x18005a28`), so it strictly
   rejects unsigned images and refuses to LOAD the (found, validated) `dtre`.
   One 2-byte Thumb edit at the unsigned-image decider (VA `0x18005984`, file
   `0x5990`: `movs r0,#0` → `movs r0,#1`) makes it accept unsigned images.
   Applied to a staged iBoot copy; patched firmware is never committed. With
   it, iBoot loads the DT and reaches `gBootArgs.commandLine = [...]`.
3. **UART CTS** (`hw/char/exynos4210_uart.c`, faithful): after `gBootArgs`,
   m68ap iBoot does a flow-controlled write to the **baseband UART1** and spins
   in `uart_write` (VA `0x18003c9e`) polling UMSTAT (`0x3cc0401c` = UART1+0x1c)
   for CTS. The exynos UART model returned UMSTAT=0 (CTS clear) → infinite
   spin. Report CTS asserted (bit 0) — the correct default for an emulated UART
   with no modem. N45AP never polls UMSTAT, so it is unaffected.

**Result (real time, `--icount-shift -1`):**
`Darwin Kernel Version 9.0.0d1: ... xnu-933.0.0.211 RELEASE_ARM_S5L8900XRB`,
then `config(...): starting on M68AP`, `AppleARMPE::start(M68AP)`, IOKit
registers `cpu0` / `vram@F400000` / `arm-io@3C000000` / `buttons` / `dock` /
`charger` / FairPlay. ~5400 serial lines. Acceptance batch: **m68ap PASS
(deepest=kernel), n45ap PASS (deepest=kernel)** — no regression. Note: run in
REAL TIME; under `-icount shift=3` the kernel does not get enough wall-clock and
the run parks in iBoot's UART loop before the banner.

**Next wall (kernel NAND FTL):** `AppleNANDFTL::_FTLRestore` rejects the
generated NAND (`_ScanForFreeBlk(0xF35) failed`, `wFreeBlkCnt=0x15`, many
`unidentified spare`) → `FTL_Open failed` → `Still waiting for root device`.
The kernel FTL is stricter than iBoot's; the generated tree needs a real
free-block pool + per-block spare (see the NEXT-SESSION prompt).

**Dead ends / techniques this session (don't repeat these):**
- **The "make the emulator report development mode to allow unsigned images"
  idea is a DEAD END — do not re-chase it.** iBoot DOES read a hardware
  security register (`security_init` at `0x18005a28` calls `0x180018e4`, which
  returns bit 4 of CHIPID `0x3e500004` — modeled by `hw/arm/ipod_touch_chipid.c`,
  offset 0x4 returns `CHIP_REVISION<<24`), so a CHIPID/fuse lever *seemed*
  plausible (like the SYSIC epoch fix). But tracing it shows the security-config
  word `0x18022fa0` is seeded to `0x002c0000` and NO code path in this RELEASE
  build ever sets its bit 4 (the allow-unsigned bit the decider `0x18005984`
  checks); the CHIPID bit only influences other config bits (bit 5, etc.). There
  is no hardware lever to accept unsigned images. Hence the iBoot patch (or,
  faithfully, reconstructing the img2 GID signatures) is required — not a CHIPID
  tweak.
- **Locating the UART1 spin:** the boot went silent after `gBootArgs` with no
  serial error. Sampling the parked CPU via the monitor (`info registers` → R15)
  gave PC `0x18003c9e`; disassembling around it showed `uart_write`'s TX/CTS
  poll of `[r4+0x1c]`, and R02 held the polled MMIO address `0x3cc0401c`.
  Decoding that against `include/hw/arm/ipod_touch.h` (`UART1_MEM_BASE
  0x3cc04000`) identified UART1+0x1c = UMSTAT. Always sample the parked PC + the
  MMIO address in registers before assuming a hang; the fix followed directly.
- **Each image-load fix only advanced the failure one gate** (validator CRC →
  validator bit-24 → secure-boot decider → post-`gBootArgs` UART), and every
  `image_load` rejection prints the SAME `failed to load device tree`, so serial
  alone never localised it — register-level lldb bisection at each gate was the
  decisive technique (see the DT-reverse-engineering log's lldb recipe).

## Session log — 2026-07-21 (device-tree load fully reverse-engineered; secure boot is the last gate)

Followed the `load_macho_image: failed to load device tree` wall all the way
down with static disassembly + **live lldb probing** (QEMU `-S -gdb tcp::…`,
hardware breakpoints; only `lldb` is on this host, it drives QEMU's gdbstub).
Each probe advanced the failure deeper, converging on a single remaining gate.

**Full device-tree load chain (all VAs, iBoot-204.3.14 @ base 0x18000000):**
1. `load_macho_image` (`0x1800d544`) loads/validates the kernelcache, then calls
   `dt_load` (`0x1800d060`) at `0x1800e07a`; on `dt_load < 0` it prints
   "failed to load device tree" (`0x1800d676`) and returns −7.
2. `dt_load` (`0x1800d060`): `image_find_by_type('dtre'=0x64747265)`
   (`0x18008376`→walks the image list at head `0x180211a8`, matching
   `descriptor[+8]`); size-check `[desc+4] ≤ 0x100000`; `image_load`
   (`0x18008340`) to dest `0x0bf00000` (globals `0x18023c20`/`0x18023c24`).
   **lltb probe: find returns 0x1802bd48 (FOUND); image_load returns −1.**
3. `image_load` (`0x18008340`→worker `0x180088cc`): checks descriptor magic
   `[desc+0xc]==0x22f5ef0e` (probe: OK), runs the IMG2 validator `0x18008478`,
   then a secure-boot/copy tail.

**IMG2 validator `0x18008478` (was the first real blocker; now cleared):**
iBoot builds a *normalised RAM copy* of the NOR IMG2 header (probe: at
`0x1802b918`) and validates THAT, not the NOR bytes. The validator, on the load
path (arg r3=0), requires: magic `"Img2"`; `crc32(header[0:0x64]) == [hdr+0x64]`
(standard zlib CRC, `0x18007780`); flags2 (`+0x1c`) **bit 24 set**
(`0x180084aa: lsls #7; bpl reject`); epoch (`+0xa`) `== 3`. Two facts nailed by
lldb: iBoot's RAM copy **clears flags2 bit 30** (NOR `0x41000000` → RAM
`0x01000000`) but **copies `+0x64` verbatim** from NOR, so the CRC must be
computed over the bit-30-cleared header. Fix (landed, `build-m68ap-nor.py`
`promote_loadable`): set `+0x1c = (flags2 & ~0x40000000) | 0x01000000` and
recompute `+0x64`. After the fix the validator PASSES (probe: reaches
`0x180084b0` and `0x1800852a`, not the `0x18008596` fail block; RAM `+0x64`
= `0x5f2a73a2` now matches the recomputed CRC).

**Remaining gate — secure boot (`image_load` tail `0x180089aa`+):** with the
validator passing, `image_load` then checks flags2 **bit 1** (`0x180089b0:
lsls #0x1e; bmi`) to choose the signed-hash path vs the unsigned path. Our
`dtre` is unsigned (bit 1 clear; zero hash at IMG2 `+0x3e0`), so it takes the
unsigned path to the decider `0x18005984(1)`, which returns "allowed" ONLY if
**bit 4 (0x10) of the security config word `0x18022fa0`** is set. It is not, so
`image_load` returns −1. The generator-made N45AP `dtre` that loads carries a
real hash at `+0x20`/`+0x3e0` (bit 1 set, signed path); the authentic IPSW
M68AP images do not. **This is a secure-boot-policy problem, not a NAND or IMG2
formatting one** — see the next-session prompt for the three ways forward.

**lldb probe recipe (reusable):** boot with
`-S -gdb tcp::PORT -icount shift=3 -serial file:… -monitor none`; then
`lldb --batch -o "gdb-remote PORT" -o "breakpoint set --hardware --address 0xADDR" -o "process continue" -o "register read …"`.
Wrap in a host `( sleep 90; pkill -9 lldb qemu-system-arm )` watchdog. Key
observation points: `0x1800d0a6` (dt_load post-load r0: ≥0 ⇒ DT loaded),
`0x1800891e` (validator entry; r0 = IMG2 ptr to dump), `0x18008596`
(validator fail), `0x18008a60`/`0x18008a64` (worker fail exits).

**Dead ends / notes:** (1) Setting only bit 24 (keeping bit 30) made the
validator fail the CRC check — the RAM-copy normalisation clears bit 30, so the
CRC must be over the cleared value; must clear bit 30 too. (2) The serial
message "failed to load device tree" is identical for EVERY `image_load`
failure mode, so it cannot localise the fault — register-level lldb was required.
(3) `image_load` runs for multiple NOR images, so breakpoints inside it fire for
non-`dtre` calls; use `0x1800d0a6` (inside `dt_load`, dtre-only) for a
dtre-specific verdict.

## Session log — 2026-07-21 (NAND payload works; kernelcache loads; DT is the wall)

Gave the generated NAND a real filesystem payload and cleared the
`Not HFS+ (signature 0x0000)` wall, advancing the boot through five new stages.

**What was done**
- `scripts/build-m68ap-hfs-payload.sh` (new): builds a minimal case-sensitive
  HFS+ (HFSX, matching the N45AP volume's `HX`/version-5 header) via macOS
  `hdiutil -layout NONE` (filesystem from byte 0, volume header at +0x400),
  sized to a 2048 multiple. It places the IPSW kernelcache at
  `/System/Library/Caches/com.apple.kernelcaches/kernelcache.s5l8900xrb` — the
  exact `$boot-path` iBoot's `fsboot` loads (string in iBoot at VA
  `0x1801a500`). No Apple content committed.
- `scripts/build-m68ap-nand.py --hfs <that dmg>` places the HFS image through
  the FTL logical mapping exactly as the N45AP tree does. Verified byte-for-byte
  that the N45AP installed NAND puts MBR@LBA0 (`sysid 0xEE`, part LBA3, size
  132854), GPT header@LBA1 (`EFI PART`, 1 entry, entsz 0x80), GPT entry@LBA2
  (HFS+ type GUID, lba_start 3), and the HFS+ volume header at page+0x400 of
  LBA3 — the `--hfs` output reproduces this layout.

**Result (verified, `iphone-nand-acceptance.py`, both cases PASS, no
regression)** — M68AP serial now reads:
```
[FTL:MSG] FTL_Open            [OK]
HFSInitPartition: 0x1802e888
Loading kernel cache at 0xb000000...data starts at 0xb000180
done
load_macho_image: failed to load device tree
```
`done` is emitted by the adler32 check inside `load_macho_image`, so the
kernelcache is decrypted (GID key), complzss-decompressed, integrity-verified,
and its Mach-O magic is present in RAM. The N45AP regression in the same batch
still reaches `gBootArgs.commandLine = [...]` and `Darwin Kernel Version`.

**Boot-path RE (iBoot-204.3.14, addresses at VA base `0x18000000`)**
- `load_macho_image = 0x1800d544`. It: validates the kernelcache IMG2
  (`Kernelcache image corrupt/too large/not valid`), checks the `complzss`
  signature (`"comp"`=`0x636f6d70` / `"lzss"`=`0x6c7a7373`), prints
  `Loading kernel cache at %#x...` + `data starts at %p`, LZSS-decompresses
  (`0x1800d3a0`), adler32-checks (`0x180075c0`) → `done`, checks Mach-O magic
  `0xfeedface`, then calls the device-tree loader.
- **Device-tree loader `dt_load = 0x1800d060`** (called at `0x1800e07a`,
  dest global `0x18023c20`→`0x0bf00000`, size global `0x18023c24`): calls
  `image_find_by_type('dtre'=0x64747265)` at `0x18008376`; if NULL → fail; if
  `[img+4] > 0x100000` → fail; else `image_load` (`0x18008340`→`0x180088cc`).
  On `< 0` it clears the globals and returns `-1`, so `load_macho_image` prints
  "failed to load device tree" (`0x1800d676`, returns `-7`) and boot drops to
  recovery. **This is why the fault is a NOR/`dtre` problem, not NAND.**
- `image_load` (`0x180088cc`) checks the descriptor magic
  (`[img+0xc] == 0x22f5ef0e`, or `"Memz"=0x4d656d7a`), then validates via
  `0x18008478` comparing its result to `[descriptor+0x10]`, then copies the
  payload through a function pointer at `[obj+0x1c]`. The exact field
  `0x18008478` validates is the open question for the next session.

**Attempts / dead ends recorded this session**
- *Static disassembly first was slow.* `load_macho_image` and its callees are
  compiler-optimized with reordered basic blocks; several disassembly windows
  landed in literal pools and decoded as garbage. The decisive signal was the
  **empirical N45AP-vs-M68AP serial comparison** (N45AP loads the DT silently
  between `done` and `gBootArgs`; M68AP fails there). Reach for the A/B boot
  before deep RE next time.
- *IMG2 header signature is NOT confirmed as the cause (do not chase it blind).*
  The N45AP NOR `dtre` has a populated hash at +0x20 and a 0x20-byte signature
  at +0x3e0; the authentic M68AP `dtre` (decrypted from the IPSW) has zeros
  there and its metadata at +0x60. But the N45AP NOR image was reprocessed by
  the devos50 generator, and a real M68AP device boots without that block, so
  iBoot-204 cannot strictly require it. Treat the header diff as a lead to
  verify against `0x18008478`, not a proven root cause.
- *Root filesystem is a separate, key-blocked track.* `022-3894-4.dmg`
  (`SystemRestoreImages`→`User`, 123 MB) is `encrcdsa` (vfdecrypt), not an 8900
  container, so the GID key does not open it and no offline key is available.
  The kernelcache-only HFS+ is deliberately minimal: it reaches kernel *load*,
  not a mountable root. Do not block the kernel-banner milestone on the root FS.

## Session log — 2026-07-21 (Data Abort SOLVED; full WMR init green)

Executed exactly the planned experiment: applied
`scripts/iphone-data-abort-hook.patch`, rebuilt, reproduced under `-icount`.
The first-Data-Abort capture gave `lr=0x1801605f` (Thumb) — the `memmove`
caller is the loop at **`0x18015fa0`**, and Capstone disassembly decoded it:

- It allocates a page buffer, then scans blocks from the top of the bank
  downward (bounds from geometry `[0x18025530]`: start `blocksPerBank-1`,
  span `blocksPerBank/10`), reading the first pages of each block.
- Each read page is `memcmp`'d against the 16-byte literal at `0x18020710`:
  **`"DEVICEINFOBBT\0\0\0"`** — this is the stored bad-block-table loader.
- On match: `memmove(dst, page+0x38, *(u32 *)(page+0x34))` — i.e. **+0x34 is
  the BBT byte count, +0x38 the bitmap**. A first presence-check pass calls it
  with `dst=NULL` (no copy); the abort happened on the second, real call.
- Our generated `bank*/524160.page` was `DEVICEINFOBBT` + 0xFF fill for the
  whole page, so the count read `0xFFFFFFFF` and the copy walked to
  `0x18100000` (end of iBoot RAM) → Data Abort. The captured live count
  `0xffe6121d` is `0xFFFFFFFF` minus the ~1.7 MB already copied. The N45AP
  page is marker + zeros (count 0 → no-op copy), which is why N45AP never
  faulted.
- This also retroactively explains the "BBT fill size doesn't change the
  fault" dead end: both the 512-byte and full-page 0xFF experiments still
  0xFF-filled the header area including +0x34.

**Fix (landed):** `build_bbt_page()` in `scripts/build-m68ap-nand.py` now
writes count `0x200` at +0x34 (4096 blocks/bank ÷ 8) and 0xFFs only the
0x200-byte bitmap at +0x38 (all blocks good), zeros elsewhere.
`scripts/test-build-m68ap-nand.py` asserts the new shape.

**Verified:** `iphone-nand-acceptance.py` (clean binary, hook reverted):
M68AP case PASS with `full_wmr_init: true`, deepest gate `ftl_open`, serial
shows `VFL_Open [OK]`, `FTL_Open [OK]`, then `HFSInitPartition` →
`Not HFS+ (signature 0x0000)` → recovery prompt (`]`). N45AP regression in
the same batch still reaches the Darwin kernel banner. The fixed NAND is
installed into the app bundle via `install-iphone-firmware.py` (new tree hash
`da27b620…`), and the bundle-default harness run passes.

**Next frontier:** NAND payload — GPT/partition pages, decrypted HFS+ root
filesystem, kernelcache placement (`--hfs` path of the constructor, so far
unexercised), then `bootx` to the kernel banner.

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

**Next failure (precise, corrected twice):** with signature + production BBT,
VFL_Open discovers the context, then iBoot takes a **Data Abort** while
executing `memmove`. A full disassembly diff first overturned the "production
VFL body" hypothesis:

- **The entire Whimory VFL/FTL code is byte-identical between the N45AP and
  M68AP iBoot-204 builds** (M68AP `0x14900-0x18000` vs N45AP shifted −0x700);
  every difference is relocation noise (BL/BLX offset high bytes, relocated
  literal-pool pointers, device strings). There is **no new field check, no new
  constant** in the validator (`0x18016120`), VFL_Open (`0x18016194`), the
  checksum funcs (`0x18015810`/`0x180157e0`), FTL_Open (`0x18015068`), or
  WMR_Init (`0x180164a0`). So the two builds require *identical* context bytes,
  and the it1g body is NOT the problem.
- `0x18017cb4` is the ARM `ldrb r3, [r1], #1` byte-copy instruction inside
  `memmove` at `0x18017bac`. The two indirect calls in `_LoadVFLCxt` dispatch
  through a **statically initialised** vtable (`[0x18025570]` set at
  `0x18015a54` to `{0x18015864, 0x18015810}`), so the original VFL callback
  corruption theory is ruled out. The direct caller of this particular
  oversized `memmove` is still unknown.
- **VFL context layout confirmed (it1g, NOT it2g):** spare `[8]==0` /
  `[9]==0x80`, `dwCxtAge` at spare `[0..3]`; data `awInfoBlk[4]` at **0x7A2**
  (literal cited at both iBoots). Optional production trailer (verified present
  in the checksum code, but N45AP accepts zeros so it is not what blocks us):
  `dwVersion`@0x7F4, `dwCheckSum`@0x7F8 `= Σ words[0..509] + 0xAABBCCDD`,
  `dwXorSum`@0x7FC `= ⊕ words[0..509] ^ 0xAABBCCDD` (510 LE u32 over bytes
  0x000–0x7F7; const at `0x1801580c`).

### Runtime diagnosis — corrected exception chain (2026-07-21)

The previous session first called this a garbage-length copy, then incorrectly
overturned that result as a bad indirect branch. The decisive evidence is the
ordered `-d int` exception log, not the final banked register state:

```text
Exception return from AArch32 irq to svc PC 0x18017cb0
Taking exception 4 [Data Abort] on CPU 0
...with DFSR 0x8 DFAR 0x18100000
Taking exception 3 [Prefetch Abort] on CPU 0
...with IFSR 0x8 IFAR 0x10
Taking exception 3 [Prefetch Abort] on CPU 0
...with IFSR 0x8 IFAR 0xc
```

- **The original exception is a Data Abort.** `0x18017cb4` is the executing
  instruction (`ldrb r3, [r1], #1`), not IFAR. `DFAR=0x18100000` equals the
  live source pointer. The active arguments are `r0=0x180fc9e0`,
  `r1=0x18100000`, `r2=0xffe6121d`; these are real `memmove` state, and the
  near-4-GiB count has walked the source to the end of the iBoot RAM mapping.
- **The prefetch aborts are secondary.** A Data Abort sets abort LR to
  `fault_pc + 8 = 0x18017cbc` and vectors to `0x10`. That vector is unmapped
  under the active MMU, so its fetch aborts; vector `0x0c` is also unmapped and
  repeats forever. Sampling only the parked CPU or IFAR after this cascade led
  to the false "fetch of `0x18017cb4`" conclusion.
- **Why the entry breakpoint misled us:** the earlier hand-rolled debugger did
  not observe the `memmove` entry, but the architectural exception LR and the
  ordered exception trace prove execution reached its byte loop normally.
  Treat the missed breakpoint as a debugger/tooling failure, not control-flow
  evidence.
- **Deterministic repro:** `-icount shift=3` reaches `FTL_Init [OK]` and then
  the same Data Abort. Host watchdog duration is not itself a guest-time
  guarantee: an 8-second sample sometimes caught the normal timer helper at
  `0x180034b8`, while a 20–25-second bound reliably caught the abort.
- **NOR isolation completed:** M68AP iBoot plus the N45AP NOR reaches the same
  post-`FTL_Init` boundary and fault. NOR/DeviceTree contents are not the
  variable. The mixed NOR adds expected epoch/image messages but does not alter
  the failure.
- iBoot Whimory code remains byte-identical to N45AP (true section delta
  **0x704**), NAND/ECC/FMI behavior is shared, and BBT fill-size changes do not
  affect the fault. This still does not prove every runtime input is correct;
  it says the next task is to find who supplied `r2=0xffe6121d`, not to redesign
  the VFL context or NAND signature.

**Reusable tooling added:** `scripts/iphone-nand-acceptance.py` now defaults to
`-icount shift=3`, stages firmware, saves a stopped monitor register/stack
snapshot, and has an opt-in `--interrupt-log`. Keep exception runs short because
the nested vector abort produces millions of repetitive lines. The temporary
CPU hook was removed from `target/arm/helper.c`; use the ready-to-apply
`scripts/iphone-data-abort-hook.patch` when the pre-mode-switch SVC LR/SP is
needed: run `git apply scripts/iphone-data-abort-hook.patch`, rebuild and
capture one repro, then run `git apply -R scripts/iphone-data-abort-hook.patch`.
`git apply --check scripts/iphone-data-abort-hook.patch` verifies that the hook
still matches the current CPU source before changing it.

**Key addresses:** `memmove=0x18017bac`; faulting byte load `0x18017cb4`;
WMR_Init `0x180164a0`; post-`FTL_Init` print return `0x18016508`; VFL_Open
`0x18016194`; geometry `[0x18025530]`.

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
  `bank0..bank7/*.page` tree. Use `build-m68ap-nand.py` (now built); do not
  confuse packing with construction.
- **The post-`FTL_Init` abort is NOT a NAND-format / VFL-body problem.** The
  iBoot code is byte-identical to N45AP (true section delta **0x704**, not
  0x700 — a 4-byte off-by-one made FIL *look* different once; it isn't). N45AP
  iBoot boots on the same `-M iPhone-2G` machine. Don't re-derive a "production
  VFL context body"; the it1g layout is what M68AP reads (awInfoBlk@0x7A2).
- **Do not dismiss the `memmove` arguments as leftovers.** The ordered
  exception trace proves this is an ordinary Data Abort while the byte-copy
  loop executes: `r1=0x18100000` is DFAR and `r2=0xffe6121d` is the bad active
  count. The earlier entry-breakpoint miss was not evidence of an indirect
  branch. Chase the caller that supplied the count.
- **Do not call the primary fault a Prefetch Abort.** The first exception is a
  Data Abort at `0x18017cb4`; the prefetch aborts only occur because the active
  MMU does not map exception vectors `0x10` and `0x0c`.
- **gdb SOFTWARE breakpoints don't work in iBoot.** `0x18000000` is a read-only
  region; `Z0` silently fails there. Use HARDWARE breakpoints (`Z1`). This cost
  a full session.
- **Don't single-step without `-icount`.** `QEMU_CLOCK_VIRTUAL` tracks wall
  time, so single-stepping makes the guest timer fly and moves the fault earlier
  (before `FTL_Init`) — an artifact. Use `-icount shift=3` for a deterministic
  repro that matches the fast path.
- **BBT fill size is not the cause** — *resolved*: both fill sizes faulted
  because both 0xFF-filled the DEVICEINFOBBT header including the count field
  at +0x34. The real layout is count@+0x34 / bitmap@+0x38 (see the 2026-07-21
  Data Abort session log). The production bitmap fill is still required
  (the it1g zero-fill fails VFL_Open's context scan at line 768).
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
