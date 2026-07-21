# Branch layout

This repository deliberately keeps the active QEMU 11 implementation separate
from its QEMU 6 history. The two generations do not share a Git ancestor, so a
normal merge or pull between them is not an appropriate reconciliation method.

## Active line

- `ipod_touch_1g` is the source-of-truth QEMU 11 line for the shared S5L8900
  emulator. It contains the iPod Touch 1G implementation, Wi-Fi/HTTP/HTTPS
  work, and the merged iPhone 2G machine and bring-up work.
- The QEMU 11 root is `e545d8bb9d` (`v11.0.2`), and the initial iPod forward
  port is `697306b42c`.
- `origin/ipod_touch_1g` tracks this same active line.
- `iphone_2g_initial_work` is a local marker for the initial QEMU 11 iPhone
  feature work at `002e822fa9`; it is fully merged into `ipod_touch_1g`.

## Preserved legacy line

- `ipod_touch_1g-qemu6-legacy` preserves the complete older QEMU 6 iPod line,
  including every commit formerly published as `origin/ipod_touch_1g` and 14
  later local commits.
- `origin/ipod_touch_1g-qemu6-legacy` is its matching remote archive.
- Historical notebooks copied into the active line retain provenance banners.
  Their dated experiments and dead ends remain useful, but current firmware
  tests must follow `AGENTS.md` and stage both NAND and NOR copies.

## Commit and branch terminology

A commit hash identifies immutable history; a branch name is only a movable
label. For example, `68b92c7b9b` belongs to the QEMU 11 ancestry even if it was
temporarily published under a differently named remote branch. Conversely,
`4221943495` belongs to the preserved QEMU 6 ancestry.

The temporary remote branch `iphone-2g-nand-bringup` duplicated the active
QEMU 11 tip and was removed after that exact tip was promoted to
`origin/ipod_touch_1g`.
