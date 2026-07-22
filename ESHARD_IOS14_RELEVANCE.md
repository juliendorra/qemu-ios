# Relevance of eShard's iOS 14 QEMU Work to the S5L8900 Project

Date reviewed: 2026-07-22

## Executive summary

eShard's iOS 14 work is useful to this project primarily as a debugging and
bring-up methodology. It is not a practical source of device-model code for
the iPod Touch 1G or original iPhone.

The architectural gap is substantial. eShard started from the `qemu-t8030`
work for a modern 64-bit iPhone platform and dealt with Pointer
Authentication, SEP-dependent services, AMX instructions, Metal, compressed
IOSurfaces, the modern dyld shared cache, and USB pairing. This repository
models the 32-bit ARM1176/S5L8900 platform used by N45AP and M68AP, with the old
VROM/LLB/iBoot chain, NOR, Whimory NAND/FTL, and the original 320 x 480 display
path.

Consequently:

- Direct code and hardware-model reuse is very low.
- The log-led binary-analysis loop, physical-buffer inspection, reversible
  patch discipline, and minimal peripheral/service stubbing are highly
  relevant.
- The decisive comparison was not with iOS 14 but with this repository's
  working N45AP path. It exposed the real board difference: N45AP advertises
  eight NAND chips while M68AP advertises four. Once the controller and
  constructor used those board-specific layouts, M68AP clean-opened FTL,
  validated both HFS B-trees, and mounted `disk0s1` as root.
- eShard's system-service triage is relevant only after that legacy reference
  path. It may help organize activation, CommCenter, and SpringBoard work, but
  none of its exact iOS 14 patches should be carried over.
- The modern restore and reverse-tethering paths do not bypass the current
  S5L8900 NAND write/persistence limitation.

The recommended course is therefore to continue the present S5L8900 design
and borrow eShard's observational workflow, not to port or merge `qemu-t8030`.

## Sources reviewed

