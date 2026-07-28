# iPhone 2G build system: one shape for every firmware

How M68AP artifacts are organised and built, as of 2026-07-27. Read this before
adding a firmware build or touching a `build-m68ap-*` script.

## The problem this replaces

The tree supported exactly one firmware, so "M68AP" and "iPhone OS 1.1.4 /
4A102" were used interchangeably. That showed up as:

- `m68ap-artifacts/stage/` **was** 1.1.4, while 1.0 lived in `stage-1.0/` with a
  different shape and a different subset of files;
- filenames carried the bootloader version (`iboot_204_m68ap_sbpatch.bin`), so
  every consumer hardcoded one build's spelling;
- `build-m68ap-homescreen-nand.py` — the *product* recipe — passed
  `--ipsw-build 4A102` literally, so it could not package any other firmware;
- `build-m68ap-nand.py` defaulted to `4A102` when `--ipsw-build` was omitted;
- `build-m68ap-var.py` sized /var from 1.1.4's `data-m68ap.dmg` by default,
  wherever it was invoked from.

None of these fail loudly. A NAND built with one firmware's filesystem and
another's FIL signature fails WMR init; a firmware booted under the wrong
security epoch wedges inside iBoot with an **empty serial log**, which looks
exactly like a hang. The cost of diagnosing either is much higher than the cost
of naming the build.

## The layout

```text
m68ap-artifacts/
  shared/
    bootrom_s5l8900        SoC-wide; the same silicon on every S5L8900
    nor_n45ap.bin          N45AP device dump; the NOR builder's SysCfg/geometry
                           template. NOT per-build, and NOT regenerable.
  builds/
    1A543a/                iPhone OS 1.0
      ipsw/                the retail IPSW and images extracted from it
      root.img             decrypted root filesystem
      data.dmg             /var sizing reference (optional)
      iboot.bin            raw iBoot for this build
      iboot-sb.bin         secure-boot-patched iBoot
      nor.bin
      nand/                generated tree + nand.pack
      build.json           per-build provenance
    1C28/                  iPhone OS 1.0.2
    3A109a/                iPhone OS 1.1.1
    4A102/                 iPhone OS 1.1.4
```

The **directory** names the version, so the **filenames** do not have to. Adding
a build means adding a directory with the same names in it, not inventing a new
naming scheme.

`scripts/m68ap_paths.py` is the only thing that knows this layout. Ask it for
paths; never spell `m68ap-artifacts/...` in a new script.

```sh
scripts/m68ap_paths.py --build 1A543a      # show the resolved paths
```

## There is no default build

Every entry point takes `--build` and refuses to run without it. All four builds
are equally valid targets, and the failure modes above are silent, so the choice
is always stated. `scripts/firmware_profiles.py` owns the option
(`add_build_argument`) and the per-build constants; `--ipsw-build` is accepted as
an alias so older command lines keep working.

```
build    version   fmt epoch  iBoot      FIL signature
1A543a   1.0      plain     0  iBoot-159  0x43303030 000C
1C28     1.0.2    plain     0  iBoot-159  0x43303030 000C
3A109a   1.1.1      enc     2  iBoot-204  0x43303032 200C
4A102    1.1.4      enc     3  iBoot-204  0x43303033 300C
n45ap    1.1        enc     2  iBoot-204  0x43303032 200C   (iPod reference)
```

Epoch and FIL signature are **firmware**-keyed. Bank topology and BBT style are
**board**-keyed. Conflating the two is what made 1.1.1-on-M68AP unbuildable
before the profiles existed.

## Building a firmware

```sh
# everything: patched root, /var, NAND tree, and the packed NAND
scripts/build-m68ap-homescreen-nand.py --build 1A543a

# boot it and verify it reaches the home screen
scripts/fb-snapshot.py --board m68ap --build 1A543a --boot-wait 420 --logs /tmp/fb
```

`fb-snapshot.py --build` fills in the iBoot, NOR, NAND, bootrom and **epoch**
from the same layout, so a verification run cannot mix one firmware's NAND with
another's epoch.

Verification is by framebuffer, not by serial: **SpringBoard never announces
itself on the serial console**. Check `kernel_0x0f400000`'s `nonzero_pct` in the
report. Scanout reads ~0% because the panel sleeps — that is the documented
"black screen is scanout, not SpringBoard" artifact, not a failure.

## Migrating an old checkout

```sh
scripts/migrate-m68ap-artifacts.py            # dry run: print the plan
scripts/migrate-m68ap-artifacts.py --apply
```

Renames only, never copies — these are hundred-megabyte trees. NAND directories
are checked against their own `nand-provenance.json` before being filed under a
build, so a tree cannot be moved under the wrong firmware.

## Status

| Build | root.img | NAND + pack | Home screen verified |
| --- | --- | --- | --- |
| 1A543a (1.0) | yes | **yes, 215.6 MiB pack, 106,858 pages** | **yes — 59.04% non-black, 2026-07-27** |
| 1C28 (1.0.2) | **yes** (decrypted 2026-07-27) | no | booted natively during bring-up, not packaged |
| 3A109a (1.1.1) | no | no | booted natively during bring-up, not packaged |
| 4A102 (1.1.4) | yes | yes, 300.3 MiB pack, 148,812 pages | yes — 73.95% non-black |

Notes:

- **1.0 has no `data.dmg`.** Its /var is built from the root filesystem's own
  `/private/var` template and sized at the stated 24 MiB default. That size is
  our choice, not a firmware constant.
- **1.0's ambient light sensor is a TSL2561**, where the machine models an
  ISL29003. It reaches the home screen anyway; whether anything depends on the
  sensor beyond a probe failure is untested.
- **1.0.2's root filesystem is staged and verified** (`ProductBuildVersion
  1C28`, `ProductVersion 1.0.2`, kernelcache dated Aug 2007). Its
  `LK_ENABLE_MBX2D` knob and `com.apple.SpringBoard.plist` are where the recipe
  expects them, so `build-m68ap-homescreen-nand.py --build 1C28` should run;
  note SpringBoard itself lives in `/System/Library/CoreServices/` on 1.0.x.
- HFS images are not bit-reproducible: `hdiutil` records timestamps, so two runs
  of the same recipe produce different `hfs_sha256` values. Reproducibility is
  at the level of *the recipe*, not of the bytes.


## The two irreplaceable inputs in `shared/`

Neither is committed (they are Apple firmware, `.gitignore`d with the rest of
`m68ap-artifacts/`), and neither can be rebuilt from anything else in this tree.
**Keep copies off this machine.**

* `bootrom_s5l8900` — burned into the SoC.
* `nor_n45ap.bin` — a real iPod touch NOR. `build-m68ap-nor.py` takes its
  SysCfg block and overall geometry and rewrites only the image-store region,
  so without it no M68AP NOR can be built at all.

`nor_n45ap.bin` earned its place here the hard way (2026-07-28). It used to live
in an untracked `data/` directory that the docs and the build script both named
— and that directory was gone, so a from-scratch NOR rebuild was impossible.
The only surviving copy was inside `/Applications/iPod Touch.app`, which is a
build *output* (repackaging rewrites it) and which N45AP maps **in place**, so a
guest write could have silently altered a build input. Nothing recorded which
template a given NOR had been built from, either.

Now: the script defaults to `shared/nor_n45ap.bin` and prints the template's
sha256 on every run, so a NOR can be traced to the dump it came from. The
current one is
`9f86a537c19c0193e72e52e60d127f4991eca813b037ff833a1ed1ab9c7dad00`.
Verified by rebuilding 1A543a's NOR from scratch and getting a byte-identical
result to the shipped artifact.
