#!/usr/bin/env bash
#
# Cross-compile QEMU's four required dependencies for wasm64.
#
# This mirrors QEMU 11.0.2's own container recipe
# (tests/docker/dockerfiles/emsdk-wasm64-cross.docker) step for step, using the
# same versions and the same flags, but natively via the emsdk installed by
# scripts/wasm/setup-toolchain.sh.  Release tarballs are used instead of git
# checkouts so no autotools regeneration (and therefore no autoconf/automake/
# libtool on the host) is needed.
#
# Usage:
#   scripts/wasm/build-deps.sh              # build whatever is missing
#   scripts/wasm/build-deps.sh --rebuild    # discard and rebuild everything
#   scripts/wasm/build-deps.sh zlib pixman  # build only the named packages
#
# Output: .wasm-toolchain/target/{lib,include,lib/pkgconfig}

set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo="$(cd "$here/../.." && pwd)"
# shellcheck source=toolchain.env
source "$here/toolchain.env"

root="$repo/$WASM_TOOLCHAIN_DIR"
target="$root/target"
src="$root/src"
emsdk="$root/emsdk"
venv="$root/venv"

if [ ! -f "$emsdk/emsdk_env.sh" ]; then
    echo "toolchain missing; run scripts/wasm/setup-toolchain.sh first" >&2
    exit 1
fi

rebuild=0
packages=()
while [ $# -gt 0 ]; do
    case "$1" in
        --rebuild) rebuild=1; shift ;;
        -h|--help) sed -n '2,18p' "${BASH_SOURCE[0]}"; exit 0 ;;
        -*) echo "unknown argument: $1" >&2; exit 2 ;;
        *) packages+=("$1"); shift ;;
    esac
