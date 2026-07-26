#!/usr/bin/env bash
#
# Install the browser-port build toolchain natively, without Docker.
#
# Docker is not required for this port.  QEMU's upstream CI uses a container
# only to get a pinned Emscripten SDK plus four cross-compiled static
# dependencies; both work fine on a native arm64 macOS host.  This script
# installs the same pinned SDK into .wasm-toolchain/ and prepares a private
# Python venv with the meson wheel already vendored in this tree
# (python/wheels/), so no new host-wide packages are needed.
#
# Usage:
#   scripts/wasm/setup-toolchain.sh
#
# Everything lands under .wasm-toolchain/ at the repository root:
#   emsdk/    the pinned Emscripten SDK
#   venv/     meson + tomli for the dependency and QEMU builds
#   target/   (created by build-deps.sh) the wasm64 sysroot
#
# Next: scripts/wasm/build-deps.sh, then scripts/wasm/build-qemu.sh

set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo="$(cd "$here/../.." && pwd)"
# shellcheck source=toolchain.env
source "$here/toolchain.env"

root="$repo/$WASM_TOOLCHAIN_DIR"
mkdir -p "$root"

# --- Python ---------------------------------------------------------------
#
# emsdk needs Python >= 3.10; macOS Command Line Tools ships 3.9.6.  We do not
# touch the host's Python (the native QEMU build depends on it): a standalone
# CPython is unpacked into the toolchain directory instead.
find_python() {
    local candidate
    for candidate in "${WASM_PYTHON:-}" python3.13 python3.12 python3.11 python3.10 python3; do
        [ -n "$candidate" ] || continue
        command -v "$candidate" >/dev/null 2>&1 || continue
        if "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; then
            command -v "$candidate"
            return 0
        fi
    done
    return 1
}

python="$(find_python || true)"
if [ -z "$python" ]; then
    case "$(uname -m)" in
        arm64|aarch64) arch=aarch64 ;;
        *)             arch=x86_64 ;;
    esac
    case "$(uname -s)" in
        Darwin) triple="$arch-apple-darwin" ;;
        *)      triple="$arch-unknown-linux-gnu" ;;
    esac
    tarball="cpython-$WASM_PYTHON_VERSION+$WASM_PYTHON_RELEASE-$triple-install_only.tar.gz"
    url="https://github.com/astral-sh/python-build-standalone/releases/download/$WASM_PYTHON_RELEASE/$tarball"
    if [ ! -x "$root/python/bin/python3" ]; then
        echo "==> no Python >= 3.10 on PATH; installing standalone CPython $WASM_PYTHON_VERSION"
        mkdir -p "$root/python"
        curl --fail --location --progress-bar "$url" |
            tar xz -C "$root/python" --strip-components=1
    fi
    python="$root/python/bin/python3"
fi
echo "==> python: $python ($("$python" --version 2>&1))"
export EMSDK_PYTHON="$python"

# --- Emscripten SDK -------------------------------------------------------
#
# Pinned to the same version QEMU 11.0.2's own container uses.  emsdk keeps
# every released toolchain, so "install <version>" is reproducible.
emsdk="$root/emsdk"
if [ ! -d "$emsdk/.git" ]; then
    echo "==> cloning emsdk"
    git clone --depth 1 https://github.com/emscripten-core/emsdk.git "$emsdk"
fi

if [ ! -f "$emsdk/upstream/emscripten/emcc" ] ||
   ! grep -qs "^$WASM_EMSDK_VERSION\$" "$root/.emsdk-version"; then
    echo "==> installing emscripten $WASM_EMSDK_VERSION"
    (cd "$emsdk" && git fetch --depth 1 origin main && git checkout FETCH_HEAD -- emsdk emsdk.py emsdk_manifest.json 2>/dev/null || true)
    "$emsdk/emsdk" install "$WASM_EMSDK_VERSION"
    "$emsdk/emsdk" activate "$WASM_EMSDK_VERSION"
    echo "$WASM_EMSDK_VERSION" > "$root/.emsdk-version"
fi

# shellcheck disable=SC1091
source "$emsdk/emsdk_env.sh" >/dev/null 2>&1
echo "==> emcc: $(command -v emcc)"
emcc --version | head -1

# --- Python venv with meson ----------------------------------------------
#
# QEMU vendors the exact meson wheel it supports (python/wheels/), so the
# dependency builds and the QEMU build agree on one meson and the host needs
# no system meson at all.
venv="$root/venv"
if [ ! -x "$venv/bin/meson" ]; then
    echo "==> creating python venv with the in-tree meson wheel"
    "$python" -m venv "$venv"
    wheel=$(ls "$repo"/python/wheels/meson-*.whl 2>/dev/null | head -1)
    if [ -n "$wheel" ]; then
        "$venv/bin/pip" install --quiet --no-index "$wheel"
    else
        "$venv/bin/pip" install --quiet "meson>=1.5.0"
    fi
    if [ -d "$repo/.build-pydeps/tomli" ]; then
        cp -R "$repo/.build-pydeps/tomli" "$venv/lib/"python*/site-packages/ 2>/dev/null || true
    else
        "$venv/bin/pip" install --quiet tomli || true
    fi
fi
echo "==> meson: $("$venv/bin/meson" --version)"

# --- Host tools the dependency builds need -------------------------------
#
# Only native helper programs; nothing is linked against them.  glib's build
# runs its own native tooling, and pkg-config resolves the wasm sysroot.
missing=()
for tool in ninja pkg-config; do
    command -v "$tool" >/dev/null 2>&1 || missing+=("$tool")
done
if [ ${#missing[@]} -gt 0 ]; then
    echo
    echo "missing host tools: ${missing[*]}" >&2
    echo "install them with: brew install ${missing[*]}" >&2
    exit 1
fi

cat <<EOF

toolchain ready in $WASM_TOOLCHAIN_DIR/
  emsdk    $WASM_EMSDK_VERSION
  meson    $("$venv/bin/meson" --version)

next: scripts/wasm/build-deps.sh
EOF
