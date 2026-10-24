#!/usr/bin/env bash
# Boot a built tree under QEMU, frozen at the reset vector so a debugger can
# attach before the first instruction.
set -uo pipefail
_self="$(readlink -f "${BASH_SOURCE[0]}")"
KBL_REPO="$(cd -P "$(dirname "$_self")/.." && pwd)"
# shellcheck source=/dev/null
source "${KBL_REPO}/lib/common.sh"

# One brace group, parsed whole before any of it runs: editing this file while a
# guest runs cannot change what the launcher does when the guest exits.
{
RUN=0; PORT=""; PORT_SET=0; MEM=""; SMP=""; KVM=0; KASLR=""; NET=1
QMP=1; QMPPORT=""; QMPPORT_SET=0   # QMP monitor: what drgn attaches through
INITRD=""; INITRD_SET=0; NO_INITRD=0; SSHPORT=""; SSHPORT_SET=0
ROOT=""; RESET_SYSDISK=0    # root fs (initramfs|disk); --reset-sysdisk remakes its disk
BOOT=""; BIOS=""   # boot mode (direct|uboot); --bios overrides the firmware
declare -a APPEND_EXTRA=()   # extra kernel command-line words (--append, CMDLINE_EXTRA)
PERSIST=""; PERSIST_SET=0   # per-tree persistent writable disk (survives reboot)
EMULATE=""; EMULATE_SET=0   # emulated controllers for the preset's drivers to bind
MODULES=1                   # append the modules kbuildlab build staged to the initramfs
BOARD=""; BOARD_SET=0       # --board: a real SoC's machine model (boards/NAME.conf)
IOMMU=""; IOMMU_SET=0
declare -a PCIE_EXTRA=()
declare -a EXTRA=()
declare -a REST=()
_usage() {
    cat <<'USAGE'
kbuildlab run [TREE] [options] [-- QEMU ARGS]
  boot a built tree under QEMU, frozen at reset for a debugger (--run to boot now)

  --run              boot now instead of freezing at reset
  --boot MODE        direct | uboot | uefi (default: the tree's BOOT)
  --kaslr|--no-kaslr randomized vs deterministic base (default: the tree's
                     KASLR, else off); --nokaslr is --no-kaslr
  --kvm|--no-kvm     x86 only; default TCG for deterministic early-boot HW bp
  --initrd PATH | --no-initrd    root fs image, or none (early-boot only)
  --root KIND        initramfs | disk (default: the tree's ROOT, else initramfs);
                     disk boots partition 2 of the tree's GPT system disk
  --reset-sysdisk    delete the system disk and make it again from ROOTFS_IMAGE
  --append WORDS     extra kernel command-line words, repeatable; added after
                     the tree's CMDLINE_EXTRA. Reaches the guest in every boot
                     mode: -append (direct), boot.scr bootargs (u-boot),
                     grub.cfg linux (uefi)
  --net|--no-net     user/virtio NIC with ssh forward, or none
  --ssh-port N       host port forwarded to guest :22 (default: the tree's
                     SSH_PORT, else 2222; a busy default moves on)
  --persist|--no-persist   attach the tree's writable disk (default: tree PERSIST)
  --emulate|--no-emulate   attach the emulated controllers the soft presets build
                     drivers for: NVMe, virtio-scsi and USB disks, USB and
                     virtio input, virtio-gpu, virtio sound, virtio
                     crypto/balloon/console, and a CTU CAN FD whose two
                     channels share one bus (default: the tree's EMULATE)
  --pcie-device SPEC a qemu -device SPEC behind a PCIe root port of its own,
                     repeatable; added after the tree's PCIE_DEVICES
  --iommu|--no-iommu DMA through the machine's IOMMU: intel-iommu on q35,
                     SMMUv3 on arm64 virt, riscv-iommu-pci on a riscv64 virt
                     with aia=aplic-imsic (default: the tree's IOMMU)
  --modules|--no-modules   append the modules `kbuildlab build` staged to the
                     initramfs (with ROOT=disk, put them on the boot partition),
                     those the devices present ask for loaded at boot
                     (default: on whenever the build has modules)
  --board [NAME]     boot on a real SoC's machine model, boards/NAME.conf, in
                     place of the tree's machine: direct boot, the tree's
                     initramfs; without NAME, list the boards for the tree's arch
  --port|-g N        gdb port (default: the tree's GDB_PORT)
  --qmp-port N       QMP port (default: the tree's QMP_PORT); this is drgn's
                     way in -- `kbuildlab drgn` uses it, and unlike the gdbstub
                     a QMP client does not pause the guest or displace gdb
  --no-qmp           no QMP monitor and no vmcoreinfo device
  --mem|-m SIZE  --smp N   (default: the tree's MEM and SMP, else 1G and 2;
                     handed to qemu's -m and -smp as written)
  --bios PATH        a relative PATH here is from the current directory; in
                     tree.conf it is from the tree's data directory
  The console is the guest's serial line: Ctrl-C goes to the guest, Ctrl-A X
  quits qemu, Ctrl-A C switches to qemu's monitor and back.
  TREE is a tree's ID (or a unique prefix of 4+ hex digits) or a path inside its source; omitted, the current tree.
USAGE
}
while [[ $# -gt 0 ]]; do
    case "$1" in
        -h|--help)   _usage; exit 0 ;;
        --run)       RUN=1; shift ;;
        --port|-g)   PORT="${2:?--port needs a number}"; PORT_SET=1; shift 2 ;;
        --qmp-port)  QMPPORT="${2:?--qmp-port needs a number}"; QMPPORT_SET=1; shift 2 ;;
        --qmp)       QMP=1; shift ;;
        --no-qmp)    QMP=0; shift ;;
        --mem|-m)    MEM="${2:?}"; shift 2 ;;
        --smp)       SMP="${2:?}"; shift 2 ;;
        --kvm)       KVM=1; shift ;;        # opt in to KVM (x86 only, faster, less deterministic)
        --no-kvm)    KVM=0; shift ;;        # default: TCG, deterministic early-boot HW breakpoints
        --kaslr)     KASLR=1; shift ;;      # boot with KASLR active
        --no-kaslr|--nokaslr) KASLR=0; shift ;;  # default: nokaslr, deterministic
        --initrd)    INITRD="${2:?--initrd needs a path}"; INITRD="$(realpath -m -s -- "$INITRD")"
                     INITRD_SET=1; shift 2 ;;
        --no-initrd) NO_INITRD=1; shift ;;  # boot without a root fs (early-boot debug)
        --root)      ROOT="${2:?--root needs initramfs or disk}"; shift 2 ;;
        --reset-sysdisk) RESET_SYSDISK=1; shift ;;
        --append)    APPEND_EXTRA+=("${2:?--append needs a string}"); shift 2 ;;
        --net)       NET=1; shift ;;
        --no-net)    NET=0; shift ;;        # no NIC at all
        --ssh-port)  SSHPORT="${2:?--ssh-port needs a number}"; SSHPORT_SET=1; shift 2 ;;
        --boot)      BOOT="${2:?--boot needs a mode (direct|uboot)}"; shift 2 ;;
        --bios)      BIOS="${2:?--bios needs a path}"; BIOS="$(realpath -m -s -- "$BIOS")"; shift 2 ;;
        --persist)    PERSIST=1; PERSIST_SET=1; shift ;;  # attach the tree's writable disk
        --no-persist) PERSIST=0; PERSIST_SET=1; shift ;;  # boot without it (volatile rootfs)
        --emulate)    EMULATE=1; EMULATE_SET=1; shift ;;
        --no-emulate) EMULATE=0; EMULATE_SET=1; shift ;;
        --pcie-device) PCIE_EXTRA+=("${2:?--pcie-device needs a device}"); shift 2 ;;
        --iommu)      IOMMU=1; IOMMU_SET=1; shift ;;
        --no-iommu)   IOMMU=0; IOMMU_SET=1; shift ;;
        --modules)    MODULES=1; shift ;;
        --no-modules) MODULES=0; shift ;;
        --board)      BOARD_SET=1; if [[ $# -gt 1 && "$2" != -* ]]; then BOARD="$2"; shift; fi; shift ;;
        --) shift; EXTRA=("$@"); break ;;
        -*) die "run: unknown option '$1' (pass QEMU arguments after --)" ;;
        *)  REST+=("$1"); shift ;;
    esac
done

tree="$(kbl_tree "${REST[0]:-}")" || exit 1
arch="$(kbl_tree_arch "$tree")" || exit 1
src="$(kbl_tree_src "$tree")" || exit 1
_statedir="$(kbl_statedir)" || exit 1

