# QEMU 11 Forward-Port Journal

This document is the working record for moving the iPod Touch 1G machine from
the current QEMU 6.2 tree to QEMU 11.0.2. It records successful milestones,
failed theories, disposable diagnostics, and the promotion criteria. The
packaged application was kept as the correctness oracle until the GUI and
power-lifecycle checks below passed on 2026-07-17.

## Repositories and branches

- Correctness oracle: `ipod_touch_1g` in the main repository.
- Port worktree: `/private/tmp/qemu-11-port`.
- Port branch: `codex/qemu-11-port`, based on upstream tag `v11.0.2`.
- Initial machine/API port: `697306b42c`.
- NAND DMA request fix: `78a43a0d56`.
- SPI2 transmit DMA request fix: `efd9ab8b54`.
- SDL absolute-pointer capture fix: `f734de901e`.
- Installed application: QEMU 11.0.2 at clean port revision `f734de901e`,
  promoted only after cold input, manual sleep/wake, timed sleep/wake, and two
  consecutive retained-wake cycles passed against the packaged binary.

The main repository's untracked `roms/edk2` directory is user-owned and is not
part of this port.

## Build configuration

The experimental engine is a native arm64, SDL, LTO-enabled `arm-softmmu`
build. The exact command is also maintained in `BUILD.md`:

```bash
cd /private/tmp/qemu-11-port/build-ipod
../configure \
  --python=/path/to/python3 \
  --target-list=arm-softmmu \
  --enable-sdl \
  --disable-cocoa \
  --disable-slirp \
  --disable-docs \
  --disable-debug-info \
  --enable-lto \
  --extra-cflags=-I/opt/homebrew/opt/openssl@3/include \
  --extra-ldflags='-L/opt/homebrew/opt/openssl@3/lib -lcrypto'
ninja qemu-system-arm
```

The OpenSSL path used while building remains host-specific. Runtime Homebrew
load paths are relocated by `scripts/install-ipod-app-engine.sh`, which stages
and verifies the engine and its recursive dylib closure before changing the
application, then signs and verifies the completed bundle.

## Milestone 1: machine boots through SpringBoard

The first QEMU 11 binary linked and exposed the `iPod-Touch` machine, but
stopped after `FTL_Init [OK]`. Several initially plausible causes were ruled
out:

- Timer 4 fired and the guest acknowledged it, so the stall was not caused by
  the first timer API adaptation.
- ADM had not been touched yet, so ADM ownership was not the boundary.
- PC sampling showed iBoot polling and waiting for an asynchronous completion,
  not looping on a bad ARM instruction.
- Old/new snapshots at the same serial marker matched code, scheduler, timer,
  and most interrupt-controller state.

The decisive difference was VIC0 source 16: DMAC0 was asserted in QEMU 6 but
not in QEMU 11. DMAC0 channel 0 was waiting to move 512 bytes from the NAND
FIFO at `0x38a00080`, using peripheral-to-memory flow control and request ID 2.
The historical fork globally bypassed PL080 request checks. Modern PL080
correctly enforces them, but exposes no request pin for the simple NAND FIFO
model to drive.

The port adds a PL080 `request-mask` property and sets only DMAC0 request bit 2
for this machine. This describes the existing NAND FIFO stub as permanently
ready without disabling flow control for every PL080 transfer. The real DMA
completion and VIC interrupt then occur, and the unmodified firmware reaches
VFL, FTL, HFS, Darwin, Z2 firmware loading, and SpringBoard. A clean headless
run reached `Configuring SpringBoard for N45AP` in 6.087 seconds. All timer,
DMA, ADM, and VIC diagnostics used to find the boundary were removed before
the port commits.

## Milestone 2: native multitouch initialization and display handoff

Headless boot success was a false completion boundary. Initially, SDL stayed
on the centered Apple logo even though the serial log continued through
SpringBoard, framebuffer user clients, MBX, HID, multitouch, USB, and configd.
The cold-touch readiness marker never opens because the OS framebuffers do not
become the active scanout.

An identical GUI harness and the same disposable NAND produced this controlled
comparison on 2026-07-17:

