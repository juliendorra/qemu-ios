# QEMU 11 Forward-Port Journal

This document is the working record for moving the iPod Touch 1G machine from
the current QEMU 6.2 tree to QEMU 11.0.2. It records successful milestones,
failed theories, disposable diagnostics, and the promotion criteria. The
current packaged application remains the correctness oracle until every GUI
and power-lifecycle check below passes.

## Repositories and branches

- Correctness oracle: `ipod_touch_1g` in the main repository.
- Port worktree: `/private/tmp/qemu-11-port`.
- Port branch: `codex/qemu-11-port`, based on upstream tag `v11.0.2`.
- Initial machine/API port: `697306b42c`.
- NAND DMA request fix: `78a43a0d56`.
- Installed application: still the known-good QEMU 6 engine from
  `95a3d80040`; do not replace it merely because the port boots headlessly.

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

The OpenSSL path in the port's Meson file is still host-specific and must be
made relocatable before application packaging.

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

## Milestone 2 blocker: kernel display handoff

Headless boot success was a false completion boundary. In SDL, QEMU 11 stays
on the centered Apple logo even though the serial log continues through
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
| Manual sleep/wake test | Completes retained wake and post-wake drag | Not yet reachable |

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

The serial comparison also places the next boundary above basic driver attach:
both engines attach `IOMobileFramebufferUserClient`, `AppleMBXUserClient`, and
the HID clients. The working engine subsequently emits SpringBoard/LayerKit
diagnostics and starts flipping buffers; the QEMU 11 run has not reached that
point. The leading hypothesis is therefore a CoreAnimation/MBX userspace
submission wait or a subtle interrupt/CPU scheduling difference, not an SDL
copy bug.

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

### Next experiments

1. Trace MBX reads and writes around `AppleMBXUserClient::attach` in both
   engines, suppressing repetitive reads and comparing the first divergent
   register/value rather than dumping all MMIO.
2. Stop both engines at that divergence and compare CPU PC/registers, VIC
   nesting/priority, LCD registers, and the relevant userspace wait object.
3. If MBX matches, compare QEMU 6 and QEMU 11 `arm1176` CPU properties and
   exception-return/interrupt behavior at the first missed display submission.
4. Fix the first modeled hardware contract that differs. Do not synthesize a
   flip, wake SpringBoard from the host, or patch guest RAM.
5. Remove every `[PORT ...]` diagnostic and verify the release binary contains
   none before making a port commit.

## Promotion matrix

The QEMU 11 engine may replace the packaged engine only after all of these pass
with the normal application NAND and a disposable copy:

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

Until then, `/Applications/iPod Touch.app` intentionally remains on QEMU 6.
