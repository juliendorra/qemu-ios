# iPhone 2G (M68AP) machine

This branch adds an `iPhone-2G` QEMU machine type alongside the existing
`iPod-Touch` machine.

## Design

The iPhone (2G) and the iPod Touch 1G use the same S5L8900 SoC (ARM1176) with
the same peripheral memory map, the same PCF50633 PMU, and the same secure boot
chain (VROM → LLB → iBoot). Device-specific behaviour comes from the firmware
images passed on the command line, not from the SoC model.

The machine is therefore implemented as a QOM subclass of the iPod Touch
machine (see `hw/arm/ipod_touch.c`):

- `TYPE_IPHONE_2G_MACHINE` inherits `TYPE_IPOD_TOUCH_MACHINE` and with it the
  full SoC/peripheral init, sleep/wake support, and key handling.
- The class carries an Apple board ID (`IPodTouchMachineClass.board_id`):
  `BOARD_ID_M68AP` (0) for the iPhone, `BOARD_ID_N45AP` (2) for the iPod.
  It is copied to `IPodTouchMachineState.board_id` at machine init so device
  models can branch on it as iPhone bring-up uncovers differences.

## What changed from the iPod profile, and why

The iPhone work has deliberately kept the shared S5L8900 implementation intact
and added only the board distinctions that real M68AP firmware requires:

| Change | Reason |
|---|---|
| `-M iPhone-2G` QOM subclass | Gives M68AP an explicit board identity without duplicating the working S5L8900 machine. |
| `board_id` on the machine state/class | Allows shared peripherals to select M68AP or N45AP behavior while preserving the iPod path. |
| S-Gold2 chardev on UART1 | The iPhone has a cellular baseband and routes its vibrator through that interface. |
| ISL29003 on I2C0 | The iPhone firmware probes this ambient-light sensor. |
| Zephyr1 multitouch mode and M68AP ATN GPIO | The iPhone controller protocol and interrupt line differ from the iPod's Zephyr2/HBPP setup. |
| M68AP image extractor | Apple IPSW boot images are 8900-encrypted/IMG2-wrapped, while the machine's direct-iBoot path expects raw ARM code. |
| iPhone smoke test | Checks board-profile divergence with N45AP firmware; it is a regression test, not proof that an M68AP kernel boots. |

Not every problem found during this work was introduced by the iPhone profile:

- **Real board differences:** SYSIC security epoch, DeviceTree/NOR images,
  NAND contents, Zephyr1, baseband, sensors, GPIOs, and call audio.
- **Existing emulator limitations exposed by M68AP, now fixed:** the SYSIC
  security epoch is board-aware (`POWER_ID` reports N45AP=2 / M68AP=3, with an
  `epoch=` machine-option override), and the watchdog has real reset semantics
  on M68AP so an early panic reboots instead of spinning at `0x18001e3c`.
- **NAND constructor: built.** `scripts/build-m68ap-nand.py` generates the
  M68AP sparse page tree; it reproduces the N45AP metadata byte-for-byte and,
  for M68AP, sets the FIL signature `0x43303033` ("300C", vs N45AP "200C") and
  a production BBT. This removes the `[WMR:ERR] no signature or no production
  format` rejection — the milestone the constructor targeted.
- **Post-`FTL_Init` Data Abort: solved.** The corrupt `memmove` count was the
  constructor's own doing: the full-page 0xFF "production BBT" fill overwrote
  the `DEVICEINFOBBT` page's length field at +0x34 with `0xFFFFFFFF`. iBoot's
  BBT loader (`0x18015fa0`) copies `*(u32*)(page+0x34)` bytes from page+0x38,
  so the copy ran past the iBoot RAM window. Fixed in
  `scripts/build-m68ap-nand.py` (count `0x200`, bitmap-only 0xFF fill):
  WMR init is now fully green — `VFL_Open [OK]`, `FTL_Open [OK]`, and iBoot
  reaches a live recovery prompt.