# --board NAME: a real SoC's machine model in place of the tree's machine, so its
# embedded buses run on real controllers.  boards/NAME.conf states it in the
# tree.conf grammar, and its values win over the tree's for this run.
board=""; _bconf=""; _buart=0; _bdtb=""; _bnic=""; _bpdrive=""; _nofwcfg=0; _nopci=0; _bdrop=()
_bget() { kbl_conf_get "$_bconf" "$1"; }
if [[ $BOARD_SET -eq 1 ]]; then
    _bl=()
    for _f in "$KBL_REPO"/boards/*.conf; do
        [[ -f "$_f" && "$(kbl_conf_get "$_f" ARCH)" == "$arch" ]] && _bl+=("$(basename "$_f" .conf)")
    done
    if [[ -z "$BOARD" ]]; then
        [[ ${#_bl[@]} -gt 0 ]] || die "no board in $KBL_REPO/boards serves $arch"
        printf 'boards for %s (kbuildlab run %s --board NAME):\n' "$arch" "$(kbl_tree_label "$tree")"
        printf '  %s\n' "${_bl[@]}"
        exit 0
    fi
    _bconf="$KBL_REPO/boards/$BOARD.conf"
    [[ "$BOARD" =~ ^[A-Za-z0-9._-]+$ && -f "$_bconf" ]] \
        || die "no board '$BOARD'; boards for $arch: ${_bl[*]:-none}"
    _barch="$(_bget ARCH)"
    [[ "$_barch" == "$arch" ]] || die "board $BOARD is for ${_barch:-no stated ARCH}, and $(kbl_tree_label "$tree") is $arch
       boards for $arch: ${_bl[*]:-none}"
    board="$BOARD"
    [[ -z "$BOOT" || "$BOOT" == direct ]] || die "--boot $BOOT: a board boots directly (--board $board)"
    [[ "$ROOT" != disk ]] || die "--root disk: a board boots the tree's initramfs (--board $board)"
    BOOT=direct
    _bmachine="$(_bget MACHINE)"; [[ -n "$_bmachine" ]] || die "$_bconf states no MACHINE"
    _bcpu="$(_bget CPU)"
    _bconsole="$(_bget CONSOLE)"; [[ -n "$_bconsole" ]] || die "$_bconf states no CONSOLE"
    _buart="$(_bget CONSOLE_UART)"; _buart="${_buart:-0}"
    [[ "$_buart" =~ ^[0-9]+$ ]] || die "CONSOLE_UART in $_bconf is '$_buart'; it is a UART's index, from 0"
    for _k in FW_CFG PCI; do
        _v="$(_bget "$_k")"
        [[ "$_v" == 0 || "$_v" == 1 ]] || die "$_k in $_bconf is '$_v'; it is 1 when the machine has one, else 0"
    done
    [[ "$(_bget FW_CFG)" == 1 ]] || _nofwcfg=1
    [[ "$(_bget PCI)" == 1 ]] || _nopci=1
    _bnic="$(_bget NIC_MODEL)"
    [[ $NET -eq 0 || -n "$_bnic" ]] || die "$_bconf states no NIC_MODEL (the board's own NIC); pass --no-net"
    _bpdrive="$(_bget PERSIST_DRIVE)"
    [[ -n "$MEM" ]] || MEM="$(_bget MEM)"
    [[ -n "$SMP" ]] || SMP="$(_bget SMP)"
    read -r -a _bkc <<<"$(_bget KCONFIG)"
    if [[ ${#_bkc[@]} -gt 0 ]]; then
        [[ -f "$src/.config" ]] || die "no .config in $src to check $board's KCONFIG against"
        _miss=()
        for _s in "${_bkc[@]}"; do
            [[ "$_s" =~ ^CONFIG_[A-Za-z0-9_]+$ ]] || die "KCONFIG in $_bconf names '$_s', which is not a CONFIG_ symbol"
            grep -Eqx "${_s}=[ym]" "$src/.config" || _miss+=("$_s")
        done
        [[ ${#_miss[@]} -eq 0 ]] || die "$(kbl_tree_label "$tree") cannot run on $board: its .config lacks
       ${_miss[*]} (KCONFIG in $_bconf)"
    fi
    _bdtb="$(_bget DTB)"
    if [[ -n "$_bdtb" ]]; then
        [[ "$_bdtb" == /* ]] || _bdtb="$src/$_bdtb"
        [[ -f "$_bdtb" ]] || die "no device tree at $_bdtb (DTB in $_bconf)
       build it first: kbuildlab build $(kbl_tree_label "$tree")"
    fi
fi

# What the tree states for its runs; a flag wins over it, and the values after
# :- are the defaults when neither says.
[[ -n "$MEM" ]] || MEM="$(kbl_tree_get "$tree" MEM)"; MEM="${MEM:-1G}"
[[ -n "$SMP" ]] || SMP="$(kbl_tree_get "$tree" SMP)"; SMP="${SMP:-2}"
[[ -n "$KASLR" ]] || KASLR="$(kbl_tree_get "$tree" KASLR)"; KASLR="${KASLR:-0}"
[[ "$KASLR" == 0 || "$KASLR" == 1 ]] || die "KASLR in $tree/tree.conf is '$KASLR'; it is 0 or 1"
# MEM and SMP go to qemu as written, so every form it takes works (2G,maxmem=8G;
# 4,cores=2) and qemu judges them; only a blank would split its command line.
[[ "$MEM" != *[[:space:]]* ]] || die "MEM is '$MEM'; it may not contain blanks"
[[ "$SMP" != *[[:space:]]* ]] || die "SMP is '$SMP'; it may not contain blanks"
# The leading size of a -m value in bytes, read as qemu reads it (a bare number
# is MiB, a fraction such as 1.5G is allowed), or nothing for a form this does
# not parse.  Only the u-boot address check below needs it.
_mem_bytes() {
    local m="${1#size=}"; m="${m%%,*}"
    awk -v m="$m" 'BEGIN {
        if (m !~ /^[0-9]+(\.[0-9]+)?[KkMmGgTt]?$/) exit 1
        u = toupper(substr(m, length(m))); n = m + 0
        if (u == "K") n *= 1024; else if (u == "G") n *= 1024 ^ 3
        else if (u == "T") n *= 1024 ^ 4; else n *= 1024 ^ 2
        printf "%.0f\n", n
    }'
}
_membytes="$(_mem_bytes "$MEM")" || _membytes=""

# A persistent disk must not be opened read-write by two guests at once, or the
# ext4 on it corrupts. QEMU's own image lock would refuse the second guest and
# abort its boot; check first so the second run degrades gracefully (boots without
# persistence) instead. Matches the exact -drive file= a live QEMU would carry.
# The system disk of ROOT=disk is checked the same way, under its drive id hd0.
_persist_busy() {   # _persist_busy DISK [DRIVE-ID]
    local disk="$1" id="${2:-kblpersist}" c
    for c in /proc/[0-9]*/cmdline; do
        [[ -r "$c" ]] || continue
        # The live -drive arg is "if=none,file=<disk>,format=raw,id=kblpersist"; match
        # that identifying tail as a substring (not a whole-line -x match, which the
        # if=none, prefix would defeat).
        # A process can exit between the glob and the read; bash itself would
        # report the failed redirection, so the group takes the message.
        { tr '\0' '\n' < "$c"; } 2>/dev/null | grep -qF "file=${disk},format=raw,id=${id}" && return 0
    done
    return 1
}

image_rel="$(kbl_tree_get "$tree" KERNEL_IMAGE_REL)"
[[ -n "$image_rel" ]] || die "$tree/tree.conf states no KERNEL_IMAGE_REL"
image="$src/$image_rel"
[[ -f "$image" ]] || die "no kernel image at $image
       build it first: kbuildlab build $(kbl_tree_label "$tree")"

qemu="$(kbl_tree_get "$tree" QEMU_BIN)"; qemu="${qemu:-$(kbl_qemu "$arch")}"
command -v "$qemu" >/dev/null 2>&1 || die "$qemu not found (stated by $tree/tree.conf)"
machine="$(kbl_tree_get "$tree" MACHINE)"; [[ -n "$machine" ]] || die "$tree/tree.conf states no MACHINE"
cpu="$(kbl_tree_get "$tree" CPU)"
console="$(kbl_tree_get "$tree" CONSOLE)"; [[ -n "$console" ]] || die "$tree/tree.conf states no CONSOLE"
[[ -n "$board" ]] && { machine="$_bmachine"; cpu="$_bcpu"; console="$_bconsole"; }
[[ -n "$PORT" ]] || PORT="$(kbl_tree_get "$tree" GDB_PORT)"
[[ -n "$PORT" ]] || die "no gdb port: state GDB_PORT in $tree/tree.conf or pass --port"

# Same guest can run more than once; give each a distinct gdb port. An explicit
# --port must be honoured or refused; a tree default that is busy auto-advances.
# Is this port taken?  Two ways, and both are needed.
#
# Something LISTENING is the obvious one.  The other is a `run` that has CLAIMED
# the port but not started qemu yet: setting a firmware chain up takes seconds
# (mkimage, mcopy, a 90-128 MB copy), nothing listens for the whole of that
# window, and a second run started inside it used to pick the same number --
# after which the loser's cleanup deleted the winner's live boot disk and run
# state.  The claim is a state file created with O_EXCL whose KBL_LAUNCHER_PID is
# alive; a file left by a dead launcher is not a claim and does not block.
_busy() {
    [[ -n "$(ss -ltnH "sport = :$1" 2>/dev/null)" ]] && return 0
    kbl_launcher_alive "$_statedir/kbl-run-$1.env"
}

# Take the port, or fail because someone else already did.  noclobber makes the
# create O_EXCL, so the winner is decided by the kernel and not by the gap
# between a test and a write.
#
# A file left by a DEAD launcher is not a claim, and _busy() already says so --
# but O_EXCL only sees that a file exists, so without this second step a crashed
# run made its port permanently unusable: `--port N` was refused as "in use" with
# nothing on it, and the default port silently shifted by one for ever.  The
# retry is exactly one, and only after the owner has been shown to be gone.
# Two runs can see the same dead owner at once, so the check and the rm run
# under flock: without it the second rm removed the first run's fresh claim.
_claim_port() {
    local port="$1" f lp; f="$_statedir/kbl-run-${port}.env"
    # The launcher's start time goes in beside its pid, for the same reason
    # qemu's does: a pid on its own is not an identity, and a reused one would
    # make a dead run's claim look live for ever.
    _try() { ( set -o noclobber
               printf 'KBL_SCHEMA=2\nKBL_GDB_PORT=%s\nKBL_LAUNCHER_PID=%s\nKBL_LAUNCHER_START=%s\nKBL_SETTING_UP=1\n' \
                   "$port" "$$" "$(kbl_proc_starttime $$ || echo)" > "$f" ) 2>/dev/null; }
    _try && return 0
    (
        flock -w 10 9 || exit 1
        lp="$(sed -n 's/^KBL_LAUNCHER_PID=//p' "$f" 2>/dev/null | head -1)"
        [[ -n "$lp" ]] && kill -0 "$lp" 2>/dev/null && exit 1      # a live owner
        rm -f "$f" 2>/dev/null
        _try
    ) 9>>"$_statedir/kbl-claim.lock"
}
_claim_aux() {   # _claim_aux KIND PORT
    local f="$_statedir/kbl-$1-$2.claim"
    _try_aux() { ( set -o noclobber
                   printf 'KBL_LAUNCHER_PID=%s\nKBL_LAUNCHER_START=%s\n' "$$" "$(kbl_proc_starttime $$ || echo)" > "$f" ) 2>/dev/null; }
    if _try_aux || ( flock -w 10 9 || exit 1
                     kbl_launcher_alive "$f" && exit 1
                     rm -f "$f" 2>/dev/null
                     _try_aux ) 9>>"$_statedir/kbl-claim.lock"; then
        _claims+=("$f")
        return 0
    fi
    return 1
}
# Prune per-run scratch whose guest is gone.  The rule (a live qemu carries that
# gdb port, AND something is listening on it) lives in common.sh so `attach`
# applies exactly the same one -- a port that an unrelated process has taken must
# not have its file reclaimed, and a port whose guest is alive must not either.
kbl_prune_state

# Choose AND claim in one loop.  Testing first and claiming later is the race
# itself: between the two, another run can take the number.  So the claim is the
# test -- if O_EXCL fails, that port is gone and the search moves on, which is
# the behaviour a caller who did not name a port already expects.  An explicit
# --port has nowhere to move to and is refused.
_orig="$PORT"
if [[ $PORT_SET -eq 1 ]]; then
    if [[ -n "$(ss -ltnH "sport = :$PORT" 2>/dev/null)" ]]; then
        die "port $PORT already has a listener (you asked for it explicitly); pick another with --port"
    fi
    if ! _claim_port "$PORT"; then
        _lp="$(sed -n 's/^KBL_LAUNCHER_PID=//p' "$_statedir/kbl-run-${PORT}.env" 2>/dev/null | head -1)"
        die "port $PORT is being set up by another run (launcher pid ${_lp:-?}); pick another with --port"
    fi
else
    _claimed=0; _span=128
    for _p in "$PORT" $(seq $((_orig + 1)) $((_orig + _span))); do
        _busy "$_p" && continue
        _claim_port "$_p" || continue         # lost the race for this one; try the next
        PORT="$_p"; _claimed=1; break
    done
    [[ $_claimed -eq 1 ]] || die "no free gdb port in $_orig..$((_orig + _span))"
    [[ "$PORT" == "$_orig" ]] || \
        say "port         $_orig busy -> using $PORT (attach with --port $PORT)"
fi

