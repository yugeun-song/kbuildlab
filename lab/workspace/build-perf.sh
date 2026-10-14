#!/usr/bin/env bash
set -uo pipefail
ROOT="$(cd -P "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
[ -d "$ROOT/rootfs" ] || { echo "build-perf: $ROOT does not look like the research workspace (no rootfs/)" >&2; exit 1; }
tree="${1:?usage: build-perf.sh TREE [JOBS]   (a kbuildlab tree ID or path; its tools/perf is built for the guest)}"
jobs="${2:-${KBL_JOBS:-5}}"
kbl="${KBUILDLAB:-kbuildlab}"
src="$("$kbl" inspect "$tree" --format src)" || exit 1
arch="$("$kbl" inspect "$tree" --format arch)" || exit 1
case "$arch" in
    x86_64)  karch=x86_64; cross=x86_64-buildroot-linux-gnu- ;;
    arm64)   karch=arm64;  cross=aarch64-buildroot-linux-gnu- ;;
    riscv64) karch=riscv;  cross=riscv64-buildroot-linux-gnu- ;;
    *) echo "build-perf: unknown arch '$arch'" >&2; exit 1 ;;
esac
host="$ROOT/rootfs/$arch/host"
[ -x "$host/bin/${cross}gcc" ] || { echo "build-perf: no buildroot toolchain in $host; run ./build-rootfs.sh $arch first" >&2; exit 1; }
IMG="${KBL_IMAGE:-kbuildlab:latest}"
CRT=""
for c in ${KBL_CONTAINER:-} podman docker nerdctl; do
  command -v "$c" >/dev/null 2>&1 && { CRT="$c"; break; }
done
[ -n "$CRT" ] || { echo "no container runtime (podman, docker or nerdctl)"; exit 1; }
IDOPT=""
if [ "$(basename "$CRT")" != podman ] || [ "$(id -u)" -eq 0 ]; then
  IDOPT="--user $(id -u):$(id -g) -e HOME=/tmp"
fi
work="$(mktemp -d "${TMPDIR:-/tmp}/build-perf.XXXXXX")" || exit 1
trap 'rm -rf "$work"' EXIT
git -C "$src" archive --format=tar HEAD -- tools include arch scripts Makefile lib kernel | tar -x -C "$work" \
    || { echo "build-perf: could not export $src at HEAD" >&2; exit 1; }
for p in "$ROOT"/rootfs/perf-*.patch; do
    [ -e "$p" ] || continue
    if patch -d "$work" -p1 -R -s -f --dry-run < "$p" >/dev/null 2>&1; then
        echo "build-perf: $(basename "$p") is already in $src"
    elif patch -d "$work" -p1 -s -f < "$p"; then
        echo "build-perf: applied $(basename "$p")"
    else
        echo "build-perf: $(basename "$p") does not apply to $src; update or remove it" >&2
        exit 1
    fi
done
mounts=(-v "$ROOT:$ROOT" -v "$work:$work")
# shellcheck disable=SC2086
"$CRT" run --rm $IDOPT "${mounts[@]}" "$IMG" sh -c "
    set -e
    export PATH='$host/bin':\$PATH
    sysroot=\$(ls -d '$host/'*-buildroot-linux-gnu/sysroot)
    export PKG_CONFIG=pkg-config PKG_CONFIG_SYSROOT_DIR=\$sysroot
    export PKG_CONFIG_LIBDIR=\$sysroot/usr/lib/pkgconfig:\$sysroot/usr/share/pkgconfig
    rm -rf /tmp/perf-build && mkdir -p /tmp/perf-build
    make -s -C '$work/tools/perf' ARCH=$karch CROSS_COMPILE=$cross \
        NO_DEMANGLE=1 NO_LIBBPF=1 NO_LIBUNWIND=1 NO_SLANG=1 NO_GTK2=1 \
        NO_LIBPERL=1 NO_LIBPYTHON=1 NO_LIBNUMA=1 NO_LIBCAP=1 NO_LIBCRYPTO=1 \
        NO_JVMTI=1 NO_LIBZSTD=1 NO_LZMA=1 NO_BACKTRACE=1 NO_LIBBABELTRACE=1 WERROR=0 \
        O=/tmp/perf-build prefix=/usr PKG_CONFIG=pkg-config -j$jobs
    cp /tmp/perf-build/perf '$ROOT/rootfs/perf-$arch'
" || { echo "build-perf: the container build failed; rootfs/perf-$arch is unchanged" >&2; exit 1; }
echo "rootfs/perf-$arch from $src at $(git -C "$src" rev-parse --short=12 HEAD); run ./build-rootfs.sh $arch to put it in the guest"
