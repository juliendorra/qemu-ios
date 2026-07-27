# Browser/WebAssembly build tooling

Tools for producing a browser build of the emulator: iPhone 2G (M68AP) running
iPhone OS 1.x — 1.0 first — downloaded and executed entirely on the viewer's
machine.

The plan of record is [`BROWSER_WASM_IMPLEMENTATION_PLAN.md`](../../BROWSER_WASM_IMPLEMENTATION_PLAN.md);
current, dated state is in [`BROWSER_WASM_STATUS.md`](../../BROWSER_WASM_STATUS.md);
the ordered next steps are in [`BROWSER_WASM_HANDOFF.md`](../../BROWSER_WASM_HANDOFF.md).

## Docker is optional

QEMU's upstream CI builds the wasm target in a container. That container exists
only to pin an Emscripten SDK and cross-compile four static dependencies
(zlib, libffi, pixman, glib), all of which work natively on macOS. The native
path is the default; the container remains available for reproducible releases.

| | native (default) | container |
| --- | --- | --- |
| setup | `setup-toolchain.sh` + `build-deps.sh` | `build-toolchain.sh` |
| build | `build-qemu.sh` | `build-qemu.sh --docker` |
| needs a daemon | no | yes |
| byte-reproducible | not guaranteed | yes (pinned image) |

Nothing is installed system-wide. Everything lands in `.wasm-toolchain/`
(git-ignored): a standalone CPython if the host's is older than 3.10, the
pinned emsdk, a meson venv built from the wheel already vendored in
`python/wheels/`, and the wasm64 sysroot.

## Order of operations

```bash
scripts/wasm/setup-toolchain.sh        # python + emsdk 4.0.10 + meson
scripts/wasm/build-deps.sh             # wasm64 zlib, libffi, pixman, glib
scripts/wasm/build-qemu.sh             # qemu-system-arm.js/.wasm -> build-wasm/

scripts/wasm/stage-assets.py --board m68ap --firmware 1.0 \
    --bootrom m68ap-artifacts/shared/bootrom_s5l8900 \
    --iboot   m68ap-artifacts/builds/1A543a/iboot-sb.bin \
    --nor     m68ap-artifacts/builds/1A543a/nor.bin \
    --nand    m68ap-artifacts/builds/1A543a/nand/nand.pack

scripts/wasm/serve.py                  # http://localhost:8010 with COOP/COEP
```

## What each tool does

- **`toolchain.env`** — every pinned version in one place. Changing a value here
  is a toolchain change: rebuild and re-record measurements in the status doc.
- **`setup-toolchain.sh`** — installs the pinned SDK and a private meson.
  Re-runnable; skips whatever is already present.
- **`build-deps.sh`** — cross-compiles the four dependencies with the same
  versions and flags as QEMU's container recipe. `--rebuild` starts over;
  naming packages (`build-deps.sh glib`) builds only those.
- **`build-toolchain.sh`** — builds the upstream container image instead, for
  reproducible/CI builds.
- **`build-qemu.sh`** — configures and builds `arm-softmmu` for wasm64 into
  `build-wasm/`. Never touches the native `build-ipod11/` reference build.
- **`stage-assets.py`** — copies bootrom/iBoot/NOR/`nand.pack` into an asset set
  and writes `asset-manifest.json` with sizes, SHA-256 digests, and the machine
  arguments. Builds the pack from a `bank0..bankN` page tree when given one.
- **`serve.py`** — static server with the cross-origin isolation headers
  threaded WebAssembly requires, plus byte ranges for the ~300 MB pack.
  `--check` verifies the headers and exits.

## Why wasm64 and TCI

`configure --cpu=wasm64` is what QEMU 11.0.2 supports for Emscripten, and its
meson refuses a WebAssembly host without `--enable-tcg-interpreter`: there is no
in-tree WebAssembly TCG backend in this version. TCI is therefore the
correctness path and the first measurement. A JIT backend (the out-of-tree
qemu-wasm patch set) is a separate, later decision — see the status doc.

`--wasm64-32bit-address-limit` keeps the address space at 32 bits, which the
128 MiB guest never exceeds and which is friendlier to browser memory limits.

## Firmware is never committed

`web/public/assets/` and `web/emulator/` are git-ignored. Staging requires
explicit source paths (or one `--from-app` bundle); no tool searches the disk
for firmware.
