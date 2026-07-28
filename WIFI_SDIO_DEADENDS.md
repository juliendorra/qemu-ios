# Wi-Fi / SDIO bring-up: attempts, dead ends, and difficulties

A companion to `WIFI_SDIO_NOTES.md` (which records the *findings*). This
file records the *process* — the approaches that failed, the wrong turns,
the unresolved mysteries, and the tooling difficulties — so the next
person does not repeat them. Roughly chronological.

## 1. Finding the plan

- The plan is **not** in the Padipop app repo. It lives in this QEMU emulator
  repo in `SLEEP_WAKE_INVESTIGATION.md`, section "Wi-Fi
  Feasibility and Implementation Path". Searching the Padipop repo for
  "wifi bridge" returns nothing.

## 2. Locating the Apple driver in the NAND (dead end)

- `strings` over the NAND banks surfaces `AirPort`/`marvell`/`WiFi`
  strings, but the individual `*.page` files do **not** contain
  `readEEPROM` / `AppleMRVL868x` contiguously — the kernelcache is
  FTL-mapped and/or compressed on the NAND, so per-page grep fails.
- The driver strings are only readable from the **running** guest (serial
  log) or a **RAM dump**, not from the NAND at rest.

## 3. Card enumeration wrong turns

- **FBR interface code 0x00** (vendor-specific) made the driver probe
  FBR1..FBR7 and give up. The driver keys on the *standard* interface
  code; **0x07 (WLAN)** is what makes it enable fn1 and proceed.
- **First firmware-download state machine was wrong.** After the helper
  it went straight to a `MV8686_DL_MAIN` state and treated *every*
  subsequent I/O-port write as raw main-firmware bytes. That silently
  swallowed the 16-byte EEPROM request and fired a bogus
  `firmware boot handshake complete (main 16 bytes)` log line. The fix
  was a distinct `MV8686_EEPROM_CMD` / `MV8686_EEPROM_READ` phase between
  helper and main firmware.

## 4. The RD_BASE "ready status" sweep

`AppleMRVL868x::readEEPROM` polls RD_BASE (fn1 0x10/0x11) and would not
proceed. It took an empirical sweep of the value the card reports there
after the helper boots to discover the semantics:

| RD_BASE reported | Driver serial result |
|---|---|
| 0x800 (initial guess) | `Timed out wating for ready status from helper firmware` |
| 0x0001, 0x0002 | timeout (same) |
| 0x0080, 0x0100, 0x0200 | timeout (same) |
| 0xFEDC | timeout (same) |
| 0x0000 | past ready, but `No transmit length from helper image` |
| **0x0010** | **proceeds** — sends a 16-byte CMD53 request, reads EEPROM |

Conclusion: RD_BASE is the card's *download-request size*; readEEPROM
waits for it to equal its own 16-byte request size. Early "flag in the
high bits" hypotheses were wrong; it is a plain size match.

## 5. Kernelcache byte-search approaches (dead ends)

Goal: read `AppleMRVL868x::readEEPROM` to learn the EEPROM/calibration
payload layout. Dumped 128 MB of guest RAM via QMP `pmemsave`
(`scripts/wifi-dev/dump-ram.py`) at the moment the driver logs
`Reading EEPROM data`. Then tried to locate the function:

1. **Absolute literal-pool search, base VA = fileoff + 0xC0000000.**
   Found the log strings, but **zero** 4-byte words pointing at them.
2. **Recomputed with the `__PRELINK` segment skew** (vmaddr − fileoff =
   0xC009F000, since prelinked kexts live in `__PRELINK`). Still **zero**
   refs.
3. **Brute-force skew detection**: two log strings are 0x9C apart and
   used by the same function, so searched for pointer-word *pairs*
   differing by exactly 0x9C. Result: only coincidental single hits, **no
   consistent skew** → the strings are not referenced by absolute
   pointers at all.
4. **Mangled C++ symbol search** (`ZN13AppleMRVL868x…`): **0 hits** — the
   kernelcache is symbol-stripped.