# The claim exists from here on, so the thing that releases it must exist from
# here on too.  Every `die` between the claim and the launch -- an unbuildable
# command line, a missing firmware file, a full tmpfs -- used to leave the claim
# behind and make that port unusable until something pruned it.
_state="$_statedir/kbl-run-${PORT}.env"
_loadaddr=""   # set by a firmware chain below; recorded with the rest of the state
shmdisk=""; shmvars=""; _bc=""; _bs=""; _gc=""; _qpid=""; _ird=""   # per-run scratch
qmpsock=""                                                 # QMP unix socket, same
wsock=""; _wpid=""; _claims=()
# A throwaway copy of a busy system disk, and a system disk being made; neither
# ever names SYSDISK itself, so the cleanup cannot remove it.
_sdcopy=""; _sdnew=""

# The terminal `-nographic` is about to put into raw mode, saved here so it can be
# put back.  qemu restores it itself when it exits normally and cannot restore it
# at all when it dies of a signal -- and dying of a signal is not hypothetical on a
# guest being debugged: a debug read that QEMU dispatches into a device model SEGVs
# it outright (hw/intc/arm_gic.c's gic_get_current_cpu() reads current_cpu->cpu_index,
# and current_cpu is NULL on the gdbstub's thread, whenever the guest has more than
# one core).  What that left behind was a shell with no echo, no line editing and no
# Ctrl-C -- measured on a pty: ECHO, ICANON, ISIG, IEXTEN, ICRNL and IXON all cleared
# -- and nothing on screen to say why, or that `reset` would undo it.
_tty_saved=""
[[ -t 0 ]] && _tty_saved="$(stty -g 2>/dev/null || echo)"

# Installed HERE, before anything below creates any of it.  The boot-disk copy is
# 88-139 MB in tmpfs and every `die` between its creation and the launch used to
# leave it there -- a failure path that fills /dev/shm and reports a build error
# as the reason.  Empty variables expand to nothing, so this is safe to arm
# before the files exist.
_cleanup() {
    # The guest goes down FIRST.  Its boot disk and OVMF variable store are among
    # the files below, so removing them while qemu is still running leaves a guest
    # whose block device has no backing file and whose run state no longer exists
    # -- `attach` cannot even name it, and nothing on screen says what happened.
    # `exec` made this case impossible by replacing this shell; running qemu as a
    # child (which is what lets the state file carry its pid, and what makes the
    # trap fire on a direct boot at all) brought the case with it.
    if [[ -n "${_wpid:-}" ]]; then
        kill -TERM "$_wpid" 2>/dev/null
        wait "$_wpid" 2>/dev/null
    fi
    if [[ -n "${_qpid:-}" ]] && kill -0 "$_qpid" 2>/dev/null; then
        kill -TERM "$_qpid" 2>/dev/null
        local _i
        for _i in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15; do
            kill -0 "$_qpid" 2>/dev/null || break
            sleep 0.2
        done
        kill -0 "$_qpid" 2>/dev/null && kill -KILL "$_qpid" 2>/dev/null
    fi
    # Only what is still OURS.  If another run has taken this port since -- which
    # it may, once our guest is gone -- its state file names a different launcher,
    # and deleting it (and the boot disk it is about to use) is exactly the damage
    # the claim above exists to prevent.
    if [[ -n "${_state:-}" && -r "$_state" ]]; then
        local _owner
        _owner="$(sed -n 's/^KBL_LAUNCHER_PID=//p' "$_state" | head -1)"
        [[ "$_owner" == "$$" ]] && rm -f "$_state"
    fi
    rm -f ${shmdisk:+"$shmdisk"} ${shmvars:+"$shmvars"} ${_ird:+"$_ird"} \
          ${_bc:+"$_bc"} ${_bs:+"$_bs"} ${_gc:+"$_gc"} ${qmpsock:+"$qmpsock"} \
          ${wsock:+"$wsock"} ${_sdcopy:+"$_sdcopy"} ${_sdnew:+"$_sdnew"} \
          ${_claims[@]+"${_claims[@]}"}
    # Last, and only once the guest that owns the terminal is gone.  A no-op on
    # every path where qemu put it back itself, and the difference between a
    # usable shell and an apparently dead one where it did not.
    #
    # SIGTTOU is ignored across it because setting terminal attributes from a
    # BACKGROUND process group raises it, and its default action stops the shell --
    # so `kbl run &` from an interactive shell would freeze here instead of tidying
    # up.  With the signal ignored, POSIX lets the call simply complete.  Reading
    # them with `stty -g` above needs no such guard; only writing them does.
    if [[ -n "${_tty_saved:-}" ]]; then
        trap '' TTOU
        stty "$_tty_saved" 2>/dev/null
        trap - TTOU
    fi
    return 0
}
# HUP is here because closing the terminal a guest runs in is a real way for this
# to end, and the one where an orphan is least likely to be noticed.  INT is
# listed for completeness: a shell that starts this asynchronously sets SIGINT to
# SIG_IGN in the child, and an ignored signal cannot be trapped, so that entry
# takes effect only for a foreground run -- which is the case where Ctrl-C is
# actually typed.
trap _cleanup EXIT HUP INT TERM

# What the root file system is: an initramfs (below), or with ROOT=disk
# partition 2 of a GPT system disk, as on a device.  --initrd and --no-initrd
# keep their meaning, so with either one this run boots no system disk.
[[ "$ROOT" == disk && ( $INITRD_SET -eq 1 || $NO_INITRD -eq 1 ) ]] \
    && die "--root disk contradicts --initrd and --no-initrd"
[[ -n "$ROOT" ]] || ROOT="$(kbl_tree_get "$tree" ROOT)"; ROOT="${ROOT:-initramfs}"
[[ "$ROOT" == initramfs || "$ROOT" == disk ]] \
    || die "ROOT is '$ROOT' ($tree/tree.conf or --root); it is initramfs or disk"
[[ $INITRD_SET -eq 1 || $NO_INITRD -eq 1 ]] && ROOT=initramfs
if [[ -n "$board" && "$ROOT" == disk ]]; then
    ROOT=initramfs
    say "root         initramfs: $board boots the tree's INITRD (its ROOT=disk needs a system disk)"
fi
[[ $RESET_SYSDISK -eq 0 || "$ROOT" == disk ]] \
    || die "--reset-sysdisk: this run boots no system disk (ROOT=initramfs, --initrd or --no-initrd)"

# Root filesystem, handed to the kernel as an initramfs.  Stated per tree with
# INITRD= in tree.conf (absolute, or relative to the tree's data directory); overridden by
# --initrd, suppressed by --no-initrd.  Without one the kernel panics at "mount
# root" the moment it is continued, so a missing-but-requested initrd is fatal
# here, not a surprise 30 seconds into boot.
if [[ $NO_INITRD -eq 0 && "$ROOT" == initramfs ]]; then
    [[ $INITRD_SET -eq 1 ]] || INITRD="$(kbl_tree_get "$tree" INITRD)"
    if [[ -n "$INITRD" ]]; then
        INITRD="$(kbl_tree_path "$tree" "$INITRD")"
        [[ -f "$INITRD" ]] || die "initrd not found: $INITRD
       build the rootfs, fix INITRD in $tree/tree.conf, or pass --no-initrd"
    fi
fi

# The build's modules, staged beside the tree by `kbuildlab build`, follow the
# rootfs as a second archive: the kernel unpacks concatenated archives as one.
# With ROOT=disk there is no initramfs to follow, so the archive rides on the
# system disk's boot partition beside the kernel, and the rootfs installs it from
# there.  (Not fw_cfg: the kernel reads a fw_cfg file a page at a time, skipping
# to each page byte by byte, which takes hours for a 30 MB archive.)
# An archive older than the build's own modules would not load into this
# kernel, so it is left out and said so.
rootfs="$INITRD"; mods=""; _mfw=""; _marc="$(kbl_tree_data "$tree")/modules.cpio.gz"
if [[ ( -n "$INITRD" || "$ROOT" == disk ) && $MODULES -eq 1 && -f "$_marc" ]]; then
    if [[ "$src/modules.order" -nt "$_marc" \
          || -n "$(find "$src" -name '*.ko' -newer "$_marc" -print -quit 2>/dev/null)" ]]; then
        warn "$_marc is older than the build's modules; booting without them
       (kbuildlab build $(kbl_tree_label "$tree") stages them again)"
    elif [[ "$ROOT" == disk ]]; then
        _mfw="$_marc"; mods="$_marc"
    else
        _ird="$_statedir/kbl-initrd-${PORT}.cpio.gz"
        cat "$INITRD" "$_marc" > "$_ird" || die "could not write $_ird"
        INITRD="$_ird"; mods="$_marc"
    fi
fi

# Boot mode: direct (-kernel, fast, immediate head.S _text) or a firmware chain.
# Stated per tree with BOOT= in tree.conf; --boot overrides; default direct.
[[ -n "$BOOT" ]] || BOOT="$(kbl_tree_get "$tree" BOOT)"
[[ -n "$BOOT" ]] || BOOT="direct"
# OVMF/UEFI wants the modern q35 chipset on x86.
if [[ "$BOOT" == uefi && "$arch" == x86_64 && "$machine" == pc ]]; then
    machine=q35
    say "machine      q35 for this UEFI boot (the tree states pc; OVMF wants q35)"
fi

# The bus of every disk this run attaches (STORAGE): virtio as always; nvme, one
# controller with a namespace per disk; or emmc, an eMMC for the system or boot
# disk and an SD card for the persistent one, each behind its own PCI SD host on
# a PCIe root port (chassis 101 and 102), so no machine's root bus is named.
STORAGE="$(kbl_tree_get "$tree" STORAGE)"; STORAGE="${STORAGE:-virtio}"
[[ "$STORAGE" == virtio || "$STORAGE" == nvme || "$STORAGE" == emmc ]] \
    || die "STORAGE in $tree/tree.conf is '$STORAGE'; it is virtio, nvme or emmc"
_chassis=100
_root_port() {
    _chassis=$((_chassis + 1))
    CMD+=(-device "pcie-root-port,id=$1,chassis=$_chassis")
}
_nvmens=0
_disk() {   # _disk sys|persist DRIVE-ID FILE VIRTIO-DEVICE [VIRTIO-PROPERTIES]
    local n=0 dev=emmc
    CMD+=(-drive "if=none,file=$3,format=raw,id=$2")
    case "$STORAGE" in
        virtio) CMD+=(-device "$4,drive=$2${5:+,$5}") ;;
        nvme)
            [[ $_nvmens -gt 0 ]] || CMD+=(-device nvme,id=kblnvme,serial=kblnvme)
            _nvmens=$((_nvmens + 1))
            CMD+=(-device "nvme-ns,drive=$2,bus=kblnvme,nsid=$_nvmens") ;;
        emmc)
            [[ "$1" == sys ]] || { n=1; dev=sd-card; }
            _root_port "kblrp$n"
            CMD+=(-device "sdhci-pci,id=kblmmc$n,bus=kblrp$n"
                  -device "$dev,drive=$2,bus=kblrp$n/kblmmc$n/sd-bus") ;;
    esac
}

