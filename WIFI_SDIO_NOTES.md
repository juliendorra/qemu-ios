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
identical across boards; it is not an M68AP gap.

**Verification boundary (be precise):** what is *observed* on M68AP is the
driver-ready state above (A–C, byte-identical to N45AP). Association (E),
DHCP (F), and Safari (G) are so far **inferred** for M68AP from the
board-agnostic model + N45AP's proven path, NOT yet observed end-to-end on
M68AP.

**Attempted the UI-driven Safari test on M68AP (2026-07-24) — BLOCKED, and
it revealed a bigger M68AP gap.** Ran the proven `ipod-https-acceptance.py`
harness (QMP taps + keyboard + screendump, `--select-wifi`) against a
booted M68AP. It failed at "Safari URL keyboard did not appear" because
**every screendump is black.** Investigation: M68AP reaches SpringBoard
(`SpringBoard[15]`) and attaches the whole display stack (`AppleH1CLCD`,
`IOMobileFramebufferUserClient`, `IOCoreSurfaceRoot`), but paints **no
framebuffer** — all bases 0% non-black and a full 0x08000000–0x10000000 RAM
scan shows the FB region empty (N45AP paints ~47% at 0x0f400000 under the
same conditions). Serial shows the device is **`[Unactivated]`**. On
iPhone OS 1.x an unactivated iPhone presents an activation screen (not the
home screen), and full activation needs the baseband (IMEI/ICCID) or a
hacktivation bypass. N45AP (iPod Touch) side-steps this — it activates
trivially and renders.

**Consequence / correction:** M68AP is NOT yet a visually usable device.
"Boots to SpringBoard with WiFi driver up" is true, but the screen is
black (unactivated), so the WiFi→Safari path can't be *driven or seen*
yet. Getting a usable M68AP home screen requires **activation** — either
the shelved baseband telephony stack, or an activation/SpringBoard bypass
("hacktivation"). This partially revises the earlier "telephony not
needed" conclusion: telephony (or a bypass) IS on the path to a usable
M68AP UI, even though the WiFi *driver* itself is board-agnostic and works.
See `IPHONE_2G_BRINGUP_HANDOFF.md`.

---

## iPhone OS 1.0 has NO Wi-Fi: it wants calibration from the DEVICE TREE, not the card (2026-07-28)

**Symptom.** On the 1.0 bundle Settings shows "Wi-Fi — No Wi-Fi", greyed out,
and no "Select a Wi-Fi Network" sheet ever appears when Safari loads a page.
1.1.4 and the iPod are fine. The Wi-Fi model is board-agnostic and unchanged,
so the difference is entirely in what each OS asks for.

**The two driver paths, from matched captures** (same launcher, same flags,
`S5L8900_DEBUG=1`; the fb-snapshot logs are useless for this because they start
after iBoot):

```
1.1.4 (works)                           1.0 (fails)
  AppleMRVL868x::probe                    AppleMRVL868x::probe
  AppleMRVL868x: probe                    -- nothing --
  AppleMRVL868x: Loading Bootstrapper     -- nothing --
  AppleMRVL868x: Reading EEPROM data      -- nothing --
  AppleMRVL868x: Starting                 AppleMRVL868x: Starting
  IO80211Interface::attach                AppleMRVL868x: Invalid calibration
                                                        data in device tree.
                                          start(SDIODeviceNub) <2> failed
```

**1.0's driver validates the device-tree calibration BEFORE it will bootstrap
the card.** It never loads the bootstrapper and never issues the EEPROM read —
so the `tx-calibration` record this model supplies over SDIO (the 128-byte
non-uniform payload in `mv8686_stage_eeprom()`) is never even requested. 1.1.4's
driver is the other design: it bootstraps the chip and reads calibration from
the card's own EEPROM, which is exactly the path the model implements. The iPod
behaves like 1.1.4 and has no baseband at all.

**Where the device-tree copy is supposed to come from: the BASEBAND.** Both
iBoots carry the feature — `strings` on 1A543a's and 4A102's `iboot-sb.bin`
both hit `Installing WIFI Calibration`, and in iBoot-159 it sits at 0x1a358
directly between `Read %d bytes from nvram in %ld usec.` (0x1a2f0) and
`Radio NVRAM Entries:` (0x1a318). That is the uart1 conversation already
documented in IPHONE_2G_BRINGUP_HANDOFF.md: iBoot's `AT+xdrv=9,1,0;` radio-NVRAM
read, which our stub answers `+XDRV: 9,1,0,0,NULL` — **zero bytes**. Hence
1.1.4's own log line, `Read 0 bytes from nvram in 1000397 usec.` (a 1 s
timeout), immediately followed by `Installing WIFI Calibration` installing
nothing usable. 1.1.4 does not care; 1.0 does.

