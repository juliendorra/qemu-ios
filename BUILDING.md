# Building the QEMU 11 iPod Touch / iPhone 2G engine (offline recipe)

> **TL;DR — reconstitute the build in one go:** run the block under
> [Verified offline recipe](#verified-offline-recipe). It produces
> `build-ipod11/qemu-system-arm` (QEMU 11.0.2, machine `iPod-Touch`) with **no
> network access**.

This repository's `ipod_touch_1g` branch is **QEMU 11.0.2 source**
(`VERSION` = 11.0.2). The packaged app in `/Applications/iPod Touch.app` runs a
QEMU 11 engine built from it.

## Why the build tree keeps "disappearing"

Two different things live here; keep them straight:

| | In git? | Survives cleanup? | How to get it back |
|---|---|---|---|
| **Source** (`.c/.h`, `VERSION`) | yes | yes | already here |
| **Build tree** (`build*/`, the `qemu-system-arm` binary, `pyvenv/`, meson/ninja state) | **no** (build output is never committed) | **only if on persistent disk** | re-run configure + ninja |

The previously-working QEMU 11 build lived in a temporary git worktree under
`/private/tmp/qemu-11-port/`. macOS purges `/private/tmp`, so that build tree
(and its bootstrapped `pyvenv`) was deleted. **Nothing in git was lost** — only
regenerable compiled output. `git worktree prune` cleans the stale record.

**Lesson: build in-tree (`build-ipod11/`), never under `/private/tmp`.**

## The three things that bite you offline

`configure` succeeds on a normal machine because it has network (it pip-installs
`meson`/`tomli`) and populated git submodules. Offline on this Mac, three gaps
must be filled by hand. All three have on-disk sources — **no network needed**:

1. **`tomli` for Python.** QEMU's meson bootstrap needs `tomli` on Python < 3.11.
   This Mac has **only** system `/usr/bin/python3` = **3.9.6**. There is **no
   Python 3.11+ interpreter** here — `/opt/homebrew/lib/python3.12/` is an
   orphaned lib dir from an uninstalled `python@3.12`, with **no binary**. So
   "just point configure at python3.12" does **not** work. Instead, vendor
   `tomli` from the local `uv` cache and expose it via `PYTHONPATH` (QEMU's
   venv is created *non-isolated*, so it honours `PYTHONPATH`).

2. **`subprojects/dtc`** is an unpopulated submodule (just a `.git` pointer; no
   system `libfdt` exists either). The full dtc source — including its
   `meson.build` — is present at the top-level `dtc/` dir; copy it in.

3. **`subprojects/keycodemapdb` revision mismatch.** The submodule is checked
   out at the old QEMU-6 revision `d21009b1`, whose `meson.build` does **not**
   export the `keymaps_csv` variable that QEMU 11's `ui/meson.build:177`
   requires (`ERROR: Requested variable "keymaps_csv" not found`). The correct
   QEMU-11 revision `f5772a62` is already in the local git object store
   (`.git/modules/ui/keycodemapdb`); extract that exact tree.

`slirp` is fine (system `libslirp` 4.9.3 via pkg-config); `capstone` is disabled.

## Verified offline recipe

Verified 2026-07-20: produces `build-ipod11/qemu-system-arm`, QEMU 11.0.2,
machine `iPod-Touch`, with no network.

```sh
cd /Users/julien/Documents/GitHub/qemu-ipod_touch_1g

# 1. Vendor tomli for python3.9 (no network) and expose via PYTHONPATH later.
mkdir -p .build-pydeps
cp -R "$(find ~/.cache/uv/archive-v0 -maxdepth 2 -name tomli -type d | head -1)" .build-pydeps/
PYTHONPATH="$PWD/.build-pydeps" python3 -c 'import tomli'   # sanity check

# 2. dtc subproject: real source (with meson.build) lives at top-level dtc/.
rm -rf subprojects/dtc && cp -R dtc subprojects/dtc && rm -rf subprojects/dtc/.git

# 3. keycodemapdb at the QEMU-11 pinned revision (submodule is on the old rev).
rm -rf subprojects/keycodemapdb && mkdir subprojects/keycodemapdb
git --git-dir=.git/modules/ui/keycodemapdb \
    archive f5772a62ec52591ff6870b7e8ef32482371f22c6 | tar -x -C subprojects/keycodemapdb
grep -q keymaps_csv subprojects/keycodemapdb/meson.build && echo "keycodemapdb OK"

# 4. Configure + build out-of-tree. PYTHONPATH carries the vendored tomli into
#    configure AND ninja (both invoke python).
rm -rf build-ipod11 && mkdir build-ipod11 && cd build-ipod11
PYTHONPATH="$PWD/../.build-pydeps" ../configure \
  --target-list=arm-softmmu \
  --enable-sdl --disable-cocoa \
  --enable-slirp --disable-capstone --disable-pie --disable-docs --disable-werror \
  --extra-cflags=-I/opt/homebrew/opt/openssl@3/include \
  --extra-ldflags=-L/opt/homebrew/opt/openssl@3/lib
PYTHONPATH="$PWD/../.build-pydeps" ninja qemu-system-arm

# Result:
./qemu-system-arm --version          # QEMU emulator version 11.0.2 ...
./qemu-system-arm -M help | grep iPod # iPod-Touch  iPod Touch
```

### Incremental rebuilds after editing `hw/arm/*.c`

```sh
cd build-ipod11
PYTHONPATH="$PWD/../.build-pydeps" ninja qemu-system-arm
```

Always keep `PYTHONPATH` pointed at `../.build-pydeps` — ninja re-invokes
python for codegen and would otherwise re-hit the `tomli` failure.

## Host toolchain actually used (macOS arm64, 2026-07-20)

- `/opt/homebrew/bin/ninja` 1.13.2, `/opt/homebrew/bin/pkg-config`
- `glib-2.0` 2.88.1, `pixman-1` 0.46.4, **system `libslirp` 4.9.3**
- OpenSSL from Homebrew: `/opt/homebrew/opt/openssl@3`
- Python: **system `/usr/bin/python3` 3.9.6 only** (see gap #1)
- `uv` 0.5.20 at `~/.local/bin/uv` (source of the vendored `tomli`)

## Do NOT

- Do **not** build under `/private/tmp` (it gets purged — that's how the last
  build tree vanished).
- Do **not** delete the top-level `dtc/ capstone/ slirp/ meson/ ui/keycodemapdb/`
  dirs or the `roms/*` / `build-release/` leftovers — they are the offline
  source of subprojects and are on the worktree-safety preserve list.
- Do **not** trust any pre-existing `build*/` binary's version blindly; run
  `--version`. A stale QEMU-6.2 `build/` previously sat next to QEMU-11 source.