# ROOT=disk: a GPT system disk, as on a device.  Partition 1 "boot" (FAT32)
# takes the kernel and the boot configuration on every run; partition 2
# "rootfs" holds a copy of the ext4 image ROOTFS_IMAGE.  Made when SYSDISK does
# not exist and never overwritten; --reset-sysdisk makes it again.  Offsets and
# the PARTUUID are always read back from the disk itself.
_sdboot=""; _p1off=""; _puuid=""
_part() {   # _part DISK N: partition N's byte offset, byte size and PARTUUID
    sfdisk --json -- "$1" 2>/dev/null | python3 -I -c 'if True:
        import json, sys
        t = json.load(sys.stdin)["partitiontable"]; s = t.get("sectorsize", 512)
        p = t["partitions"][int(sys.argv[1]) - 1]
        print(p["start"] * s, p["size"] * s, p.get("uuid", "").lower())' "$2" 2>/dev/null
}
# Built under a temporary name and renamed into place, so a failure halfway
# leaves no disk that a later run would trust.
_mksysdisk() {
    local bps rsz ptype out p1off p1size p2off p2size
    local -a attr=(-A 1:set:2)
    bps="$(kbl_tree_get "$tree" BOOTPART_SIZE)"
    [[ -n "$bps" ]] || die "ROOT=disk: state BOOTPART_SIZE (the size of the FAT32 boot partition,
       e.g. 256M) in $tree/tree.conf"
    [[ "$bps" =~ ^[1-9][0-9]*[KMGT]$ ]] \
        || die "BOOTPART_SIZE in $tree/tree.conf is '$bps'; it is a whole number of K, M, G or T"
    [[ -f "$_rimg" ]] || die "rootfs image not found: $_rimg (ROOTFS_IMAGE in $tree/tree.conf)"
    rsz="$(stat -L -c%s "$_rimg")" || rsz=0     # Buildroot's rootfs.ext4 is a symlink
    (( rsz > 0 && rsz % 512 == 0 )) || die "$_rimg is not a whole number of 512-byte sectors"
    command -v sgdisk  >/dev/null 2>&1 || die "sgdisk (gptfdisk) required to make the system disk"
    command -v mformat >/dev/null 2>&1 || die "mtools (mformat) required to make the system disk"
    ptype=0700; [[ "$BOOT" == uefi ]] && { ptype=EF00; attr=(); }
    _sdnew="$(mktemp -p "$(dirname "$_sysdisk")" ".$(basename "$_sysdisk").XXXXXX")" \
        || die "cannot create a file beside $_sysdisk"
    { truncate -s "$bps" "$_sdnew" && truncate -s "+$rsz" "$_sdnew" && truncate -s +2M "$_sdnew"; } \
        || die "cannot size $_sdnew"
    # Alignment 1, so partition 2 ends exactly where the image does; partition 1
    # starts at 1 MiB all the same.
    out="$(sgdisk -o -a 1 -n "1:2048:+$bps" -t "1:$ptype" -c 1:boot ${attr[@]+"${attr[@]}"} \
                  -n "2:0:+$((rsz / 512))" -t 2:8300 -c 2:rootfs "$_sdnew" 2>&1)" \
        || die "sgdisk could not partition $_sdnew: $out"
    read -r p1off p1size _ < <(_part "$_sdnew" 1)
    read -r p2off p2size _ < <(_part "$_sdnew" 2)
    [[ -n "$p1off" && -n "$p2off" ]] || die "sfdisk --json reads no partitions back from $_sdnew"
    [[ "$p2size" == "$rsz" ]] || die "partition 2 of $_sdnew is $p2size bytes; $_rimg is $rsz"
    out="$(mformat -i "${_sdnew}@@${p1off}" -F -T "$((p1size / 512))" -h 64 -s 32 -v BOOT :: 2>&1)" \
        || die "mformat could not make FAT32 in partition 1 (BOOTPART_SIZE=$bps; FAT32 needs about 33M): $out"
    dd if="$_rimg" of="$_sdnew" bs=64K seek="$p2off" oflag=seek_bytes conv=notrunc,sparse status=none \
        || die "could not copy $_rimg into partition 2 of $_sdnew"
    mv -f "$_sdnew" "$_sysdisk" || die "could not rename $_sdnew to $_sysdisk"
    _sdnew=""
    say "sysdisk      made $_sysdisk: boot $bps FAT32 (type $ptype), rootfs from $_rimg"
}
if [[ "$ROOT" == disk ]]; then
    _rimg="$(kbl_tree_get "$tree" ROOTFS_IMAGE)"
    [[ -n "$_rimg" ]] || die "ROOT=disk: state ROOTFS_IMAGE (an ext4 file-system image) in $tree/tree.conf"
    _rimg="$(kbl_tree_path "$tree" "$_rimg")"
    _sysdisk="$(kbl_tree_get "$tree" SYSDISK)"
    _sysdisk="$(kbl_tree_path "$tree" "${_sysdisk:-disk/system.img}")"
    if [[ "$BOOT" == uefi ]]; then
        _efil="$(kbl_tree_get "$tree" EFI_LOADER)"
        [[ -n "$_efil" ]] || die "ROOT=disk with boot uefi: state EFI_LOADER (a GRUB EFI binary whose
       embedded config loads /grub.cfg) in $tree/tree.conf"
        _efil="$(kbl_tree_path "$tree" "$_efil")"
        [[ -f "$_efil" ]] || die "EFI loader not found: $_efil (EFI_LOADER in $tree/tree.conf)"
    fi
    command -v sfdisk  >/dev/null 2>&1 || die "sfdisk (util-linux) required for ROOT=disk"
    command -v mcopy   >/dev/null 2>&1 || die "mtools (mcopy) required for ROOT=disk"
    command -v python3 >/dev/null 2>&1 || die "python3 required for ROOT=disk (it reads sfdisk --json)"
    mkdir -p "$(dirname "$_sysdisk")" || die "cannot create $(dirname "$_sysdisk")"
    # Claimed like the persistent disk.  When another run holds it, this run
    # boots a throwaway copy, which the cleanup removes.
    if ! _persist_busy "$_sysdisk" hd0 \
       && _claim_aux sysdisk "$(printf '%s' "$_sysdisk" | sha1sum | cut -c1-16)"; then
        if [[ $RESET_SYSDISK -eq 1 ]]; then rm -f "$_sysdisk" || die "could not remove $_sysdisk"; fi
        [[ -e "$_sysdisk" ]] || _mksysdisk
        _sdboot="$_sysdisk"
    else
        [[ $RESET_SYSDISK -eq 0 ]] || die "--reset-sysdisk: $_sysdisk is in use by another run"
        _tdata="$(kbl_tree_data "$tree")"; mkdir -p "$_tdata" || die "cannot create $_tdata"
        _sdcopy="$(mktemp -p "$_tdata" --suffix=.img "sysdisk-${PORT}.XXXXXX")" \
            || die "cannot create a throwaway copy of $_sysdisk in $_tdata"
        cp --reflink=auto --sparse=always -f "$_sysdisk" "$_sdcopy" \
            || die "could not copy $_sysdisk to $_sdcopy"
        say "sysdisk      $_sysdisk is in use by another run -- booting a throwaway copy"
        _sdboot="$_sdcopy"
    fi
    [[ -f "$_rimg" && "$_rimg" -nt "$_sysdisk" ]] \
        && say "sysdisk      $_rimg is newer than $_sysdisk; --reset-sysdisk makes the disk again from it"
    read -r _p1off _ _ < <(_part "$_sdboot" 1)
    read -r _ _ _puuid < <(_part "$_sdboot" 2)
    [[ -n "$_p1off" && -n "$_puuid" ]] || die "sfdisk --json reads no boot and rootfs partitions from
       $_sdboot; make the disk again with --reset-sysdisk"
fi

# Persistent per-tree disk: a writable virtio-blk image on SSD, kept between runs
# (and reboots) so the guest's /persist survives. Opt-in (--persist, or PERSIST=1
# in tree.conf); its path/size are stated by the tree, never hardcoded here. The
# guest's S45persist init script finds it by the serial set below and mounts it.
# Every failure here is non-fatal -- the guest just boots without /persist --
# except a size an SD card (STORAGE=emmc, or a board's SD slot) cannot have.
persist_ok=0; persist_disk=""; persist_dev=""
[[ $PERSIST_SET -eq 1 ]] || PERSIST="$(kbl_tree_get "$tree" PERSIST)"
if [[ "$PERSIST" == 1 ]]; then
    persist_disk="$(kbl_tree_get "$tree" PERSIST_DISK)"; persist_disk="${persist_disk:-disk/persist.img}"
    persist_disk="$(kbl_tree_path "$tree" "$persist_disk")"
    _psize="$(kbl_tree_get "$tree" PERSIST_SIZE)"; _psize="${_psize:-2G}"
    if [[ -n "$board" && -z "$_bpdrive" ]]; then
        say "persist      $board states no PERSIST_DRIVE -- booting without /persist"
    elif ! mkdir -p "$(dirname "$persist_disk")" 2>/dev/null; then
        say "persist      cannot create $(dirname "$persist_disk") -- booting without /persist"
    elif _persist_busy "$persist_disk" \
         || ! _claim_aux persist "$(printf '%s' "$persist_disk" | sha1sum | cut -c1-16)"; then
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
            # QEMU's sd-card takes only a power-of-two size, and rounding would
            # change the disk, so any other size is refused.
            _sdwhy=""
            if [[ -n "$board" ]]; then [[ ",$_bpdrive," == *,if=sd,* ]] && _sdwhy="on $board"
            elif [[ "$STORAGE" == emmc ]]; then _sdwhy="with STORAGE=emmc"; fi
            if [[ -n "$_sdwhy" ]]; then
                _pb="$(stat -c%s "$persist_disk" 2>/dev/null)" || _pb=0
                if (( _pb == 0 || (_pb & (_pb - 1)) != 0 )); then
                    if [[ $_pfresh -eq 1 ]]; then
                        rm -f "$persist_disk"
                        die "PERSIST_SIZE in $tree/tree.conf is $_psize; $_sdwhy the persistent
       disk is an SD card, whose size QEMU wants a power of two"
                    fi
                    die "$persist_disk is $_pb bytes; $_sdwhy it is an SD card, whose size
       QEMU wants a power of two (resize it, or remove it and state such a PERSIST_SIZE)"
                fi
            fi
            # Format a freshly created image now, so the guest only ever mounts it.
            # If the host has no mkfs.ext4 the guest's S45persist formats on first
            # boot instead -- both paths converge on a labelled ext4.
            if [[ $_pfresh -eq 1 ]] && command -v mkfs.ext4 >/dev/null 2>&1; then
                mkfs.ext4 -q -L kblpersist -F "$persist_disk" >/dev/null 2>&1 \
                    || say "persist      host mkfs failed; the guest will format $persist_disk"
            fi
            # virtio over PCIe, as everywhere: every machine here has a PCIe host.
            persist_dev="virtio-blk-pci"
            persist_ok=1
        fi
    fi
fi

[[ $IOMMU_SET -eq 1 ]] || IOMMU="$(kbl_tree_get "$tree" IOMMU)"; IOMMU="${IOMMU:-0}"
[[ "$IOMMU" == 0 || "$IOMMU" == 1 ]] || die "IOMMU in $tree/tree.conf is '$IOMMU'; it is 0 or 1"
_iommu=""
if [[ "$IOMMU" == 1 && -n "$board" ]]; then
    IOMMU=0; _bdrop+=("iommu        none: kbuildlab knows no IOMMU of $board")
