#!/usr/bin/env bash
# Pre-build a UEFI ESP disk for a tree: OVMF -> grub2 -> kernel. GPT + FAT ESP
# holding grub (BOOTX64.EFI, embedded cfg), the kernel and the initramfs. Built
# once on SSD; run-qemu makes a per-run RAM copy and patches grub.cfg bootargs.
# usage: build-esp.sh TREE (a kbuildlab tree name)
# output: <the tree's data directory>/boot/esp.img
set -uo pipefail
tree="${1:?usage: build-esp.sh TREE}"
# Everything about the tree comes from kbuildlab, which keeps its registry.
kbl="${KBUILDLAB:-kbuildlab}"
command -v "$kbl" >/dev/null 2>&1 || { echo "kbuildlab is not on PATH (or set \$KBUILDLAB)" >&2; exit 1; }
src="$("$kbl" inspect "$tree" --format src)" || exit 1
data="$("$kbl" inspect "$tree" --format data)" || exit 1
[ -d "$src" ] || { echo "no kernel source at $src" >&2; exit 1; }
path() { case "$1" in /*) echo "$1" ;; "~"/*) echo "$HOME/${1#\~/}" ;; *) echo "$data/$1" ;; esac; }

image="$src/$("$kbl" get "$tree" KERNEL_IMAGE_REL)"; initrd="$(path "$("$kbl" get "$tree" INITRD)")"
[ -f "$image" ]  || { echo "no kernel image: $image"; exit 1; }
[ -f "$initrd" ] || { echo "no initrd: $initrd"; exit 1; }
command -v grub-mkstandalone >/dev/null || { echo "grub-mkstandalone required"; exit 1; }
command -v sgdisk >/dev/null || { echo "sgdisk required"; exit 1; }

out="$data/boot"; mkdir -p "$out"; disk="$out/esp.img"
# Up to date means the payloads are IN there, not merely that the file is newer.
# Every one of these images was found holding a formatted but payload-less
# filesystem while the mtime guard reported it current, because an mtime says
# when a file was written and nothing about what is in it.  run-qemu copies the
# current kernel into its own per-run copy on every boot, so the miss had no
# symptom -- the guest booted from a disk that could not have booted it alone.
_has_payload() {
    [ -f "$disk" ] || return 1
    mdir -i "$disk@@1M" ::/vmlinuz         >/dev/null 2>&1 &&
    mdir -i "$disk@@1M" ::/EFI/BOOT/BOOTX64.EFI >/dev/null 2>&1 &&
    { [ ! -f "$initrd" ] || mdir -i "$disk@@1M" ::/rootfs.cpio.gz >/dev/null 2>&1; }
}
if [ -f "$disk" ] && [ "$disk" -nt "$image" ] && [ "$disk" -nt "$initrd" ] && _has_payload; then
    echo "up to date: $disk"; exit 0
fi
# The embedded config just chainloads /grub.cfg from the ESP, which run-qemu
# rewrites per run with that run's bootargs (kaslr/console) -- the grub.cfg
# counterpart of u-boot's boot.scr. search keeps it robust to disk enumeration.
cfg="$(mktemp)"; efi="$(mktemp)"; realcfg="$(mktemp)"
cat > "$cfg" <<'EOF'
insmod part_gpt
insmod fat
search --no-floppy --set=root --file /grub.cfg
configfile /grub.cfg
EOF
cat > "$realcfg" <<'EOF'
search --no-floppy --set=root --file /vmlinuz
linux /vmlinuz console=ttyS0 nokaslr
initrd /rootfs.cpio.gz
boot
EOF
grub-mkstandalone -O x86_64-efi -o "$efi" "boot/grub/grub.cfg=$cfg" >/dev/null 2>&1 \
    || { echo "grub-mkstandalone failed"; rm -f "$cfg" "$efi" "$realcfg"; exit 1; }

sz=$(( ( $(stat -c%s "$image") + $(stat -c%s "$initrd") + $(stat -c%s "$efi") ) / 1048576 + 40 ))
echo "building $disk (${sz}MiB)"
rm -f "$disk"; dd if=/dev/zero of="$disk" bs=1M count="$sz" status=none
sgdisk -n 1:2048:0 -t 1:EF00 -c 1:ESP "$disk" >/dev/null 2>&1
mformat -i "$disk@@1M" -v ESP ::
mmd -i "$disk@@1M" ::/EFI ::/EFI/BOOT
mcopy -i "$disk@@1M" "$efi"     ::/EFI/BOOT/BOOTX64.EFI
mcopy -i "$disk@@1M" "$realcfg" ::/grub.cfg
mcopy -i "$disk@@1M" "$image"   ::/vmlinuz
mcopy -i "$disk@@1M" "$initrd"  ::/rootfs.cpio.gz
rm -f "$cfg" "$efi" "$realcfg"
echo "done: $disk"