Root cause: the kext references its strings via **PC-relative `ADR`**
(position-independent code), so there is no absolute pointer to grep for.
Locating the function needed ARM-aware analysis rather than another byte
search. `scripts/wifi-dev/find_readeeprom.py` captures approaches 1–2 for
reference; these attempts were not evidence that the format was unknowable.

## 6. EEPROM content experiments (dead ends)

- Serving a synthetic EEPROM (MAC + zeros) makes the driver read the
  payload and then **kernel-panic**: `kernel abort type 4 … far=0x10c`,
  `Fatal Exception`, immediately after `Reading EEPROM data`. The zeroed
  blob parses into a bad structure and a near-null (+0x10c) dereference.
- An **all-0xFF** fill experiment was inconclusive: the boot's QMP port
  collided with a stray QEMU from a prior run and produced empty logs
  (see difficulties below). Not retried — content guessing is
  low-probability without the real format.

## 7. Research to confirm the format is Apple-proprietary

- Linux **libertas** (`if_sdio.c`, `if_sdio.h`, `host.h`, `defs.h`):
  gave the exact fn1 register map, firmware framing, command IDs, and
  packet types — all used in the model — but has **no EEPROM-read step**.
- **iphonelinux** `openiboot/wlan.c` (same 8686 hardware): also loads
  helper + main firmware with **no EEPROM step**.
- Therefore `readEEPROM` is specific to Apple's `AppleMRVL868x`; the format
  ultimately had to be recovered from the Apple driver.

## 8. Resolution and later protocol traps

ARM-aware analysis recovered the parser's format: an eight-byte
`de ad 00 04 be ef ca fe` header followed by big-endian
`[key][word count][data]` records. Key 1 is calibration and key 2 is the
MAC address. The earlier 7904-byte read was also explained: scratch 0x34/35
is the signed EEPROM response length at that stage, so leaving the later
`0xFEDC` firmware-ready marker there was a state-machine bug.

Three more plausible-looking implementations were wrong and are worth
calling out:

- SDIO event IDs are raw in the 8686 mailbox; shifting by three is from the
  older 8385 register event path and breaks the deep-sleep awake event.
- ASSOCIATE response command ID is the legacy exception `0x8012`, not
  `0x8050`; the latter makes Apple time out the otherwise successful join.
- SLIRP returns the first DHCP Offer synchronously while Apple's ASSOCIATE
  command is still queued. Delivering it immediately loses the Offer in the
  old network stack; it must be released after the association/link packets.

## 9. Previously unresolved questions

- The 7904-byte read was the signed interpretation of stale scratch value
  `0xFEDC`; the correct response length is `0x0800`.
- The full field-level meaning of the fixed 16-byte request remains
  unneeded; the exact request and response contract are captured.
- Apple's firmware mailbox was exercised through scan, authenticate,
  associate, deep sleep/wake, Ethernet TX/RX, DHCP, ARP, TCP, and HTTP.

## 10. Tooling / process difficulties

- **Stray QEMU processes**: parallel boots on fixed QMP ports collided;
  one run left a QEMU holding port 4494 and produced empty logs. Needed a
  `pkill -f "qemu-system-arm.*iPod-Touch"` between runs. Future harnesses
  should allocate unique ports and clean up children reliably.
- **`git add -A` swept the build directory**: the first commits
  accidentally added the entire `build-ipod/` tree (3042 files). Caught
  during review, reset the branch, added `/build-ipod/` to `.gitignore`,
  and recommitted source-only. **Side effect: the 5 granular progression
  commits were collapsed into 1**, so the incremental history (skeleton →
  mailbox → EEPROM RE → gating) survives only in this document and
  `WIFI_SDIO_NOTES.md`, not as separate commits.

## 11. Outcome

The blocker was resolved without a hardware EEPROM dump. The launched dev
build now associates, obtains `10.0.2.15` from SLIRP, and loads a host-served
HTTP page in Safari. Modern HTTPS remains an application-layer limitation of
the 2007 browser; an HTTP reverse proxy on the host is the practical bridge
to contemporary TLS sites.