elif [[ "$IOMMU" == 1 ]]; then
    case "$arch:${machine%%,*}" in
        x86_64:q35|x86_64:pc-q35-*)  _iommu=intel-iommu;     _isym=CONFIG_INTEL_IOMMU ;;
        arm64:virt|arm64:virt-*)     _iommu=smmuv3;          _isym=CONFIG_ARM_SMMU_V3 ;;
        riscv64:virt|riscv64:virt-*)
            [[ ",$machine," == *,aia=aplic-imsic,* ]] \
                || die "IOMMU: riscv-iommu-pci signals only through MSIs, and $machine has no IMSIC
       to take them (MACHINE=virt,aia=aplic-imsic with a CPU that has Ssaia adds one)"
            _iommu=riscv-iommu-pci; _isym=CONFIG_RISCV_IOMMU_PCI ;;
        *) die "IOMMU: kbuildlab knows no IOMMU of the $arch machine $machine" ;;
    esac
    [[ -f "$src/.config" ]] && grep -Eqx "${_isym}=y" "$src/.config" \
        || die "$(kbl_tree_label "$tree") cannot use $_iommu: its .config lacks ${_isym}=y"
    [[ "$_iommu" == smmuv3 ]] && machine="$machine,iommu=smmuv3"
fi

append="console=$console"
[[ $KASLR -eq 1 ]] || append="$append nokaslr"
[[ "$_iommu" == intel-iommu ]] && append="$append intel_iommu=on"
[[ "$ROOT" == disk ]] && append="$append root=PARTUUID=$_puuid rootwait rw"
# Extra command-line words.  Stated per tree with CMDLINE_EXTRA= in tree.conf and
# added to with --append; never hardcoded here, because what a guest needs on its
# command line is a property of that guest, not of the launcher.
#
# One thing this carries that is not a kernel parameter: the kernel hands any
# unrecognised key=value on its command line to init as an ENVIRONMENT VARIABLE
# (init/main.c unknown_bootoption).  That is the only place early enough to reach
# PID 1 itself, which is why the rootfs alone cannot do it -- but it is also not
# sufficient, because busybox init does not pass its environment on to what it
# starts.  Measured: /proc/1/environ carries the value while a login shell's own
# copy is empty.  The rootfs therefore re-exports it (rootfs/post-build.sh writes
# the two lines into /etc/init.d/rcS and /etc/profile.d), and the two halves
# together are what make it reach the processes that would otherwise trap.
# v4.6-arm64/tree.conf carries the measured before/after and what still escapes.
# What can safely travel every path.  A direct boot hands the string to execve as
# one argument and would take anything, but the same string also has to survive
# `setenv bootargs '<it>'` in a generated u-boot script and a `linux <it>` line in
# a generated grub.cfg -- neither of which is a shell, and neither of which has an
# escape for the character that ends its own quoting.  A single quote would end
# the u-boot argument early and let the rest of the value run as u-boot COMMANDS;
# a newline would do the same in both.  Refuse those here, where the value is
# still identifiable, instead of producing a guest that boots with a truncated
# command line and no indication of it.
_cmdline_ok() {   # _cmdline_ok VALUE SOURCE
    # Neither destination is a shell, and neither has an escape for the character
    # that ends its own quoting -- so the answer is to refuse the character, here,
    # where the value can still be named.  u-boot's hush ends a single-quoted
    # argument at the next `'` and then parses the rest as COMMANDS; grub treats
    # `;` as a command separator and expands `$NAME` before the kernel ever sees
    # the line, so a value with a `$` in it reaches the guest silently rewritten
    # while the run state records what was asked for.  Both were reproduced.
    case "$1" in
        *\'*)   die "$2 contains a single quote.  u-boot's 'setenv bootargs' argument ends
       at that quote and the rest is run as u-boot commands: $1" ;;
        *'"'*)  die "$2 contains a double quote, which grub.cfg does not quote: $1" ;;
        *';'*)  die "$2 contains a semicolon, which ends the grub.cfg 'linux' line and
       makes the rest a separate grub command: $1" ;;
        *'$'*)  die "$2 contains a '\$'.  grub substitutes \$NAME before the kernel sees the
       line, so the guest would boot with something other than this: $1" ;;
        *'`'*)  die "$2 contains a backquote: $1" ;;
        *[$'\n\r']*) die "$2 contains a newline, which splits both the u-boot script and
       the grub.cfg into extra commands: $1" ;;
    esac
}
_cmdx="$(kbl_tree_get "$tree" CMDLINE_EXTRA)"
if [[ -n "$_cmdx" ]]; then
    _cmdline_ok "$_cmdx" "CMDLINE_EXTRA in $tree/tree.conf"
    append="$append $_cmdx"
fi
for _w in "${APPEND_EXTRA[@]+"${APPEND_EXTRA[@]}"}"; do
    [[ -n "$_w" ]] || continue
    _cmdline_ok "$_w" "--append"
    append="$append $_w"
done
# The console is the guest's serial line, multiplexed with qemu's monitor on
# this terminal and with signals off: Ctrl-C is a byte for the guest (it stops a
# `ping` there instead of killing qemu), Ctrl-A X quits qemu, Ctrl-A C reaches
# the monitor.  Stated rather than left to -nographic, whose own default is a
# console WITH signals, and no monitor at all once QMP below takes the monitor
# slot -- which is how Ctrl-C came to end the whole guest.  -nodefaults, because
# every device the guest has is stated here: it drops QEMU's default CD-ROM,
# floppy, parallel port and VGA.
declare -a CMD=("$qemu" -machine "$machine" -m "$MEM" -smp "$SMP" -nographic -nodefaults
                -chardev stdio,id=kblcon,mux=on,signal=off)
# On a board the console is one of its UARTs; the UARTs before it get a null backend.
for ((_u = 0; _u < _buart; _u++)); do CMD+=(-serial null); done
CMD+=(-serial chardev:kblcon -mon chardev=kblcon,mode=readline)
case "$_iommu" in
    intel-iommu)     CMD+=(-device intel-iommu) ;;
    riscv-iommu-pci) CMD+=(-device riscv-iommu-pci,off=off) ;;
esac

# An entropy source.  A headless guest has no keyboard, no disk seek noise and no
# hardware RNG, so its CRNG is seeded by whatever the kernel can manufacture on
# its own -- and only kernels from 5.4 can (try_to_generate_entropy, jitter
# entropy).  virtio-rng-pci exists on every machine kbuildlab drives (checked with
# `-device help` on all three qemu-system binaries) and costs nothing; the guest
# still needs CONFIG_HW_RANDOM_VIRTIO to use it, which the preset sets.
#
# This device is necessary and not sufficient on a pre-4.8 kernel, and the
# difference was measured rather than assumed.  On v4.6.7/arm64 the driver binds,
# khwrngd runs, and /proc/sys/kernel/random/entropy_avail reads ~1300 by t=2s --
# yet getrandom() still blocked until t=110s.  The reason is that 4.6 has two
# pools: khwrngd feeds the INPUT pool, while getrandom() waits on the NONBLOCKING
# one, which is refilled only by a READ of /dev/urandom and only once per
# kernel.random.urandom_min_reseed_secs (60).  Nothing reads urandom in that
# window, so the guest waits with a full pool.  The rootfs's S00entropy drops the
# interval and forces one refill; that is the half of the fix the launcher cannot
# do, because there is no way to set a sysctl from a 4.6 kernel command line.
if [[ $_nopci -eq 0 ]]; then CMD+=(-device virtio-rng-pci)
else _bdrop+=("rng          none: $board has no PCI bus for virtio-rng-pci"); fi




