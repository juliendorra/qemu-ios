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
| NOR | `nor_m68ap.bin` dump (contains syscfg/IMG2 with the m68ap board config) |
| NAND | NAND dump from an iPhone1,1 |

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

- **Blocked on real m68ap firmware dumps** (iBoot, NOR with syscfg, NAND):
  none of the iPhone-only models can be exercised for real until then; the
  Zephyr1 protocol in particular is implemented from the openiboot reference
  but has never seen the real AppleZephyr driver.
- **Speaker/receiver audio codec** differences (the baseband owns call audio
  via `at+xdrv=0,...`; the stub just OKs those commands).
- **Proximity sensor** (distinct from the ALS) if the m68ap kernel probes one.
- Possible board-ID strap reads in m68ap iBoot (GPIO reads all return 0,
  which conveniently equals the M68AP board ID).