## 6. Feeding iBoot a radio-NVRAM payload: the reply format is NOT the obvious one (2026-07-28)

Goal: make 1.0's Wi-Fi work by answering iBoot's `AT+xdrv=9,1,0;` radio-NVRAM
read with real calibration instead of `NULL`, so `Installing WIFI Calibration`
has something to install (see WIFI_SDIO_NOTES.md for why 1.0 needs the
device-tree copy and 1.1.4 does not).

**Two things this DID establish, and they are worth keeping.**

1. **iBoot-159 really does perform the read.** Traced with
   `IT_BASEBAND_TRACE` on a 1.0 boot: `AT+xdrv=9,1,0;` at t=2.72 s and again at
   t=4.72 s, each answered `+XDRV: 9,1,0,0,NULL`, then one `AT+cgsn;`. So the
   "1.0 gets no calibration because the stub returns NULL" chain is measured at
   the iBoot end now, not inferred. (1.0's iBoot prints almost nothing, which is
   why this needed the uart trace rather than the serial log.)

2. **1.1.4 is the instrumented oracle for this work.** Its iBoot-204 prints
   `Read %d bytes from nvram in %ld usec.`, which is the only feedback signal
   available; 1.0 prints nothing. So iterate the reply format against 1.1.4,
   and only then check 1.0 for the driver message. The lab loop is
   `IT_BASEBAND_RULES=<file>` + `S5L8900_DEBUG=1` on the bundle launcher;
   the nvram read happens ~2.7 s into guest time, so a probe needs well under a
   minute of boot, not a full run to SpringBoard.

**What was falsified.** Baseline (`...,0,NULL`) is *accepted*: one 1 s retry
cycle, then `Read 0 bytes` and iBoot moves on. Every attempt to put the payload
after the fourth comma was *rejected* — iBoot re-asked at 1 Hz until its 5 s
window expired:

| reply after `+XDRV: 9,1,` | payload | result |
|---|---|---|
| `0,0,<hex>` | 64 B hex | 5.0 s of retries, `Read 0 bytes` |
| `0,64,<hex>` (len = bytes) | 64 B hex | 5.0 s, `Read 0 bytes` |
| `0,128,<hex>` (len = chars) | 64 B hex | 5.0 s, `Read 0 bytes` |
| `0,0,"<hex>"` (quoted) | 64 B hex | 5.0 s, `Read 0 bytes` |
| `{int},64,<hex>` | 64 B hex | 5.0 s, `Read 0 bytes` |
| `0,8,<hex>` | **8 B hex** | **2.0 s**, `Read 0 bytes` |

**The one real clue: the retry count tracks payload length** (NULL → 1 s, 8 B →
2 s, 64 B → 5 s = the cap). iBoot is consuming the reply, not ignoring it, and
something length-dependent goes wrong. Hex-after-the-comma is the wrong
transport.

**Stop guessing; disassemble.** The remaining hypotheses (raw bytes after the
header line rather than comma-separated, a different field order, a chunked
multi-block protocol) are cheap to state and expensive to test one boot at a
time. The right next step is ARM analysis of iBoot-159 around the string
literals already located: `+xdrv=9,1,` (0x1a2b0), `%s%d` (0x1a2a8),
`radio_get_atreply(): TIMEOUT!` (0x1a27c), `Failed to read block %d out of the
radio.` (0x1a2c4), `Read %d bytes from nvram` (0x1a2f0), `Radio NVRAM Entries:`
(0x1a318), `  Type: 0x%04x  Size: 0x%04x  Purpose:` (0x1a330). That last one
also gives the *store* format for free once the transport is known: a record
stream of `[u16 type][u16 size][data]`.

Note the rules engine cannot express arbitrary binary today — `sgold2_unescape()`
handles only `\r \n \t \\`, so a raw-byte transport needs a `\xNN` escape added
to `hw/arm/ipod_touch_baseband.c` first.