- **NAND payload: works (kernelcache loads).** The constructor's `--hfs` path
  now carries a case-sensitive HFS+ (HFSX) boot partition holding the
  kernelcache at `/System/Library/Caches/com.apple.kernelcaches/kernelcache.s5l8900xrb`
  (built with `scripts/build-m68ap-hfs-payload.sh`). m68ap iBoot mounts HFS+,
  loads the kernelcache, decrypts it (GID key), complzss-decompresses it, passes
  its adler32 check, and validates the Mach-O — clearing `Not HFS+`.
- **Still open (current blocker):** `load_macho_image: failed to load device
  tree`. iBoot loads the device tree from the **NOR** `dtre` image (loader
  `dt_load=0x1800d060` → `image_find_by_type('dtre')` → `image_load`), which
  rejects our synthetic M68AP `dtre`. This is a NOR image-load problem, not
  NAND. Full evidence, addresses, attempts and next-session prompt:
  `IPHONE_2G_BRINGUP_HANDOFF.md`.
- **Root filesystem (for SpringBoard): separate, key-blocked.** The genuine
  root FS `022-3894-4.dmg` is `encrcdsa`/vfdecrypt-encrypted (not an 8900
  container), so the GID key does not open it; the kernelcache-only HFS+ reaches
  kernel *load*, not a mountable root.
- **Deliberate mixed-artifact failures:** M68AP iBoot rejects N45AP NOR IMG2
  entries for their security epoch and rejects the N45AP NAND's WMR signature.
  A synthetic `nor_m68ap.bin` (`scripts/build-m68ap-nor.py`) clears the NOR
  rejection (0 epoch mismatches) and `scripts/build-m68ap-nand.py` clears the
  NAND signature rejection; the boot blocker is now the post-`FTL_Init` Data
  Abort above.

## Launching

```bash
cd build && ./qemu-system-arm \
  -M iPhone-2G,bootrom=ipod_files/bootrom_s5l8900,iboot=<iboot_204_m68ap.bin>,nand=<m68ap nand dir> \
  -serial mon:stdio -cpu max -m 1G -d unimp \
  -pflash <nor_m68ap.bin> \
  -monitor unix:/tmp/qemu-monitor.sock,server,nowait
```

Required m68ap firmware files (not in this repo):

| Image | Notes |
|---|---|
| bootrom | Same `bootrom_s5l8900` dump as the iPod Touch — the bootrom is per-SoC, not per-device |
| iBoot | `iboot_204_m68ap.bin` from iPhone OS 1.1.x/2.x for iPhone1,1 |
| NOR | `nor_m68ap.bin` — build it with `scripts/build-m68ap-nor.py` from the extracted M68AP IMG2 containers plus a real N45AP NOR (for SysCfg); accepted by m68ap iBoot with 0 epoch mismatches |
| NAND | Generated M68AP sparse page tree from the IPSW root filesystem; the constructor is the next implementation step. A physical iPhone1,1 dump is optional validation input, not a required artifact. |

## Historical NAND provenance and route decision

The original qemu-ios ports establish a simpler route than emulating a full
restore:

