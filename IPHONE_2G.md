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

## Verified

- `-M help` lists `iPhone-2G  iPhone 2G (M68AP)`.
- Smoke boot of `-M iPhone-2G` with the n45ap images reaches iBoot decryption
  and PMU/USB init, proving the inherited machine init path is intact.

## Expected bring-up work (not yet implemented)

Hardware the iPhone has and the iPod Touch lacks; the m68ap iBoot/kernel will
poke at these and may need stub or real models:

- **Baseband** (S-Gold2) on UART/GPIO — iBoot checks baseband GPIOs; the
  kernel's baseband stack will time out or hang without at least stubs.
- **Vibrator** GPIO.
- **Proximity + ambient light sensor** (I2C/ADC path).
- **Speaker/receiver audio codec** differences.
- **Multitouch** firmware differs (iPhone Zephyr1 vs iPod Zephyr2) — the SPI
  multitouch model may need the m68ap calibration/firmware handshake.

Branch on `nms->board_id == BOARD_ID_M68AP` when adding these.