| Observation | QEMU 6 release engine | QEMU 11 port |
|---|---|---|
| SpringBoard serial marker | Reached | Reached |
| Visible result | Home screen | Apple logo remains |
| Cold touch readiness | Opens after stable OS frames | Never opens |
| LCD interrupt | Approximately 60 Hz | Approximately 60 Hz |
| Guest clears LCD status | Yes | Yes, once per frame |
| Scanout bases | Rotates `0x0fe00000`, `0x0f400000`, `0x0f496000` | Remains `0x0fe00000` |
| Manual sleep/wake test | Completes retained wake and post-wake drag | Initially unreachable |

The instrumented QEMU 6 release run reached SpringBoard in 8.529 seconds,
opened cold input at 13.894 seconds, entered OOCSHDWN after the test's Power
press, reported `System Wake` 11.765 seconds after Home, reloaded Z2, and
accepted the post-wake drag. The packaged engine also passed with the same
NAND. These comparisons prove that the NAND image and test sequence are valid.

### What the trace proves

The SDL renderer is not failing to notice a framebuffer change. QEMU 11 never
receives that change. It sees iBoot program window 1 to `0x0fe00000`, then sees
the guest acknowledge LCD interrupt status at offset `0x18` around 60 times per
second. It does not see the kernel/SpringBoard program `0x0f400000` or
`0x0f496000`. QEMU 6 receives the full per-frame register sequence and rotates
among all three buffers.

The custom LCD device source is otherwise identical between the two trees;
its only port changes are header paths and the const-correct class callback.
The interrupt mask/status model works sufficiently for the guest to receive
and acknowledge vertical blanking. Waiting more than 20 seconds after
SpringBoard, forcing display invalidation, or changing dirty tracking cannot
create a guest MMIO write that never happened.

The serial comparison placed the next boundary above basic driver attach:
both engines attach `IOMobileFramebufferUserClient`, `AppleMBXUserClient`, and
the HID clients. The working engine subsequently emitted SpringBoard/LayerKit
diagnostics and started flipping buffers; the initial QEMU 11 run had not
reached that point. This first suggested a CoreAnimation/MBX userspace wait,
but the later Z2 trace placed the actual boundary earlier.

### Decisive Z2 and DMA evidence

A bounded Z2 command trace made the failure reproducible. Both engines
completed the same first 32 commands: four groups containing command `0x1a`
and seven `0x18` transfers. QEMU 6 then issued one more `0x18`, followed by a
49,117-byte `0x30` firmware transaction and the 261-byte calibration
transaction. QEMU 11 stopped immediately before that firmware upload.

The stopped QEMU 11 state showed that the final small SPI command had actually
completed: SPI2 status contained the COMPLETE bit and the controller was in
DMA mode. DMAC1 channel 3, however, was still enabled with this state:

```text
source         0x088b6000
destination    0x3d200010  (SPI2 TXDATA)
LLI            0x08a18010
control        0x04089c00  (3072 units still pending)
configuration  0x00008b81
```

Configuration `0x8b81` is memory-to-peripheral flow control with destination
request ID 14. The historical QEMU fork globally bypassed PL080 request
checks, so the transfer ran without an explicit SPI request. QEMU 11 correctly
waited forever. The NAND milestone had deliberately enabled only DMAC0 request
2, leaving this second hidden dependency exposed.

The final fix keeps PL080 flow-control checking intact and declares only
DMAC1 request 14 permanently asserted. That is appropriate for the current
SPI2 FIFO stub because it consumes TXDATA synchronously. The native driver
then performs its real firmware and calibration DMA transfers, completes Z2
initialization, and SpringBoard programs the normal rotating scanout buffers.

### Rejected fixes and false leads

- Forcing `w1_framebuffer_base` to a known OS buffer would display memory the
  guest did not select and would hide the real failed handoff. It is not an
  acceptable fix.
- Restoring a continuous host scan of all old framebuffers would recreate the
  expensive sleep/wake workaround that was already removed, and could choose
  an inactive triple buffer.
