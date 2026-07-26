# Browser/WebAssembly port — live status

The dated, working state of the browser port. The design of record is
[`BROWSER_WASM_IMPLEMENTATION_PLAN.md`](BROWSER_WASM_IMPLEMENTATION_PLAN.md);
this file records what is actually built, what is proven, and what is next.

**Target:** iPhone 2G (M68AP) running iPhone OS 1.1.4, downloaded and executed
entirely in the viewer's browser. The plan was written for N45AP/iPod touch; the
tooling is board-agnostic and both asset sets are producible.

---

## Session — 2026-07-26: toolchain and tools

### What changed since the plan was written

The plan's two largest prerequisites are already done, which removes most of its
Phase 1:

- **The forward port happened.** This tree is QEMU **11.0.2**, not 6.2.50
  (`QEMU_11_PORT.md`).
- **Emscripten support is in-tree and upstream.** QEMU 11.0.2 already carries
  `host_os == 'emscripten'`, `--cpu=wasm64`, `util/coroutine-wasm.c`,
  `os-wasm.c`, `configs/meson/emscripten.txt`, and a container recipe at
  `tests/docker/dockerfiles/emsdk-wasm64-cross.docker`. No out-of-tree patch set
  is needed to *build*.
- **The packed NAND exists.** `scripts/pack-ipod-nand.py` and the reader in
  `hw/arm/ipod_touch_nand.c` already implement `IPODNAND` v1 (mmap'd, sorted
  index, binary search), and the packaged apps ship one. The plan's "specify and
  implement the pack" work is done for the read path. The copy-on-write overlay
  is still unimplemented.

### Proven this session

- **Docker is not required.** Emscripten **4.0.10** installs and runs natively on
  this arm64 macOS host (`emcc --version` verified). The container is retained
  only as the reproducible/CI path.
- **The host's Python is too old for emsdk** (Command Line Tools ships 3.9.6;
  emsdk requires ≥ 3.10). Rather than changing the host's Python — the native
  QEMU build depends on it — `setup-toolchain.sh` unpacks a pinned standalone
  CPython 3.12.13 into `.wasm-toolchain/`.
- **meson comes from this tree.** QEMU vendors `python/wheels/meson-1.10.0.whl`;
  the toolchain venv installs it offline, so no system meson is needed.
- **zlib 1.3.1, libffi 3.5.2 and pixman 0.44.2 cross-compile to wasm64**
  natively, same versions and flags as the upstream container.
  - **macOS-only correction:** zlib's `configure` detects a Darwin *build* host
    and swaps the archiver for Apple's `libtool`, which rejects emcc's wasm
    objects (`adler32.o is not an object file`). `--uname=Linux` forces the
    generic branch and keeps `AR=emar`. The container never hits this because
    its build host is Linux.

### Blocked: the host disk is full

The volume is at **100% (≈300 MiB free of 228 GiB)**. Two steps died on
`ENOSPC` and are unfinished, not broken:

- **glib 2.84.0** did not install (`target/lib/pkgconfig/glib-2.0.pc` absent);
  its build tree plus the pcre2 subproject needs roughly 1–2 GiB.
- **Asset staging** failed mid-copy of the 300 MiB `nand.pack`; the partial
  `web/public/assets/` was deleted.

Rough space needed to finish: **~1–2 GiB** for glib, **~2–3 GiB** for the QEMU
wasm build tree, **~300 MiB** for the staged asset set. `.wasm-toolchain/` is
currently 1.8 GiB (1.6 GiB of that is the emsdk itself).

### Tools added (`scripts/wasm/`)

| tool | purpose |
| --- | --- |
| `toolchain.env` | every pinned version in one place |
| `setup-toolchain.sh` | standalone Python (if needed) + emsdk 4.0.10 + meson venv |
| `build-deps.sh` | wasm64 zlib/libffi/pixman/glib into `.wasm-toolchain/target` |
| `build-toolchain.sh` | the container alternative (reproducible/CI) |
| `build-qemu.sh` | `arm-softmmu` for wasm64/TCI into `build-wasm/` |
| `stage-assets.py` | asset set + hashed `asset-manifest.json` |
| `serve.py` | COOP/COEP static server with byte ranges and `--check` |

Plus a `web/` shell: capability gate, asset loader (download → verify →
Cache Storage), worker lifecycle, canvas display target, and the input bridge
(pointer with capture, Home/Power, the native `H`/`P` bindings, blur cleanup).

### Decisions taken

- **Native toolchain by default, container for releases.** Nothing about the
  build requires Linux, and routine work should not need a running daemon.
- **wasm64 + TCI.** QEMU 11.0.2's meson refuses a WebAssembly host without
  `--enable-tcg-interpreter`; there is no in-tree WebAssembly TCG backend at
  this version. TCI is the correctness path and the first measurement.
  `--wasm64-32bit-address-limit` keeps the address space at 32 bits, which the
  128 MiB guest never exceeds.
- **Assets stay out of git.** `web/public/assets/` and `web/emulator/` are
  ignored; staging needs explicit paths or one `--from-app` bundle.

### Not yet done / open

- **The wasm QEMU build has never been run to completion.** Expect real work in
  the iPod device code: it uses `fopen`/`g_mapped_file` per artifact, and mmap
  over a ~300 MiB pack in MEMFS is the first memory question to measure.
- **No display bridge.** QEMU is built `-display none`; the plan's display
  listener (surface + dirty rects → `OffscreenCanvas`) is unwritten, so the
  worker's `frame` message has no producer yet.
- **No input path into QEMU.** The frontend emits input events; the worker
  drops them until the bridge exists.
- **The worker's module instantiation is provisional** — written against
  Emscripten's documented shape, to be re-checked against the first real
  `qemu-system-arm.js`.
- **No copy-on-write overlay.** Guest writes have nowhere to go in the browser;
  the base pack must stay immutable.
- **Performance is unmeasured.** If TCI cannot reach an acceptable time to
  SpringBoard, the out-of-tree [qemu-wasm](https://github.com/ktock/qemu-wasm)
  JIT becomes a requirement rather than an optimization. That is the next
  go/no-go signal.
- **`crypto.subtle` has no streaming digest**, so the loader hashes the whole
  300 MiB pack in memory. Acceptable on desktop; revisit for constrained hosts.

### Reproduction

```bash
scripts/wasm/setup-toolchain.sh
scripts/wasm/build-deps.sh
scripts/wasm/build-qemu.sh
scripts/wasm/stage-assets.py --from-app "/Applications/iPhone 2G.app" \
    --board m68ap --firmware 1.1.4
scripts/wasm/serve.py            # then open http://localhost:8010
```
