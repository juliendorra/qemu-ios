# Running other iPhone OS 1.x builds on M68AP (1.0 / 1.0.x / 1.1.x)

> Evaluation written 2026-07-25. Every table entry marked **measured** was read
> out of the real IPSW on this machine, not taken from a wiki.
>
> **UPDATE 2026-07-26 — iPhone OS 1.1.1 (3A109a) REACHES THE HOME SCREEN.**
> The profile work below was implemented and 1.1.1 booted to SpringBoard on the
> first attempt with no new emulator code. See
> [Result: 1.1.1 runs](#result-111-runs). 1.0.2 was then attempted and is
> blocked on ONE emulator gap — see
> [Result: 1.0.2 blocked](#result-102-is-blocked-in-the-emulator-not-in-the-artifacts).
> Section 3's "work required" list below is the ORIGINAL estimate, kept for the
> record; the ✅/⛔ markers say what survived contact.

Today `-M iPhone-2G` runs exactly one firmware: **iPhone OS 1.1.4 / 4A102**
(see [`IPHONE_2G.md`](IPHONE_2G.md), [`M68AP_HOMESCREEN_CASE_STUDY.md`](M68AP_HOMESCREEN_CASE_STUDY.md)).
This document answers: why that build, what a second build costs, and in which
order the other builds should be attempted.

## 1. Why 1.1.4 was chosen

It was a deliberate, documented shortcut, not an accident.
[`IPHONE_2G_BRINGUP_HANDOFF.md:2557`](IPHONE_2G_BRINGUP_HANDOFF.md) states it
plainly:

> The 1.1.4 iBoot is **iBoot-204** — the *same* build the n45ap machine already
> runs. (1.0/1A543a would be the museum-accurate target per the feasibility doc,
> but 1.1.4 is the pragmatic first boot: same iBoot version, freely archived.)

So the reasoning was: reuse the known-good N45AP bootloader generation, the
already-hardcoded GID key, the already-understood 8900/IMG2 container shape, and
a published VFDecrypt key — and spend the budget on the board differences (NAND
topology, Zephyr1, baseband, epoch) instead of on a second bootloader.
[`IPHONE_2G_OS_1_FEASIBILITY.md`](IPHONE_2G_OS_1_FEASIBILITY.md) had originally
named **1.0 / 1A543a** as the target; 1.1.4 replaced it for cost reasons only.

The choice was sound, but it left a debt: **there is no firmware-version axis in
the code at all.** The only variation point is `board_id` (M68AP vs N45AP), and
"M68AP" is used throughout as a synonym for "4A102 / iBoot-204.3.14". Every
fixed-offset patch, NAND constant, and lockdownd byte pattern silently assumes
that one build.

## 2. Measured version matrix

All seven 1.x IPSWs are still served by Apple's CDN, unsigned but intact.
Downloaded and verified here: 1.0 (SHA-1 `fb8bb3ee…`, matches the wiki), 1.0.2,
1.1.1, plus the existing 1.1.4.

| Build | 8900 format byte (+0x7) | Security epoch (+0x3e) | iBoot | FIL/WMR signature | Root FS |
|---|---|---|---|---|---|
| 1.0 `1A543a` | **4 = plaintext** | **0** | **iBoot-159** | **`000C` (0x43303030)** | `694-5262-39.dmg` |
| 1.0.2 `1C28` | **4 = plaintext** | **0** | **iBoot-159** | **`000C`** | `694-5298-5.dmg` |
| 1.1.1 `3A109a` | 3 = AES/GID | **2** | **iBoot-204** | **`200C` (0x43303032)** | `022-3602-17.dmg` |
| 1.1.4 `4A102` (current) | 3 = AES/GID | 3 | iBoot-204 | `300C` (0x43303033) | `022-3894-4.dmg` |
| N45AP iPod (reference) | 3 | 2 | iBoot-204 | `200C` | — |

Two consequences jump out of that table.

**1.1.1 is a near-clone of the working N45AP baseline.** Same iBoot build, same
security epoch (2), and the *same NAND signature word* the iPod uses. The
"one 4-byte FIL constant" that blocked M68AP NAND init (see the
`m68ap-wmr-signature` memory) is `200C` for 1.1.1 — a value the constructor
already emits. 1.1.1 should therefore be the **cheapest** second version, quite
possibly cheaper than 1.1.4 was.

**1.0/1.0.2 are a different bootloader generation.** Plaintext images, epoch 0,
iBoot-159, and a third signature word. iBoot-159 has no epoch concept at all —
where iBoot-204 says `miu_init: Epoch Mismatch` / `Ignoring image with
mismatching security epoch`, iBoot-159 says `Ignoring old image without trust
information`. Every iBoot-derived constant in the tree has to be re-derived
against 159.

### What the 1.0 root filesystem actually contains (measured)

The existing `scripts/decrypt-m68ap-rootfs.sh` opened the 1.0 root DMG
**unmodified** (it already takes the VFDecrypt key as `$3`; the public 1A543a
key is `28c909fc…82d`), producing a 193,613,824-byte HFS+ volume. Inside it:

- `/etc/fstab` is **identical in shape** to 1.1.4: `disk0s1 / hfs ro` plus
  `disk0s2 /private/var hfs rw,noexec`. The two-partition GPT the NAND
  constructor already emits is correct for 1.0 — no change.
- The kernelcache sits at the same path the fsboot default expects,
  `/System/Library/Caches/com.apple.kernelcaches/kernelcache.s5l8900xrb`, and
  iBoot-159 contains that same default-boot-path string. `build-m68ap-hfs-payload.sh`
  needs no path change.
- `LK_ENABLE_MBX2D` **exists** in 1.0's LayerKit, so the MBX-2D software-compositing
  shortcut (fault 2 of the home-screen fix) ports as-is.
- lockdownd has the **same activation architecture** — `/Library/Lockdown/data_ark.plist`,
  `ActivationState`, `FactoryActivated`, `ActivationStateAcknowledged`, and the same
  device-certificate validation strings. The hacktivation *strategy* ports; the byte
  patterns do not.
- Kernel is `Darwin 9.0.0d1 … xnu-933.0.0.178 RELEASE_ARM_S5L8900XRB`.

### Driver-level differences that hit our device models

Read from the decompressed kernelcaches:

| | 1.0 | 1.1.1 | 1.1.4 |
|---|---|---|---|
| TVOut swap device (`AppleH1TVOut`) | **absent** | present | present |
| Ambient light sensor | **`AppleTSL2561` only** | `AppleEmbeddedLightSensor` + ISL29003 | same as 1.1.1 |
| Display / panel | `AppleH1CLCD` + `AppleMerlotLCD` | same | same |
| Multitouch | `AppleMultitouchSPI` | same | same |
| Audio | `AppleWM875xAudio` | Wolfson/WM8758/WM8991 | same as 1.1.1 |

The **TVOut absence in 1.0 is good news**: the single hardest fix in the 1.1.4
bring-up (the swap-device teardown window) has nothing to hook in 1.0, because
the kernel has no such device. 1.1.1 does have it — but the workaround is
already runtime-derived from the kernel's own console announcement, so it should
port to 1.1.1 for free.

The **ALS is a real gap for 1.0**: `hw/arm/ipod_touch_isl29003.c` models a part
the 1.0 kernel never probes. 1.0 wants a TSL2561 on I²C instead. Whether that
blocks boot or merely logs a probe failure is unknown until tested.

## 3. Work required

Ordered by how badly each item breaks on a version change.

### Fails safe today, must be re-derived per build

These already verify an anchor and refuse to act on a mismatch, so a wrong build
produces a clean error rather than corruption — but each needs a new offset for
each new firmware:

- ✅ **Wrong — no re-derivation needed.** `scripts/patch-m68ap-iboot.py` now
  locates the helper by pattern; it is byte-identical in all three bootloaders
  including iBoot-159.
- ✅ **Wrong — no re-derivation needed.** `scripts/hacktivate-m68ap.py` now
  derives the lockdownd constants by parsing the binary in place.
- ✅ **Right, and worse than stated.** The `hw/arm/ipod_touch.c` charge-wait
  patch was pinned to `+0x21458` and `+0x09980`; the second is an *N45AP*
  address, so it had never applied on the iPhone at all. Both are now
  pattern-located.
- ✅ **Wrong — needed no work.** `scripts/build-m68ap-nor.py` `promote_loadable`
  was accepted by iBoot-159 with zero trust or epoch rejections, unchanged.

### Breaks silently (no guard at all)

- `scripts/build-m68ap-nand.py`: `SIG_M68AP = 0x43303033` is **board**-keyed but
  is really **firmware**-keyed. Correct mapping is `000C` for 1.0/1.0.x, `200C`
  for 1.1.1 (and the iPod), `300C` for 1.1.4.
- SYSIC security epoch: hardcoded N45AP=2 / M68AP=3. 1.1.1 needs 2 and 1.0 needs
  0. The `-M iPhone-2G,epoch=N` override already exists, so this is a default
  selection problem, not new plumbing.
- `hw/arm/ipod_touch.c` poweroff-loop PC window `0xc005a6c0…0xc005a6d8` — a
  1.1.4 kernel VA used for wake detection.

### Missing capability (small, well-scoped)

- **8900 format-4 support in the kernelcache path.** `scripts/extract-m68ap-images.py`
  already branches on the format byte (`0x03` encrypted / `0x04` plaintext), so
  the boot images are fine. `scripts/extract-kernelcache.py` does **not**, and
  dies on every 1.0 kernelcache (`Data must be padded to 16 byte boundary`). A
  one-line guard (`dec = enc if d[7] == 4 else aes_cbc_decrypt(...)`) is
  sufficient — verified in a scratchpad copy: it decompressed the 1.0
  kernelcache to a 5,888,896-byte ARM Mach-O on the first try.
- **TSL2561 ambient-light model** for 1.0, alongside the existing ISL29003.
  (Not yet reached — 1.0.2 stops before the kernel runs.)
- ⛔ **The one that actually blocks 1.0, and was not on this list at all:**
  iBoot-159's NAND page reads never reach `hw/arm/ipod_touch_adm.c`. See the
  1.0.2 result section.
- (Checked and *not* a difference: the kernelcache is `8900 → complzss` with no
  IMG2 wrapper in 1.0, 1.1.1 and 1.1.4 alike. `extract-kernelcache.py`'s
  docstring claims 1.1.x interposes an IMG2 layer; it does not.)

### Structural (the real fix)

The version dimension itself. `IPHONE_2G_OS_1_FEASIBILITY.md:118-140` already
specified it — a firmware profile carrying `{build, iBoot version, epoch, FIL
signature, VFDecrypt key, patch table}`, selected by machine option, with a
manifest that rejects mismatched artifacts. Nothing of that exists yet. Doing it
*before* the second build lands is what stops this from becoming three parallel
sets of magic numbers.

The model to copy is the TVOut workaround: it derives its address from what the
guest prints, so it is the one piece of this codebase that is already
version-independent by construction.

## 4. How to download and use the other versions

Every 1.x IPSW is a plain HTTP fetch, no auth, no signing window (restore
signing is irrelevant — we never do a real restore):

```bash
curl -s "https://api.ipsw.me/v4/device/iPhone1,1?type=ipsw" | python3 -c "import json,sys;[print(f['version'],f['buildid'],f['sha1sum'],f['url']) for f in json.load(sys.stdin)['firmwares']]"
```

Verified live and hash-checked during this evaluation:

| Build | Size | SHA-1 |
|---|---|---|
| 1.0 `1A543a` | 95,604,348 | `fb8bb3ee2e9a997affbb97868599f2995c78209c` |
| 1.0.2 `1C28` | 95,627,324 | `7f5c0ff1f84a0202b75a55c3fcb362e415334d1e` |
| 1.1.1 `3A109a` | 159,668,150 | `d441dd1c71ce18f25d8fc4faa71c1e6eaa02d02c` |

VFDecrypt keys are per-build and published on The Apple Wiki, but they do not
have to be trusted blindly: for all pre-3.0 firmwares the key is stored as
plaintext in the `__restore` segment of the ASR binary inside the restore
ramdisk, so the pipeline can extract its own key and cross-check.

Per [`AGENTS.md`](AGENTS.md), none of this material is committed — the IPSWs and
everything derived from them live in the gitignored `m68ap-artifacts/`.

## 5. Test plan

The existing harnesses already cover most of this; what is missing is that none
of them are parameterised by build.

**Gate 0 — artifact acceptance (no emulator).** Extract each build's images,
assert the measured tuple (format byte, epoch, iBoot version string, FIL
signature) matches a committed manifest, decrypt the root FS, and assert the
fstab shape and kernelcache path. This is where a wrong-build artifact should be
rejected, and it costs seconds. Nothing like it exists today.

**Gate 1 — iBoot reaches a recovery prompt.** NOR built by `build-m68ap-nor.py`
accepted with zero rejections (for 1.0: zero *trust* rejections, since there is
no epoch check), and WMR init green (`VFL_Open [OK]`, `FTL_Open [OK]`). This is
the gate that the FIL signature governs — `scripts/iphone-nand-acceptance.py` and
`scripts/analyze-m68ap-ftl-open.py` already measure it.

**Gate 2 — kernel boots and mounts root.** `BSD root: disk0s1`, `/private/var`
mounted, launchd starting services. `scripts/compare-s5l8900-startup.py` gives
the ordered-event diff against the N45AP oracle; for 1.1.1 that oracle is an
unusually good match (same iBoot, same epoch, same NAND format), so a divergence
there is high-signal.

**Gate 3 — SpringBoard reaches the home screen.** Reuse the existing judge:
`scripts/fb-snapshot.py` for the framebuffer plus the non-black-percentage
screen judge, with the known trap recorded in the case study — the *activation*
screen is also brightly lit, so "non-black" alone is not proof; the check must
distinguish home screen from activation screen.

**Gate 4 — per-version device deltas.** For 1.0 specifically: confirm whether the
missing TSL2561 blocks boot or merely logs, and confirm that no TVOut wait
appears (it should not — the kernel has no such device). For 1.1.1: confirm the
runtime-derived TVOut window still locates itself from the console line.

Every boot test must keep the hard timeout discipline already established in
`scripts/iphone-smoke-test.py` (an untimed boot wait once wedged a session for
two hours).

## Result: 1.1.1 runs

Implemented and verified on 2026-07-26, in one pass, with **no changes to the
emulator's C code** — every change was in the toolchain:

| Gate | 3A109a result |
|---|---|
| 0 artifact acceptance | passes (format 3, epoch 2, iBoot-204, signature `200C`) |
| 1 WMR init | `FIL_Init/BUF_Init/VFL_Init/VFL_Open/FTL_Open [OK]`, four banks, 512 pages/subblock |
| 2 kernel + root | `Darwin 9.0.0d1 … xnu-933.0.0.203`, `BSD root: disk0s1`, `/dev/disk0s2 on /private/var` |
| 3 SpringBoard | **home screen renders** — 60.7 % non-black at `0x0f496000` |

What it took beyond the profile table: the same two guest-data fixes 1.1.4
needs (`LK_ENABLE_MBX2D=0`, lockdownd activation patch), and the epoch set to 2.
Nothing else.

Three things that the evaluation listed as "must be re-derived per build" turned
out **not** to need it, once they were located by pattern instead of by offset:

- **The secure-boot bypass.** The 32 bytes ending at the patch site (containing
  the literal `0x18022fa0` and its test/branch) occur exactly once in *every*
  1.x m68ap iBoot — 4A102 at `0x5990`, 3A109a at `0x5930`, and 1A543a's
  iBoot-159 at `0x5350`. The helper is byte-identical across the whole line;
  only its address moves. `patch-m68ap-iboot.py` now finds it, and its 4A102
  output is byte-identical to the previously staged patched image.
- **The lockdownd activation patch.** Same structure in both builds, different
  literal-pool values (`0x0007def0`/isa `0x384c73b8` in 1.1.1 vs
  `0x0009d8a0`/`0x384ff3b8` in 1.1.4). `hacktivate-m68ap.py` now derives them:
  it locates the unique `state\0+Unactivated\0` site, walks back to lockdownd's
  Mach-O header (the binary is contiguous in the HFS image), parses its load
  commands, computes the string's VA, and finds the two `__cfstring` constants
  that point at it. Verified to reproduce 1.1.4's exact three patch offsets.
- **The TVOut swap-device window.** Already runtime-derived, and it paid off
  immediately: 1.1.1's swap device is at VA `0xc09c7400`, a different address
  from 1.1.4's `0xc09c8400`, and the console tap relocated the window with no
  intervention.

Still genuinely per-build, and still unsolved for other versions: the iBoot
**charge-wait** patch in `hw/arm/ipod_touch.c`, whose fixed guest addresses do
not match 3A109a (it logs `charge-wait word at 0x18009980 is 0xaf034b21
(expected 0x004c4b40); leaving unpatched` and boots anyway). It should get the
same pattern-locator treatment.

One caveat carried over unchanged from 1.1.4: the **scanout** is still black
(0.003 %) while the framebuffer is fully rendered — the known
"black screen is scanout, not SpringBoard" behaviour, not a 1.1.1 regression.

Reproduce with:

```bash
python3 scripts/iphone-firmware-acceptance.py --all-known m68ap-artifacts/unpacked
```

```bash
python3 scripts/fb-snapshot.py --board m68ap --epoch 2 --iboot-m68ap m68ap-artifacts/stage-1.1.1/iboot_204_m68ap_sbpatch.bin --nor-m68ap m68ap-artifacts/stage-1.1.1/nor_m68ap.bin --nand-m68ap m68ap-artifacts/stage-1.1.1/nand --boot-wait 300 --logs /tmp/fbsnap-1.1.1
```

## Result: 1.0.2 is blocked in the emulator, not in the artifacts

Attempted 2026-07-26. Everything up to storage works; the wall is a real
emulator gap, and it is now precisely located.

**What passes.** Gate 0 (format 4, epoch 0, iBoot-159, signature `000C`); the
secure-boot patch applies at `0x5350` with no change to the tool; the synthetic
NOR is accepted by iBoot-159 with **no** trust or epoch rejection; iBoot-159
runs to its banner; the NAND generator emits the `000C` tree; and the root
filesystem decrypts.

The VFDecrypt key did not need a wiki. For every pre-3.0 firmware the key is
stored in the clear inside the restore ramdisk's `/usr/sbin/asr`, so unwrapping
the ramdisk (itself an 8900 container) and scanning `asr` for a 72-hex-char
string recovers it. 1C28's key was recovered that way and verified by decrypting
the DMG; it is now in the profile.

**What fails.** iBoot-159 identifies the chips (`Bank 0..3 - id 0xa514d3ad`),
reports the right geometry, and returns `[OK]` from `FIL_Init`, `BUF_Init`,
`VFL_Init` and `FTL_Init` — then:

```
[WMR:ERR] read only version (0, 0)
[WMR:ERR] no signature or no production format
sphwNandReadCapacity failed
root filesystem mount failed
```

**The signature is not the problem.** Disassembling iBoot-159 at the failing
check (`0x18016060`) shows the expected word loaded literally at `0x1801606c`:
`r5 = 0x43303030` — exactly the `000C` the generator writes, and the same value
it then prints as `Apple NAND Driver (AND)`. The printf's two arguments are the
production-format result and a signature-found flag; both come back 0, meaning
the scan never found the word it was looking for.

**iBoot-159 does not use the ADM at all.** With `IT_ADM_TRACE=1` (which logs
*every* ADM register access, not just the command behind an `ADM_CTRL2 == 0x2`
write), a 1.1.1 boot shows the full DMA setup — `0x04`/`0x00` control writes,
then the `0x50`/`0x84`/`0x88`/`0x8c` section addresses — and a 1.0.2 boot shows
**nothing whatsoever**. `-d unimp` reports no unimplemented MMIO either. So the
1.0 bootloader drives the NAND controller registers directly rather than through
the DMA engine.

> **CORRECTION.** An earlier revision of this document concluded from that fact
> that "iBoot-159 reads pages through a path `ipod_touch_adm.c` does not service,
> and no NAND content can fix it". **That conclusion was wrong.** Not using the
> ADM does not mean the reads fail — the direct path is modelled, and it works.
> The error was inferring a *second* fact (reads fail) from the *one* fact
> measured (no ADM traffic). Committed as a finding before it was tested.

**The direct reads work, and return the right bytes.** `IT_NAND_WATCH=0/0` shows
1.0.2 really does read `bank0/0`, present. `IT_NAND_FIFO=1` then shows the word
arithmetic on the way out:

```
[NAND-FIFO] page 0 fmdnum 2047 spare 0 -> word[0] = 0x43303030   (1.0.2)
[NAND-FIFO] page 0 fmdnum 2047 spare 0 -> word[0] = 0x43303032   (1.1.1)
```

Each firmware receives exactly the signature it is looking for. 1.0.2 then keeps
scanning — pages 0, 1, 2, 3 … of bank 0, data then spare for each — and still
reports `no signature`. So the page is read, the correct word is delivered, and
the scan rejects it anyway.

**Also ruled out:** `FMCSTAT`. It returns bits 1–12 with bit 0 deliberately
clear, which looked like a candidate for "the read reports failure". Overriding
it (`IT_NAND_FMCSTAT=0x1fff`, `0xffffffff`) changes nothing; `0x3` only breaks
the boot earlier.

**Where that leaves it.** The acceptance condition is not the signature word
itself. Remaining candidates, in order of cheapness to test:

1. The page's **spare** bytes. Every data read is followed by a spare read, and
   our `bank0/0.page` spare is all zeros (as is the real N45AP one — but the
   real iPod NAND is only ever exercised by iBoot-204). A 1.0-era FIL may
   require a valid spare/metadata mark before accepting the page.
2. The scan may want the signature at a page other than 0, or on more than one
   bank — it visits pages 0…N sequentially, which is not the behaviour of code
   that has already found what it wants at page 0.
3. The disassembled site at `0x18016060` may simply not be the code that emits
   the message; a breakpoint there would settle it.

**The breakpoint was taken, and it narrowed things sharply.** QEMU's stub needs
`-S` (a client attaching to a free-running guest never stops it); with that, a
scripted GDB-remote client breaks at `0x180160f8` and reports:

```
r3 (value read) = 0x00000000      r5 (signature wanted) = 0x43303030
buffer at 0x98031258 = 00 00 00 00 …  (all zero, every iteration)
```

The comparison is reached only when the read call returns success, so **the read
reports success and leaves its buffer empty.** The signature word is fine; the
data never arrives in the buffer being compared.

**One real gap found on the way, now fixed:** the buffer lives at `0x98031258`,
in a window the machine never mapped. QEMU returns zero for unassigned reads
without faulting, so the guest silently read zeros rather than crashing. That
window is a legitimate uncached alias of SDRAM (`RAM_MEM_BASE + 0x90000000`) —
both iBoot-159 *and* iBoot-204 carry ~120 constants pointing into it, so it is
not a 1.0 peculiarity, merely something 1.1.x's hot paths never needed. It is
now mapped (`RAM_UNCACHED_MEM_BASE`), verified to alias correctly, and
regression-clean: the iPod still renders at 47.2 % non-black and 1.1.1 still
reaches `BSD root: disk0s1`.

**It did not unblock 1.0.** With the window mapped the buffer is still all zero,
so the missing write is elsewhere. What is now known: the sequential page reads
seen via `IT_NAND_FIFO` (pages 0, 1, 2 … with correct data) are *not* landing in
this scan's buffer, which means either they belong to an earlier phase entirely,
or the FIL read path writes through a mechanism the model does not implement and
still reports success.

**Next step:** single-step from the read call at `0x180160ec` into `fp->[0x14]`
and find where it intends to deposit the page — that is now a bounded question
with the debugger working. The GDB-remote client is in the session scratchpad;
it needs `-S` on the QEMU command line.

## 1.0 bring-up round 2: the ECC engine was hollow

The blocker was never the NAND image. **The S5L8900 has two ways to move a NAND
page into memory, and the emulator only implemented one of them.** iPhone OS
1.1.x moves pages with the ADM DMA engine; iPhone OS 1.0 uses the NAND *ECC*
engine at `0x38F00000`, which this tree modelled as a stub that raised an
interrupt and copied nothing. Same silicon, different subset — and the unused
subset was empty.

Two defects, fixed together in `hw/arm/ipod_touch_nand_ecc.c`:

1. **No data path at all.** The block stored neither `NANDECC_DATA` nor
   `NANDECC_ECC` and never wrote to guest memory, so every destination buffer
   stayed zero.
2. **No region selector.** Once it copied *something*, it copied the main page
   for every transfer. The firmware issues **two** transfers per page and
   distinguishes them by `NANDECC_SETUP` bits [1:0] = (sector count − 1), a
   sector being 512 bytes:

   | setup & 3 | sectors | region |
   |---|---|---|
   | 3 | 4 | the 2048-byte main page |
   | 0 | 1 | the 64-byte spare / metadata |

   `_LoadVFLCxt` identifies its context page by `spare[8] == 0 && spare[9] ==
   0x80` (disassembled at `0x18015d28`–`0x18015d34`). With the spare transfer
   delivering page data instead, those bytes were always zero, so it scanned
   every block on bank 0 and rejected all of them.

Result on 1.0.2, from `no signature or no production format` to:

```
[FTL:MSG] VFL_Open   [OK]
[FTL:MSG] FTL_Open   [OK]
HFSInitPartition: 0x18030898
Loading kernel cache at 0xb000000...data starts at 0xb000180
done
```

Gate 1 is passed and the real 1.0.2 kernelcache is read off the generated NAND
through the HFS+ filesystem. Regression-clean: the iPod still renders at 47.2 %
non-black and 1.1.1 still reaches `BSD root: disk0s1`.

### SOLVED: `load_macho_image: failed to load device tree`

**iBoot-159 hardcodes failure for unsigned flash images**, independently of the
security config. Its loader tests IMG2 flags2 **bit 1** ("signed") and takes a
path that sets the return value to −1 before it ever consults the config:

```
0x1800843e  ldr r3,[r4,#0x1c] / lsls r2,r3,#0x1e / bpl -> unsigned path
0x180084a4  movs r4,#1
0x180084a6  rsbs r4,r4,#0          ; r4 = -1   <- the failure, unconditional
0x180084b2  cmp r4,#0 / bge -> return r4
0x180084b6  <security-config helper>   ; consulted, but r4 is already -1
```

Apple's own all_flash containers ship with bit 1 **clear** (the IPSW `dtre`
header is `0x40000000`), so every NOR image we build takes this path. That is
why relaxing the config helper was not enough, and why forcing the image
validator to report "trusted" changed nothing — both act *after* `r4` is fixed
at −1.

Fix: one instruction, `movs r4,#1` → `movs r4,#0`, so the following `rsbs`
computes −0 = 0 and the load succeeds. The destination address and size are
already stored by `0x1800842e`, so nothing else is required. It is located by a
20-byte pattern that is unique in **both** iBoot-159 builds (1C28 and 1A543a,
both at `0x84a4`) and **absent from every iBoot-204 image**, so 1.1.x skips it
automatically. `patch-m68ap-iboot.py` applies it; the 4A102 output is still
byte-identical to the previously staged image.

**Result: the iPhone OS 1.0.2 kernel boots.**

```
gBootArgs.commandLine = [debug=0x8 kextlog=0xfff cpus=1 rd=disk0s1 serial=1 ...]
Darwin Kernel Version 9.0.0d1: Fri Jun 22 00:38:56 PDT 2007;
    root:xnu-933.0.1.178.obj~1/RELEASE_ARM_S5L8900XRB
iBoot version: iBoot-159
70 prelinked modules
AppleARMPE::start(M68AP)
```

68 serial lines became 1329.

#### How it was found, after a false start

Static Thumb disassembly had been producing fictional addresses (see the
alignment note below). The fix was to stop guessing: run with
`-d in_asm -D <file>`, which makes **QEMU** dump every translation block it
decodes — real entry addresses and their exact bytes. Disassembling *those*
byte ranges is guaranteed correctly aligned, and the set of blocks that appear
is itself the executed path. The failing branch was obvious within one pass.
**Use `-d in_asm` before hand-disassembling anything in this bootloader.**

### SOLVED: the `IOIpodUSBDevice` stall — the PMU was on the wrong I2C bus

The M68AP device tree makes `pmu,pcf50635` a child of the **i2c0** node (the
node sits between the i2c0 and i2c1 nodes in the 1C28 DeviceTree); N45AP puts it
on i2c1. The machine attached it to i2c1 for both boards, so every M68AP PMU
read returned 0xFF from an unanswered bus.

1.1.x survives that — it simply believes it is permanently on external power,
which is exactly where the long-standing "always shows the charging battery" and
`disabling idle sleep` symptoms come from — but it stalls the 1.0 kernel dead in
`IOIpodUSBDevice`'s power path. With the PMU on the board's real bus:

```
ApplePCF50635PMUPowerSource: cap 63, ext 0, chrgCap 0, chrg 0   (1.0.2, correct)
ApplePCF50635PMUPowerSource: cap 100, ext 1, chrgCap 1, chrg 1  (was, from 0xFF)
```

The `IOIpodUSBDevice` stall disappears entirely and the kernel proceeds to
root-device matching. N45AP is unaffected by construction; 1.1.1 still reaches
`BSD root: disk0s1` and the iPod still renders at 47.2 % non-black.

### Current wall: the two releases upload DIFFERENT ADM firmware

1.0.2 now reaches the root-device wait:

```
Waiting on <dict ... <key>BSD Name</key><string>disk0s1</string> ... >
```

The kernel's `AppleNANDFTL` starts, `_FILInit` advertises ReadMultiple /
ReadScattered / WriteMultiple, and WMR reports `FIL_Init`, `BUF_Init`,
`VFL_Init`, `FTL_Init` all `[OK]` — then nothing. No `VFL_Open`, no
`IOFlashBlockDevice`, no error.

The cause is visible in one line of each boot log:

```
AppleS5L8900XADMFMC::start: Loading ADM/FMC firmware 'CalmADMFMCFirmware-14'   (1.0.2)
AppleS5L8900XADMFMC::start: Loading ADM/FMC firmware 'CalmADMFMCFirmware-17'   (1.1.1)
```

**The ADM command-block layout belongs to that uploaded blob, not to the
hardware.** `hw/arm/ipod_touch_adm.c` reads the command word at a hardcoded
`data2 + 0x1104 + 0x24`, which is firmware-17's layout. Under firmware-14 that
address is zero, so every command decodes as `0x0` and no NAND operation is ever
performed — hence a kernel that initialises the FTL fine and then never opens
it.

`IT_ADM_DUMP=1` scans the data2 section and shows firmware-14's block at
**`data2 + 0x840`**, e.g.:

```
[ADM-SCAN] data2=0x08a20000
   +0840: 00 05 00 00  00 03 00 00  00 03 00 00  00 01 00 00
          00 00 00 00  00 04 00 00  00 01 02 03  00 00 00 00
```

Those little-endian words (`0x500`, `0x300`, `0x300`, `0x100`, …) are the same
command codes the 0x1104 layout uses, so this looks like a descriptor list
rather than a single command. Decoding it is the next task, and it is a real
piece of work rather than a constant: the model needs a per-firmware ADM layout,
selected by which blob the guest uploaded.

#### Attempted and reverted: "same layout, different base"

The obvious first guess was that firmware-14 uses firmware-17's field layout at
a shifted base — if the command word is at `data2 + 0x840` rather than
`data2 + 0x1104 + 0x24`, then base = `data2 + 0x81c`. Implemented as a fallback
(use `+0x81c` when `+0x1104` holds no command) and measured:

- Commands **do** start decoding: `0x700`, then `0x500` repeatedly.
- The boot gets further — 1216 → 1835 serial lines.
- But the data is wrong: WMR reads `nSig 0x5f005043` where it wants
  `0x43303030`, reports `Unit NAND format info 0x5F005043 0x32327324`, and then
  `NAND format invalid (corrupt, read error or blank NAND device)` followed by a
  panic into the remote debugger.

So the page/bank fields are **not** at the same relative offsets, and 0x500
(a write code in the 17 layout) appearing hundreds of times during a read-only
boot is a further sign the fields are being misread. Reverted: a guess that
turns a clean wait into a panic on corrupt data is worse than the wait, and it
would mask the real layout. Firmware-14's descriptor format needs to be read
properly — most cheaply from the uploaded blob itself, which the guest hands us
at `ADM_CODE_SEC_ADDR`.

## Dead ends, false paths and wrong turns (2026-07-26)

The process, not just the findings — so the next attempt does not repeat them.
Roughly chronological.

### Claims I made that were wrong, and how they were caught

- **"Both extractors die on 1.0 images."** False.
  `scripts/extract-m68ap-images.py` already branched on the 8900 format byte;
  only `scripts/extract-kernelcache.py` did not. Caught by reading the file
  before editing it. The lesson is narrow but real: the evaluation asserted a
  code fact from a *symptom* (a traceback from one tool) rather than from the
  source.
- **"1.1.x wraps the kernelcache 8900 → IMG2 → complzss, 1.0 does not."**
  False, and the claim came from `extract-kernelcache.py`'s own docstring. No
  1.x kernelcache has an IMG2 wrapper — verified by decrypting 1A543a, 3A109a
  and 4A102 and looking at the first 8 bytes of each (all `complzss`). A
  `kernelcache_has_img2` field had already been added to the profile before this
  was checked; it was removed rather than left encoding a difference that does
  not exist. **Docstrings are not measurements.**
- **"The 1.0 NAND signature might be wrong."** This was the natural hypothesis
  when 1.0.2's WMR rejected the tree, and it was wrong. Disassembly showed
  iBoot-159 loading the expected word literally at VA `0x1801606c` as
  `0x43303030` — precisely what the generator writes. Two boots were spent
  around this before disassembling; disassembling first would have been cheaper.

### Hypotheses tested and killed (for the 1.0 WMR failure)

- **"`read only version (0, 0)` means the VFL context's `dwVersion` is unset."**
  Plausible — our generator leaves it zero. Killed by reading the same field out
  of the **real** N45AP NAND from the shipping iPod bundle: it is zero there
  too, on a NAND that boots. Not the cause.
- **"The 1.0-era signature page carries extra words (a version pair) after
  word0."** Killed by dumping `bank0/0.page` from the real iPod NAND: word0 then
  2044 zero bytes. Not the cause.
- **"Some other `?00C`-shaped constant is the real signature."** The shape scan
  found `900C` in both kernels alongside the expected word; it is unrelated.

The thing that actually settled it was a **discriminating experiment, not more
analysis**: run iBoot-159 against *1.1.1's* NAND tree. If the tree were at
fault, the failure would change. It did not — same error, and still zero ADM
commands — which rules out NAND content entirely and points at the read path.
Reach for the experiment that can distinguish two hypotheses before reaching for
the next hypothesis.

### Tooling traps

- **`fb-snapshot.py` had no way to pass the security epoch.** Booting 1.1.1's
  epoch-2 images under the M68AP default of 3 produced an **empty serial log and
  a fully black framebuffer report** — which reads exactly like "SpringBoard
  never rendered", and was very nearly recorded as a 1.1.1 result. It was
  actually iBoot refusing every NOR image before printing a line. The tell was
  `serial.log` being *zero bytes*, not merely short: a real boot that fails late
  still logs. Fixed by adding `--epoch`; **treat an empty serial log as "it
  never started", never as "it ran and did nothing"**.
- **`hdiutil attach -owners on` makes root-owned guest files unwritable.** The
  repo's own `lab_workspace.attached()` deliberately omits it, so the mounting
  user owns everything and the SpringBoard plist can be edited. Adding `-owners
  on` "for correctness" cost a `PermissionError`. When a helper exists, its
  omissions are usually deliberate.
- **`timeout(1)` does not exist on macOS.** Every ad-hoc boot needs the
  background-and-kill watchdog pattern instead. The repo's rule that no boot may
  be run untimed still stands (an untimed wait once wedged a session for two
  hours).
- **A hand-typed byte pattern is a liability.** The first version of the iBoot
  locator had a transposed byte and matched **zero** times in all three images.
  That failed safe, but the lesson is to build such constants from the bytes
  themselves (slice them out of a real image and print the hex) rather than
  retyping them from a hexdump.
- **A short N45AP boot looks like a regression when it is not.** After changing
  the charge-wait patch, a 120-second iPod boot stopped at `power supply type
  usb host` with no kernel — alarming, and *not* caused by the change. The
  documented path (`fb-snapshot.py --board n45ap`) reached the home screen at
  47.2% non-black. Regression-check with the harness the project already uses,
  not with an ad-hoc invocation.

### What actually worked, and why

- **Locate by pattern, verify by equivalence.** Every patch converted from a
  fixed offset to a pattern was checked by reproducing the *old* result exactly
  on the build the old code was written for: the iBoot patch output is
  byte-identical on 4A102, the lockdownd patch lands on 1.1.4's same three
  offsets, and the charge-wait patch produces a byte-identical N45AP image.
  A "generalisation" that cannot reproduce the original is a rewrite, not a
  generalisation.
- **Derive from the guest, not from a constant.** The TVOut window was already
  runtime-derived and cost nothing on 1.1.1, whose swap device sits at a
  different address. That is the pattern the rest of the tree should follow.
- **Get the key from the artifact.** The VFDecrypt key for 1.0.2 was recovered
  from the IPSW's own restore ramdisk rather than a wiki, and verified by
  actually decrypting the image.

### Round 2 (the 1.0 NAND push) — what did NOT work

- **"iBoot-159 does not use the ADM, therefore its reads fail."** Corrected
  above. It does not use the ADM *and* its reads work; those are two facts and
  only one was measured.
- **`FMCSTAT` bit 0.** The model returns bits 1–12 with bit 0 clear, which
  looked like "the read reports not-ready". Swept `0x1fff`, `0xffffffff`
  (identical behaviour) and `0x3` (fails earlier). Not the cause. The
  `IT_NAND_FMCSTAT` override that made this a one-run experiment is worth
  keeping.
- **Spare bytes on the signature page.** Tried `spare[0xA] = 0xFF`, an all-`0xFF`
  spare, and `spare[9] = 0x80`; all three still failed. The spare *was* the
  problem, but on a different page (the VFL context) and for a different reason
  (it was never delivered at all).
- **VFL context `dwVersion` / extra signature-page words.** Both killed by
  reading the **real** N45AP NAND out of the shipping iPod bundle: it has zeros
  in exactly those fields and boots fine.
- **Mapping `0x98000000` to SDRAM.** Right idea (bit 31 selects the uncached
  view), wrong target: the buffer at `0x98031258` is the uncached alias of
  `0x18031258`, inside the **iBoot RAM** window, not SDRAM. Mapping the wrong
  region changed nothing, which is exactly why it looked like a dead end rather
  than a half-fix. Both aliases are now mapped via `UNCACHED_MEM_BIT`.
- **Forcing the image validator to report "trusted".** Verified under the
  debugger that r5 becomes 1, and the device tree still does not load. Trust
  level is not the device-tree gate.

### Debugger notes (this is the tool that broke the deadlock)

- **QEMU's gdbstub needs `-S`.** A client attaching to a free-running guest
  never stops it, so `Z0` + `c` silently does nothing and the guest boots to
  completion. Two runs were lost to this before it was noticed; a third was lost
  to `lldb -b`, which hangs against the bare stub.
- A ~60-line GDB-remote client in Python (packet framing, `Z0`, `c`, `g`, `m`)
  works fine and is far more predictable here than a full debugger. Reading
  registers *and* dereferencing guest pointers at a breakpoint is what turned
  "the scan rejects our page" into "the spare buffer is never written".
- Breakpoint at the *comparison* rather than at the failure message. The message
  is many frames away from the decision; the `cmp` is the decision.

## 6. Recommended order

1. ~~**Add the firmware-profile dimension first**~~ ✅ **Done** —
   `scripts/firmware_profiles.py`, the format-4 guard, and
   `scripts/iphone-firmware-acceptance.py`.
2. ~~**1.1.1 / 3A109a**~~ ✅ **Done, home screen reached.** See above.
3. **1.0.2 / 1C28** — attempted; blocked in the emulator's NAND read path, see
   above. Its cost is *not* what this document first guessed: the secure-boot
   bypass and the NOR validator needed no work at all, and the VFDecrypt key was
   recoverable from the IPSW. What it needs is one ADM command implemented.
4. **1.0 / 1A543a** last, as the museum-accurate original. It shares iBoot-159
   with 1.0.2, so it should follow immediately once the read path works. Its
   bonuses are the absent TVOut device and, still outstanding, its TSL2561
   ambient-light sensor in place of the ISL29003.

Rough shape of the remaining effort: the 1.0 family is still a genuine second
bring-up (different bootloader generation, plaintext images, epoch 0, a third
NAND signature, and the TSL2561 sensor), but it is cheaper than this document
originally estimated — the secure-boot patch already locates itself in
iBoot-159, and the acceptance gate will tell you within seconds whether the
artifacts are what you think they are.
