#!/usr/bin/env bash
# Write a standalone GRUB x86_64-efi binary (EFI/BOOT/BOOTX64.EFI) whose embedded
# config chainloads /grub.cfg from whichever partition holds it: the FAT boot
# partition that kbuildlab rewrites per run. build-esp.sh embeds the same stub
# through this script.
# usage: build-grub-efi.sh OUT
set -uo pipefail
out="${1:?usage: build-grub-efi.sh OUT}"
command -v grub-mkstandalone >/dev/null 2>&1 || { echo "build-grub-efi: grub-mkstandalone required" >&2; exit 1; }
cfg="$(mktemp)" || exit 1
trap 'rm -f "$cfg" "$out.tmp"' EXIT
cat > "$cfg" <<'EOF'
insmod part_gpt
insmod fat
search --no-floppy --set=root --file /grub.cfg
configfile /grub.cfg
EOF
if ! log="$(grub-mkstandalone -O x86_64-efi -o "$out.tmp" "boot/grub/grub.cfg=$cfg" 2>&1)"; then
    echo "build-grub-efi: grub-mkstandalone failed:" >&2
    printf '%s\n' "$log" >&2
    exit 1
fi
mv -f "$out.tmp" "$out" || { echo "build-grub-efi: cannot write $out" >&2; exit 1; }
