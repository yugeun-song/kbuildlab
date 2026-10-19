#!/usr/bin/env bash
# Build U-Boot for the eMMC-booting boards: the stock QEMU defconfig plus the
# fragments firmware/u-boot-*.config (MMC over PCI), out of tree in
# firmware/u-boot/build-ARCH with the host cross compiler on PATH. The U-Boot
# sources are only read.
# usage: build-uboot.sh <arm64|riscv64>
# output: firmware/u-boot/build-ARCH/u-boot.bin
set -uo pipefail
here="$(cd -P "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
src="$here/u-boot"
die() { echo "build-uboot: $*" >&2; exit 1; }
unset KCONFIG_CONFIG

case "${1:-}" in
    arm64)   defconfig=qemu_arm64_defconfig;         cross=aarch64-linux-gnu-; bootcmd='bootflow scan -lb' ;;
    riscv64) defconfig=qemu-riscv64_smode_defconfig; cross=riscv64-linux-gnu-; bootcmd='run distro_bootcmd' ;;
    *)       die "usage: build-uboot.sh <arm64|riscv64>" ;;
esac
arch="$1"
[ -f "$src/configs/$defconfig" ] || die "no U-Boot source at $src (configs/$defconfig is missing)"
command -v "${cross}gcc" >/dev/null 2>&1 || die "${cross}gcc is not on PATH"
frags=("$here/u-boot-mmc-pci.config")
[ -f "$here/u-boot-$arch.config" ] && frags+=("$here/u-boot-$arch.config")
[ -f "${frags[0]}" ] || die "missing fragment ${frags[0]}"
build="$src/build-$arch"
mkdir -p "$build" || die "cannot create $build"
mk() { make -C "$src" O="$build" CROSS_COMPILE="$cross" "$@"; }

# Buildroot's flow for kconfig packages: defconfig, merge, olddefconfig. The merge
# runs in the build directory because merge_config.sh writes its temporary file
# into the current one.
mk "$defconfig" >/dev/null || die "make $defconfig failed"
(cd "$build" && sh "$src/scripts/kconfig/merge_config.sh" -m .config "${frags[@]}") >/dev/null \
    || die "merge_config.sh failed"
mk olddefconfig >/dev/null || die "make olddefconfig failed"

# Kconfig drops a request whose dependencies are unmet without a word.
lost=0
for f in "${frags[@]}"; do
    while IFS= read -r line; do
        case "$line" in
            CONFIG_*=*|"# CONFIG_"*" is not set")
                grep -qxF -- "$line" "$build/.config" \
                    || { echo "build-uboot: lost from .config: $line" >&2; lost=1; } ;;
        esac
    done < "$f"
done
grep -qxF "CONFIG_BOOTCOMMAND=\"$bootcmd\"" "$build/.config" \
    || { echo "build-uboot: the boot command is no longer \"$bootcmd\"" >&2; lost=1; }
[ "$lost" -eq 0 ] || die "$build/.config does not hold the fragments; not building"

mk -j"$(nproc)" u-boot.bin || die "the build failed"
echo "done: $build/u-boot.bin"
