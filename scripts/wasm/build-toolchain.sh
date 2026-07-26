#!/usr/bin/env bash
#
# Build the pinned Emscripten toolchain image used for the browser port.
#
# This wraps QEMU's own upstream cross-build container recipe
# (tests/docker/dockerfiles/emsdk-wasm64-cross.docker), which pins the
# Emscripten SDK and cross-compiles the wasm64 static dependencies QEMU needs:
# zlib, libffi, pixman and glib.  We do not maintain a second recipe; we only
# tag the result under a project-local name so the build scripts have a stable
# handle and so a toolchain change is a visible, reviewable commit here.
#
# Usage:
#   scripts/wasm/build-toolchain.sh [--platform linux/arm64] [--no-cache]
#
# The image tag and the SDK/dependency versions are recorded in
# scripts/wasm/toolchain.env, which every other wasm script sources.

set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo="$(cd "$here/../.." && pwd)"
# shellcheck source=toolchain.env
source "$here/toolchain.env"

platform=""
extra=()
while [ $# -gt 0 ]; do
    case "$1" in
        --platform)
            platform="$2"
            shift 2
            ;;
        --platform=*)
            platform="${1#--platform=}"
            shift
            ;;
        --no-cache)
            extra+=(--no-cache)
            shift
            ;;
        -h|--help)
            sed -n '2,20p' "${BASH_SOURCE[0]}"
            exit 0
            ;;
        *)
            echo "unknown argument: $1" >&2
            exit 2
            ;;
    esac
done

dockerfile="$repo/tests/docker/dockerfiles/emsdk-wasm64-cross.docker"
if [ ! -f "$dockerfile" ]; then
    echo "missing upstream toolchain recipe: $dockerfile" >&2
    exit 1
fi

if ! docker info >/dev/null 2>&1; then
    echo "the Docker daemon is not reachable; start Docker/OrbStack first" >&2
    exit 1
fi

if [ -n "$platform" ]; then
    extra+=(--platform "$platform")
fi

echo "building $WASM_BUILDER_IMAGE from $dockerfile"
echo "  emsdk    $WASM_EMSDK_VERSION"
echo "  glib     $WASM_GLIB_VERSION"
echo "  pixman   $WASM_PIXMAN_VERSION"
echo "  libffi   $WASM_FFI_VERSION"

DOCKER_BUILDKIT=1 docker build \
    -t "$WASM_BUILDER_IMAGE" \
    -f "$dockerfile" \
    --build-arg "EMSDK_VERSION_QEMU=$WASM_EMSDK_VERSION" \
    --build-arg "GLIB_MINOR_VERSION=$WASM_GLIB_MINOR_VERSION" \
    --build-arg "GLIB_VERSION=$WASM_GLIB_VERSION" \
    --build-arg "PIXMAN_VERSION=$WASM_PIXMAN_VERSION" \
    --build-arg "FFI_VERSION=$WASM_FFI_VERSION" \
    ${extra[@]+"${extra[@]}"} \
    "$repo/tests/docker/dockerfiles"

echo
echo "toolchain image ready: $WASM_BUILDER_IMAGE"
docker image inspect "$WASM_BUILDER_IMAGE" \
    --format 'digest {{index .RepoDigests 0}}{{println}}id     {{.Id}}' 2>/dev/null || true
echo "next: scripts/wasm/build-qemu.sh"