# Enough room for the copy before making it, with the size read from the file
# rather than assumed.  `cp` onto a full tmpfs produces a truncated image that
# boots into a firmware prompt with no explanation.
_shm_room_for() {
    local src="$1" need avail
    need="$(stat -c%s "$src" 2>/dev/null)" || return 0
    avail="$(df -B1 --output=avail "$_statedir" 2>/dev/null | tail -1 | tr -dc 0-9)"
    [[ -n "$avail" ]] || return 0
    (( avail > need + (need / 4) )) || die "$_statedir has $((avail / 1048576)) MiB free; this run needs \
$((need / 1048576)) MiB for the boot-disk copy. Free some, or point \$KBL_STATE_DIR elsewhere."
}
_bdisk=""; _bvirt=""   # the disk the boot reads first, and its virtio device
case "$BOOT" in
    direct)
        CMD+=(-kernel "$image" -append "$append")
        [[ -n "$INITRD" ]] && CMD+=(-initrd "$INITRD")
        [[ -n "$_bdtb" ]] && CMD+=(-dtb "$_bdtb")
        if [[ "$ROOT" == disk ]]; then
            _bdisk="$_sdboot"
            _bvirt="virtio-blk-pci"
        fi
        ;;
    uboot)
        ub="${BIOS:-$(kbl_tree_get "$tree" UBOOT)}"
        [[ -n "$ub" ]] || die "boot uboot: state UBOOT= in $tree/tree.conf or pass --bios"
        ub="$(kbl_tree_path "$tree" "$ub")"
        [[ -f "$ub" ]] || die "u-boot not found: $ub"
        if [[ "$ROOT" != disk ]]; then
            bd="$(kbl_tree_get "$tree" BOOTDISK)"; bd="$(kbl_tree_path "$tree" "${bd:-boot/uboot.img}")"
            [[ -f "$bd" ]] || die "no boot disk: $bd -- build it first with your workspace's firmware/build-bootdisk.sh (not shipped with kbuildlab; see README)"
        fi
        # Where boot.scr loads the image and the initramfs, which is also where
        # the kernel is entered (attach's hardware breakpoint).  On QEMU's virt
        # machine the defaults are its own: RAM at 1 GiB on arm64, at 2 GiB on
        # riscv64.  riscv64 enters at RAM base + text_offset whatever the load
        # address, because u-boot's booti moves every image in RAM there; u-boot
        # itself was loaded there too, but has relocated by the time boot.scr
        # runs.  Any other machine states both, and states RAM_BASE for the check
        # that they lie in the guest's RAM; virt's RAM base is its own.
        _ramb=""; _dla=""; _dra=""
        case "$arch:${machine%%,*}" in
            arm64:virt|arm64:virt-*)     _ramb=0x40000000; _dla=0x40200000; _dra=0x48000000 ;;
            riscv64:virt|riscv64:virt-*) _ramb=0x80000000; _dla=0x80200000; _dra=0x88000000 ;;
        esac
        _rb="$(kbl_tree_get "$tree" RAM_BASE)"
        if [[ -n "$_rb" ]]; then
            [[ "$_rb" =~ ^0x[0-9a-fA-F]+$ ]] || die "RAM_BASE in $tree/tree.conf is '$_rb'; it is a hex address"
            _ramb="$_rb"
        fi
        la="$(kbl_tree_get "$tree" UBOOT_LOADADDR)"; la="${la:-$_dla}"
        ra="$(kbl_tree_get "$tree" UBOOT_RDADDR)";   ra="${ra:-$_dra}"
        [[ -n "$la" && -n "$ra" ]] || die "boot uboot on $arch $machine: state UBOOT_LOADADDR and UBOOT_RDADDR in
       $tree/tree.conf (only QEMU's virt machine has defaults)"
        for _a in "$la" "$ra"; do
            [[ "$_a" =~ ^0x[0-9a-fA-F]+$ ]] || die "boot uboot: '$_a' is not a hex address (UBOOT_LOADADDR / UBOOT_RDADDR)"
            [[ -n "$_ramb" ]] || continue
            (( _a >= _ramb )) && { [[ -z "$_membytes" ]] || (( _a < _ramb + _membytes )); } \
                || die "boot uboot: $_a lies outside this guest's RAM ($_ramb + $MEM); fix UBOOT_LOADADDR / UBOOT_RDADDR"
        done
        command -v mkimage >/dev/null 2>&1 || die "mkimage (uboot-tools) required for boot uboot"
        command -v mcopy  >/dev/null 2>&1 || die "mtools (mcopy) required for boot uboot"
        _loadaddr="$la"                                 # attach's HW-bp goes here
        if [[ "$ROOT" == disk ]]; then
            _bdisk="$_sdboot"; _fat="${_sdboot}@@${_p1off}"
            _bfull="larger than partition 1? raise BOOTPART_SIZE and pass --reset-sysdisk"
        else
            _shm_room_for "$bd"
            shmdisk="$_statedir/kbl-boot-${PORT}.img"
            cp -f "$bd" "$shmdisk" || die "could not copy $bd to $shmdisk"
            _bdisk="$shmdisk"; _fat="${shmdisk}@@1M"
            _bfull="larger than the free space in $bd? rebuild it: firmware/build-bootdisk.sh"
        fi
        # u-boot ignores QEMU -append, so this run's bootargs ride in boot.scr.
        # The initrd is loaded, and named to booti, only when there is one: `-`
        # in its place is how booti is told there is none.  Loading it anyway
        # under --no-initrd would boot a root filesystem the caller asked not to
        # have, and the flag would look like it did nothing.
        # U-Boot's script boot method exports where it found boot.scr; only the
        # virtio boot disk keeps the fixed address it has always had.
        # shellcheck disable=SC2016
        _ldev='${devtype} ${devnum}:${distro_bootpart}'
        [[ "$ROOT" != disk && "$STORAGE" == virtio ]] && _ldev="virtio 0:1"
        _bc="$(mktemp -p "$_statedir" "kbl-tmp-${PORT}.XXXXXX")"; _bs="$(mktemp -p "$_statedir" "kbl-tmp-${PORT}.XXXXXX")"
        { printf 'load %s %s /Image\n' "$_ldev" "$la"
          [[ -n "$INITRD" ]] && printf 'load %s %s /rootfs.cpio.gz\n' "$_ldev" "$ra"
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
        mcopy -o -i "$_fat" "$_bs" ::/boot.scr \
            || die "could not write boot.scr into $_bdisk"
        rm -f "$_bc" "$_bs"; _bc=""; _bs=""
        # The boot disk carries the kernel and the initramfs as files, put there
        # by firmware/build-bootdisk.sh on the day it ran.  Copy today's in over
        # them.  What this writes to is the per-run /dev/shm copy, so the
        # on-disk image is untouched -- and a kernel rebuilt since then cannot
        # be shadowed by the one baked in.  That failure has no symptom: the
        # guest boots either way, and only the version string says which.
        mcopy -o -i "$_fat" "$image" ::/Image \
            || die "could not write $image into $_bdisk -- $_bfull"
        if [[ -n "$INITRD" ]]; then
            mcopy -o -i "$_fat" "$INITRD" ::/rootfs.cpio.gz \
                || die "could not write $INITRD into the boot-disk copy (see above)"
        else
            mdel -i "$_fat" ::/rootfs.cpio.gz >/dev/null 2>&1 || true
        fi
        _bvirt="virtio-blk-pci"
        # Firmware placement differs: on arm64 u-boot IS the -bios firmware; on
        # riscv the M-mode firmware is OpenSBI (QEMU's default -bios) and u-boot
        # is its S-mode payload, loaded via -kernel.
        case "$arch" in
            riscv64) CMD+=(-kernel "$ub") ;;
            *)       CMD+=(-bios "$ub") ;;
        esac
        ;;
    uefi)
        # UEFI: OVMF firmware (pflash CODE read-only + a per-run writable VARS
        # copy) boots grub2 from an ESP disk. Paths per tree; --bios overrides CODE.
        code="${BIOS:-$(kbl_tree_get "$tree" OVMF_CODE)}"; vars="$(kbl_tree_get "$tree" OVMF_VARS)"
        [[ -n "$code" && -n "$vars" ]] || die "boot uefi: state OVMF_CODE= and OVMF_VARS= in $tree/tree.conf"
        code="$(kbl_tree_path "$tree" "$code")"; vars="$(kbl_tree_path "$tree" "$vars")"
        [[ -f "$code" && -f "$vars" ]] || die "OVMF firmware not found: $code / $vars"
        if [[ "$ROOT" != disk ]]; then
            bd="$(kbl_tree_get "$tree" BOOTDISK)"; bd="$(kbl_tree_path "$tree" "${bd:-boot/esp.img}")"
            [[ -f "$bd" ]] || die "no ESP disk: $bd -- build it first with your workspace's firmware/build-esp.sh (not shipped with kbuildlab; see README)"
        fi
        # Where the EFI stub puts the kernel's image base.  It is not the boot
        # protocol's 0x1000000: the stub allocates, and the trees here measured
        # 0x1800000.  Stated per tree, because a wrong one is a debugger
        # calibrated to an address the kernel never runs at.
        la="$(kbl_tree_get "$tree" UEFI_ENTRY)"
        [[ -n "$la" ]] || die "boot uefi: state UEFI_ENTRY (the image base the EFI stub chooses) in
       $tree/tree.conf.  Measure it once: boot with --boot uefi --no-kaslr, attach,
       and read _text's physical address from \`kearly status\`."
        _loadaddr="$la"
        command -v mcopy >/dev/null 2>&1 || die "mtools (mcopy) required for boot uefi"
        if [[ "$ROOT" == disk ]]; then
            _bdisk="$_sdboot"; _fat="${_sdboot}@@${_p1off}"
            _bfull="larger than partition 1? raise BOOTPART_SIZE and pass --reset-sysdisk"
        else
            _shm_room_for "$bd"
            shmdisk="$_statedir/kbl-boot-${PORT}.img"
            cp -f "$bd" "$shmdisk" || die "could not copy $bd to $shmdisk"
            _bdisk="$shmdisk"; _fat="${shmdisk}@@1M"
            _bfull="larger than the free space in $bd? rebuild it: firmware/build-esp.sh"
        fi
        shmvars="$_statedir/kbl-vars-${PORT}.fd"
        cp -f "$vars" "$shmvars" || die "could not copy $vars to $shmvars"
        # Rewrite grub.cfg with this run's bootargs (kaslr/console).  The initrd
        # line is written only when there is one: grub loads what the config
        # names, so leaving it in under --no-initrd would boot a root filesystem
        # the caller asked not to have, and the flag would look like it did
        # nothing.
        # OVMF's console leaves SGR 40 in force, which bands every kernel line on a
        # palette whose color0 is not the background.  On its own serial terminal
        # grub answers a color_normal write with ESC[m, which clears it.
        _gc="$(mktemp -p "$_statedir" "kbl-tmp-${PORT}.XXXXXX")"
        { [[ "$console" =~ ^ttyS([0-9]+)$ ]] \
              && printf 'serial --unit=%s\nterminal_output serial\nset color_normal=$color_normal\n' "${BASH_REMATCH[1]}"
          printf 'search --no-floppy --set=root --file /vmlinuz\n'
          printf 'linux /vmlinuz %s\n' "$append"
          [[ -n "$INITRD" ]] && printf 'initrd /rootfs.cpio.gz\n'
          printf 'boot\n'; } > "$_gc"
        mcopy -o -i "$_fat" "$_gc" ::/grub.cfg \
            || die "could not write grub.cfg into $_bdisk"
        rm -f "$_gc"; _gc=""
        # The ESP carries the kernel and the initramfs as files, and
        # firmware/build-esp.sh put whatever was current the day it ran into
        # them.  Copy today's in over them.  What this writes to is the per-run
        # /dev/shm copy, so the on-disk ESP is untouched, and a kernel rebuilt
        # since then cannot be shadowed by the one baked in -- a failure with no
        # symptom, because the guest boots either way and only the version
        # string says which.
        mcopy -o -i "$_fat" "$image" ::/vmlinuz \
            || die "could not write $image into $_bdisk -- $_bfull"
        if [[ -n "$INITRD" ]]; then
            mcopy -o -i "$_fat" "$INITRD" ::/rootfs.cpio.gz \
                || die "could not write $INITRD into the ESP copy (see above)"
        else
            # Nothing names it now, but an unreferenced file is still a file a
            # future grub.cfg could pick up.  Best effort; absence is fine.
            mdel -i "$_fat" ::/rootfs.cpio.gz >/dev/null 2>&1 || true
        fi
        # The system disk's partition 1 is its ESP, so the loader goes there too.
        # -D s skips a directory that is there; without it mtools asks on a terminal.
        if [[ "$ROOT" == disk ]]; then
            mmd -D s -i "$_fat" ::/EFI >/dev/null 2>&1; mmd -D s -i "$_fat" ::/EFI/BOOT >/dev/null 2>&1
            mcopy -o -i "$_fat" "$_efil" ::/EFI/BOOT/BOOTX64.EFI \
                || die "could not write $_efil into $_bdisk"
        fi
        CMD+=(-drive "if=pflash,format=raw,readonly=on,file=${code}"
              -drive "if=pflash,format=raw,file=${shmvars}")
        _bvirt="virtio-blk-pci"
        ;;
    *) die "unknown boot mode '$BOOT' (known: direct, uboot, uefi)" ;;
esac
# The disk the boot reads first: a firmware chain's boot disk, or the system disk.
[[ -n "$_bdisk" ]] && _disk sys hd0 "$_bdisk" "$_bvirt"
# Attach the persistent disk (after any boot disk, so it never displaces hd0). The
# serial is how the guest tells it apart from the boot disk regardless of which
# /dev/vdX each lands on; nvme, emmc and a board's slot carry none, and there the guest
# finds it by its ext4 label kblpersist. Not in the cleanup trap: this image is meant
# to persist.
if [[ $persist_ok -eq 1 && -n "$board" ]]; then
    CMD+=(-drive "file=${persist_disk},format=raw,id=kblpersist,${_bpdrive}")
elif [[ $persist_ok -eq 1 ]]; then
    _disk persist kblpersist "$persist_disk" "$persist_dev" serial=kblpersist
fi
# ROOT=disk: today's modules archive goes on the boot partition as
# /modules.cpio.gz; without a current one, an old archive there is removed so the
# guest cannot install modules of another build.
if [[ "$ROOT" == disk ]]; then
    if [[ -n "$_mfw" ]]; then
        mcopy -o -i "${_sdboot}@@${_p1off}" "$_mfw" ::/modules.cpio.gz \
            || die "could not write $_mfw into the boot partition of $_sdboot -- larger than
       partition 1? raise BOOTPART_SIZE and pass --reset-sysdisk"
    else
        mdel -i "${_sdboot}@@${_p1off}" ::/modules.cpio.gz >/dev/null 2>&1 || true
    fi