- The display timer is not stuck at 10 Hz; both engines deliver and acknowledge
  the modeled 60 Hz LCD interrupt.
- The port did not omit another historical generic-QEMU patch discovered so
  far. Auditing commits from the original iPod development found the known
  PL080 edits, already replaced by the scoped request mask, but no separate
  ARM/TCG or display-core compatibility patch.
- The QEMU 11 guest is not generally frozen: serial services continue well
  after SpringBoard configuration.
- A bounded MBX trace was a dead end. Adding it changed timing enough to expose
  an early USB PHY registration panic before MBX executed. Removing the probe
  restored the stable Apple-logo stall; no MBX workaround was retained.
- Waiting an additional minute did not advance the stalled Z2 sequence.
- The QEMU 6 and QEMU 11 SPI FIFO pop APIs have equivalent semantics, so the
  modern FIFO rename was not the cause.
- The first request-mask experiment used bit 13 due to a manual decode error.
  A second live DMAC dump still showed all 3,072 units pending; decoding
  `(0x8b81 >> 6) & 0x1f` correctly yielded request 14. No bit-13 change was
  committed.
- Every `[PORT11 MT]`, LCD, MBX, and DMA diagnostic was removed before the
  clean release build.

### Promotion validation

The clean port binary and then the exact installed binary were tested through
QMP-driven 60 Hz drags and guest serial/hardware markers. For the installed
revision, a representative manual cycle reached SpringBoard in 6.771 seconds,
cold input readiness in 11.740 seconds, OOCSHDWN 20.591 seconds after Power,
`System Wake` 11.549 seconds after Home, and retained touch readiness in 12.399
seconds. A separate no-key run entered timed OOCSHDWN after 79.541 seconds and
also completed retained wake and post-wake drag. A same-process test completed
two consecutive manual sleep/wake/Z2-reload/drag cycles.

The automated log scanner's literal `panic` result is a known false positive
from the guest text `Panic Fail Count: 0`; the runs contained no kernel panic,
data abort, assertion failure, or QEMU crash.

## Milestone 3: preserve the visible host cursor

The first promoted QEMU 11 app automatically captured and hid the macOS cursor
when it entered the iPod display. This made normal mouse-driven touch input
appear unusable even though QMP-injected absolute input still worked.

This was another omitted compatibility change rather than a multitouch-device
failure. The QEMU 6 iPod fork had deliberately disabled SDL's automatic grab
at the absolute-input mode change, on window entry, and when the pointer moved
inside the display. Starting from upstream QEMU 11 silently restored all three
calls during the forward-port.

Revision `f734de901e` restores the old behavior for absolute pointing devices:
touch coordinates continue to be forwarded without confining or hiding the
host pointer. Relative mouse devices retain QEMU's normal click-to-grab path.
The full cold-touch/manual-sleep/Home-wake/post-wake-drag regression passed.
A real macOS UI automation click against both the development binary and the
exact installed binary left the window title unchanged instead of adding
QEMU's grab-release shortcut, confirming that capture did not engage.

## Promotion matrix

The QEMU 11 engine replaced the packaged engine after these checks passed with
the normal application resources and a disposable NAND copy:

1. Cold SDL boot reaches the lock/home screen with no stale Apple logo.
2. Home and Power keys work through QEMU's modern input API.
3. Tap and controlled 60 Hz drag work after the real cold readiness boundary.
4. Power enters guest-driven OOCSHDWN; no host pause/suspend is involved.
5. Power and Home both perform retained-RAM wake without a normal SpringBoard
   relaunch, panic, or data abort.
6. Z2 firmware reloads and touch works immediately after readiness.
7. Timed and manual sleep use the same path.
8. Multiple sleep/wake cycles preserve the foreground application and never
   leak the status bar or battery/iBoot buffers.
9. The binary and dynamic libraries are relocated into the app, codesigned,
   launched from `/Applications`, and retested there.

Items 1-7 and 9 were exercised directly. Item 8 passed for two consecutive
home/lock-screen cycles without a status-bar or battery-buffer leak. A longer
interactive foreground-application soak remains useful ongoing regression
coverage, but is no longer a forward-port blocker.
