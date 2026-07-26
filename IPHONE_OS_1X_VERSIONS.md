# Running other iPhone OS 1.x builds on M68AP (1.0 / 1.0.x / 1.1.x)

> Evaluation written 2026-07-25. Every table entry marked **measured** was read
> out of the real IPSW on this machine, not taken from a wiki.
>
> **UPDATE 2026-07-26 — iPhone OS 1.1.1 (3A109a) REACHES THE HOME SCREEN.**
> The profile work below was implemented and 1.1.1 booted to SpringBoard on the
> first attempt with no new emulator code. See
> [Result: 1.1.1 runs](#result-111-runs). The 1.0 family is still unattempted.

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

- `scripts/patch-m68ap-iboot.py` — secure-boot bypass at file offset `0x5990`
  with anchor `0x5984`, explicitly labelled "iBoot-204.3.14". iBoot-159 needs its
  own offsets (it is a different bootloader, and its trust check is structured
  differently — no epoch gate).
- `scripts/hacktivate-m68ap.py` — lockdownd patterns embed 1.1.4 literal-pool
  values (`b8f34f38`, `a0d80900`, CFString length 11→9).
- `hw/arm/ipod_touch.c` iBoot charge-wait patch — absolute `IBOOT_BASE+0x21458`
  and `+0x09980`, verified against expected words.
- `scripts/build-m68ap-nor.py` `promote_loadable` — IMG2 flag normalisation
  reverse-engineered from iBoot-204's validator at VA `0x18008478`.

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

## 6. Recommended order

1. ~~**Add the firmware-profile dimension first**~~ ✅ **Done** —
   `scripts/firmware_profiles.py`, the format-4 guard, and
   `scripts/iphone-firmware-acceptance.py`.
2. ~~**1.1.1 / 3A109a**~~ ✅ **Done, home screen reached.** See above.
3. **1.0.2 / 1C28** before 1.0 — same bootloader generation as 1.0 but the more
   widely-used shipping build, so it is the better-documented target for
   activation behaviour.
4. **1.0 / 1A543a** last, as the museum-accurate original. Its cost is dominated
   by iBoot-159: new secure-boot bypass offsets, a re-derived NOR image
   validator, and a new hacktivation pattern. Its bonus is the absent TVOut
   device.

Rough shape of the remaining effort: the 1.0 family is still a genuine second
bring-up (different bootloader generation, plaintext images, epoch 0, a third
NAND signature, and the TSL2561 sensor), but it is cheaper than this document
originally estimated — the secure-boot patch already locates itself in
iBoot-159, and the acceptance gate will tell you within seconds whether the
artifacts are what you think they are.