fi
# User-mode (SLIRP) networking on a virtio NIC: no host setup, gives the guest
# DHCP + DNS + outbound (ping/tcpdump/apt), 10.0.2.15 behind gateway 10.0.2.2.
# virtio-net-pci works on every machine here (pc, arm64 virt, riscv virt all PCIe).
# A host->guest:22 forward lets neovim edit files in the guest over SSH
# (nvim scp://root@localhost:<sshport>//path, password "root").
_lan=()
if [[ $NET -eq 1 ]]; then
    [[ -n "$SSHPORT" ]] || SSHPORT="$(kbl_tree_get "$tree" SSH_PORT)"; SSHPORT="${SSHPORT:-2222}"
    if [[ $SSHPORT_SET -eq 1 ]]; then
        { ! _busy "$SSHPORT" && _claim_aux ssh "$SSHPORT"; } \
            || die "ssh port $SSHPORT is in use (you asked for it); pick another with --ssh-port"
    else
        _so="$SSHPORT"; SSHPORT=""
        for _p in "$_so" $(seq $((_so + 1)) $((_so + 128))); do
            _busy "$_p" && continue
            _claim_aux ssh "$_p" || continue
            SSHPORT="$_p"; break
        done
        [[ -n "$SSHPORT" ]] || die "no free ssh-forward port near $_so"
    fi
    # The primary NIC is virtio over PCIe unless NIC_DEVICE in tree.conf names
    # another device type.
    _nicdev="$(kbl_tree_get "$tree" NIC_DEVICE)"; _nicsay="${_nicdev:-virtio}"
    _nicdev="${_nicdev:-virtio-net-pci}"
    # A board's NIC is on the SoC, so it takes its backend from -nic.
    if [[ -n "$board" ]]; then
        _nicsay="$_bnic"; CMD+=(-nic "user,model=${_bnic},hostfwd=tcp:127.0.0.1:${SSHPORT}-:22")
    else
        CMD+=(-netdev "user,id=net0,hostfwd=tcp:127.0.0.1:${SSHPORT}-:22"
              -device "${_nicdev},netdev=net0")
    fi
    # LAN_NICS: more ports of the guest's own on one private segment (hub 1; the
    # legacy -net options use hub 0), so XDP and RDMA can run between them; qemu
    # warns that this hub reaches no host network, which is the point.
    read -r -a _lan <<<"$(kbl_tree_get "$tree" LAN_NICS)"
    if [[ $_nopci -eq 1 && ${#_lan[@]} -gt 0 ]]; then
        _bdrop+=("lan          none: $board has no PCI bus for ${_lan[*]}"); _lan=()
    fi
    _n=0
    for _t in ${_lan[@]+"${_lan[@]}"}; do
        CMD+=(-netdev "hubport,id=kbllan${_n},hubid=1" -device "${_t},netdev=kbllan${_n}")
        _n=$((_n + 1))
    done
else
    # Omitting our NIC is not the same as having none: qemu creates its DEFAULT
    # user-mode NIC on any machine that has one, so --no-net used to print
    # "net none" over a guest that had a working DHCP lease.  `-nic none` is what
    # actually suppresses it, and any measurement taken under --no-net -- boot
    # timing, entropy, syscall counts -- depended on this being true.
    CMD+=(-nic none)
fi
use_kvm=0
[[ "$arch" == "x86_64" && "$KVM" -eq 1 && -r /dev/kvm && -w /dev/kvm ]] && use_kvm=1
# `-cpu host` needs KVM; under TCG (the default, wanted for deterministic early
# -boot HW breakpoints) fall back to a fully-featured emulated CPU.
if [[ "$cpu" == "host" && $use_kvm -eq 0 ]]; then
    cpu="max"
    say "cpu          max (the tree states host, which needs KVM; this run uses TCG)"
fi
# Paging profile as CPU features (stated per tree, never hardcoded): the kernel
# uses the widest paging the CPU advertises, so e.g. CPU_PAGING="la57=off" pins
# x86 to 4-level and "sv48=off,sv57=off" pins riscv to Sv39 -- no kernel rebuild.
cpu_paging="$(kbl_tree_get "$tree" CPU_PAGING)"
[[ -n "$cpu" && -n "$cpu_paging" ]] && cpu="${cpu},${cpu_paging}"
[[ -n "$cpu" ]] && CMD+=(-cpu "$cpu")
[[ $use_kvm -eq 1 ]] && CMD+=(-enable-kvm)
# Where the gdbstub listens.  `-gdb tcp::N` binds 0.0.0.0 AND [::], and a gdbstub
# is an unauthenticated read/write channel into the guest's memory -- so the
# default is the loopback address, and a tree that genuinely wants to be reached
# from another machine says so with GDB_BIND= rather than every tree being
# exposed because one of them needed it.  run-gdb.sh, the one reader of this,
# parses the tcp:HOST:PORT form.
_gbind="${KBL_GDB_BIND:-$(kbl_tree_get "$tree" GDB_BIND)}"; _gbind="${_gbind:-127.0.0.1}"
CMD+=(-gdb "tcp:${_gbind}:${PORT}")

# drgn's way in (`kbuildlab drgn`).  Both transports: drgn identifies a Linux
# guest by itself only over the unix socket, and the TCP port is what makes the
# guest reachable the way the gdb port is.  Neither pauses the guest nor
# displaces the one client the gdbstub serves.
#
# QMP_PORT is stated by the tree, never derived from GDB_PORT: a computed port is
# a contract nobody wrote down, and it collides the first time two trees are
# given adjacent gdb ports.
if [[ $QMP -eq 1 ]]; then
    [[ -n "$QMPPORT" ]] || QMPPORT="$(kbl_tree_get "$tree" QMP_PORT)"
    [[ -n "$QMPPORT" ]] || die "no qmp port: state QMP_PORT in $tree/tree.conf, pass
       --qmp-port, or run with --no-qmp (which also drops the vmcoreinfo device
       and leaves drgn no way in)"
    if [[ $QMPPORT_SET -eq 1 ]]; then
        { ! _busy "$QMPPORT" && _claim_aux qmp "$QMPPORT"; } \
            || die "qmp port $QMPPORT is in use (you asked for it); pick another with --qmp-port"
    else
        _qo="$QMPPORT"; QMPPORT=""
        for _p in "$_qo" $(seq $((_qo + 1)) $((_qo + 128))); do
            _busy "$_p" && continue
            _claim_aux qmp "$_p" || continue
            QMPPORT="$_p"; break
        done
        [[ -n "$QMPPORT" ]] || die "no free qmp port in $_qo..$((_qo + 128))"
        [[ "$QMPPORT" == "$_qo" ]] || say "qmp          $_qo busy -> using $QMPPORT"
    fi
    qmpsock="$_statedir/kbl-qmp-${PORT}.sock"
    rm -f "$qmpsock"
    CMD+=(-qmp "unix:${qmpsock},server=on,wait=off"
          -qmp "tcp:127.0.0.1:${QMPPORT},server=on,wait=off")
    # advertises the guest's VMCOREINFO note; the guest half is
    # CONFIG_FW_CFG_SYSFS + CONFIG_VMCORE_INFO
    if [[ $_nofwcfg -eq 0 ]]; then CMD+=(-device vmcoreinfo)
    else _bdrop+=("vmcoreinfo   none: $board has no fw_cfg -- drgn has no way in"); fi
fi
# Hardware the guest can have without owning any: controllers QEMU emulates,
# each driven by its real driver (presets/soft/emulated.config builds them), so
# probe, DMA and interrupt paths are all there to trace.  Disks are null-co --
# reads return zeros, writes vanish -- so nothing is created on the host; the
# CTU CAN FD's two channels share one bus and hear each other.  All PCI, so a
# board without a PCI bus has none.
[[ $EMULATE_SET -eq 1 ]] || EMULATE="$(kbl_tree_get "$tree" EMULATE)"
if [[ "$EMULATE" == 1 && $_nopci -eq 1 ]]; then
    EMULATE=0; _bdrop+=("emulate      none: $board has no PCI bus for the emulated controllers")
fi
if [[ "$EMULATE" == 1 ]]; then
    # Beside STORAGE=nvme's controller this one needs a serial of its own: QEMU
    # derives the subsystem NQN from it, and Linux rejects a second controller
    # of one subsystem.
    _ens=kblnvme; [[ "$STORAGE" == nvme ]] && _ens=kblnvme-null
    CMD+=(-blockdev node-name=kblnvme,driver=null-co -device "nvme,serial=${_ens},drive=kblnvme"
          -device virtio-scsi-pci,id=kblscsi -blockdev node-name=kblscsid,driver=null-co
          -device scsi-hd,bus=kblscsi.0,drive=kblscsid
          -device qemu-xhci,id=kblxhci -device usb-kbd,bus=kblxhci.0 -device usb-tablet,bus=kblxhci.0
          -blockdev node-name=kblusb,driver=null-co -device usb-storage,bus=kblxhci.0,drive=kblusb
          -device virtio-keyboard-pci -device virtio-tablet-pci -device virtio-gpu-pci
          -audiodev none,id=kblvsnd -device virtio-sound-pci,audiodev=kblvsnd
          -object cryptodev-backend-builtin,id=kblcrypto -device virtio-crypto-pci,cryptodev=kblcrypto
          -device virtio-balloon-pci
          -chardev null,id=kblvcon -device virtio-serial-pci -device virtconsole,chardev=kblvcon
          -object can-bus,id=kblcan -device ctucan_pci,canbus0=kblcan,canbus1=kblcan)
fi
read -r -a _pcie <<<"$(kbl_tree_get "$tree" PCIE_DEVICES)"
_pcie+=(${PCIE_EXTRA[@]+"${PCIE_EXTRA[@]}"})
if [[ $_nopci -eq 1 && ${#_pcie[@]} -gt 0 ]]; then
    _bdrop+=("pcie         none: $board has no PCI bus for ${_pcie[*]}"); _pcie=()
fi
_n=0
for _d in ${_pcie[@]+"${_pcie[@]}"}; do
    _root_port "kblpcie$_n"
    CMD+=(-device "$_d,bus=kblpcie$_n")
    _n=$((_n + 1))
done
wsock=""
if [[ -t 0 && $_nopci -eq 1 ]]; then
    _bdrop+=("winsize      none: $board has no PCI bus for the virtio-serial port")
elif [[ -t 0 ]]; then
    wsock="$_statedir/kbl-wsz-${PORT}.sock"
    rm -f "$wsock"
    _wsdev="virtio-serial-pci"
    CMD+=(-chardev "socket,id=kblwsz,path=${wsock},server=on,wait=off"
          -device "${_wsdev},id=kblwszbus"
          -device "virtserialport,bus=kblwszbus.0,chardev=kblwsz,name=org.kbuildlab.winsize")
fi
[[ $RUN -eq 1 ]] || CMD+=(-S)
# QEMU_ARGS: the tree's extra qemu arguments, split on blanks, before -- ARGS.  A
# board's own replace them: they attach its peripherals, the tree's suit its machine.
if [[ -n "$board" ]]; then
    read -r -a _qargs <<<"$(_bget QEMU_ARGS)"
    [[ -n "$(kbl_tree_get "$tree" QEMU_ARGS)" ]] \
        && _bdrop+=("qemu args    the tree's QEMU_ARGS left out on $board; -- ARGS still apply")
else
    read -r -a _qargs <<<"$(kbl_tree_get "$tree" QEMU_ARGS)"
fi
CMD+=(${_qargs[@]+"${_qargs[@]}"})
CMD+=("${EXTRA[@]+"${EXTRA[@]}"}")

# Record this run so `attach` (and through it an editor's adapter) can match
# the exact boot combination on this gdb port.  Written here, not earlier,
# because the ssh port and the final command line are only settled once the NIC
# and the boot mode have been resolved, and a state file that says "" for a port
# that was in fact allocated is worse than one that does not exist.
#
# The file name and the KBL_ key format are a contract with the readers (attach,
# drgn, resume, trees), each of which parses `KBL_[A-Z0-9_]+=` lines.  Keys are
# therefore only ADDED here, never renamed, so a reader that knows the older set
# keeps working against a file a newer launcher wrote.
_write_state() {   # _write_state [QEMU_PID]
    local qpid="${1:-}" tmp
    # The record is line-oriented, so a value containing a newline would end its
    # own line and every reader would take the first line as the whole value -- a
    # tree path silently truncated, after which the operator cannot attach to
    # their own guest by name or by path.  Refuse where it is written.
    kbl_state_safe "the tree path" "$tree"
    kbl_state_safe "the kernel command line" "$append"
    local -a rec=(
        "KBL_SCHEMA=2"
        "KBL_TREE=$tree" "KBL_ARCH=$arch"
        "KBL_BOOT=$BOOT" "KBL_KASLR=$KASLR" "KBL_GDB_PORT=$PORT"
        "KBL_GDB_BIND=$_gbind" "KBL_FROZEN=$(( RUN == 0 ))"
        # A firmware chain passes the cmdline itself, so it never appears on
        # qemu's own command line; recording it is the only way anything else can
        # read back what this guest was actually booted with.
        "KBL_CMDLINE=$append"
        "KBL_MACHINE=$machine" "KBL_SMP=$SMP" "KBL_MEM=$MEM"
        "KBL_LAUNCHER_PID=$$" "KBL_LAUNCHER_START=$(kbl_proc_starttime $$ || echo)"
        "KBL_STARTED=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    )
    [[ -n "$_loadaddr" ]] && rec+=("KBL_LOADADDR=$_loadaddr")
    [[ $NET -eq 1 ]] && rec+=("KBL_SSH_PORT=$SSHPORT")
    [[ $QMP -eq 1 ]] && rec+=("KBL_QMP_PORT=$QMPPORT" "KBL_QMP_SOCK=$qmpsock")
    [[ $persist_ok -eq 1 ]] && rec+=("KBL_PERSIST=$persist_disk")
    rec+=("KBL_ROOT=$ROOT")
    [[ -n "$_sdboot" ]] && rec+=("KBL_SYSDISK=$_sdboot")
    [[ -n "$board" ]] && rec+=("KBL_BOARD=$board")
    if [[ -n "$qpid" ]]; then
        # pid alone is not an identity: pids are reused.  The process start time
        # from /proc pins it, so a state file that outlives its guest can be told
        # apart from one describing the process actually on this port.
        rec+=("KBL_QEMU_PID=$qpid" "KBL_QEMU_START=$(kbl_proc_starttime "$qpid" || echo)")
    fi
    # Written whole and renamed into place.  Appending KBL_LOADADDR separately, as
    # this used to, left a window of several hundred milliseconds (mkimage, mcopy,
    # a 90 MB cp) in which `attach` could read a record that said BOOT=uboot with
    # no load address -- and then skip the hardware breakpoint and stop somewhere
    # the kernel never runs, with nothing on screen to say why.  rename(2) is
    # atomic, so a reader sees either the old record or the new one.
    tmp="$(mktemp -p "$_statedir" "kbl-tmp-${PORT}.XXXXXX")" || return 0
    printf '%s\n' "${rec[@]}" > "$tmp" && mv -f "$tmp" "$_state" || rm -f "$tmp"
}
_write_state

say "guest        $(kbl_tree_label "$tree") ($arch) on :$PORT"
[[ -n "$board" ]] && say "board        $board (-M $machine${_bdtb:+, -dtb $_bdtb})"
for _d in "${_bdrop[@]+"${_bdrop[@]}"}"; do say "$_d"; done
if [[ "$BOOT" == direct ]]; then
    say "boot         direct (-kernel; head.S _text immediately)"
    if [[ "$ROOT" == initramfs ]]; then
        [[ -n "$INITRD" ]] && say "initrd       $rootfs" \
                           || say "initrd       none -- kernel panics at mount-root if continued (early-boot only)"
    fi
elif [[ "$ROOT" == disk ]]; then
    say "boot         $BOOT firmware chain; kernel on the system disk's partition 1"
else
    say "boot         $BOOT firmware chain; rootfs on boot disk"
fi
[[ "$ROOT" == disk ]] && say "root         disk $_sdboot (PARTUUID $_puuid)"
[[ -z "$board" && "$STORAGE" != virtio ]] && say "storage      $STORAGE (every disk this run attaches)"
[[ -n "$mods" && -z "$_mfw" ]] && say "modules      $mods appended; loaded at boot for the devices present"
[[ -n "$_mfw" ]] && say "modules      $mods on the boot partition (/modules.cpio.gz); the rootfs installs it"
[[ $NET -eq 1 ]] && say "net          user/$_nicsay (guest 10.0.2.15); ssh -p $SSHPORT root@localhost (pw: root)" \
                 || say "net          none (--no-net)"
[[ ${#_lan[@]} -gt 0 ]] && say "lan          ${_lan[*]} on one private segment inside the guest"
[[ $persist_ok -eq 1 ]] && say "persist      $persist_disk -> guest /persist (survives reboot)"
[[ "$EMULATE" == 1 ]] && say "emulate      nvme, virtio-scsi, usb, virtio input/gpu/sound/crypto/balloon/console, can x2"
[[ ${#_pcie[@]} -gt 0 ]] && say "pcie         ${_pcie[*]}, each behind a root port of its own"
[[ -n "$_iommu" ]] && say "iommu        $_iommu: device DMA goes through IOVA mappings"
[[ $QMP -eq 1 ]] && say "qmp          :$QMPPORT and $qmpsock$([[ $_nofwcfg -eq 0 ]] && echo ' (drgn reads the guest here; it does not pause it)')" \
                 || say "qmp          none (--no-qmp) -- drgn has no way in"
[[ $KASLR -eq 1 ]] && say "kaslr        on (randomized; 'kearly kaslr auto' calibrates the slide)" \
                   || say "kaslr        off (nokaslr, deterministic)"
# Copy-pasteable: the name is quoted (it may contain spaces) and the port is
# named whenever it is not the tree's default -- which is exactly when a second
# guest of this tree exists and the bare name would land in the chooser.
_atree="$(printf '%q' "$(kbl_tree_label "$tree")")"
[[ "$PORT" == "$(kbl_tree_get "$tree" GDB_PORT)" ]] \
    && _acmd="kbuildlab attach $_atree" \
    || _acmd="kbuildlab attach $_atree --port $PORT"
[[ $RUN -eq 1 ]] || say "frozen       attach with: $_acmd"
say "console      Ctrl-C goes to the guest; Ctrl-A X quits qemu, Ctrl-A C its monitor"
[[ -n "$wsock" ]] && say "winsize      the guest console follows this terminal's rows and columns"
# Printed frozen or not: drgn reads a booted kernel, so this is what to run once
# the guest is up, not instead of the attach above.
if [[ $QMP -eq 1 && $_nofwcfg -eq 0 ]]; then
    [[ "$QMPPORT" == "$(kbl_tree_get "$tree" QMP_PORT)" ]] \
        && _dcmd="kbuildlab drgn $_atree" \
        || _dcmd="kbuildlab drgn $_atree --qmp-port $QMPPORT"
    say "drgn         once booted: $_dcmd"
fi
# Run qemu as a child rather than exec'ing it.  Two things need that.  The
# cleanup trap has to fire in EVERY boot mode -- `exec` replaces this shell and
# runs no trap, which is why a direct boot used to leave its state file behind
# for `attach` to read against a later guest on the same port.  And the state
# file has to carry qemu's own pid, which only exists once it is forked.
#
# `<&0` is what keeps the serial console usable, and it is not decoration.  With
# job control off -- which it is in every non-interactive shell, and this is one
# -- POSIX assigns an asynchronous command's stdin to /dev/null unless stdin was
# EXPLICITLY redirected.  Without it, qemu's -nographic serial chardev reads EOF
# for ever: the guest prints and accepts nothing, and nobody can log in.
# Measured: a plain `&` child has /proc/PID/fd/0 -> /dev/null, with `<&0` it is
# the terminal; and with it in place, typing root/root at a `kbl run` guest logs
# in and the guest answers.
#
# Job control being off also means the child stays in this shell's process
# group -- measured inside a pty, same pgid, state S+ -- so the console's
# raw-mode handling is what it was under `exec`.  With signal=off, Ctrl-C reaches
# neither qemu nor this script; it is a byte on the guest's serial line.
"${CMD[@]}" <&0 &
_qpid=$!
if [[ -n "$wsock" ]]; then
    python3 -I "${KBL_REPO}/lib/winsize.py" "$wsock" "$_qpid" 3 3<&0 </dev/null >/dev/null 2>&1 &
    _wpid=$!
fi
_write_state "$_qpid"
# 2>/dev/null drops bash's own job-termination line, which reports a guest dying as
# `run-qemu.sh: line NNN: 60816 Segmentation fault (core dumped) "${CMD[@]}" 0<&0`:
# this script's internal line number, this script's variable name, and not one word
# about the guest.  The status still carries the signal, so the report below is the
# launcher's own and says what actually happened.
wait "$_qpid" 2>/dev/null; _rc=$?
if [[ $_rc -gt 128 ]]; then
    _sig="$(kill -l "$((_rc - 128))" 2>/dev/null || echo "$((_rc - 128))")"
    case "$_sig" in
        # How a guest is ENDED, not how it dies: Ctrl-C in a foreground run reaches
        # qemu through the process group, and the cleanup above ends it with TERM
        # then KILL.  Reporting those as a crash would cry wolf on every exit.
        INT|TERM|HUP|QUIT|KILL) : ;;
        *)
            warn "guest on :$PORT died of SIG$_sig -- it did not shut down."
            [[ "$_sig" == SEGV ]] && warn \
"       A debugger can do this from the outside, and on this machine it is the
       likeliest cause: a debug read of an address that translates to a device
       region makes qemu dispatch into the device model, and arm_gic.c reads
       current_cpu->cpu_index there -- NULL on the gdbstub's thread -- whenever the
       guest has more than one core (--smp $SMP here).  gdbtools' safemem guard keeps
       a debugger off those addresses; \`kearly safemem status\` says whether it was
       armed.  The terminal has been restored; the guest and its run state are gone." ;;
    esac
fi
exit $_rc
}
