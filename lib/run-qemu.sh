#!/usr/bin/env bash
# Boot a built tree under QEMU, frozen at the reset vector so a debugger can
# attach before the first instruction.
set -uo pipefail
_self="$(readlink -f "${BASH_SOURCE[0]}")"
KBL_REPO="$(cd -P "$(dirname "$_self")/.." && pwd)"
# shellcheck source=/dev/null
source "${KBL_REPO}/lib/common.sh"

RUN=0; PORT=""; PORT_SET=0; MEM=1G; SMP=2; KVM=0; KASLR=0; NET=1
INITRD=""; INITRD_SET=0; NO_INITRD=0; SSHPORT=""; SSHPORT_SET=0
BOOT=""; BIOS=""   # boot mode (direct|uboot); --bios overrides the firmware
PERSIST=""; PERSIST_SET=0   # per-tree persistent writable disk (survives reboot)
declare -a EXTRA=()
declare -a REST=()
_usage() {
    cat <<USAGE
kbuildlab run [TREE] [options] [-- QEMU ARGS]
  boot a built tree under QEMU, frozen at reset for a debugger (--run to boot now)

  --run              boot now instead of freezing at reset
  --boot MODE        direct | uboot | uefi (default: the tree's BOOT)
  --kaslr|--no-kaslr randomized vs deterministic base (default: --no-kaslr)
  --kvm|--no-kvm     x86 only; default TCG for deterministic early-boot HW bp
  --initrd PATH | --no-initrd    root fs image, or none (early-boot only)
  --net|--no-net     user/virtio NIC with ssh forward, or none
  --ssh-port N       host port forwarded to guest :22 (default 2222, auto-avoids)
  --persist|--no-persist   attach the tree's writable disk (default: tree PERSIST)
  --port|-g N        gdb port (default: the tree's GDB_PORT)
  --mem|-m SIZE  --smp N  --bios PATH
  TREE is a name, directory, or kernel source root; omitted, the current tree.
USAGE
}
while [[ $# -gt 0 ]]; do
    case "$1" in
        -h|--help)   _usage; exit 0 ;;
        --run)       RUN=1; shift ;;
        --port|-g)   PORT="${2:?--port needs a number}"; PORT_SET=1; shift 2 ;;
        --mem|-m)    MEM="${2:?}"; shift 2 ;;
        --smp)       SMP="${2:?}"; shift 2 ;;
        --kvm)       KVM=1; shift ;;        # opt in to KVM (x86 only, faster, less deterministic)
        --no-kvm)    KVM=0; shift ;;        # default: TCG, deterministic early-boot HW breakpoints
        --kaslr)     KASLR=1; shift ;;      # boot with KASLR active
        --no-kaslr)  KASLR=0; shift ;;      # default: nokaslr, deterministic
        --initrd)    INITRD="${2:?--initrd needs a path}"; INITRD_SET=1; shift 2 ;;
        --no-initrd) NO_INITRD=1; shift ;;  # boot without a root fs (early-boot debug)
        --net)       NET=1; shift ;;
        --no-net)    NET=0; shift ;;        # no NIC at all
        --ssh-port)  SSHPORT="${2:?--ssh-port needs a number}"; SSHPORT_SET=1; shift 2 ;;
        --boot)      BOOT="${2:?--boot needs a mode (direct|uboot)}"; shift 2 ;;
        --bios)      BIOS="${2:?--bios needs a path}"; shift 2 ;;
        --persist)    PERSIST=1; PERSIST_SET=1; shift ;;  # attach the tree's writable disk
        --no-persist) PERSIST=0; PERSIST_SET=1; shift ;;  # boot without it (volatile rootfs)
        --) shift; EXTRA=("$@"); break ;;
        -*) die "run: unknown option '$1' (pass QEMU arguments after --)" ;;
        *)  REST+=("$1"); shift ;;
    esac
done

tree="$(kbl_tree "${REST[0]:-}")" || exit 1
arch="$(kbl_tree_arch "$tree")"
src="$(kbl_tree_src "$tree")"