Both device trees declare the same properties (`calibration`, `tx-calibration`,
`local-mac-address` are present in 1A543a's and 4A102's `DeviceTree.m68ap.bin`),
so this is not a DT schema difference — the property is there and empty.

**Not the cause, checked:** the SysCfg region is byte-identical between the two
bundles' NORs, so this is not a SysCfg difference. (The 1.0 NOR does carry far
fewer IMG2 containers than 1.1.4's — `dtre` only, versus dtre/batC/logo/nsrv/
batl/batL/recm — which is worth a look on its own, but the calibration does not
come from a NOR container.)

**Open link, stated honestly:** that the 1.0 DT property is empty *because* the
radio NVRAM came back NULL is inferred, not measured. iBoot-159 logs almost
nothing in our setup (its output stops after `Reading 8900 header`), so the
absence of `Installing WIFI Calibration` from the 1.0 capture is NOT evidence it
skipped the step. What is measured is that the driver rejects whatever is
there.

**Cheapest experiment for whoever picks this up.** The rules mechanism already
exists: `IT_BASEBAND_RULES` + `scripts/baseband-rules/*.rules`, where
`eager.rules` line 3 currently answers
`at+xdrv=9,1,` with `+XDRV: 9,1,0,{int},NULL`. Replace the `NULL` with a
real-shaped radio-NVRAM payload carrying a WiFi calibration record whose CRC
satisfies iBoot (it prints `WIFI Calibration Data (crc %u)`), boot 1.0, and see
whether `Invalid calibration data in device tree` goes away. If it does, the fix
belongs in the baseband stub, not in the Wi-Fi model. Note the record must not
be all-0x00 or all-0xff — the same uniformity check that
`mv8686_stage_eeprom()` already works around applies here.

### SOLVED (2026-07-28): two zero-filled device-tree properties, no baseband work needed

The section above proposed fixing this in the baseband stub. **That was the
wrong direction, and the question "why does 1.1.4 work then?" is what exposed
it.** 1.1.4 gets exactly the same NULL radio NVRAM and its Wi-Fi is fine —
proved by booting 1.1.4 with `silent.rules`, a baseband that answers *nothing*:
`Loading Bootstrapper` → `Reading EEPROM data` → `Starting` → attaches. So the
device-tree calibration is not what makes Wi-Fi work; it is only what 1.0's
driver *gates* on. Feeding iBoot a real radio NVRAM would have been a large
detour to fix a check, not a dependency.

The actual state of the properties, read straight out of both DeviceTree blobs:

```
1A543a   tx-calibration    len=1024   all-zero
4A102    tx-calibration    len=1024   all-zero      <- identical
both     local-mac-address len=6      all-zero  (x3 nodes)
```

Both firmwares ship the same empty properties. 1.1.4 never looks; 1.0 checks
and refuses. The rejection is the *same uniformity rule* the SDIO model already
works around in `mv8686_stage_eeprom()` — "all 0x00 or all 0xff is invalid".

**The fix: fill them.** The device tree sits in the NOR `dtre` container in
plaintext (`tx-calibration` is at 0x139c4 in the 1.0 NOR), and nothing verifies
it at that granularity, so patching the property in place is enough. Done on a
NOR *copy* and booted with `S5L8900_NOR=`, 1.0 clears both gates in turn:

```
tx-calibration := non-uniform 1024 bytes
  -> "Invalid calibration data in device tree."  GONE
  -> new gate: "AppleMRVL868x: MAC Address is all 00's."
local-mac-address := 02:1a:11:e0:86:86+n  (locally administered, per node)
  -> AppleMRVL868x: Starting
     IO80211Controller::attachInterfaceWithMacAddress called!
     IO80211Interface::attach(AppleMRVL868x)
     IONetworkStack::attach(IO80211Interface)
     AppleMRVL868x: Ethernet address 02:1a:11:e0:86:87
```

Confirmed in a clean `fb-snapshot.py` run with the patched NOR: the interface
attaches AND the home screen still renders normally, so the DT edit costs
nothing elsewhere.

**Where this belongs permanently.** The NOR is generated
(`scripts/build-m68ap-nor.py`), so the property fill belongs there, applied to
every build — it is harmless for 1.1.4/1.1.1 (which ignore the DT copy) and it
also gives them a real MAC in the device tree instead of zeros. It is NOT a
guest-image modification: it fills firmware fields that on real hardware are
populated from the phone's own radio NVRAM, which an emulator has no source
for. The synthetic values must stay obviously synthetic (locally administered
MAC, non-uniform calibration) and be declared in the firmware provenance like
every other generated-image edit.

**Still true, and still the honest limit:** these are *fabricated* calibration
bytes. They satisfy the driver's sanity check; they are not real RF calibration
and nothing in the emulated radio consumes them.