- [Emulating an iPhone in QEMU - Part I](https://www.eshard.com/blog/emulating-ios-14-with-qemu),
  Georges Gagnerot, April 2025.
- [Emulating an iPhone in QEMU - Part II](https://www.eshard.com/blog/emulating-ios-14-with-qemu-part2),
  Axel Cohen, June 2025.

The posts describe the work and refer to several upstream projects, but many
of eShard's patching and diffing tools are internal. They are not a drop-in
patch series for this repository.

## Architectural comparison

| Area | eShard iOS 14 target | This project |
|---|---|---|
| CPU and ABI | 64-bit modern Apple platform, including arm64e/PAC behavior | ARM1176, ARMv6, 32-bit ARM/Thumb |
| Boards | T8030-era iPhone emulation, with experiments presenting older chip identities | N45AP iPod Touch 1G and M68AP original iPhone |
| Boot dependencies | SEP/keybag dependencies, PAC, modern signature enforcement | S5L8900 firmware epoch, IMG2 validation, AES/SHA engines, original boot policy |
| Storage | Modern restore-oriented stack | NOR plus Whimory NAND/VFL/FTL and HFS+: eight active banks on N45AP, four on M68AP |
| Graphics | IOSurface, IOMFB, compressed surfaces, Metal/AMX dependencies, software-rendering patches | Working S5L8900 LCD/framebuffer path shared by the iPod and iPhone profiles |
| Input | Modern IOHID/AppleMultitouchDevice registration and injected events | Zephyr2 on N45AP and a modeled Zephyr1 SPI protocol on M68AP |
| Userspace | backboardd, FrontBoard, PreBoard, SpringBoard, modern dyld cache | iPhone OS 1.x IOKit, launchd, CommCenter, SpringBoard, and launch-era activation |
| Networking | Modern USB pairing, reverse tethering, CDC NCM and userspace tools | Emulated Marvell SDIO Wi-Fi and host HTTP/HTTPS bridge; older USB stack |

The platforms share QEMU, XNU ancestry, IOKit concepts, device trees, Apple
boot components, and a need to tolerate missing hardware. Those similarities
make the investigative techniques portable, but they do not make the device
registers, drivers, boot patches, or service patches portable.

## Findings that are directly useful

### 1. Trace the failing data path, not only the final error

In the display investigation, eShard first established that IOMFB was detected
and that planes were configured. It then dumped the physical DMA-backed
surfaces and interpreted the buffers independently. This distinguished
"display hardware exists" from "the guest produced a usable surface."

That method was applied to the M68AP storage path, but the closest working
system supplied the answer before deeper `FTL_Open` disassembly was needed.
The N45AP controller-identification trace showed eight valid NAND IDs and a
1024-page superblock. M68AP firmware expects four valid IDs, four absent slots,
and a 512-page superblock. The emulator had incorrectly advertised eight
identical chips to both boards.

After making NAND identification board-aware and constructing M68AP storage
for four active banks, the real 4A102 kernel:

- clean-opens AppleNANDFTL without `_FTLRestore`;
- receives the real extents B-tree header instead of a zero buffer;
- validates the extents and catalog B-trees;
- returns zero from `_vfs_mountroot`; and
- reports `BSD root: disk0s1`.

This replaces the earlier theory that an unknown M68AP-only FTL context check
was the remaining storage variable. The context bytes were valid; the
controller topology made the same logical block map differently.

The repository already contains the correct foundation:

- `scripts/analyze-m68ap-ftl-open.py` reproducibly locates the stripped
  `FTL_Open` path and verifies the physical metadata page.
- `scripts/m68ap-ftl-trace.py` runs a bounded staged boot and emits a JSON
  result.
- `contrib/plugins/m68ap-ftl-trace.c` records executed basic blocks and stops
  at the clean-open or restore verdict.

The reusable outcome is the paired observation itself: trace the same semantic
event on the working iPod and the iPhone, including controller identification,
derived geometry, logical request, physical bank/page, and returned bytes.
That evidence is more reliable than assuming the two S5L8900 boards have
identical storage topology.

### 2. Use the closest working system as an oracle

eShard compared emulated output with physical devices and compared behavior
between different iPhone generations. That revealed that the newer target was
feeding compressed surfaces to the GPU while the older device exposed raw
surfaces.

For this project, the working N45AP machine is an especially strong oracle:
it uses the same S5L8900 SoC, NAND geometry, Whimory family, and QEMU device
models. Comparisons should be performed at equivalent observable boundaries:

- NAND controller commands and bank/page translations;
- VFL and FTL context selection;
- copied in-memory context fields;
- table-read order and return status;
- the clean-open branch sequence.

A physical iPhone 2G can remain an optional comparison oracle if one is
available, but it must not become a required firmware source or a prerequisite
for reproducible construction from a user-supplied IPSW.

### 3. Keep patches explicit, reversible, and version-checked

eShard moved away from opaque QEMU-side kernel patching and built a textual
patch workflow that made each modified binary and purpose reviewable. The
general lesson fits this repository's firmware policy even though eShard's
tools and patches are not reusable here.

The S5L8900 project should continue to require:

- staged NAND, NOR, and iBoot copies for all diagnosis;
- hashes and firmware/build identity for every input;
- verification of original opcodes before any guest-memory patch;
- a clear distinction between diagnostic skips and production emulation;
- machine-readable reporting of which patches were applied;
- no committed Apple-derived patched image.

The M68AP trace plugin already follows much of this discipline: its optional
USB/SDIO startup skips verify the original prologue, modify guest memory only,
and identify themselves as diagnostic. Those skips must not be mistaken for
the final device-model implementation.

### 4. Minimal stubs are valuable when they preserve the real interface

eShard often implemented or enabled only enough hardware for an IOKit service
to register, then used a higher-level path to continue testing. This is useful
when the goal is to isolate the next gate, provided the stub preserves the
guest-visible protocol and missing behavior is documented.

This project already applies that idea more faithfully for M68AP:

- the S-Gold2 UART stub acknowledges the baseband AT interface;
- the ISL29003 supplies the register behavior needed by its real driver;
- the Zephyr1 model implements the original controller's boot and runtime SPI
  protocol and reuses QEMU's existing input path.

If later bring-up exposes another nonessential peripheral, a minimal
board-gated stub is preferable to patching a shared N45AP path. A one-run
driver/service disable can still be useful to prove causality, but it should be
reported as diagnostic and replaced with a real interface stub where
practical.

### 5. System-service logs become important after root mount

In Part II, eShard used system logs to determine that data migration, SEP
calls, CommCenter, power management, backlight handling, and residual Metal
calls were blocking visible SpringBoard progress. The exact services and
patches are iOS 14-specific, but the triage order is portable:

1. Establish an ordered boot milestone in logs.
2. Identify the first service that is repeatedly crashing or waiting.
3. Reverse only that service and its immediate framework dependency.
4. Temporarily stub or disable it to prove that it is the gate.
5. Implement the smallest faithful emulated interface or documented local
   provisioning path.
6. Add the observation to the scripted acceptance case.

For iPhone OS 1.x, this becomes relevant after `AppleNANDFTL` attaches and the
root filesystem mounts. Likely areas include launch-era activation and
CommCenter/baseband behavior. The existing S-Gold2 stub should be validated
against the real driver before disabling CommCenter. If a one-run CommCenter
disable allows SpringBoard to proceed, that is evidence about the stub rather
than a proposed production configuration.

## Findings that may be useful later

### Framebuffer validation

The iPod's display path already works and M68AP shares the S5L8900 display
foundation, so eShard's IOMFB and compressed-IOSurface implementation is not
needed. If the iPhone reaches SpringBoard but remains black, its method is
still valuable: capture the programmed framebuffer address, dump physical
memory, interpret it independently as 320 x 480 x 32-bit pixels, and determine
whether the failure is rendering, scanout, pixel format, or power/backlight
state.

### Touch service comparison

eShard compared `ioreg` output between the emulator and a real device to find
the missing service chain leading to `AppleMultitouchDevice`. A similar
registry comparison could help if the M68AP Zephyr1 driver does not publish
its expected service after root mount. However, the current modeled Zephyr1
wire protocol is preferable to replacing it with a fake modern digitizer
service.

### Userspace debugging

Disabling dyld-cache ASLR was transformative for eShard's modern environment.
iPhone OS 1.x has a different userspace and does not present the same arm64e
dyld-cache problem, so its exact patches do not apply. The broader idea remains
useful: use stable addresses and host-side symbols or extracted binaries where
possible, and avoid repeatedly attaching to a service if QEMU can capture the
needed boundary with less timing disturbance.

## Techniques that should not be imported

### Modern CPU and security work

The PAC, arm64e, A13 instruction, SEP/keybag, biometric, and modern code-signing
work solves problems that do not exist in the S5L8900/iPhone OS 1.x execution
environment. Porting it would add complexity to shared QEMU code without
advancing either N45AP or M68AP.

### Metal, AMX, and compressed IOSurfaces

The iOS 14 software-rendering patches, AMX fallbacks, Metal-context checks, and
compressed-surface workaround belong to a much newer graphics stack. The
working N45AP framebuffer is the correct reference for M68AP.

### Spoofing a different chip identity

eShard obtained usable uncompressed surfaces by presenting an older chip ID
to parts of iOS 14. This was a productive diagnostic for that graphics stack,
but it would be dangerous as an M68AP strategy. This project has already
demonstrated that M68AP/N45AP identity changes the security epoch, DeviceTree,
NAND metadata, touch protocol, baseband, sensors, and GPIO behavior. Presenting
N45AP identity could hide the real iPhone-specific defect and create a mixed
artifact boot that cannot serve as a correctness oracle.

Device-tree or identity changes are acceptable only as bounded, staged
diagnostics with an explicit hypothesis and a matching negative control.

### Modern USB restore and reverse tethering

eShard's companion-QEMU restore and USB reverse-tethering path depends on a
modern USB/pairing/network stack. It does not solve this repository's current
storage limitation: NAND program/erase writes are captured in `_new.page`
files but are not replayed by reads. Therefore `_FTLRestore`, DFU restore, and
`asr` cannot create a persistent first-boot state until the NAND model supports
complete program, erase, overlay readback, and reboot persistence.

Likewise, modern CDC NCM reverse tethering is not a replacement for the
existing S5L8900 SDIO Wi-Fi work. It could only be reconsidered after the old
USB device behavior has been established independently.

### Exact userspace patches

Patches for PreBoard, PurpleBuddy, BackBoardServices, FrontBoard,
SpringBoardFoundation, mobileactivationd, QuartzCore, and modern CommCenter
are tied to iOS 14 binaries and service architecture. At most, they identify
categories of gates to look for in iPhone OS 1.x logs.

## Recommended application to the current M68AP work

### Immediate priority: follow the working N45AP startup sequence

The paired N45AP/M68AP storage observation is complete. Preserve its proven
board distinction: N45AP advertises eight NAND chips; M68AP advertises four.
Keep the constructor, controller identification, derived superblock size, HFS
reads, and root-mount gates in one scripted regression. Do not resume
`FTL_Open` disassembly unless a future run actually regresses before clean-open.

The current frontier is after root mount. A detailed instrumented M68AP run
continues through substantial driver setup, while lighter runs expose startup
ordering failures as USB, SDIO, and then baseband request legacy GPIO platform
functions. A blanket "platform functions unavailable" change is invalid: it
breaks the common S5L8900 platform expert. The next comparison should therefore
observe the working N45AP GPIO function-parent publication and consumer order,
then compare the corresponding M68AP USB/SDIO/baseband requests.

### After root-device attachment

Keep `scripts/iphone-nand-acceptance.py` reporting ordered gates for:

1. root mount;
2. launchd;
3. SpringBoard configuration or equivalent startup marker;
4. framebuffer activity/screenshot;
5. CommCenter stability;
6. Zephyr1 service registration and one injected touch;
7. activation/provisioning state.

If progress stops, use one scripted boot to collect serial/system logs and
process-crash evidence. Only then add a bounded diagnostic service skip or
device stub, and preserve the N45AP regression in the same batch.

### Longer-term fidelity

Implement NAND program, erase, readback overlay, and persistence as a separate
track. Once that exists, restore/format behavior can become a meaningful test
rather than a workaround for clean-open. Keep real DFU restore optional and do
not make it a prerequisite for reproducible IPSW-derived first boot.

## Final assessment

The eShard work is not too different to be useful, but its useful layer is the
method rather than the implementation. The most valuable pattern is:

> establish a guest-visible milestone, trace the real executed path, inspect
> the exact physical/in-memory data at the failing boundary, compare it with
> the closest working oracle, and apply only a reversible diagnostic change
> before retesting the whole case.

That pattern already matches this repository's proven debugging loop. Applied
first to the working iPod, it found the four-bank/eight-bank controller
difference and cleared M68AP storage through root mount. The same method should
now be used at the legacy IOKit startup boundary. The iOS 14 implementation is
not a design authority for that work; N45AP behavior and the iPhone OS 1.x
DeviceTrees/drivers are.
