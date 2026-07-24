# Wi-Fi / SDIO bring-up notes (stage 0+)

Working notes for the Wi-Fi implementation plan carried forward in
`SLEEP_WAKE_INVESTIGATION.md` ("Wi-Fi Feasibility and Implementation Path").
Everything here is derived from the running guest (`IPOD_SDIO_TRACE=1`) and
the N45AP device tree in NOR — not from datasheet guesses.

This file records the **findings**. For the **attempts, dead ends, failed
disassembly approaches, unresolved mysteries, and tooling difficulties**,
see `WIFI_SDIO_DEADENDS.md`. Dev harnesses are in `scripts/wifi-dev/`.

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
request → return a valid 2048-byte Apple EEPROM record stream → begin the
main-firmware download → expose the runtime mailbox.

### The 16-byte EEPROM request (captured)

`readEEPROM` sends this 16-byte CMD53 write once RD_BASE reads 16:

```
14 00 00 00  00 00 00 02  00 00 00 00  00 00 00 00
```

It then reads exactly 0x800 bytes. The accepted payload is a big-endian
record stream:

```
de ad 00 04 be ef ca fe       image header
[be16 key][be16 words][data]  records; words includes the 4-byte record header
```

Key 1 becomes the `tx-calibration` property and key 2 becomes
`local-mac-address`. The behavioral model supplies a stable, non-uniform
128-byte calibration record and the QEMU NIC MAC. Scratch 0x34/0x35 must
contain the signed response length (`0x0800`) during this exchange; using
the later firmware-ready marker (`0xFEDC`) here was the cause of the earlier
7904-byte read and kernel panic.

### Runtime mailbox and networking details

- Event packets carry the raw event ID. Shifting the ID left by three is an
  88W8385 register-path convention and makes Apple miss `DEEP_SLEEP_AWAKE`.
- `CMD_802_11_DEEP_SLEEP` is fire-and-forget. HOST_POWER_UP schedules the
  awake event one virtual second later; repeated polls must not postpone it.
- `CMD_802_11_ASSOCIATE` has the protocol's exceptional reply ID `0x8012`,
  not the otherwise natural `0x8050`.
- Apple sends its first DHCP Discover just before ASSOCIATE reaches the
  mailbox. SLIRP can synchronously return the Offer, so the model defers that
  backend frame until after the association response and link event.
- The machine exposes the card as a QEMU NIC and uses the default user-mode
  SLIRP backend. Build QEMU with `-Dslirp=enabled` (or `--enable-slirp`).

Wi-Fi is enabled by default. `IPOD_MV_WIFI=0` remains as a bring-up escape
hatch; `IPOD_SDIO_TRACE=1` enables the verbose controller/card trace.

## Status against the plan's milestone ladder

| Milestone | State |
|---|---|
| A. Host controller | **Done** — no unknown SDIO MMIO; CMD52/CMD53 complete; IRQ on VIC 0x2A, no storms |
| B. Card enumeration | **Done** — `AppleMRVL868x` attaches; CCCR/FBR/CIS accepted; fn1 enabled |
| (helper) | **Done** — 2432-byte bootstrapper downloaded byte-exact |
| C. Firmware mailbox | **Done** — Apple EEPROM accepted, firmware loaded, command/event/data paths active |
| D. Scan | **Done** — deterministic open `iPod Emulator Network` appears in Settings |
| E. Association | **Done** — Apple accepts the 0x8012 response and link-sensed event |
| F. Network transport | **Done** — QEMU NIC + SLIRP, DHCP gives the guest 10.0.2.15, ARP and bidirectional Ethernet verified |
| G. Safari demo | **Done** — Safari rendered `http://10.0.2.2:8080/` from a host HTTP server |
| H. Power lifecycle | **Done** — deep-sleep/wake loops no longer trigger the Apple command watchdog; when one does fire (idle-lock churn), the card model now survives the driver's hand-of-god power cycle and warm-reloads its firmware |
| I. DNS / hostnames | **Done** — the distributed NAND was missing the mDNSResponder launchd job; `scripts/ipod-nand-restore-dns.py` restores it and Safari resolves hostnames directly (see `DNS_RESOLVER_NOTES.md`) |

## M68AP (iPhone 2G) parity — verified 2026-07-24

The Wi-Fi model is board-agnostic: `ipod_touch.c` creates the `mv8686` NIC
unconditionally for both boards, and the milestone ladder above was proven
on N45AP. A direct A/B (both booted headless to SpringBoard) shows M68AP
reaches a **byte-identical driver-ready state** to N45AP:

    AppleMRVL868x: Firmware loaded.
    AppleMRVL868x Hardware Details:
    AppleMRVL868x: Ethernet address 52:54:00:12:34:56
    IO80211Controller::attachInterfaceWithMacAddress called!
    IO80211Interface::attach(AppleMRVL868x)
    IONetworkStack::attach(IO80211Interface)

So milestones A–C (host controller, enumeration, firmware mailbox) hold on
M68AP unchanged. Association (E) and DHCP (F) are **driver-initiated on
network selection** — a `CMD_802_11_ASSOCIATE` the guest sends when a
network is joined from Settings — so NEITHER board auto-associates in a
headless boot (both show 0 associations without UI). This is expected and
identical across boards; it is not an M68AP gap. Conclusion: **M68AP
inherits the iPod's working Wi-Fi with no board-specific work**; to reach
Safari, drive the SpringBoard UI to select "iPod Emulator Network", same
as N45AP. Telephony/baseband is therefore not required for M68AP network
connectivity.