done
if [ ${#packages[@]} -eq 0 ]; then
    packages=(zlib libffi pixman glib)
fi

# shellcheck disable=SC1091
source "$emsdk/emsdk_env.sh" >/dev/null 2>&1
export PATH="$venv/bin:$PATH"

mkdir -p "$target" "$src"

# The exact flag set from the upstream container.  -sMEMORY64=1 must be present
# for every dependency or the objects cannot link into a wasm64 QEMU.
export CFLAGS="-O3 -pthread -DWASM_BIGINT -sMEMORY64=1"
export CXXFLAGS="$CFLAGS"
export LDFLAGS="-sWASM_BIGINT -sASYNCIFY=1 -sMEMORY64=1 -L$target/lib"
export CPATH="$target/include"
export PKG_CONFIG_PATH="$target/lib/pkgconfig"
export EM_PKG_CONFIG_PATH="$PKG_CONFIG_PATH"

cross_file="$root/cross.meson"
write_cross_file() {
    # Meson needs the compiler flags spelled out in the cross file; emcc's
    # wrappers do not forward CFLAGS to meson's compile checks.
    local extra_cflags="${1:-}"
    local cflags="$CFLAGS $extra_cflags"
    {
        echo "[host_machine]"
        echo "system = 'emscripten'"
        echo "cpu_family = 'wasm64'"
        echo "cpu = 'wasm64'"
        echo "endian = 'little'"
        echo
        echo "[binaries]"
        echo "c = 'emcc'"
        echo "cpp = 'em++'"
        echo "ar = 'emar'"
        echo "ranlib = 'emranlib'"
        echo "pkgconfig = ['pkg-config', '--static']"
        echo
        echo "[built-in options]"
        printf "c_args = ["; printf "'%s', " $cflags | sed 's/, $//'; echo "]"
        printf "cpp_args = ["; printf "'%s', " $cflags | sed 's/, $//'; echo "]"
        printf "objc_args = ["; printf "'%s', " $cflags | sed 's/, $//'; echo "]"
        printf "c_link_args = ["; printf "'%s', " $LDFLAGS | sed 's/, $//'; echo "]"
        printf "cpp_link_args = ["; printf "'%s', " $LDFLAGS | sed 's/, $//'; echo "]"
    } > "$cross_file"
}

fetch() {
    # fetch <url> <directory> — download and unpack into $src/<directory>
    local url="$1" dir="$src/$2" archive="$src/$(basename "$1")"
    if [ "$rebuild" = 1 ]; then
        rm -rf "$dir"
    fi
    if [ -d "$dir" ]; then
        return
    fi
    if [ ! -f "$archive" ]; then
        echo "==> downloading $(basename "$url")"
        curl --fail --location --silent --show-error -o "$archive.part" "$url"
        mv "$archive.part" "$archive"
    fi
    mkdir -p "$dir"
    tar xf "$archive" -C "$dir" --strip-components=1
}

wants() {
    local name="$1" item
    for item in "${packages[@]}"; do
        [ "$item" = "$name" ] && return 0
    done
    return 1
}

# --- zlib -----------------------------------------------------------------
if wants zlib; then
    echo "=== zlib $WASM_ZLIB_VERSION"
    fetch "https://github.com/madler/zlib/releases/download/v$WASM_ZLIB_VERSION/zlib-$WASM_ZLIB_VERSION.tar.gz" zlib
    (
        cd "$src/zlib"
        # --uname=Linux is a macOS-only correction: zlib's configure sees a
        # Darwin build host and swaps the archiver for Apple's libtool, which
        # rejects emcc's wasm objects ("not an object file").  Forcing the
        # generic branch keeps emconfigure's AR=emar.  The container never hits
        # this because its build host is Linux.
        emconfigure ./configure --prefix="$target" --static --uname=Linux
        emmake make install -j"$(getconf _NPROCESSORS_ONLN)"
    )
fi

# --- libffi ---------------------------------------------------------------
# QEMU only needs libffi's headers here (glib links it), matching the
# container's `make install SUBDIRS='include'`.
if wants libffi; then
    ffi_version="${WASM_FFI_VERSION#v}"
    echo "=== libffi $ffi_version"
    fetch "https://github.com/libffi/libffi/releases/download/v$ffi_version/libffi-$ffi_version.tar.gz" libffi
    (
        cd "$src/libffi"
        emconfigure ./configure --host=wasm64-unknown-linux \
            --prefix="$target" --enable-static \
            --disable-shared --disable-dependency-tracking \
            --disable-builddir --disable-multi-os-directory \
            --disable-raw-api --disable-docs
        emmake make install SUBDIRS='include' -j"$(getconf _NPROCESSORS_ONLN)"
    )
fi

# --- pixman ---------------------------------------------------------------
if wants pixman; then
    echo "=== pixman $WASM_PIXMAN_VERSION"
    fetch "https://gitlab.freedesktop.org/pixman/pixman/-/archive/pixman-$WASM_PIXMAN_VERSION/pixman-pixman-$WASM_PIXMAN_VERSION.tar.gz" pixman
    write_cross_file
    (
        cd "$src/pixman"
        rm -rf _build
        meson setup _build --prefix="$target" --cross-file="$cross_file" \
            --default-library=static \
            --buildtype=release -Dtests=disabled -Ddemos=disabled
        meson install -C _build
    )
fi

# --- glib -----------------------------------------------------------------
if wants glib; then
    echo "=== glib $WASM_GLIB_VERSION"

    # Emscripten has no resolver; glib links res_query unconditionally.  The
    # container stubs it out and so do we.
    mkdir -p "$root/stub"
    cat > "$root/stub/res_query.c" <<'EOT'
#include <netdb.h>
int res_query(const char *name, int class,
              int type, unsigned char *dest, int len)
{
    h_errno = HOST_NOT_FOUND;
    return -1;
}
EOT
    (
        cd "$root/stub"
        emcc $CFLAGS -c res_query.c -fPIC -o libresolv.o
        emar rcs libresolv.a libresolv.o
        mkdir -p "$target/lib"
        cp libresolv.a "$target/lib/"
    )

    fetch "https://download.gnome.org/sources/glib/$WASM_GLIB_MINOR_VERSION/glib-$WASM_GLIB_VERSION.tar.xz" glib
    write_cross_file "-Wno-incompatible-function-pointer-types"
    (
        cd "$src/glib"
        rm -rf _build
        meson setup _build --prefix="$target" --cross-file="$cross_file" \
            --default-library=static --buildtype=release \
            --force-fallback-for=pcre2 \
            -Dselinux=disabled -Dxattr=false -Dlibmount=disabled -Dnls=disabled \
            -Dtests=false -Dglib_debug=disabled -Dglib_assert=false \
            -Dglib_checks=false
        # Emscripten's final link does not provide these, and meson's checks
        # cannot see that.  Same workaround as the upstream container.
        sed -i.bak -E "/#define HAVE_POSIX_SPAWN 1/d" ./_build/config.h
        sed -i.bak -E "/#define HAVE_PTHREAD_GETNAME_NP 1/d" ./_build/config.h
        meson install -C _build
    )
fi

echo
echo "wasm64 sysroot: $target"
ls "$target/lib"/*.a 2>/dev/null | sed 's/^/  /'
echo "next: scripts/wasm/build-qemu.sh"
