#!/usr/bin/env bash
#
# Build qemu-system-arm for the browser (wasm64 + TCI).
#
# Two backends, same configure arguments:
#   --native  (default) the emsdk installed by scripts/wasm/setup-toolchain.sh
#   --docker            the pinned container from scripts/wasm/build-toolchain.sh
#
# The native reference build in build-ipod11/ is never touched: this configures
# a separate build directory ($WASM_BUILD_DIR, default build-wasm/), so a
# browser build can never quietly become the thing we compare against.
#
# Usage:
#   scripts/wasm/build-qemu.sh
#   scripts/wasm/build-qemu.sh --configure     # force a fresh configure
#   scripts/wasm/build-qemu.sh --docker
#
# Emitted artifacts (in $WASM_BUILD_DIR):
#   qemu-system-arm.js      ES module loader emitted by Emscripten
#   qemu-system-arm.wasm    the emulator itself

set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo="$(cd "$here/../.." && pwd)"
# shellcheck source=toolchain.env
source "$here/toolchain.env"

backend=native
do_configure=0
while [ $# -gt 0 ]; do
    case "$1" in
        --native)    backend=native; shift ;;
        --docker)    backend=docker; shift ;;
        --configure) do_configure=1; shift ;;
        -h|--help)   sed -n '2,20p' "${BASH_SOURCE[0]}"; exit 0 ;;
        *)           echo "unknown argument: $1" >&2; exit 2 ;;
    esac
done

build="$repo/${WASM_BUILD_DIR}${IT_WASM_TCI:+-tci}"
mkdir -p "$build"
jobs="$(getconf _NPROCESSORS_ONLN 2>/dev/null || echo 4)"

# The wasm64 host requires the TCG interpreter: QEMU 11.0.2 has no native
# WebAssembly TCG backend, and meson.build errors out without
# --enable-tcg-interpreter.  TCI is therefore the correctness path and the
# first performance measurement; a JIT backend is a later, separate decision
# recorded in BROWSER_WASM_STATUS.md.
#
# --static because Emscripten links a single module; --disable-tools because
# the browser only needs the system emulator.
configure_args=(
    --static
    --cpu=wasm64
    --target-list="$WASM_TARGET_LIST"
    # The JIT build drops --enable-tcg-interpreter: with tcg/wasm64 present,
    # meson selects the WebAssembly TCG backend instead. Set IT_WASM_TCI=1 to
    # go back to the interpreter for comparison.
    ${IT_WASM_TCI:+--enable-tcg-interpreter}
    --disable-tools
    --disable-docs
    --disable-werror
    # QEMU's emscripten cross file (configs/meson/emscripten.txt) sets its own
    # link arguments, so these have to arrive through configure rather than the
    # LDFLAGS environment variable, which it overrides.
    #
    # -lnodefs.js lets a Node run mount the real filesystem, which is how the
    # emulator is booted headlessly for testing before the browser asset
    # pipeline exists. -lworkerfs.js is its browser counterpart (read-only
    # Blob/File access from a worker). Without them Emscripten stubs both out
    # with "NODEFS is no longer included by default".
    # NB the literal single quotes: run() evals this array flattened into one
    # string, so a value containing a space must carry its own quoting.
    "--extra-ldflags='-lnodefs.js -lworkerfs.js'"
)

# -sMEMORY64=2 keeps the address space at 32 bits, which the 128 MiB guest never
# approaches and which is kinder to browser memory limits. The WebAssembly TCG
# backend upstream defaults to the FULL 64-bit mode (-sMEMORY64=1) though, and
# its EM_JS glue encodes pointers differently between the two, so
# IT_WASM_MEMORY64_FULL=1 selects the mode the backend is developed against.
if [ -z "${IT_WASM_MEMORY64_FULL:-}" ]; then
    configure_args+=(--wasm64-32bit-address-limit)
fi

if [ "$backend" = docker ]; then
    if ! docker image inspect "$WASM_BUILDER_IMAGE" >/dev/null 2>&1; then
        echo "toolchain image $WASM_BUILDER_IMAGE is missing" >&2
        echo "run scripts/wasm/build-toolchain.sh, or use the default --native" >&2
        exit 1
    fi
    run() {
        docker run --rm -i \
            -v "$repo:/qemu" -w "/qemu/$WASM_BUILD_DIR" \
            "$WASM_BUILDER_IMAGE" bash -lc "$1"
    }
else
    root="$repo/$WASM_TOOLCHAIN_DIR"
    if [ ! -f "$root/emsdk/emsdk_env.sh" ]; then
        echo "native toolchain missing; run scripts/wasm/setup-toolchain.sh" >&2
        exit 1
    fi
    if [ ! -f "$root/target/lib/pkgconfig/glib-2.0.pc" ]; then
        echo "wasm64 dependencies missing; run scripts/wasm/build-deps.sh" >&2
        exit 1
    fi
    # shellcheck disable=SC1091
    source "$root/emsdk/emsdk_env.sh" >/dev/null 2>&1
    export PATH="$root/venv/bin:$PATH"
    export CPATH="$root/target/include"
    export PKG_CONFIG_PATH="$root/target/lib/pkgconfig"
    export EM_PKG_CONFIG_PATH="$PKG_CONFIG_PATH"
    export CFLAGS="-O3 -pthread -DWASM_BIGINT"
    # -lnodefs.js lets a Node run mount the real filesystem, which is how the
    # emulator is booted headlessly for testing before the browser asset
    # pipeline exists. -lworkerfs.js is its browser counterpart (read-only
    # Blob/File access from a worker). Both are inert when unused; without
    # them Emscripten stubs NODEFS out with "no longer included by default".
    export LDFLAGS="-sWASM_BIGINT -sASYNCIFY=1 -L$root/target/lib"
    run() { ( cd "$build" && eval "$1" ); }
fi

if [ "$do_configure" = 1 ] || [ ! -f "$build/build.ninja" ]; then
    echo "==> configuring $WASM_BUILD_DIR for wasm64/TCI ($backend)"
    if [ "$backend" = docker ]; then
        run "emconfigure /qemu/configure ${configure_args[*]}"
    else
        run "emconfigure $repo/configure ${configure_args[*]}"
    fi
fi

# Emscripten names the emitted target after the JS loader, not the bare
# executable: ninja knows "qemu-system-arm.js" and building it also produces
# the .wasm beside it. Asking for "qemu-system-arm" fails with
# "unknown target ... did you mean 'qemu-system-arm.js'?".
echo "==> building qemu-system-arm.js (wasm)"
run "ninja -j$jobs qemu-system-arm.js"

echo
echo "artifacts:"
ls -la "$build"/qemu-system-arm* 2>/dev/null | sed 's/^/  /' || {
    echo "  none found - check the build log above" >&2
    exit 1
}
