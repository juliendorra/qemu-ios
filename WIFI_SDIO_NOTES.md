# Wi-Fi / SDIO bring-up notes (stage 0+)

Working notes for the Wi-Fi implementation plan in
`SLEEP_WAKE_INVESTIGATION.md` ("Wi-Fi Feasibility and Implementation Path").
Everything here is derived from the running guest (`IPOD_SDIO_TRACE=1`) and
the N45AP device tree in NOR — not from datasheet guesses.

## Device tree facts (NOR `nor_n45ap.bin`, node `sdio`)

- `compatible` = `sdio,s5l8900x`, `device_type` = `sdio`
- `ABRTreg` = offset `0x00d00000`, size `0x1000` → matches `SDIO_MEM_BASE`
  `0x38d00000`
- `interrupts` = **0x2A** (VIC line for the SDIO controller)
- `clock-gates` = 0x0B, `clock-ids` = 2
- `function-power_enable` = GPIO function, pin word `0x00001701`
- `local-mac-address` present but zeroed in NOR (filled from SysCfg at boot)

## Host controller contract observed from the Apple driver

Boot with the register stub produces a complete SDIO enumeration attempt
(~250 trace lines, then the driver gives up because every response is 0).

Registers actually touched:

| Offset | Role (observed) |
|---|---|
| 0x08 | CMD. Bits 5:0 = SD command index; bit 31 launches. Bits 16–18 = response-type field (CMD5→0x40000, CMD3→0x60000, CMD7→0x10000, CMD52→0x50000) |
| 0x0C | ARG (standard SDIO CMD52 argument encoding: bit31 W, 30:28 fn, 25:9 reg, 7:0 data) |
| 0x14 | Status-clear: guest writes back the DSTA value to ack |
| 0x18 | DSTA: guest polls; stub returns 0x11 (bit0 ready + bit4 cmd complete) which the guest accepts and acks via 0x14 |
| 0x20–0x2C | RESP0–3, read after every command |

Command sequence at cold boot (AirPort driver probe):

1. CMD52 write fn0 reg 0x11 (FN0 block size high) = 0
2. CMD52 write fn0 reg 0x04 (IEN) = 0x01 (master int enable)
3. CMD52 read FBR1/2/3 (regs 0x100, 0x200, 0x300)
4. CMD5 arg=0 (OCR probe), CMD5 with OCR
5. CMD3 (RCA), CMD7 (select)
6. CMD52 read CCCR 0x00 (revision), 0x08 (capability), 0x09–0x0B (CIS ptr)
7. CMD52 write CCCR 0x07 = 0x82 (4-bit bus, CD disable)
8. CMD52 reads 0x07, 0x10, 0x11; then FBR probes 0x100..0x700

Facts established by later trace iterations:

- The function scan keys on the FBR standard-interface code: presenting
  0x07 (WLAN) in FBR1 makes the driver enable fn1 and continue; 0x00 makes
  it give up after probing FBR1..7.
- Data path (matches iphone-linux `iphone-sdio.c`): BADDR 0x44 holds the
  guest-physical DMA address, BLKLEN 0x48 and NUMBLK 0x4C the geometry;
  DCTRL 0x04 is FIFO reset (3 then 0) and 0x10 kicks the transfer after
  the CMD53 launch; CTRL 0x00 gets 0x8005. SDIO_IRQ 0x38 bit0 = data
  done, bit1 = card interrupt, both write-one-to-clear, masked by 0x3C.
- The driver programs fn1 block size 32 (FBR 0x110/0x111) and transfers
  in 32-byte blocks.
- The driver does NOT skip firmware download: after enabling the function
  and writing CONFIG (fn1 0x03) |= HOST_POWER_UP, it streams the Marvell
  helper image as 64-byte CMD53 writes to the I/O port in Libertas
  `if_sdio_prog_helper` framing ([le32 chunk size][data], zero size ends,
  ~2.4 KB total), then polls fn1 RD_BASE 0x10/0x11 for the main-image
  request size (Libertas `if_sdio_prog_real`), streams the main image,
  and finally polls scratch 0x34/0x35 for 0xFEDC.

## Card identity

Marvell 88W8686-compatible SDIO card: 1 I/O function, CIS MANFID vendor
0x02DF, device 0x9103, FBR1 interface code 0x07 (WLAN), CIS1 FUNCE with
512-byte max block size. The behavioral firmware model accepts the
helper/main download blindly and reports ready; mailbox packets use the
Libertas framing ([le16 size][le16 type], types 0=data 1=cmd 3=event).

