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
| Board-aware NAND identification and ADM striping | N45AP exposes eight active chips and a 1024-page superblock; M68AP exposes four active chips, four absent ID slots, and a 512-page superblock. Multi-page ADM reads now stripe across the board's active bank count instead of a fixed eight. |
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
- **The M68AP Darwin kernel boots.** Three fixes cleared the device-tree wall
  and the kernel handoff: (1) `build-m68ap-nor.py promote_loadable` normalises
  the NOR IMG2 flags2/CRC so iBoot's image validator accepts the `dtre` image;
  (2) `scripts/patch-m68ap-iboot.py` bypasses this RELEASE iBoot's secure-boot
  enforcement (it never sets the allow-unsigned config bit) — the standard
  "pwnage"-equivalent, applied to a staged iBoot copy, never committed; (3)
  `hw/char/exynos4210_uart.c` reports UART CTS asserted so iBoot's baseband
  UART1 write does not spin before the kernel jump. Result: `Darwin Kernel
  Version ... RELEASE_ARM_S5L8900XRB`, platform expert matches M68AP, IOKit
  registers. Run in REAL TIME (`--icount-shift -1`).
- **Root filesystem: SOLVED.** `scripts/decrypt-m68ap-rootfs.sh` decrypts the real
  1.1.4 root FS (`022-3894-4.dmg`, vfdecrypt/encrcdsa — NOT the GID key) with the
  public VFDecrypt key and extracts the raw 266 MB HFS+ (kernelcache inside). The
  NAND generator (`build-m68ap-nand.py --hfs`) was diffed byte-for-byte against the
  real `generate_nand.c` — faithful (only BBT/GPT differ).
- **Root storage: solved by following the iPod path.** The working N45AP boot
  advertises eight valid NAND IDs and derives 1024 pages per superblock. M68AP
  expects four valid IDs plus four absent slots and derives 512. The emulator
  had advertised eight identical chips to both boards. With board-aware NAND
  identification and the constructor's four-bank M68AP layout, the 4A102
  kernel clean-opens FTL, receives the real extents and catalog B-tree data,
  returns zero from `_vfs_mountroot`, and prints `BSD root: disk0s1`.
- **Launchd and service startup now run.** Two further iPod-guided corrections
  closed the post-root gap. First, ADM command `0x200` was hard-coded to eight
  banks; an M68AP four-page request therefore assigned no pages and delivered
  zeroes at dyld `0x2fe0d580`. Board-count striping restores the exact loader
  instruction and hundreds of normal user blocks. Second, iPhone 1.1.4's
  `/etc/fstab` requires a read-only root (`disk0s1`) plus writable
  `/private/var` (`disk0s2`), unlike the one-partition iPod seed.
  `build-m68ap-nand.py --data-hfs` now emits both GPT/HFS partitions.
  A bounded run mounts both, starts launchd services and mDNSResponder, with no
  former libz failure or reboot.
- **Still open (current blocker): SpringBoard's initializer phase.** A
  process-aware kernel observer proves launchd requests
  `/System/Library/CoreServices/SpringBoard.app/SpringBoard`, kernel `execve`
  returns zero, and the resulting process does not enter the common exit path
  during the bounded run. SpringBoard then executes in `dyld`, loads the same
  dependency sequence as the working N45AP guest through event 280, and enters
  the same initializer phase. M68AP matches the N45AP call semantics through
  event 313, with every observed kernel return successful. A post-return trace
  then corrected the apparent stop: both guests execute the corresponding
  libSystem allocator and Mach-O section-scan path for hundreds of thousands
  of blocks. N45AP makes its next VM allocation only after at least one million
  observed libSystem blocks. This removes
  executable lookup, launchd policy, dependency loading, and an immediate
  loader crash from the blocker set. A 300-second low-overhead boot still does
  not reach the marker, so another blind wait is not useful; the next scripted
  test must retain process identity across the long post-313 user path and
  report its first semantic divergence. The root-domain reference
  adjustment used in these runs remains diagnostic-only; its production
  ownership source is still unresolved.
- **The iPod comparison is scripted.** `scripts/compare-s5l8900-startup.py`
  converts both serial logs into ordered JSON events. The current pair has 116
  shared service starts: N45AP successfully registers `IOIpodUSBDevice` and
  reaches SpringBoard; an unadjusted M68AP run panics inside that service's
  start path. The M68AP-only SDIO reset and baseband functions come later and
  use the same already-resolved GPIO parent, so the first production boundary
  is now the M68 USB service lookup/order rather than storage.
