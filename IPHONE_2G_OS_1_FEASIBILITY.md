# Original iPhone / iPhone OS 1.0 Feasibility

## Verdict

An original-iPhone UI demo is feasible and should be implemented as a sibling
board profile, not as a separate emulator and not as a firmware swap in the
current iPod profile.

Confidence is high for reaching SpringBoard and demonstrating the 2007 Phone,
SMS, iPod, Safari, and Settings UI with `No Service` or a deterministic fake
carrier. Confidence is lower for authentic activation, Visual Voicemail, and
live GSM: those depend on the separate baseband and carrier services and are
not required for a useful museum/demo build.

The strongest architectural evidence is the libimobiledevice device table:
the original iPhone is `iPhone1,1` / `m68ap` / CPID `0x8900`, while the first
iPod touch is `iPod1,1` / `n45ap` / the same CPID `0x8900`.
[Source](https://cgit.libimobiledevice.org/libirecovery.git/tree/src/libirecovery.c?h=1.2.0&id=624f83fa5fad7bd6ac2f8dc1d7076f67e5ef4d39)
The historical iPhoneLinux/OpeniBoot project also supported the first iPhone
and first iPod touch in one hardware port, including shared framebuffer,
interrupt, MMU, clock, serial, USB-serial, and NAND work.
[Source](https://linuxoniphone.blogspot.com/2008/11/linux-on-iphone.html)

## Target identity

- Device: original iPhone, later commonly called iPhone 2G
- Product type: `iPhone1,1`
- Board: `M68AP`
- SoC family / CPID: S5L8900 / `0x8900`
- Launch firmware: version 1.0, build `1A543a`
- Restore identity: `iPhone1,1_1.0_1A543a_Restore.ipsw`
- Known SHA-1: `fb8bb3ee2e9a997affbb97868599f2995c78209c`

The version/build/filename/hash are independently catalogued in the historical
firmware table.
[Source](https://www.theiphonewiki.com/wiki/Firmware/iPhone/1.x)
Apple confirms the original iPhone was introduced in 2007, and its launch
announcement describes the phone, widescreen iPod, Internet, multi-touch, GSM,
EDGE, Wi-Fi, Bluetooth, proximity, and ambient-light behavior that the demo
should represent.
[Original-iPhone support page](https://support.apple.com/en-ph/docs/iphone/131510),
[Apple launch announcement](https://www.apple.com/newsroom/2007/01/09Apple-Reinvents-the-Phone-with-iPhone/)

Apple's launch-era material called the device software simply iPhone software
or described Apple's OS X platform; `iPhone OS 1.0` is the clearest modern
project label. Apple later used the iPhone OS product name officially.

## What can be reused

The current `iPod-Touch` machine already supplies most of the expensive base:

- 32-bit ARM/S5L8900 execution and the current memory map
- dual VIC, timers, GPIO, PMU, I2C, SPI, NAND/NOR, AES/SHA, USB, LCD, and touch
- 128 MiB RAM and a 320 × 480, 32-bit framebuffer path
- bootrom, iBoot, NOR, NAND, and app-bundle loading infrastructure
- macOS packaging, input mapping, framebuffer snapshots, and sleep/wake repair

The present bundle contains only N45AP artifacts (`iboot_204_n45ap.bin`,
`nor_n45ap.bin`, and its NAND tree). No M68AP/iPhone assets or board profile are
present, so the existing bundle cannot become an iPhone by changing its name.

## Missing work

### Board and boot chain

- Add an `iPhone-2G`/M68AP machine profile sharing a common S5L8900 core with
  the N45AP profile.
- Load M68AP bootrom/iBoot/NOR/NAND/kernel/device-tree data from user-selected
  artifacts and validate their product/build/hash before boot.
- Move every guest instruction patch and address in the sleep/wake work into a
  firmware profile. N45AP addresses must never be applied blindly to 1A543a.
- Identify M68AP GPIO polarity/numbering and board-specific PMU, audio, camera,
  proximity, ambient-light, vibration, and radio-facing devices.

### Activation and baseband

The launch workflow required iTunes/AT&T activation.
[Apple's June 2007 activation announcement](https://www.apple.com/newsroom/2007/06/26Apple-and-AT-T-Announce-iTunes-Activation-and-Sync-for-iPhone/)
The demo therefore needs either a legally obtained already-provisioned NAND or
a local, documented demo/provisioning path. It should not depend on the
long-retired carrier activation service.

The cellular baseband is a separate system. The first milestone should stub
the application-processor interface just far enough for CommCenter and
SpringBoard to remain healthy and show `No Service`. Historical OpeniBoot work
listed baseband support as missing even after common first-iPhone/first-iPod
hardware was running, which is useful evidence that baseband is the distinct
risk rather than the ARM/S5L8900 core.
[Source](https://linuxoniphone.blogspot.com/2008/11/linux-on-iphone.html)

For the requested demo, deterministic fake calls, contacts, SMS, signal bars,
and voicemail are much faster and safer than emulating a real GSM modem or
connecting to a cellular network. Apple described launch service as including
SMS and Visual Voicemail, so these are the right visible demo targets.
[Source](https://www.apple.com/newsroom/2007/06/26AT-T-and-Apple-Announce-Simple-Affordable-Service-Plans-for-iPhone/)

## Artifact policy

Do not commit or redistribute Apple firmware, decrypted images, keys, or an
activated NAND. The repository should contain only:

- loaders and extraction/conversion tooling
- a manifest of expected filenames, sizes, and hashes
- instructions for user-supplied, legally obtained IPSW/device dumps
- generated empty/demo state that contains no Apple code

An IPSW alone may not match the raw bootrom/NOR/NAND layout expected by the
current machine. The loader should report exactly which components can be
derived and which must come from a user's device dump.

## Fastest implementation plan

1. Preserve the now-working N45AP baseline with scripted boot, P, H, click, and
   screenshot checks.
2. Refactor `ipod_touch_machine_init()` into a shared S5L8900 machine plus
   explicit N45AP and M68AP board descriptors. Keep behavior unchanged first.
3. Add an M68AP artifact manifest and inspection tool for a user-selected
   `iPhone1,1` 1A543a IPSW/dump; reject mismatched builds.
4. Boot M68AP through iBoot/kernel and reach SpringBoard with firmware-specific
   compatibility patches kept in a separate, guarded table.
5. Add minimal baseband/activation stubs for stable `No Service` UI.
6. Add deterministic demo telephony: incoming-call screen, call timer,
   contacts, SMS threads, voicemail list, and carrier/signal state.
7. Only then consider camera, audio routing, proximity behavior, networking,
   or a more complete baseband protocol.

## Go/no-go criteria

Proceed once a lawful M68AP artifact set is available. The first go milestone
is iBoot output plus a recognizable M68AP device tree; the second is a kernel
serial log; the third is SpringBoard with Phone and SMS icons. Stop and reassess
only if the required 1A543a boot components cannot be derived or dumped, not
because baseband is absent—the UI demo deliberately does not require real GSM.