## Apple driver identity and the EEPROM divergence (from serial log)

The guest driver is `AppleMRVL868x`. Its own log confirms the images:
`SD8686_HELPER is 2432 bytes` (our helper download matches byte-for-byte)
and `SD8686_FIRMWARE is 123020 bytes`. Bring-up order differs from Linux
libertas:

1. `Loading Bootstrapper` — streams the 2432-byte helper (Libertas
   `if_sdio_prog_helper` framing).
2. `Reading EEPROM data` — `readEEPROM()`. **This step has no libertas
   equivalent** and is the gate that was blocking Wi-Fi bring-up.
3. Only after EEPROM success does it load the 123020-byte main firmware.

### readEEPROM handshake, reverse-engineered from the guest

`readEEPROM` polls RD_BASE (fn1 0x10/0x11) and treats it as the card's
"download request size":

- It waits until RD_BASE **equals the size it intends to send** — its
  EEPROM request is **16 bytes**, so it waits for RD_BASE == 0x0010.
  Empirically swept: RD_BASE ∈ {0x800, 0x100, 0x80, 0x02, 0x01} →
  `Timed out wating for ready status from helper firmware`; RD_BASE == 0
  → past ready but `No transmit length from helper image`; RD_BASE ==
  0x10 → proceeds to send a 16-byte CMD53 write (the request) and then
  reads the EEPROM response.
- After the 16-byte request, the host reads the EEPROM payload back over
  CMD53 from the I/O port.

The model now walks this: helper-end → RD_BASE=16 → accept the 16-byte
request → stage an EEPROM image (MAC + zeros) and RX_LEN → serve the read
→ RD_BASE=0x800 to begin the main-firmware download.

### The 16-byte EEPROM request (captured)

With `IPOD_MV_WIFI=1`, `readEEPROM` sends this 16-byte CMD53 write once
RD_BASE reads 16:

```
14 00 00 00  00 00 00 02  00 00 00 00  00 00 00 00
```

It then reads the EEPROM payload back over CMD53 and parses it.

### Open unknown — the EEPROM payload format (external dependency)

The EEPROM **payload format** that makes the driver accept calibration
data and a MAC is not known, and feeding a synthetic image makes the
driver parse garbage and **panic the kernel during boot**
(`kernel abort type 4 ... far=0x0000010c` immediately after
`Reading EEPROM data`). Confirmed there is no open-source reference:
Linux libertas (`if_sdio.c`) and iphonelinux (`openiboot/wlan.c`) both
load helper + main firmware with **no** EEPROM step — the read is
Apple-specific to `AppleMRVL868x`.

Getting the format requires one of:
- A real N45AP Wi-Fi EEPROM/calibration dump, or
- Offline disassembly of `AppleMRVL868x::readEEPROM` with a tool that
  resolves ARM PC-relative (ADR) string references. The in-RAM
  kernelcache is C++-symbol-stripped and uses PIC string refs, so a
  literal-pool pointer search does not locate the function; a proper
  Ghidra/IDA load of the extracted kernelcache is needed.

### Safe default and opt-in

Because a synthetic EEPROM panics the kernel, the default build lets
`readEEPROM` time out gracefully (the driver logs "no calibration",
gives up, and the system still boots to SpringBoard with Wi-Fi absent —
no boot/sleep regression). The experimental bring-up (EEPROM handshake,
main-firmware download, mailbox, scan/associate, tx/rx) is opt-in behind
`IPOD_MV_WIFI=1` for continued development, per the plan's requirement
that Wi-Fi be an opt-in setting that never blocks the vCPU or regresses
the sleep path.

## Status against the plan's milestone ladder

| Milestone | State |
|---|---|
| A. Host controller | **Done** — no unknown SDIO MMIO; CMD52/CMD53 complete; IRQ on VIC 0x2A, no storms |
| B. Card enumeration | **Done** — `AppleMRVL868x` attaches; CCCR/FBR/CIS accepted; fn1 enabled |
| (helper) | **Done** — 2432-byte bootstrapper downloaded byte-exact |
| C. Firmware mailbox | **Blocked** at Apple `readEEPROM`; MAC query needs the EEPROM payload format |
| D. Scan | Modeled (deterministic open BSS) but not reachable until C |
| E. Association | Modeled but not reachable until C |
| F. Network transport | Card tx/rx implemented; NAT backend (libslirp) installed, netdev wiring pending |
| G. Internet demo (Safari) | **Not reached** — gated on C and F |
| H. Power lifecycle | Default build does not regress boot/sleep (Wi-Fi opt-in) |