# A persistent disk must not be opened read-write by two guests at once, or the
# ext4 on it corrupts. QEMU's own image lock would refuse the second guest and
# abort its boot; check first so the second run degrades gracefully (boots without
# persistence) instead. Matches the exact -drive file= a live QEMU would carry.
_persist_busy() {
    local disk="$1" c
    for c in /proc/[0-9]*/cmdline; do
        [[ -r "$c" ]] || continue
        # The live -drive arg is "if=none,file=<disk>,format=raw,id=kblpersist"; match
        # that identifying tail as a substring (not a whole-line -x match, which the
        # if=none, prefix would defeat).
        tr '\0' '\n' < "$c" 2>/dev/null | grep -qF "file=${disk},format=raw,id=kblpersist" && return 0
    done
    return 1
}

image_rel="$(kbl_tree_get "$tree" KERNEL_IMAGE_REL)"
[[ -n "$image_rel" ]] || die "$tree/tree.conf states no KERNEL_IMAGE_REL"
image="$src/$image_rel"
[[ -f "$image" ]] || die "no kernel image at $image
       build it first: kbuildlab build $(basename "$tree")"

qemu="$(kbl_tree_get "$tree" QEMU_BIN)"; qemu="${qemu:-$(kbl_qemu "$arch")}"
command -v "$qemu" >/dev/null 2>&1 || die "$qemu not found (stated by $tree/tree.conf)"
machine="$(kbl_tree_get "$tree" MACHINE)"; [[ -n "$machine" ]] || die "$tree/tree.conf states no MACHINE"
cpu="$(kbl_tree_get "$tree" CPU)"
console="$(kbl_tree_get "$tree" CONSOLE)"; [[ -n "$console" ]] || die "$tree/tree.conf states no CONSOLE"
[[ -n "$PORT" ]] || PORT="$(kbl_tree_get "$tree" GDB_PORT)"
[[ -n "$PORT" ]] || die "no gdb port: state GDB_PORT in $tree/tree.conf or pass --port"

# Same guest can run more than once; give each a distinct gdb port. An explicit
# --port must be honoured or refused; a tree default that is busy auto-advances.
_busy() { [[ -n "$(ss -ltnH "sport = :$1" 2>/dev/null)" ]]; }
if _busy "$PORT"; then
    if [[ $PORT_SET -eq 1 ]]; then
        die "port $PORT is in use (you asked for it explicitly); pick another with --port"
    fi
    _orig="$PORT"; PORT=""
    for _p in $(seq $((_orig + 1)) $((_orig + 128))); do _busy "$_p" || { PORT="$_p"; break; }; done
    [[ -n "$PORT" ]] || die "no free gdb port near $_orig"
    say "port         $_orig busy -> using $PORT (attach with --port $PORT)"
fi