- **Deliberate mixed-artifact failures:** M68AP iBoot rejects N45AP NOR IMG2
  entries for their security epoch and rejects the N45AP NAND's WMR signature.
  A synthetic `nor_m68ap.bin` (`scripts/build-m68ap-nor.py`) clears the NOR
  rejection (0 epoch mismatches) and `scripts/build-m68ap-nand.py` clears the
  NAND signature rejection; those early mixed-artifact failures are no longer
  the boot blocker.

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
| NAND | Generated M68AP sparse page tree from the IPSW root filesystem using `scripts/build-m68ap-nand.py`; a physical iPhone1,1 dump is optional validation input, not a required artifact. |

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
> iBoot with **0** firmware-epoch rejections. With real m68ap iBoot, that NOR,
> and the four-bank generated M68AP NAND, iBoot and the kernel clean-open FTL,
> both HFS B-trees validate, `disk0s1` mounts as root, `disk0s2` mounts at
> `/private/var`, and launchd starts services. A process-aware kernel trace
> proves SpringBoard's `execve` succeeds and the process remains alive. A clean
> N45AP oracle matches M68AP through loader event 280 and initializer event
> 313. Both then execute corresponding allocator/section-scan code for a long
> user-space interval; N45AP's next allocation occurs only after at least one
> million observed libSystem blocks. The boundary is therefore late inside
> initialization, not storage, launchd
> policy, dependency loading, or platform-function lookup. The
> diagnostic root-domain retain remains necessary; see the handoff for
> evidence and the scripted continuation procedure.

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

What remains between today's real M68AP Darwin-kernel boot and SpringBoard:

1. **Board identity and matched boot artifacts:** ✅ **Done.** SYSIC epoch 3,
   watchdog reset, M68AP iBoot/NOR/DeviceTree, the staged secure-boot relaxation,
   UART CTS, the M68AP NAND signature, and the corrected production BBT all pass.
2. **IPSW-derived root filesystem and physical NAND construction:** ✅ **Done.**
   `scripts/decrypt-m68ap-rootfs.sh` extracts the 266 MB HFS+ from the 1.1.4
   VFDecrypt DMG, and `scripts/build-m68ap-nand.py --hfs` reproduces the reference
   generator's FTL/VFL/data layout apart from the intentional M68AP BBT and GPT
   differences. iBoot loads and starts the real M68AP kernelcache.
3. **Board-correct NAND topology and AppleNANDFTL clean-open:** ✅ **Done.**
   Direct observation of the working N45AP boot showed eight valid NAND IDs and
   a 1024-page superblock. M68AP requires four valid IDs, four absent slots, and
   a 512-page superblock. With that distinction in the controller and
   constructor, the 4A102 kernel clean-opens the raw IPSW-derived seed without
   restore. The former bank-7/bank-3 metadata mirroring theory is superseded.
4. **HFS root mount:** ✅ **Done.** The corrected four-bank mapping delivers the
   real extents header at `bank3/25858.page`; both extents and catalog B-trees
   validate, `_vfs_mountroot` returns zero, the HFS mount result is zero, and
   the kernel prints `BSD root: disk0s1`.
5. **launchd:** ✅ **Done under the diagnostic root-domain retain.** `bsd_init`
   completes, `/sbin/launchd` exec returns zero, both required HFS volumes are
   mounted, and launchd starts CommCenter, configd, mDNSResponder, lockdownd,
   mediaserverd, notifyd, and the other launch-era services.
6. **SpringBoard:** ⛔ **Current blocker.** Launchd requests the correct
   executable; the kernel returns zero from its `execve`, assigns a distinct
   process, and that process does not exit during the bounded observation.
   Process-specific trap logging proves dyld loads more than forty distinct
   frameworks and libraries. A clean N45AP oracle and M68AP then enter the same
   initializer sequence and match through event 313, including successful
   returns. Both then traverse corresponding allocator and Mach-O section-scan
   functions; the earlier 512-block M68AP limit was a tracing cap, not a stop.
   N45AP requests a 64 KiB VM allocation only after at least one million
   observed libSystem blocks. Preserve current-process identity across this
   long interval and find the first semantic divergence rather than extending
   a blind timeout.
7. **iPhone-only services:** then validate Zephyr1 touch, proximity/ALS, PMU,
   CommCenter/baseband behavior, and call audio against the real drivers.

Full DFU/restore belongs to a later fidelity track after NAND program, erase,
and replayable persistence exist; it is not a prerequisite for the generated
physical-page first boot.