| Port | What the history shows |
|---|---|
| iPod Touch 1G (N45AP) | NAND device work started in qemu-ios commit [`c333b6490a`](https://github.com/devos50/qemu-ios/commit/c333b6490a474fe0132332c7f0e383ed30887be7); [`b66eef5008`](https://github.com/devos50/qemu-ios/commit/b66eef50088b78ca272ed7a560ab1fc0c2183871) records NAND read plus kernel boot. Generator commit [`a893e27`](https://github.com/devos50/qemu-ios-generate-nand/commit/a893e27145622a0ebd9d98fbf2d8fc3c8481b7fe) added bank support and HFS placement; the `it1g_nand_filesystem` tag preserves the mature 1G format. It builds an eight-bank, 2048+64-byte sparse page tree with FIL, BBT, VFL and FTL metadata. The upstream author explicitly documents the released NAND as generated from the IPSW root filesystem. |
| iPod Touch 2G (N72AP) | Commit [`1300c08302`](https://github.com/devos50/qemu-ios/commit/1300c08302e6c5f5d26664ced2a9336e2c5947f9) temporarily bypassed FTL reads while bringing the port up. [`5e9f53bfd8`](https://github.com/devos50/qemu-ios/commit/5e9f53bfd8ab3f2969138672daa3605eb7f406ef) removed that bypass from the boot path, and the final port reads generated 4096+64-byte physical pages. Generator commit [`ec11f38`](https://github.com/devos50/qemu-ios-generate-nand/commit/ec11f38c099cdeb529405356bfd17a02f16acc91) is the final format cleanup; its `ipod_touch_2g` branch adds `NANDDRIVERSIGN`, VFL version/vendor fields, mapping pages, BBT, GPT and HFS data. |

The staged metadata pages from the currently bundled N45AP NAND match the 1G
generator byte-for-byte: `bank0/0.page`, `bank0/4480.page`, every bank's
`524160.page`, and the corresponding spare data have the same SHA-256 values
as freshly generated pages. That is positive generator provenance and directly
contradicts the previous description of the bundle as an unsynthesized device
dump. It does not establish the provenance of every later filesystem page, so
future claims must be backed by a manifest rather than inference.

Primary references:

- [Upstream 1G construction instructions](https://devos50.github.io/blog/2022/ipod-touch-qemu-pt2/#manually-generating-the-nand-image)
- [`qemu-ios-generate-nand` history and 1G/2G branches](https://github.com/devos50/qemu-ios-generate-nand)
- [Final iPod Touch 2G running instructions](https://github.com/devos50/qemu-ios/blob/ipod_touch_2g/RUNNING.md)

Decision: implement the same physical-page construction architecture for
M68AP. Do not make a device dump or S5L8900 DFU/USB restore a first-boot
dependency. A real restore remains valuable later for write-path fidelity and
cross-validation.

## iPhone-only hardware modeled (M68AP only, iPod path untouched)

All of it is gated on `nms->board_id == BOARD_ID_M68AP` in
`ipod_touch_machine_init()`. Constants and protocols were taken from the
openiboot iPhone-2G drivers (`plat-s5l8900/`, `radio-pmb8876/` in
iDroid-Project/openiBoot), which are the best public reference for this
hardware.

### S-Gold2 baseband stub (`hw/arm/ipod_touch_baseband.c`)

The baseband sits on **UART1** and speaks plain AT commands. The machine
attaches a `chardev-sgold2` chardev to UART1 that acknowledges every AT
command with `OK` so radio bring-up cannot stall waiting for the modem:

- `at+xdrv=9,1,<idx>` (baseband NVRAM read) answers `+XDRV: 9,1,0,<idx>,NULL`
  so NVRAM enumeration terminates immediately.
- `at+xdrv=4,...` is the **vibrator** — on the M68AP the vibrator has no GPIO,
  it is driven through the baseband. The stub logs these to stderr.
- `at+cops?` reports registration with a fake "QEMU" carrier;
  `atd...` (dial) is logged and, with `at+xcallstat=1` armed, immediately
  reported active.

Baseband GPIOs (openiboot `hardware/radio.h`: BB_ON 0x1807, RADIO_ON 0x1507,
BB_RESET 0x700, BB_DETECT 0x701) need no work: the GPIO model ignores writes
and returns 0 on reads, and BB_DETECT == 0 already means "comm board present".

### ISL29003 ambient light sensor (`hw/arm/ipod_touch_isl29003.c`)

I2C bus 0, 7-bit address 0x49 (device tree 8-bit 0x92). Implements the
8-register map used by openiboot's `als-ISL29003.c`: command/control registers
read back as written (the driver verifies the round-trip), the 16-bit sensor
register pair reports a constant mid-range 0x0800, and the register pointer
auto-increments for the 2-byte data read.

### Zephyr1 multitouch protocol (`hw/arm/ipod_touch_multitouch.c`)

The iPhone's touch controller runs the Zephyr1 firmware, a completely
different wire protocol from the iPod's Zephyr2 HBPP. The multitouch
peripheral now has a `zephyr1` mode (set from the machine board_id) that
implements, per openiboot `multitouch-z1.c`:

- Bootloader: 0x400-byte `0xC2` data packets (A-speed firmware to
  0x40000000), `05 00 00 06` checksum-verify answered `D0 00 ck ck`,
  `0xC4` execute, and the raw (unframed) main-firmware upload.
- Runtime: `0xD0` interface version, `0x8F` report info, `0x82` get report
  (family ID, sensor info/region/dimensions), `0x64`/`0x65` frame-length
  polls and `0x68` frame reads, all `0xAA`-framed with 16-bit checksums.
  Frames reuse the existing `MTFrameHeader`/`FingerData` structures, so
  mouse-driven touch injection works unchanged.
- ATN interrupt moves from GPIO 0x9b (group 4, bit 27; iPod) to GPIO 0xa3
  (group 5, bit 3; iPhone).

One modeling caveat: the SPI model has no chip-select boundaries, and the Z1
main firmware is clocked out raw with no framing. The upload phase is tx-only
(the driver ignores responses until the separate verify transaction), so the
model absorbs bytes into a running checksum and *tentatively* answers
whenever the byte stream matches the verify pattern — a false partial match
only produces response bytes the driver never reads. A firmware image that
happens to contain the literal sequence `05 00 00 06` would end the upload
early; revisit if a real m68ap driver ever hits this.

## Smoke test

`scripts/iphone-smoke-test.py` boots `-M iPhone-2G` with the n45ap images
and checks the divergence points that are observable without m68ap firmware:
kernel boot, AppleISL29003 probing the ALS stub, the expected
`Could not detect HBPP` Zephyr-mode mismatch, no panics, and both machine
types still listed by `-M help`. Every wait has a timeout and the whole
script sits under a SIGALRM watchdog, so it can never hang a caller
(an untimed boot wait once wedged a session for two hours — always wrap
QEMU boot tests in a hard timeout).

```bash
IPOD_QEMU=build/qemu-system-arm python3 scripts/iphone-smoke-test.py
```

## Verified

- `-M help` lists both machines; `iPod-Touch` still passes the full
  `scripts/ipod-acceptance-test.py` sleep/wake matrix.
- Smoke boot of `-M iPhone-2G` with the n45ap images boots the **full
  Darwin kernel** (not just iBoot) with no panic, and:
  - `AppleISL29003` probes, starts, and registers against the new ALS stub
    (iPod OS 1.1 ships the driver; it matches and completes power-state
    transitions).
  - `AppleMultitouchZ2SPI` reports "Could not detect HBPP" — expected: the
    n45ap firmware speaks Zephyr2 while the iPhone machine now runs the
    controller in Zephyr1 mode. Real m68ap firmware loads the Z1 driver.
  - The baseband chardev sits idle (iPod OS has no radio stack) without
    disturbing UART init.

## Remaining bring-up work

> **Live status:** see [`IPHONE_2G_BRINGUP_HANDOFF.md`](IPHONE_2G_BRINGUP_HANDOFF.md).
> The m68ap iBoot (from the 1.1.4 IPSW) decrypts with the GID key already in
> `hw/arm/ipod_touch_8900_engine.h`. The board-aware SYSIC epoch fix and the
> M68AP watchdog are now **landed in code** (no debugger override needed), and a
> synthetic `nor_m68ap.bin` (`scripts/build-m68ap-nor.py`) is accepted by m68ap
> iBoot with **0** security-epoch rejections. With real m68ap iBoot + that NOR
> and the generated M68AP NAND, iBoot reaches `FTL_Init [OK]` before a Data
> Abort in a `memmove` with a corrupt count. See the handoff for the exact
> exception chain and continuation procedure.

- **Firmware obtained** (1.1.4 IPSW, iBoot-204 — same build as n45ap): iBoot,
  LLB, and device tree all decrypt with the shared S5L8900 GID key. The Zephyr1
  protocol is still implemented only from the openiboot reference and has never
  seen the real AppleZephyr driver.
- **Speaker/receiver audio codec** differences (the baseband owns call audio
  via `at+xdrv=0,...`; the stub just OKs those commands).
- **Proximity sensor** (distinct from the ALS) if the m68ap kernel probes one.
- Possible board-ID strap reads in m68ap iBoot (GPIO reads all return 0,
  which conveniently equals the M68AP board ID).

## Path to a full iPhone OS 1 boot

What separates today's state (Darwin kernel boots under `-M iPhone-2G`
with iPod images) from a real iPhone OS 1.x boot to SpringBoard:

1. **Make SYSIC epoch board-specific.** ✅ **Done.** `POWER_ID` reports epoch 3
   for M68AP and 2 for N45AP (`IPodTouchSYSICState.power_epoch`), overridable
   with the `epoch=` machine option. Genuine m68ap iBoot-204.3.14 now
   initializes the M68 display, prints its banner, and reaches the NAND/FTL
   stage automatically — no debugger override.
2. **Provide matched boot artifacts.**
   - NOR: ✅ **Done.** `scripts/extract-m68ap-images.py` retains the full
     decrypted M68AP IMG2 containers (epoch 3) and `scripts/build-m68ap-nor.py`
     assembles `nor_m68ap.bin` from them over a real N45AP NOR's SysCfg. m68ap
     iBoot accepts it with 0 epoch mismatches.
   - NAND metadata: ✅ **Done.** `scripts/build-m68ap-nand.py` generates the
     M68AP page tree (FIL signature `0x43303033`, production BBT with the
     correct DEVICEINFOBBT count/bitmap layout). WMR init is fully green:
     `VFL_Open [OK]` / `FTL_Open [OK]`, recovery prompt reachable.
   - NAND payload: ✅ **Kernelcache loads.** The constructor's `--hfs` path
     places a case-sensitive HFS+ boot partition (built by
     `scripts/build-m68ap-hfs-payload.sh`) with the kernelcache at the boot
     path; `HFSInitPartition` succeeds and iBoot loads/decrypts/decompresses/
     Mach-O-validates the kernelcache. `scripts/pack-ipod-nand.py` remains the
     final compaction step. A full bootable root FS still needs the decrypted
     `022-3894-4.dmg` (vfdecrypt-encrypted; key not available offline).
   - Device tree: ⛔ **Open — the current blocker.** `load_macho_image` then
     fails to load the `dtre` NOR image; see `IPHONE_2G_BRINGUP_HANDOFF.md`.
3. **Multitouch Zephyr1 against the real driver.** The Z1 model follows
   openiboot, but the real `AppleZephyr` kext has never run against it;
   the raw-upload verify heuristic (see caveat above) is the likeliest
   first breakage.
4. **Baseband bring-up depth.** iPhone OS's CommCenter is far more
   demanding than the current OK-to-everything stub: SIM status, IMEI,
   signal-strength unsolicited responses, and the multiplexed audio
   channel (`at+xdrv=0`). Expect iterative stubbing guided by actual
   CommCenter traffic in the serial log. SpringBoard itself should
   tolerate a dead radio (real devices boot with no SIM).
5. **Proximity sensor** — openiboot models it as part of the ALS path on
   m68ap; whether iPhone OS 1.x hard-requires it is unknown until the
   real kernel boots.
6. **Risks:** iBoot may read board-strap GPIOs we return as 0 (happens to
   match M68AP); the m68ap device tree may reference S5L8900 peripherals
   the iPod firmware never touches (unimplemented-register aborts); the exact
   M68AP WMR production fields may differ from both public iPod formats.

Realistic sequencing: the epoch and NOR work are complete. Next, port the
historical NAND-construction design, first targeting a WMR-clean iBoot probe,
then kernel/root mount, then SpringBoard. Only after that should full
DFU/restore and persistent NAND writes compete for effort.