# Root filesystem, handed to the kernel as an initramfs.  Stated per tree with
# INITRD= in tree.conf (a path, absolute or relative to the tree); overridden by
# --initrd, suppressed by --no-initrd.  Without one the kernel panics at "mount
# root" the moment it is continued, so a missing-but-requested initrd is fatal
# here, not a surprise 30 seconds into boot.
if [[ $NO_INITRD -eq 0 ]]; then
    [[ $INITRD_SET -eq 1 ]] || INITRD="$(kbl_tree_get "$tree" INITRD)"
    if [[ -n "$INITRD" ]]; then
        [[ "$INITRD" == /* ]] || INITRD="$tree/$INITRD"
        [[ -f "$INITRD" ]] || die "initrd not found: $INITRD
       build the rootfs, fix INITRD in $tree/tree.conf, or pass --no-initrd"
    fi
fi

# Boot mode: direct (-kernel, fast, immediate head.S _text) or a firmware chain.
# Stated per tree with BOOT= in tree.conf; --boot overrides; default direct.
[[ -n "$BOOT" ]] || BOOT="$(kbl_tree_get "$tree" BOOT)"
[[ -n "$BOOT" ]] || BOOT="direct"
# OVMF/UEFI wants the modern q35 chipset on x86.
[[ "$BOOT" == uefi && "$arch" == x86_64 && "$machine" == pc ]] && machine=q35

# Persistent per-tree disk: a writable virtio-blk image on SSD, kept between runs
# (and reboots) so the guest's /persist survives. Opt-in (--persist, or PERSIST=1
# in tree.conf); its path/size are stated by the tree, never hardcoded here. The
# guest's S45persist init script finds it by the serial set below and mounts it.
# Every failure here is non-fatal -- the guest just boots without /persist.
persist_ok=0; persist_disk=""; persist_dev=""
[[ $PERSIST_SET -eq 1 ]] || PERSIST="$(kbl_tree_get "$tree" PERSIST)"
if [[ "$PERSIST" == 1 ]]; then
    persist_disk="$(kbl_tree_get "$tree" PERSIST_DISK)"; persist_disk="${persist_disk:-disk/persist.img}"
    [[ "$persist_disk" == /* ]] || persist_disk="$tree/$persist_disk"
    _psize="$(kbl_tree_get "$tree" PERSIST_SIZE)"; _psize="${_psize:-2G}"
    if ! mkdir -p "$(dirname "$persist_disk")" 2>/dev/null; then
        say "persist      cannot create $(dirname "$persist_disk") -- booting without /persist"
    elif _persist_busy "$persist_disk"; then
        say "persist      $persist_disk is in use by another run -- booting without /persist"
    else
        _pfresh=0
        if [[ ! -f "$persist_disk" ]]; then
            if truncate -s "$_psize" "$persist_disk" 2>/dev/null; then
                _pfresh=1
            else
                say "persist      cannot create $persist_disk (${_psize}) -- booting without /persist"
                rm -f "$persist_disk" 2>/dev/null
            fi
        fi
        if [[ -f "$persist_disk" ]]; then
            # Format a freshly created image now, so the guest only ever mounts it.
            # If the host has no mkfs.ext4 the guest's S45persist formats on first
            # boot instead -- both paths converge on a labelled ext4.
            if [[ $_pfresh -eq 1 ]] && command -v mkfs.ext4 >/dev/null 2>&1; then
                mkfs.ext4 -q -L kblpersist -F "$persist_disk" >/dev/null 2>&1 \
                    || say "persist      host mkfs failed; the guest will format $persist_disk"
            fi
            # virtio transport per arch, matching the NIC convention: PCIe where it
            # is reliably probed (x86 pc, arm64 virt), MMIO on riscv virt.
            case "$arch" in riscv64) persist_dev="virtio-blk-device" ;; *) persist_dev="virtio-blk-pci" ;; esac
            persist_ok=1
        fi
    fi
fi

append="console=$console"
[[ $KASLR -eq 1 ]] || append="$append nokaslr"
declare -a CMD=("$qemu" -machine "$machine" -m "$MEM" -smp "$SMP" -nographic)

# An entropy source.  A headless guest has no keyboard, no disk seek noise and no
# hardware RNG, so its CRNG is seeded by whatever the kernel can manufacture on its
# own -- and only kernels from 5.4 can (try_to_generate_entropy, jitter entropy).
# Older ones never initialise it, and the first process to call getrandom() blocks
# for ever: on v4.6 that is ssh-keygen in the rootfs's S50sshd, which stops rcS
# before init ever spawns a getty, so the guest boots to a blank console instead of
# a login prompt.  virtio-rng-pci exists on every machine kbuildlab drives (checked
# with `-device help` on all three qemu-system binaries) and costs nothing; the
# guest still needs CONFIG_HW_RANDOM_VIRTIO to use it, which the preset now sets.
CMD+=(-device virtio-rng-pci)

# Prune stale per-run scratch (RAM disks + state files) whose guest is gone.
for _f in /dev/shm/kbl-boot-*.img /dev/shm/kbl-vars-*.fd /dev/shm/kbl-run-*.env; do
    [[ -e "$_f" ]] || continue
    _fp="${_f##*-}"; _fp="${_fp%.img}"; _fp="${_fp%.fd}"; _fp="${_fp%.env}"
    _busy "$_fp" || rm -f "$_f"
done
# Record this run so `attach` can match the exact boot combination (mode, load
# address) on this gdb port -- the same kernel/name can run in several combos.
_state="/dev/shm/kbl-run-${PORT}.env"
{ printf 'KBL_TREE=%s\n' "$tree"; printf 'KBL_ARCH=%s\n' "$arch"
  printf 'KBL_BOOT=%s\n' "$BOOT"; printf 'KBL_KASLR=%s\n' "$KASLR"; } > "$_state"

shmdisk=""; shmvars=""   # per-run RAM copies (boot disk / OVMF vars)
case "$BOOT" in
    direct)
        CMD+=(-kernel "$image" -append "$append")
        [[ -n "$INITRD" ]] && CMD+=(-initrd "$INITRD")
        ;;
    uboot)
        ub="${BIOS:-$(kbl_tree_get "$tree" UBOOT)}"
        [[ -n "$ub" ]] || die "boot uboot: state UBOOT= in $tree/tree.conf or pass --bios"
        [[ "$ub" == /* ]] || ub="$tree/$ub"
        [[ -f "$ub" ]] || die "u-boot not found: $ub"
        bd="$(kbl_tree_get "$tree" BOOTDISK)"; bd="${bd:-boot/uboot.img}"
        [[ "$bd" == /* ]] || bd="$tree/$bd"
        [[ -f "$bd" ]] || die "no boot disk: $bd -- pre-build it (firmware/build-bootdisk.sh)"
        la="$(kbl_tree_get "$tree" UBOOT_LOADADDR)"; la="${la:-0x40200000}"
        ra="$(kbl_tree_get "$tree" UBOOT_RDADDR)";   ra="${ra:-0x48000000}"
        command -v mkimage >/dev/null 2>&1 || die "mkimage (uboot-tools) required for boot uboot"
        command -v mcopy  >/dev/null 2>&1 || die "mtools (mcopy) required for boot uboot"
        printf 'KBL_LOADADDR=%s\n' "$la" >> "$_state"   # attach's HW-bp goes here
        shmdisk="/dev/shm/kbl-boot-${PORT}.img"; cp -f "$bd" "$shmdisk"
        # u-boot ignores QEMU -append, so this run's bootargs ride in boot.scr.
        # The initrd is loaded, and named to booti, only when there is one: `-`
        # in its place is how booti is told there is none.  Loading it anyway
        # under --no-initrd would boot a root filesystem the caller asked not to
        # have, and the flag would look like it did nothing.
        _bc="$(mktemp -p /dev/shm)"; _bs="$(mktemp -p /dev/shm)"
        { printf 'load virtio 0:1 %s /Image\n' "$la"
          [[ -n "$INITRD" ]] && printf 'load virtio 0:1 %s /rootfs.cpio.gz\n' "$ra"
          printf "setenv bootargs '%s'\n" "$append"
          if [[ -n "$INITRD" ]]; then
              printf 'booti %s %s:${filesize} ${fdtcontroladdr}\n' "$la" "$ra"
          else
              printf 'booti %s - ${fdtcontroladdr}\n' "$la"
          fi
        } > "$_bc"
        # mkimage's arch name for riscv is "riscv", not the tree's "riscv64".
        _mka="$arch"; [[ "$arch" == riscv64 ]] && _mka="riscv"
        mkimage -A "$_mka" -O linux -T script -C none -d "$_bc" "$_bs" >/dev/null 2>&1 \
            || die "mkimage failed to build boot.scr"
        mcopy -o -i "${shmdisk}@@1M" "$_bs" ::/boot.scr
        rm -f "$_bc" "$_bs"
        # The boot disk carries the kernel and the initramfs as files, put there
        # by firmware/build-bootdisk.sh on the day it ran.  Copy today's in over
        # them.  What this writes to is the per-run /dev/shm copy, so the
        # on-disk image is untouched -- and a kernel rebuilt since then cannot
        # be shadowed by the one baked in.  That failure has no symptom: the
        # guest boots either way, and only the version string says which.
        mcopy -o -i "${shmdisk}@@1M" "$image" ::/Image \
            || die "could not write $image into the boot-disk copy -- larger than the free space in $bd? rebuild it: firmware/build-bootdisk.sh"
        if [[ -n "$INITRD" ]]; then
            mcopy -o -i "${shmdisk}@@1M" "$INITRD" ::/rootfs.cpio.gz \
                || die "could not write $INITRD into the boot-disk copy (see above)"
        else
            mdel -i "${shmdisk}@@1M" ::/rootfs.cpio.gz >/dev/null 2>&1 || true
        fi
        case "$arch" in x86_64) _blk="virtio-blk-pci" ;; *) _blk="virtio-blk-device" ;; esac
        # Firmware placement differs: on arm64 u-boot IS the -bios firmware; on
        # riscv the M-mode firmware is OpenSBI (QEMU's default -bios) and u-boot
        # is its S-mode payload, loaded via -kernel.
        case "$arch" in
            riscv64) CMD+=(-kernel "$ub") ;;
            *)       CMD+=(-bios "$ub") ;;
        esac
        CMD+=(-drive "if=none,file=${shmdisk},format=raw,id=hd0"
              -device "${_blk},drive=hd0")
        ;;
    uefi)
        # UEFI: OVMF firmware (pflash CODE read-only + a per-run writable VARS
        # copy) boots grub2 from an ESP disk. Paths per tree; --bios overrides CODE.
        code="${BIOS:-$(kbl_tree_get "$tree" OVMF_CODE)}"; vars="$(kbl_tree_get "$tree" OVMF_VARS)"
        [[ -n "$code" && -n "$vars" ]] || die "boot uefi: state OVMF_CODE= and OVMF_VARS= in $tree/tree.conf"
        [[ "$code" == /* ]] || code="$tree/$code"; [[ "$vars" == /* ]] || vars="$tree/$vars"
        [[ -f "$code" && -f "$vars" ]] || die "OVMF firmware not found: $code / $vars"
        bd="$(kbl_tree_get "$tree" BOOTDISK)"; bd="${bd:-boot/esp.img}"
        [[ "$bd" == /* ]] || bd="$tree/$bd"
        [[ -f "$bd" ]] || die "no ESP disk: $bd -- pre-build it (firmware/build-esp.sh)"
        la="$(kbl_tree_get "$tree" UEFI_ENTRY)"; la="${la:-0x1000000}"
        printf 'KBL_LOADADDR=%s\n' "$la" >> "$_state"
        command -v mcopy >/dev/null 2>&1 || die "mtools (mcopy) required for boot uefi"
        shmdisk="/dev/shm/kbl-boot-${PORT}.img"; cp -f "$bd" "$shmdisk"
        shmvars="/dev/shm/kbl-vars-${PORT}.fd"; cp -f "$vars" "$shmvars"
        # Rewrite grub.cfg with this run's bootargs (kaslr/console).  The initrd
        # line is written only when there is one: grub loads what the config
        # names, so leaving it in under --no-initrd would boot a root filesystem
        # the caller asked not to have, and the flag would look like it did
        # nothing.
        _gc="$(mktemp -p /dev/shm)"
        { printf 'search --no-floppy --set=root --file /vmlinuz\n'
          printf 'linux /vmlinuz %s\n' "$append"
          [[ -n "$INITRD" ]] && printf 'initrd /rootfs.cpio.gz\n'
          printf 'boot\n'; } > "$_gc"
        mcopy -o -i "${shmdisk}@@1M" "$_gc" ::/grub.cfg; rm -f "$_gc"
        # The ESP carries the kernel and the initramfs as files, and
        # firmware/build-esp.sh put whatever was current the day it ran into
        # them.  Copy today's in over them.  What this writes to is the per-run
        # /dev/shm copy, so the on-disk ESP is untouched, and a kernel rebuilt
        # since then cannot be shadowed by the one baked in -- a failure with no
        # symptom, because the guest boots either way and only the version
        # string says which.
        mcopy -o -i "${shmdisk}@@1M" "$image" ::/vmlinuz \
            || die "could not write $image into the ESP copy -- larger than the free space in $bd? rebuild it: firmware/build-esp.sh"
        if [[ -n "$INITRD" ]]; then
            mcopy -o -i "${shmdisk}@@1M" "$INITRD" ::/rootfs.cpio.gz \
                || die "could not write $INITRD into the ESP copy (see above)"
        else
            # Nothing names it now, but an unreferenced file is still a file a
            # future grub.cfg could pick up.  Best effort; absence is fine.
            mdel -i "${shmdisk}@@1M" ::/rootfs.cpio.gz >/dev/null 2>&1 || true
        fi
        CMD+=(-drive "if=pflash,format=raw,readonly=on,file=${code}"
              -drive "if=pflash,format=raw,file=${shmvars}"
              -drive "if=none,file=${shmdisk},format=raw,id=hd0"
              -device "virtio-blk-pci,drive=hd0")
        ;;
    *) die "unknown boot mode '$BOOT' (known: direct, uboot, uefi)" ;;
esac
# Attach the persistent disk (after any boot disk, so it never displaces hd0). The
# serial is how the guest tells it apart from the boot disk regardless of which
# /dev/vdX each lands on. Not in the cleanup trap: this image is meant to persist.
if [[ $persist_ok -eq 1 ]]; then
    CMD+=(-drive "if=none,file=${persist_disk},format=raw,id=kblpersist"
          -device "${persist_dev},drive=kblpersist,serial=kblpersist")
fi
# User-mode (SLIRP) networking on a virtio NIC: no host setup, gives the guest
# DHCP + DNS + outbound (ping/tcpdump/apt), 10.0.2.15 behind gateway 10.0.2.2.
# virtio-net-pci works on every machine here (pc, arm64 virt, riscv virt all PCIe).
# A host->guest:22 forward lets neovim edit files in the guest over SSH
# (nvim scp://root@localhost:<sshport>//path, password "root").
if [[ $NET -eq 1 ]]; then
    [[ -n "$SSHPORT" ]] || SSHPORT=2222
    if _busy "$SSHPORT"; then
        if [[ $SSHPORT_SET -eq 1 ]]; then
            die "ssh port $SSHPORT is in use (you asked for it); pick another with --ssh-port"
        fi
        _so="$SSHPORT"; SSHPORT=""
        for _p in $(seq $((_so + 1)) $((_so + 128))); do _busy "$_p" || { SSHPORT="$_p"; break; }; done
        [[ -n "$SSHPORT" ]] || die "no free ssh-forward port near $_so"
    fi
    # virtio NIC transport: PCIe on x86 (pc) and arm64 (virt); MMIO on riscv,
    # whose virt PCIe host is not reliably probed but whose virtio-mmio always is.
    case "$arch" in
        riscv64) _nicdev="virtio-net-device" ;;
        *)       _nicdev="virtio-net-pci" ;;
    esac
    CMD+=(-netdev "user,id=net0,hostfwd=tcp:127.0.0.1:${SSHPORT}-:22"
          -device "${_nicdev},netdev=net0")
fi
use_kvm=0
[[ "$arch" == "x86_64" && "$KVM" -eq 1 && -r /dev/kvm && -w /dev/kvm ]] && use_kvm=1
# `-cpu host` needs KVM; under TCG (the default, wanted for deterministic early
# -boot HW breakpoints) fall back to a fully-featured emulated CPU.
[[ "$cpu" == "host" && $use_kvm -eq 0 ]] && cpu="max"
# Paging profile as CPU features (stated per tree, never hardcoded): the kernel
# uses the widest paging the CPU advertises, so e.g. CPU_PAGING="la57=off" pins
# x86 to 4-level and "sv48=off,sv57=off" pins riscv to Sv39 -- no kernel rebuild.
cpu_paging="$(kbl_tree_get "$tree" CPU_PAGING)"
[[ -n "$cpu" && -n "$cpu_paging" ]] && cpu="${cpu},${cpu_paging}"
[[ -n "$cpu" ]] && CMD+=(-cpu "$cpu")
[[ $use_kvm -eq 1 ]] && CMD+=(-enable-kvm)
CMD+=(-gdb "tcp::${PORT}")
[[ $RUN -eq 1 ]] || CMD+=(-S)
CMD+=("${EXTRA[@]+"${EXTRA[@]}"}")

say "guest        $(basename "$tree") ($arch) on :$PORT"
if [[ "$BOOT" == direct ]]; then
    say "boot         direct (-kernel; head.S _text immediately)"
    [[ -n "$INITRD" ]] && say "initrd       $INITRD" \
                       || say "initrd       none -- kernel panics at mount-root if continued (early-boot only)"
else
    say "boot         $BOOT firmware chain; rootfs on boot disk"
fi
[[ $NET -eq 1 ]] && say "net          user/virtio (guest 10.0.2.15); ssh -p $SSHPORT root@localhost (pw: root)" \
                 || say "net          none (--no-net)"
[[ $persist_ok -eq 1 ]] && say "persist      $persist_disk -> guest /persist (survives reboot)"
[[ $KASLR -eq 1 ]] && say "kaslr        on (randomized; 'kearly kaslr auto' calibrates the slide)" \
                   || say "kaslr        off (nokaslr, deterministic)"
[[ $RUN -eq 1 ]] || say "frozen       attach with: kbuildlab attach $(basename "$tree")"
if [[ -n "$shmdisk" ]]; then
    trap 'rm -f "$shmdisk" "$shmvars" "$_state"' EXIT INT TERM
    "${CMD[@]}"
else
    exec "${CMD[@]}"
fi
