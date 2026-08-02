# Running other iPhone OS 1.x builds on M68AP (1.0 / 1.0.x / 1.1.x)

> **Artifact paths moved (2026-07-27).** Commands quoted below use the old
> layout — `m68ap-artifacts/stage/` (which was 1.1.4), `stage-1.0/`,
> `extracted/`. Every build now lives in `m68ap-artifacts/builds/<BUILD>/` with
> version-neutral filenames, and every tool takes an explicit `--build`. The
> quoted commands are kept as the dated record of what was run; to re-run them
> today, translate the paths using
> [`M68AP_BUILD_LAYOUT.md`](M68AP_BUILD_LAYOUT.md) — usually
> `--build <BUILD>` replaces the path arguments entirely.


> Evaluation written 2026-07-25. Every table entry marked **measured** was read
> out of the real IPSW on this machine, not taken from a wiki.
>
> **STATUS 2026-07-26.**
> - **1.1.1 (3A109a): reaches the SpringBoard HOME SCREEN**, first attempt, no
>   emulator code changes. See [Result: 1.1.1 runs](#result-111-runs).
> - **1.0.2 (1C28): REACHES THE SPRINGBOARD HOME SCREEN.** iBoot-159, the
>   Darwin kernel, `BSD root: disk0s1`, `/private/var`, launchd, and SpringBoard
>   rendering at 59% non-black with the frame actually scanned out (45%). Six
>   emulator gaps were fixed to get there, every one of them an unimplemented
>   corner of hardware that 1.1.x never touches.
> - **1.0 (1A543a): REACHES THE HOME SCREEN TOO.** It shares iBoot-159 with
>   1.0.2 and needed no further emulator work — the same pipeline, run with
>   `--build 1A543a`, produced `Darwin ... xnu-933.0.0.178`,
>   `BSD root: disk0s1`, and SpringBoard at 59.0% non-black.
>
> Section 3's "work required" list below is the ORIGINAL estimate, kept for the
> record; the ✅/⛔ markers say what survived contact. Nearly every real blocker
> turned out to be an unimplemented corner of hardware that 1.1.x never touches,
> not anything about the firmware.

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
- `LK_ENABLE_MBX2D` **exists** in 1.0's LayerKit, but the later in-app HOME
  investigation proved that the software-compositing shortcut does **not**
  port as-is. It steers the primary LayerKit compositor, while 1A543a's older
  app-snapshot/backing-store client can still initialize MBX2D and submit work.
  The product therefore also makes `_mbx2DInitialize` fail in the disposable
  staged NAND; the installed firmware artifact remains unchanged.
- lockdownd has the **same activation architecture** — `/Library/Lockdown/data_ark.plist`,
  `ActivationState`, `FactoryActivated`, `ActivationStateAcknowledged`, and the same
  device-certificate validation strings. The hacktivation *strategy* ports; the byte
  patterns do not.
- Kernel is `Darwin 9.0.0d1 … xnu-933.0.0.178 RELEASE_ARM_S5L8900XRB`.

### LayerKit quirk: 1.0 has a separately surviving MBX2D backing-store path

A **backing store** here is a CoreSurface in guest RAM containing the retained
pixels of an application window. During HOME dismissal, SpringBoard can reuse
that surface for the app-to-home transition instead of asking the application
to redraw. LayerKit copies/converts/composites it with the wallpaper, dock and
icon grid; it is an intermediate guest surface, not the final LCD framebuffer
and not a host/QEMU image.

The measured version difference is:

| | 1.0 / 1A543a | 1.1.4 / 4A102 |
|---|---|---|
| Primary compositor steering | Reads `LK_ENABLE_MBX2D=0` and selects software compositing. | The prepared product NAND carries the same setting and selects guest software compositing. |
| App-snapshot/backing-store transition | An older, separately surviving client still calls `_mbx2DInitialize`, submits the legacy shared-surface stream, and waits for MBX retirement despite the primary-compositor setting. | The tested app/HOME transition does not enter that failing legacy client and needs no per-launch `_mbx2DInitialize` patch. |
| Emulator consequence | The launcher must make `_mbx2DInitialize` report failure in the disposable 1A543a clone so this second consumer also falls back to guest software. | No binary initialization patch is applied. |
| Pixels in the working product | Original LayerKit software renderer running as guest ARM code. | Original LayerKit software renderer running as guest ARM code. |

Observed 1.0 control flow:

```text
LK_ENABLE_MBX2D=0
    -> primary LayerKit compositor selects software
    -> legacy app-snapshot/backing-store client still calls _mbx2DInitialize
    -> apparent success submits real MBX2D work
    -> missing surface-ring retirement causes repeated recovery timeouts
```

This makes 1.0 look architecturally split while 1.1.4 looks more unified: in
the tested newer transition, the software/MBX decision covers the rendering
work without a second legacy client escaping it. Treat “unified” as a
**supported inference**, not recovered Apple source intent—the proprietary code
is stripped and no source-level ownership change has been proven. What is
directly established is the behavioral boundary: `LK_ENABLE_MBX2D=0` is not a
global ban on all MBX2D consumers in 1A543a, `_mbx2DInitialize` failure stops
the legacy submissions, and 4A102 does not reproduce them in the same test.

Full traces and the exact workaround guard are in
[`IN_APP_BUTTON_INVESTIGATION.md`](IN_APP_BUTTON_INVESTIGATION.md#what-backing-store-means-here-and-why-the-ordinary-flag-is-insufficient);
the current emulator/device boundary is in
[`MBX_SDO_MMU_HANDOFF.md`](MBX_SDO_MMU_HANDOFF.md#what-the-current-mbx-model-actually-does).

### Driver-level differences that hit our device models

Read from the decompressed kernelcaches:

| | 1.0 | 1.1.1 | 1.1.4 |
|---|---|---|---|
| TVOut swap device (`AppleH1TVOut`) | **absent** | present | present |
| Ambient light sensor | `AppleTSL2561` | same | same |
| Display / panel | `AppleH1CLCD` + `AppleMerlotLCD` | same | same |
| Multitouch | `AppleMultitouchSPI` | same | same |
| Audio | `AppleWM875xAudio` | Wolfson/WM8758/WM8991 | same as 1.1.1 |

The **TVOut absence in 1.0 is good news**: the single hardest fix in the 1.1.4
bring-up (the swap-device teardown window) has nothing to hook in 1.0, because
the kernel has no such device. 1.1.1 does have it — but the workaround is
already runtime-derived from the kernel's own console announcement, so it should
port to 1.1.1 for free.

The **ALS is not a version difference at all** (corrected 2026-07-27; an earlier
revision of this table claimed 1.1.x used an ISL29003 and called 1.0's TSL2561 "a
real gap"). The part cannot change between OS releases — it is the same M68AP
board — and the device trees agree: `builds/1A543a/…/DeviceTree.m68ap.bin` and
`builds/4A102/…/DeviceTree.m68ap.bin` both carry an `als` node with
`compatible = "als,tsl2561"`, `reg = 0x49`, under i2c0. Every 1.x build loads
`AppleTSL2561`; the string `ISL29003` appears in no guest log from any build.

Our `hw/arm/ipod_touch_isl29003.c` is misnamed but harmless: it is a generic
8-register I²C stub at the right address on the right bus, and the real driver
attaches and starts cleanly (`AppleTSL2561::start(als) <1>`, then
`IOHIDUserClientIniter` and `IOHIDEventServiceUserClient` bind to it) on 1.0 and
1.1.1 alike. It works because the stub masks the register pointer to 3 bits, so
TSL2561's DATA0LOW/HIGH (0x0C/0x0D) alias onto regs 4/5 and return the hardcoded
`0x0800` = 2048 counts while DATA1 reads 0 — channel1/channel0 = 0 feeds the lux
formula a constant, plausible mid-bright value. Two cosmetic infidelities: the ID
register (0x0A) aliases to reg 2 and reads `0x00` instead of TSL2561's `0x5x`, and
CONTROL reads back `0x03` instead of the real chip's `0x33`. Apple's driver gates
on neither. The only behavioural effect is that auto-brightness sees a fixed
ambient level and never varies, which is what you want in an emulator. See
[IPHONE_2G.md](IPHONE_2G.md) for the device-model description.

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
  iBoot-159 does not use the ADM at all (it drives the controller directly, and
  its reads work once the ECC engine has a data path). The ADM *does* matter for
  the 1.0 KERNEL, but for a different reason — the command-block layout belongs
  to the uploaded firmware blob. See the 1.0.2 result sections.
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

1.1.x survives that — it simply believes it is permanently on external power —
but it stalls the 1.0 kernel dead in `IOIpodUSBDevice`'s power path. With the
PMU on the board's real bus:

```
ApplePCF50635PMUPowerSource: cap 63, ext 0, chrgCap 0, chrg 0   (1.0.2, correct)
ApplePCF50635PMUPowerSource: cap 100, ext 1, chrgCap 1, chrg 1  (was, from 0xFF)
```

The `IOIpodUSBDevice` stall disappears entirely and the kernel proceeds to
root-device matching. N45AP is unaffected by construction; 1.1.1 still reaches
`BSD root: disk0s1` and the iPod still renders at 47.2 % non-black.

#### Does this also fix the iPhone's sleep/battery/clock symptoms? Partly at most

`SLEEP_BATTERY_SCREEN_FIX.md` carries a note diagnosing this same bus mismatch
and predicting that it explains the iPhone's "always shows the charging
battery", "clock stuck at the epoch", `disabling idle sleep`, "never auto-locks"
and "never reaches OOCSHDWN" symptoms. **The mechanism is the same defect and
this commit is the fix that note deferred** — independently confirmed: the
`pmu,pcf506*` node sits under i2c0 in *all three* M68AP device trees checked
(4A102, 3A109a, 1C28).

The **symptom** half is a separate claim and is NOT verified here. What was
measured is 1.0.2 only: sane PMU values and the `IOIpodUSBDevice` stall
clearing. Two attempts to observe the 1.1.x symptoms failed to produce evidence
either way — short 1.1.1 runs stop before the power source reports, and the
1.1.4 run does not log `ApplePCF50635PMUPowerSource` at its kextlog level.

There is also a concrete reason to expect "never reaches OOCSHDWN" to survive
this fix: the wake path in `hw/arm/ipod_touch.c:920` detects the guest's
power-off loop by a hardcoded **N45AP kernel VA window**
(`pc >= 0xc005a6c0 && pc <= 0xc005a6d8`). M68AP runs a different kernel build,
so that detection cannot fire whatever the I2C bus does. Treat the sleep/wake
symptoms as still open until measured on a run that actually reaches them.

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

## Open issues on the iPhone side (2026-07-27)

All four 1.x builds reach the home screen. What is still wrong, in priority
order, with the evidence needed to resume each:

1. ~~**1.0 cannot be woken from its park.**~~ **SOLVED 2026-07-27** — see
   "1.0's dead park was a main-loop deadlock" below. Power and Home now wake
   it. **Still true and still a trap: do not sample the framebuffer with QMP
   stop/cont while parked — `cont` un-parks the device and invalidates the
   test.**
2. ~~**No Apple logo during boot, on every iPhone version.**~~ **SOLVED
   2026-07-27 — two stacked faults.** The enumeration hypothesis was right:
   1 `image 0x…` line on 1.1.4 versus the iPod's 7. But the cause was **not**
   the store layout — our spacing is fine. iBoot walks the store by
   `next = this + (u32 at header+0x18) * 0x40`; the IPSW containers ship
   `0xFFFFFFFF` in that field and `build-m68ap-nor.py` never filled it in, so
   the first step left the store entirely. Fixing that put the logo in iBoot's
   framebuffer — and the screen was **still** black, because
   `lcd_refresh()` scanned out display window 1 while iBoot draws into window
   2, on *both* boards. The iPod was masking that second bug: its kernel adopts
   iBoot's 0x0fe00000 into window 1 at ~13 s, so the logo looked continuous.
   Now: screenout 2.173% from t=4 s on N45AP, 1.1.4 and 1.0 alike. Derivation,
   dead ends and traps in `IPHONE_2G_BRINGUP_HANDOFF.md` and
   `M68AP_RENDER_HANDOFF.md` §4.
3. ~~**ADM commands `0x400` and `0x100` unimplemented.**~~ **SOLVED
   2026-07-27** — `0x400` is **WriteMultiple**, not the flush/standby it looked
   like, and it was never 1.0-only: 1.1.4 issues it too. Every page it carried
   was being dropped, on every version. `0x100` is a bank inventory with
   nothing to do. See "ADM `0x400` is WriteMultiple" below.
4. **A guest-written NAND stops booting once enough has been written** (new,
   and pre-existing — it reproduces with `0x400` dropped, so WriteMultiple did
   not cause it). Small write volumes DO survive a restart on every target
   tested; past a few hundred pages, M68AP 1.0 wedges in iBoot and 1.1.4 panics
   after FTL init. Narrowed on 1.0 to the three pages the single-page write
   path puts on the FTL context section, with five candidate causes killed by
   measurement. `scripts/nand-persistence-probe.py` reproduces and bisects it
   in one command. Full evidence in "Separate, pre-existing: a written NAND
   will not boot again" below.
5. **Installed 1.1.4/1.1.1 bundles predate this session's fixes** — notably the
   PMU bus, which is what makes them show the big charging battery. Repackaging
   picks that up, and they will then idle-sleep like real devices (correct);
   iBoot-204 parks properly so their wake should stay fast.

Not an issue, recorded to stop it being re-investigated: the black screendump
with a non-black framebuffer is the known panel-auto-sleep artefact.

### 1.0's dead park was a main-loop deadlock, not a swallowed key (2026-07-27)

The wake key was never the problem, and neither was any `return` in
`ipod_touch_key_event()` — **the handler was never reached, because the whole
QEMU main loop was deadlocked from the instant the park happened.** The
give-away is cheap and was missed for a session: while "parked", a QMP client
`connect()`s successfully (the listen backlog accepts it) but **never receives
the greeting**. Nothing was servicing the monitor.

`sample <pid>` on the frozen process gives the exact stack:

```
qemu_main_loop → main_loop_wait → qemu_clock_run_all_timers
  → timerlist_run_timers            (running a QEMU_CLOCK_VIRTUAL timer)
    → pcf50633_prewarm_deadline
      → vm_stop → do_vm_stop → pause_all_vcpus
        → qemu_clock_enable(QEMU_CLOCK_VIRTUAL, false)
          → qemu_event_wait(&tl->timers_done_ev)   ← blocks forever
```

`pause_all_vcpus()` disables the virtual clock, and `qemu_clock_enable()` waits
for every timerlist on that clock to *finish running its callbacks*. We are
inside that very callback, so the event it waits on can never be set. QEMU
documents this precisely, in the comment above `qemu_clock_enable()` in
`util/qemu-timer.c`: the function "should not be used from the callback of a
timer that is based on @clock. Doing so would cause a deadlock."

The 25 s deadline added in `88e73d8cec` is a `QEMU_CLOCK_VIRTUAL` timer and
called `vm_stop()` directly from its callback. The type-4 commit path never hit
this because it parks from a **bottom half** (`prewarm_park_bh`), and BHs run
outside `timerlist_run_timers`. That is also why the iPod, which always takes
the type-4 path, was unaffected — and why "keys reach a suspended VM" was a
true statement that pointed at the wrong suspect.

Fix (one line): the deadline sets `prewarm_no_park` and schedules
`prewarm_park_bh` instead of calling `vm_stop()` itself. The BH does the stop,
handles the wake-arrived-first race it already handled, and prints the usual
`[WAKE] Pre-warmed wake parked; awaiting Power/Home`.

Measured after the fix on 1.0/1A543a: park at ~162 s emits **both** the
deadline line and the parked line (the second line alone proves the main loop
survived); QMP then answers `{"status": "suspended"}`; `send-key h` logs
`[WAKE] Home starting retained-RAM wake boot`, RESUME + guest RESET events
follow, serial grows 144 KB → 217 KB, and the framebuffer comes back at **45.6 %
non-black** on all three bases. `scripts/lock-unlock-probe.py --board n45ap
--cycles 4` stays 4/4, "first failing cycle: none".

Two things worth keeping from this:

- **A parked/suspended VM that ignores QMP entirely is a deadlocked QEMU, not a
  guest problem.** Test the monitor before instrumenting the device model: if
  the greeting never arrives, go straight to `sample`.
- `IT_KEY_TRACE=1` now prints every button event with keycode plus
  `prewarm_active/parked/no_park` and the suppress flags, at the top of
  `ipod_touch_key_event()`. Zero lines out of it means the handler is not being
  called at all — which is a main-loop question, not a key-routing one.

## Dead ends, false paths and wrong turns (2026-07-26)

The process, not just the findings — so the next attempt does not repeat them.
Roughly chronological.

### Claims I made that were wrong, and how they were caught

- **"1.0's park swallows the wake key somewhere in `ipod_touch_key_event()`."**
  (2026-07-27.) False, and instructive: every supporting premise was *true*
  ("keys reach a suspended VM" — yes, on the iPod; "`prewarm_active` and
  `prewarm_parked` are set" — yes) while the conclusion was wrong. The handler
  was never called; the whole main loop was deadlocked by `vm_stop()` inside a
  `QEMU_CLOCK_VIRTUAL` timer callback. Caught by instrumenting the **entry** of
  the handler rather than a suspected branch (zero lines out ≠ wrong branch
  taken), and by noticing that a QMP client `connect()`s while parked but never
  gets the greeting. See "1.0's dead park was a main-loop deadlock" above, and
  finding #96 in `SLEEP_WAKE_INVESTIGATION.md`. Related: a **stale row** in that
  file's dead-ends table claiming `sendkey` never reaches
  `ipod_touch_key_event()` helped make "the key is being lost" look plausible;
  it has been corrected in place.
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

#### What the section scans actually found (so the next attempt can skip them)

Three windows were scanned live, with `IT_ADM_DUMP=1` and `IT_ADM_FIELDS=1`:

- **`data2 + 0x840`** — the block that made firmware-14 look decodable. It is
  **byte-identical across every command** (checked over 10 consecutive
  commands), so it is a static configuration table, not a command block. Its
  contents support that reading: `0x500, 0x300, 0x300, 0x100, 0, 0x400`
  (the command codes the firmware supports), then `00 01 02 03` — a four-entry
  bank map, matching M68AP's four active banks — then `0x1a00`.
- **`data2 + 0x1104`** — firmware-17's command block. All zeroes under
  firmware-14.
- **`data1`** — a dense table of big-endian pointers (`0x000802aa`,
  `0x000802b2`, `0x000802ba`, …) into the `0x0008xxxx` range: the uploaded
  blob's own jump/function table, not a command interface.

So firmware-14's per-command block is in none of the obvious places, and the
layout is defined by the CalmRISC code the kernel uploads at
`ADM_CODE_SEC_ADDR`. Two ways forward, cheapest first:

1. **Watch the writes, not the memory.** Log every guest write into the data
   sections between one `ADM_CTRL2 == 2` and the next; whatever the kernel
   stores immediately before kicking the engine *is* the command block, wherever
   it lives. This needs no understanding of the blob at all and is the
   recommended next step.
2. Disassemble the uploaded blob. It is a Calm/CalmRISC DSP image, not ARM, so
   this is a project in its own right — treat it as the fallback.

#### The write-diff located the structures, but not a command block

`IT_ADM_DIFF=1` snapshots the ADM sections on every engine kick and reports the
words that changed since the previous one — the recommended approach, since
whatever the guest writes before kicking *is* the command. What it shows for
firmware-14:

```
kick #2   data2+0834..0860  the static config table being installed
          data2+1c68: 00000000 -> 0060b208   (big-endian pointer 0x08b26000)
          data2+1c6c: 00000000 -> 00200000   (its length)
kick #3+  data2+0c68: 01000000 -> 02000000 -> 03000000 ...   (nothing else)
```

So after setup, **only a single big-endian counter at `data2+0xc68` changes per
kick** — an index, not a command. Following the pointer at `data2+0x1c68` leads
to a region whose contents include ASCII kernel data (`link`, `<irq`), so it is
either not the descriptor ring or is aliased with something else.

The per-command descriptors therefore live somewhere the section registers do
not point at, and the indirection is set up by the uploaded blob itself.

#### Failed shortcut: forcing the plain-FMC fallback

Both `AppleS5L8900XADMFMC` and `AppleS5L8900XFMC` probe the same
`flash-controller0` nub (visible in any boot log), and the plain FMC driver
would use the direct register path that iBoot-159 already drives successfully in
this model — sidestepping the ADM command interface entirely. Tried making the
ADM never report ready so `ADMFMC::start` would fail and IOKit would fall back.

It does not fall back: IOKit retries `AppleS5L8900XADMFMC::start` forever
(45,568 serial lines of it in a 300 s run, re-uploading `CalmADMFMCFirmware-14`
each time). Removed. If this is revisited, the driver has to be made to *decline
the match* rather than fail its start.

> The *process* — every wrong turn, rabbit hole and escape — is written up in
> [`IOS_1_0_BRINGUP_CASE_STUDY.md`](IOS_1_0_BRINGUP_CASE_STUDY.md). This file
> keeps the measurements.

## Result: iPhone OS 1.0.2 reaches the home screen

Six emulator fixes, in the order they were hit:

1. **NAND ECC engine data path** — the block moved no bytes at all.
2. **ECC region selector** — `NANDECC_SETUP` bits[1:0] = sector count − 1;
   4 sectors = main page, 1 sector = spare. Copying the page for both left
   `spare[8]/spare[9]` zero, so `_LoadVFLCxt` never recognised its context page.
3. **Uncached memory aliases** — bit 31 of a physical address selects the
   uncached view; neither window was mapped.
4. **iBoot-159's unsigned-image rejection** — it hardcodes −1 before consulting
   the security config. One instruction, pattern-located, skipped on iBoot-204.
5. **PMU on i2c0** — M68AP's device tree puts it there; we had it on i2c1, so
   every read returned 0xFF and the 1.0 kernel stalled in `IOIpodUSBDevice`.
6. **ADM command layout** — belongs to the uploaded firmware blob, not the
   hardware. Firmware-14 keeps its command at `data2+0x0824+0x24` and its **page
   number at +0x444** (firmware-17 uses `+0x1104+0x24` and `+0x244`). Until the
   page offset was right every read targeted page 0, so the kernel's
   production-format scan never found `DEVICEINFOBBT` and panicked.

Plus the same two guest-data fixes 1.1.x needs — the lockdownd activation patch
(which located 1.0.2's binary on its own, at a different address) and
`LK_ENABLE_MBX2D=0` — and one new one:

7. **Report the USB charger present (`MBCS1`)** on M68AP. With the PMU finally
   answering, the guest saw no external power and idle-slept within seconds of
   launchd, which the OOCSHDWN path turns into a reboot loop. Verified by A/B:
   **45.4% screenout with it, 0.58% without.** N45AP is deliberately excluded —
   the iPod's sleep/wake support depends on being able to idle-sleep.

**1.0 / 1A543a followed for free.** The same pipeline with `--build 1A543a`
(extract → patch iBoot → NOR → decrypt root → hacktivate + `LK_ENABLE_MBX2D=0`
→ /var → NAND) boots the original June-2007 software: `Darwin Kernel Version
9.0.0d1 ... xnu-933.0.0.178`, `BSD root: disk0s1`, framebuffer 59.0% non-black.
No emulator changes were needed beyond the six above — as predicted, because it
shares iBoot-159 and blob -14 with 1.0.2.

Reproduce:

```bash
python3 scripts/fb-snapshot.py --board m68ap --epoch 0 --iboot-m68ap m68ap-artifacts/stage-1.0.2/iboot_159_m68ap_sbpatch.bin --nor-m68ap m68ap-artifacts/stage-1.0.2/nor_m68ap.bin --nand-m68ap m68ap-artifacts/stage-1.0.2/nand --boot-wait 85 --logs /tmp/fb102
```

Known remaining behaviour: the device still idle-sleeps a little after the home
screen appears, and the OOCSHDWN wake path then restarts the SoC, so a long run
shows repeated boots. The home screen renders well before that. The sleep path
itself is N45AP-tuned (see the poweroff-loop VA note) and has not been ported.

### Firmware-17 vs firmware-14: the complete field map

Captured with `IT_ADM_DIFF=3000` on both (kernel-era kicks, so the fields in
active use), by diffing what the guest writes between engine kicks:

| field | firmware-17 (1.1.x) | firmware-14 (1.0.x) | relative |
|---|---|---|---|
| base | `data2+0x1104` | `data2+0x0824` | — |
| command | `+0x1128` | `+0x0848` | base+0x24 (same) |
| page count | `+0x112c` | `+0x084c` | base+0x28 (same) |
| bank | `+0x1148` | `+0x0868` | base+0x44 (same) |
| **page number** | `+0x1348` | `+0x0c68` | **+0x244 vs +0x444** |
| DMA target / length | `+0x1b48` / `+0x1b4c` | `+0x1c68` / `+0x1c6c` | **+0xa44 vs +0x1444** |

Four of the six fields sit at identical relative offsets; only the page number
and the DMA descriptor move. The page number is the one that mattered — the
model pulls page data through the NAND FIFO rather than honouring the DMA
target, so that field's offset is not currently used by the emulator at all.
(Recorded because it *would* matter if the model ever performs the transfer
itself.)

Bank values confirm the decode: the low byte of the bank word cycles
`…00 → …01 → …02 → …03` in both.

### ADM `0x400` is WriteMultiple (2026-07-27)

`0x400` was logged as `Unrecognized ADM command: 1024` and guessed above to be
a flush/sync/standby, because it lands immediately before every sleep. **It is
a page WRITE** — the third of the three operations `_FILInit` advertises:

| ADM cmd | FIL operation |
|---|---|
| `0x200` | ReadMultiple |
| `0x300` | ReadScattered (and the single-page read) |
| `0x400` | **WriteMultiple** |
| `0x500` | single-page write |

It appears before a sleep because that is when the FTL commits its dirty pages.
So the model was not missing a hint to power down — it was **throwing away
exactly the writes the guest most wanted kept**, silently.

**This was never a 1.0-only gap.** 1.1.4 issues `0x400` too (once per 240 s
run, 12 pages committed); 1.0 issues it far more often only because it
sleep-cycles. Every version has been losing these writes.

#### The descriptor, and the trap in it

Captured with `IT_ADM_UNK=1`, which dumps the command window for any command
the model does not implement — `IT_ADM_DIFF` is no use here because it wants
the kick index up front and `0x400` arrives hundreds of kicks in, at no fixed
count. Layout is the same under both blobs, each at its own base/`page_off`:

| offset | meaning |
|---|---|
| `+0x24` | command (`0x400`) |
| `+0x28` | page count — 4, 0xc, 0x10, 0x14 seen |
| `+0x2c` | `0xc`, constant: the spare-record size |
| `+0x44` | bank bytes, `00 01 02 03` |
| `page_off` | the **base** page, repeated once per bank |
| data3 | one `0xc`-byte spare record per page, packed |

The spare records are the same 12-byte shape the model already writes back as
read completions (byte 10 = `0xff` FTL mark). They must be copied at kick time:
the page data arrives afterwards through the FIFO, and data3 is the guest's own
buffer to reuse meanwhile.

**The trap:** `page_off` looks like a per-page list — for a 16-page command it
holds eight plausible page numbers. It is not. Decoding it that way (first
implementation, measured) sent every entry past the eighth to **bank0/page0**,
the FIL signature page: 24 of 52 multi-page writes in a 300 s run went there.
The give-away is that on a 12-page and a 16-page command the first four words
differ while entries 4..7 stay byte-identical — those four are stale from an
earlier command. Only `num_banks` entries are ever current, exactly as on the
`0x200` read path.

So the real rule is `0x200`'s, which is what makes reads and writes agree:

```
bank = i % num_banks
page = base + i / num_banks
```

Confirmed independently by the page sequence: a 16-page write at base 26514
covers 26514..26517 across four banks, and the next command starts at 26522. A
page-per-entry reading would have had that same command consume 26514..26529,
so the follow-up would be reprogramming pages it had just written — which no
FTL does.

`count` really is a page count, not a byte or sector count: the guest streams
exactly `count * 2048` bytes through the FIFO (FMDNUM lands on 0 at entry
`count-1`), and data3 holds `count` consecutive spare records, their logical
page numbers incrementing by one per entry.

#### `0x100` is a bank inventory, and correctly does nothing

Measured with the same tool: no page count, no bank, no page list, all-zero
descriptor. The chip-ID table it asks about is already in data3 — the model
puts it there on the `ADM_CTRL == 3` start-up. 1.1.x has always issued it and
always proceeded with this model doing nothing, which is the evidence that
"nothing" is right rather than a second gap. It is now a documented no-op
instead of an `Unrecognized ADM command` line.

#### What it took in the model, and what was measured after

`hw/arm/ipod_touch_nand.c` could only write one page per transfer: FMDNUM was
read as an absolute offset into a 2 KiB buffer, and the flush was inline in the
FIFO handler. A multi-page write streams every page through that same FIFO, so
the page index now comes from the offset *within* the current page and the
bank/page/spare are swapped in at each 2 KiB boundary — the mirror image of
what the multi-page read path already did.

Verified (`IT_NAND_WRITABLE=1 IT_NAND_WRITE=1`, `scripts/fb-snapshot.py`):

- 1.0 / 1A543a, 300 s: 52 pages committed across 13 `0x400` commands, striping
  26514..26517 over banks 0-3, spares consecutive, **zero** page-0 writes, no
  WMR/FTL/VFL errors, home screen at 59.0 % non-black.
- 1.1.4 / 4A102, 260 s: 12 pages committed, SpringBoard reached — no regression
  on the version that was already working.
- `Unrecognized ADM command` no longer appears in either.

#### Separate, pre-existing: a written NAND will not boot again

Found while trying to prove the writes survive a reboot, and **not caused by
this change** — it reproduces identically with `0x400` dropped. Re-booting from
a NAND directory a writable run has written to (`--nand-m68ap <prev>/stage/nand`)
wedges in iBoot: Apple logo at 75 % non-black, **zero** serial lines, then
`OOCSHDWN` / "Application processor awaiting power loss". The kernel never
starts.

A/B, both with `IT_NAND_WRITABLE=1`, 1.0 / 1A543a:

| run | fresh NAND | reboot from its own written NAND |
|---|---|---|
| `0x400` implemented | `BSD root`, home screen 59.0 % | wedged in iBoot, 0 serial lines |
| `0x400` dropped | `BSD root`, home screen 59.0 % | wedged in iBoot, 0 serial lines |

So the damage comes from the pre-existing single-page (`0x500`) write path or
from the writable model generally, not from WriteMultiple. Nothing in the tree
had ever booted from a guest-written NAND before, because in the default
read-only mode every write lands in a `<page>_new.page` file that is never read
back.

##### What the investigation established (2026-07-27)

`scripts/nand-persistence-probe.py` automates boot → write → reboot, and
`--bisect` delta-debugs the written pages down to a minimal breaking set. What
it found, in order:

1. **Persistence mostly works.** A run stopped *before* the guest sleeps writes
   326 pages and reboots fine (12 s to `BSD root`). The iPod does 488 pages and
   reboots fine. So writes reach the media and are read back correctly.
2. **The failure needs the sleep.** Only a boot A carried through
   `System Sleep` breaks the reboot — that is when the FTL commits its context.
3. **It is one 23-page context write**, at page 25728-25733: pristine + those
   23 pages wedges, pristine + the *other* 382 written pages boots normally.
4. **Within that context write, WriteMultiple is innocent.** The 20 pages
   `0x400` wrote boot fine on their own; the **3 pages the single-page `0x500`
   path wrote** — banks 1-3 of page 25728 — wedge on their own. That page is
   `FTL_CXT_SECTION_START * 1024` = vpn 205824, i.e. the first page of the FTL
   context section, and banks 1-3 of it are the first three
   logical→virtual mapping pages the generator writes.
5. **iBoot dies exactly there.** `IT_NAND_TRACE_PAGES` shows the failing boot's
   last fetches are 25728/0, 25728/1, 25728/2, 25728/3, 25729/0 and then
   nothing — 1240 fetches against 11867 for a healthy boot.

##### Ruled out, each by measurement — do not re-run these

| hypothesis | how it died |
|---|---|
| the model silently drops block **erases** | `IT_NAND_CMDS` shows the guest issuing only `0xff`/`0x90`/`0x00`/`0x30`/`0x70` in a 200 s writable session — no erase opcode, no program opcode, and zero unrecognized ADM commands. It never erases. |
| the spare's **tail garbage** | Real bug and fixed (below), but the wedge is byte-identical with and without the fix. |
| a **missing eccMarker** on the generator's mapping pages | Those 18 pages really do ship with an all-zero spare while every other generated page carries `spare[10] = 0xFF`. Patching the marker into all 18 changes nothing. |
| **ECC bytes** in the spare | There are none anywhere — pristine spares are a 12-byte record then zeros. |
| **NOR** state diverging between boots | The guest does not modify the NOR at all: boot A's copy is byte-identical to the pristine image. |
| a **torn write** from the probe killing the VM | The context run ends with its own `ffffffff` terminator page, so it completed. |

##### The one real bug this did find

The single-page write copied `NAND_BYTES_PER_SPARE` (64) bytes out of data3 into
the spare, when the guest only ever puts a `0xc`-byte record there. The other 52
bytes belong to the model — at start-up data3 holds the bank chip-ID table — so
**every singly-written page carried `NAND_CHIP_ID` and other emulator scratch in
its spare**. Fixed to take the record and zero the rest, which is both what the
guest wrote and what the pristine image's own pages look like. It does not fix
the reboot.

##### Scope across the targets

Measured with the probe. "short" is a boot A stopped once the guest has
committed pages; "long" carries it through the sleep, or simply runs until the
write count is in the hundreds.

| target | short boot A | long boot A | how boot B fails |
|---|---|---|---|
| M68AP 1.0 / 1A543a | **boots** (326 pages) | **fails** (391-405 pages) | wedges in iBoot, 0 serial lines |
| M68AP 1.1.4 / 4A102 | **boots** (200 pages) | **fails** (712 pages) | kernel runs, `FIL/BUF/VFL/FTL [OK]`, then **panics** into the remote debugger — 4130 serial lines |
| M68AP 1.0.2 / 1C28 | not tested | not tested | artifacts absent from this tree (IPSW-derived, never committed) |
| M68AP 1.1.1 / 3A109a | not tested | not tested | same |
| N45AP iPod | **boots** (116 and 488 pages) | not reproduced | — its boot A never reached a sleep |

Two things this changes. It is **not a 1.0 problem and not an iBoot problem** —
1.1.4 gets all the way through FTL init and then panics, which is a different
symptom of the same inconsistent media. And it is **not strictly about the
sleep** — 1.1.4 never slept; it just wrote enough (712 pages) to restructure
something. The sleep matters on 1.0 only because that is when its FTL commits.

##### The mechanism, established

Two more experiments pinned it down.

**It is the data, not the spare.** Hybrid pages, rebuilt one field at a time on
the three breaking pages:

| bank1-3 of 25728 | boot B |
|---|---|
| guest's DATA (zeros) + pristine spare | **wedges**, 0 serial lines |
| pristine DATA + guest's context spare | **boots**, 2790 lines |

So the context markers, `dwCxtAge`, and the spare type are all irrelevant —
iBoot needs the **logical→virtual mapping-table content** that used to be at
those pages. The `dwCxtAge` signed-vs-unsigned question posed above never
needed answering; it was the wrong question.

**And there is no erase anywhere.** `IT_ADM_SEQ` logs every engine kick in
order. A whole boot-through-sleep session:

```
   1 cmd 0x100      6322 cmd 0x300      339 cmd 0x500
 850 cmd 0x200         4 cmd 0x400
```

One `0x100` at start-up, reads, and writes. No erase, through the ADM or the
register interface. The guest never erases, so "the model swallows an erase"
is dead in both directions: the model does not implement one, and nothing asks
for one.

What is left is a **collision**, and it is now understood end to end:

- Sub-block 201 (`FTL_CXT_SECTION_START`, `pages_per_subblock` = 4 × 128 =
  512 vpns) is laid out `+0` CXT index, `+1..+18` the 18 mapping tables,
  `+511` the FTL meta.
- At shutdown the guest's FTL saves a new 23-page context into the **same**
  sub-block starting at `+1` — straight over the mapping tables.
- iBoot then reads `+0`, the meta at `+511` (page 25855 bank3 — visible in the
  page trace), and follows that meta's `adwMapTablePtrs`, which still say
  `1, 2, 3, …`. Those pages now hold the new context's header, so iBoot reads
  a header where it expects a mapping table and gives up.

##### False paths in this round — both looked right, both were wrong

**"The generator puts the mapping tables in the wrong place."** Moving all 18
from `+1..+18` to `+64..+81` and repointing the meta's `adwMapTablePtrs` makes
the whole cycle pass: boot A through the sleep (396 pages), boot B up in 12 s.
It is not the fix. The **real iPod NAND has the identical layout** — CXT index
at bank0/25728 and 18 mapping tables immediately after it — so the placement is
faithful, and relocating only buys the two or three saves it takes for the
context to grow into the new location. Recorded because it *works*, which makes
it exactly the kind of change that gets committed by mistake.

**"The iPod is the ground truth that proves the emulator innocent."** It is
not. The iPod's VFL context page is byte-identical to the generated one —
`dwGlobalCxtAge=0`, `aFTLCxtVbn=(0,0,0)`, `wNextCxtPOffset=0`, spare age 1,
type 0x80. That image is *generated too*, not a device dump. Its persistence
pass earlier in this session only means its FTL never got as far as a context
save (its boot A never slept). It carries the same latent bug.

##### What the reference says

[openiBoot's `plat-s5l8900/ftl.c`](https://github.com/iDroid-Project/openiBoot/blob/master/plat-s5l8900/ftl.c)
is the same FTL on the same SoC, so it is worth more than another guess:

- `FTL_Open` **scans** the control block and takes the context from the **last
  valid page**, choosing by a *decrementing* sequence number guarded by
  `usnDec > 0`. That is our `dwCxtAge` — `0xffffffe9` in the guest's pages,
  `0` in the generator's, and `0` fails the guard.
- A new context is **appended sequentially** (`++pstFTLCxt->FTLCtrlPage`)
  **without erasing**; a block is only erased when it fills and rotates.

So the guest writing at `+1..+23` is correct behaviour, and the collision with
the mapping tables is a property of how the image is laid out, not a misbehaving
guest and not a missing emulator feature.

##### More false paths — all four measured, all four wrong

| tried | result |
|---|---|
| `aFTLCxtVbn[0..2] = 0, 1, 2` so the FTL can rotate between blocks 201/202/203 instead of rewriting one in place | no change; the save still lands at `+1` in block 201 |
| move the FTL meta off `+511` to `+19`, right after the mapping tables, so a frontier scan can track the guest | **breaks boot A entirely** — 0 writes, never comes up |
| blank **only** `+511`, changing nothing else (the control for the above) | **also breaks boot A** — so the meta at the block's last page is load-bearing and read from a FIXED location, not found by scanning |
| make a page with no backing store read as **erased** (`0xFF` data and spare) instead of zeroes-with-a-hand-placed-marker | boot A fine — no regression — but boot B still wedges |

The last one is worth keeping anyway, as `IT_NAND_ERASED_FF=1`, off by default.
It is a genuine fidelity gap: by default an unwritten page answers "I hold valid
data, logical page 0", so *nothing* that walks a block for the boundary between
written and erased pages can ever find it. It is simply not what breaks this.

##### Where it actually stands

Everything now points at one contradiction, and it is a contradiction in the
**image**, not the emulator:

- the meta at `+511` is required to boot and is read from a fixed location;
- its `adwMapTablePtrs` point at `+1..+18`;
- nothing ever rewrites `+511`;
- the guest's FTL correctly appends its context at `+1..+23`, over those tables.

So the first context save always destroys the mapping tables the only meta
anyone reads still points at. The one configuration that survives a full cycle
is moving the tables clear of the append zone (`+64`, verified) — and that only
postpones it by the two or three saves it takes for the context to grow that
far.

Ranked next steps, cheapest first:

1. **Read the real `FTL_Open` / `FTL_Commit`**, not a summary of it — where a
   freshly formatted device puts its meta, and whether `+511` is genuinely
   fixed or is just where a *full* control block's newest context ends up. That
   single answer decides whether the generator should place the meta low and
   let it migrate, or whether iBoot-159 differs from openiBoot here.
2. **Disassemble iBoot-159's FTL open path** for the fixed offset it reads.
   Heavier, but definitive, and the binary is in the tree.
3. Only then change `build-m68ap-nand.py`, and regenerate every build's NAND —
   which also needs 1.0.2 and 1.1.1 artifacts, absent from this tree.

Two traces stay in the tree for this: `IT_ADM_UNK=1` (descriptor dump for any
unimplemented command) and `IT_NAND_WRITE=1` (every committed page with its
spare and FMDNUM, counted separately for single and multi-page writes — a
shared cap hides the handful of multi-page ones behind thousands of singles,
which is exactly what happened the first time this was measured).


### Sleep and wake on 1.0 — sleep is correct, and wake works

An earlier revision of this section framed the 1.0 idle-sleep as a defect and
went looking for ways to suppress it. **That framing was wrong.** Sleeping after
idle is correct behaviour; the only thing worth checking is whether the device
comes back.

**It does.** `scripts/lock-unlock-probe.py --board m68ap --cycles 3` against the
1A543a stage: 3 of 3 cycles pass, `first failing cycle: none`, and the
post-wake screenshot is a live home screen. The old N45AP symptom (cycle 1
passes, cycle 2 dead) does **not** reproduce here.

The wake goes through the **pre-warm** path, not the poweroff-loop detector:

```
[PMU]  OOCSHDWN=0x02 ... Application processor awaiting power loss
[WAKE] Pre-warming retained-RAM wake after OOCSHDWN
[WAKE] Power requested wake during pre-warm boot
```

A Power press then completes it and the framebuffer comes back at 45.5%
non-black.

Two honest caveats:

- **`hw/arm/ipod_touch.c`'s poweroff-loop detector is still pinned to an N45AP
  kernel VA window** (`0xc005a6c0..0xc005a6d8`) and cannot match on M68AP. I
  wrote a build-independent replacement (detect the tight `b .` with IRQ+FIQ
  masked in kernel space) and then **discarded it**, because an A/B with the
  probe showed it changes nothing: 2/2 cycles pass with and without it. M68AP
  wake does not reach that branch. Recorded so the next person does not
  "fix" it on spec either — get a failing case first.
- **1.0 never shows a lock screen**, so the probe's slide is a no-op on an
  already-unlocked home screen. Its `unlocked` verdict here means "the screen
  came back and stayed live", not "slide-to-unlock was exercised". Every state
  in the report is `kind: "home"`. Whether 1.0 should present the lock screen
  after a retained-RAM wake (which is really a reboot) is a separate question.

For the record, what does *not* stop the sleep: SpringBoard exposes
`SBDisableIdleSleep` and `SBDisableAutoDim` and rejects an `SBAutoLockTime` of 0
(*"Tried to set an autolock duration of 0"*); injecting both as `true` into
`/var/mobile/Library/Preferences/com.apple.springboard.plist` changed nothing
(3 boots / 2 sleeps over 400 s). The sleep is initiated by the kernel's idle
path, below SpringBoard. The trigger is that the guest starts its USB stack,
finds no host to enumerate with — this machine models the OTG device side and
attaches no host — and reports `cable removed`; with no charger and no input the
kernel idles. That is all *correct emulated behaviour*; nothing needs
suppressing.

### The packaged 1.0 app is not stuck asleep — it is CYCLING

Reported 2026-07-27: "iOS 1.0 app seems stuck in sleep, H doesn't wake it."
Reproduced against the bundle itself (launcher + staged writable NAND + QMP),
and it is not a stuck device:

```
boot -> home screen (45.5% non-black)  ->  idle sleep ~40 s later
     -> OOCSHDWN -> pre-warm reboot -> home again -> sleep ...
```

Four boots, three `System Sleep` events and four `BSD root` in a single 300 s
run — a **~75 s cycle**. The screen is only lit for part of each cycle, so a
button press usually lands mid-reboot and shows black. The guest is alive
throughout (IOKit keeps logging).

**H is not being ignored.** Every press is accepted and logged —
`[WAKE] Home requested wake during pre-warm boot` — it just arrives while a
pre-warm boot is already in flight rather than parked, so it passes through
instead of completing a wake.

Why it sleeps, per cycle, from the serial log:

```
IOIpodUSBDevice::gated_message cable is connected, starting stack
IOIpodUSBDevice.cpp power- suspend=0 limit=100      <- 100 mA, unconfigured
IOIpodUSBDevice::gated_message cable removed, stopping stack
System Sleep
```

The device connects, negotiates the pre-enumeration 100 mA, finds no host to
configure it (this machine models the OTG **device** side and attaches no
host), concludes the cable is gone, and with no external power the kernel
idle-sleeps. **1.1.x never reports `cable removed`** — that is the difference to
chase.

**Two unimplemented ADM commands surfaced here**, both logged as
`Unrecognized ADM command`:

| cmd | count | when |
|---|---|---|
| `0x400` (1024) | 13 | immediately before **every** sleep — likely a flush/standby |
| `0x100` (256) | 4 | once per boot at init; 1.1.x issues it too, apparently harmlessly |

`0x400` sitting on the pre-sleep path makes it the first thing to implement.

**Both are implemented now — and the "flush/standby" guess above was wrong.**
See "ADM `0x400` is WriteMultiple" below.

**Tried and reverted:** adding the adapter-present bits
(`MBCS1_ADPPRES|ADPOK`) alongside the USB ones. `ext` stayed 0, the sleeps
continued, and the reported charge fields got *worse* (0 mA / kind 0, against
100 mA / kind 16384 with the USB bits alone). So the driver's `ext` flag does
not come from MBCS1, and that guess is not in the tree.

### What the ADM actually is, and why only 1.0 needs new work

The S5L8900's flash controller has a companion **DSP core** — a Samsung "Calm"
RISC. `AppleS5L8900XADMFMC` uploads a firmware blob to it (the
`Loading ADM/FMC firmware 'CalmADMFMCFirmware-NN'` line), programs section base
addresses into the ADM registers (`0x50` code, `0x84`/`0x88`/`0x8c`
data1/2/3), writes command descriptors into those buffers, and kicks the engine
with `ADM_CTRL2 = 2`. The DSP then drives the NAND.

So the command interface is **not a hardware register spec**. It is a private
software ABI between two halves of Apple's own code — kernel driver and DSP
firmware — that ship together in one OS release. `hw/arm/ipod_touch_adm.c` does
not run the DSP at all; it *impersonates* it, reading the descriptors out of
guest memory at the offsets one blob version uses and doing the NAND access
itself.

That is why the version matters and why the bootloader is unaffected:

| | uses the ADM? | works here? |
|---|---|---|
| iBoot-159 (1.0/1.0.x) | **no** — drives the controller registers directly | yes, after the ECC-engine fix |
| iBoot-204 (1.1.x) | no | yes |
| kernel, 1.1.x | yes, blob **-17** | yes — the shim was written against it |
| kernel, 1.0.x | yes, blob **-14** | **no** — different ABI revision |

#### Provenance of the shim (asked 2026-07-26)

There is no written specification, and none in this repo's history:
`ipod_touch_adm.c` arrives whole with `697306b42c` ("Port the iPod Touch 1G
machine to QEMU 11"), i.e. it is upstream devos50 code and the fork does not
carry his history for it. His Part II post does not document the ADM at all.
Part I documents the *method*, not the layout:

- Ghidra on the bootloader/kernel images
- openiboot's NAND driver as a reference for "the physical layout of the NAND
  memory ... and the I/O interactions"
- a leaked iBoot source containing NAND drivers
- and, explicitly, that deciphering the FMC's I/O operations "took me several
  weeks of trial and error"

The shim's own comments corroborate the trial-and-error origin — "this seems to
be the control register", "some kind of start-up command?", "dunno, write some
bytes to data4_sec_addr". Note also that the leaked iBoot source would not help
here even if consulted: iBoot drives the FMC **directly** and never speaks the
kernel's ADM ABI.

#### Is there a Calm DSP implementation for QEMU? No.

Checked in this tree — `target/` holds alpha, arm, avr, hexagon, hppa, i386,
loongarch, m68k, microblaze, mips, or1k, ppc, riscv, rx, s390x, sh4, sparc,
tricore, xtensa. No CalmRISC, and no public QEMU port of one exists. The
architecture is real and documented enough to be portable in principle —
CalmRISC16 is a Harvard core with a 24-bit `CalmMAC24` coprocessor, has an eCos
target and published papers — but that route means writing a **new QEMU CPU
target from scratch**.

Its one advantage is that it would be version-proof: run the uploaded blob for
real and every firmware revision works, with no per-version shim. That is not a
reasonable trade for one more boot. **Extending the shim to firmware-14 is the
right call**; this is recorded only so the option is not re-discovered from
scratch later.

### Round 3 (kernel bring-up) — what did NOT work

- **Forcing the image validator to report "trusted".** `movs r5,#4` →
  `movs r5,#1` at file `0x7fec`. The validator's return really does change
  (r5 = 1, confirmed under the debugger) and the device tree still fails,
  because iBoot-159 fixes the return value at −1 *earlier*, on the unsigned-image
  path, before the trust level or the security config is ever consulted. A fix
  aimed one layer above the actual decision.
- **Hand-disassembling iBoot from guessed addresses.** Produced a table of
  plausible instruction addresses, several of which do not exist; breakpoints on
  them silently never fired, which looked like impossible control flow. Cost
  several runs. Superseded by `-d in_asm` — see below.
- **"Same fields, shifted base" for ADM firmware-14.** Documented in full above:
  it decodes commands and gets 600 more serial lines, then reads garbage and
  panics. Reverted.
- **Assuming the ADM offset is a hardware property.** It is not — it belongs to
  the firmware blob the kernel uploads. Two releases on the same silicon use two
  different layouts, which is invisible until you compare the
  `Loading ADM/FMC firmware '...'` line in each boot log. When a per-version
  difference appears in a *driver-visible* structure, check whether the guest
  uploaded the code that defines it.

### The through-line for 1.0

Every 1.0 blocker so far has been an **unimplemented corner of the hardware**
rather than anything about the firmware: the ECC engine's data path, its
main-page/spare region selector, the uncached address aliases, the PMU's real
I2C bus, and now the ADM's per-firmware command layout. 1.1.x never exercises
any of them, so they sat empty for years without anyone noticing. Expect the
remaining 1.0 work to have the same shape, and prefer "which register block does
this path actually touch?" over "what is different about this firmware?".

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
