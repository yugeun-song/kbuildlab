#!/usr/bin/env bash
# Attach drgn to a running guest over QEMU's QMP monitor.
#
# run-gdb.sh's counterpart, same discovery and same chooser.  The channel is what
# differs: the gdbstub serves one client and pauses the guest on connect, QMP
# does neither, so drgn can sit alongside an attached gdb on a running guest.
set -uo pipefail
_self="$(readlink -f "${BASH_SOURCE[0]}")"
KBL_REPO="$(cd -P "$(dirname "$_self")/.." && pwd)"
# shellcheck source=/dev/null
source "${KBL_REPO}/lib/common.sh"

PORT=""; PORT_SET=0; QMPPORT=""; QMPPORT_SET=0; LIST=0; FIRST=0; SYMBOLS=1; TCP=0
declare -a REST=() PASS=()
_usage() {
    cat <<USAGE
kbuildlab drgn [TREE] [options] [-- DRGN ARGS]
  attach drgn to a running guest over QMP, with the tree's vmlinux as symbols

  --port|-p N        pick the guest by its GDB port -- the same identity
                     'attach' uses, and what the run state is keyed on
  --qmp-port N       pick it by its QMP port instead
  --tcp              connect over the QMP TCP port rather than the unix socket.
                     Reachable from elsewhere, but drgn cannot read vmcoreinfo
                     over TCP, so the guest is no longer identified as Linux
                     unless you pass --vmcoreinfo yourself
  --no-symbols       do not pass the tree's vmlinux to drgn
  --list|-l          list live guests and exit; never prompts
  --first            with several matches, take the lowest port instead of asking
  -- DRGN ARGS       passed straight to drgn (a script and its arguments, -e ...)

  TREE is a name, directory, or kernel source root; omitted, the current tree.

  drgn reads a BOOTED kernel.  A guest still frozen at its reset vector has none
  yet; this says so rather than letting drgn fail at it.
USAGE
}
while [[ $# -gt 0 ]]; do
    case "$1" in
        -h|--help)     _usage; exit 0 ;;
        --port|-p)     PORT="${2:?--port needs a number}"; PORT_SET=1; shift 2 ;;
        --qmp-port)    QMPPORT="${2:?--qmp-port needs a number}"; QMPPORT_SET=1; shift 2 ;;
        --tcp)         TCP=1; shift ;;
        --no-symbols)  SYMBOLS=0; shift ;;
        --list|-l)     LIST=1; shift ;;
        --first)       FIRST=1; shift ;;
        --) shift; PASS=("$@"); break ;;
        -*) die "drgn: unknown option '$1' (pass drgn arguments after --)" ;;
        *)  REST+=("$1"); shift ;;
    esac
done

command -v drgn >/dev/null 2>&1 || die "drgn is not installed
       Arch: pacman -S drgn   others: pip install drgn"

# --------------------------------------------------------------- which guest
_want_tree=""
if [[ -n "${REST[0]:-}" ]]; then
    _want_tree="$(kbl_tree "${REST[0]}")" || exit 1
elif [[ $PORT_SET -eq 0 && $QMPPORT_SET -eq 0 ]]; then
    _want_tree="$(kbl_tree "" 2>/dev/null)" || _want_tree=""
fi

# A QMP port names a guest only through the run state, so it resolves to the gdb
# port everything else is keyed on before discovery starts.
if [[ $QMPPORT_SET -eq 1 ]]; then
    _sd="$(kbl_statedir)" || exit 1
    _found=""
    for _f in "$_sd"/kbl-run-*.env; do
        [[ -r "$_f" ]] || continue
        [[ "$(kbl_state_get "$_f" KBL_QMP_PORT)" == "$QMPPORT" ]] || continue
        _found="$(kbl_state_get "$_f" KBL_GDB_PORT)"; break
    done
    [[ -n "$_found" ]] || die "drgn: no run recorded QMP port $QMPPORT.  'kbuildlab drgn --list'
       shows what is live; a guest started with --no-qmp has no QMP port at all."
    if [[ $PORT_SET -eq 1 && "$PORT" != "$_found" ]]; then
        die "drgn: --qmp-port $QMPPORT belongs to the guest on gdb port $_found, not $PORT"
    fi
    PORT="$_found"; PORT_SET=1
fi

kbl_pick_guest drgn "$_want_tree" "$PORT" "$PORT_SET" "$FIRST" "$LIST"; _rc=$?
[[ $_rc -eq 2 ]] && exit 0
[[ $_rc -eq 0 ]] || exit $_rc

kbl_row_fields "$KBL_GUEST_ROW"
PORT="$_r_port"
if [[ -n "$_r_tree" ]]; then
    if [[ -n "$_want_tree" && "$(readlink -f "$_r_tree")" != "$(readlink -f "$_want_tree")" ]]; then
        die "drgn: --port $PORT is running $(basename "$_r_tree"), not $(basename "$_want_tree")
       (recorded in $_r_sf when that port was started)"
    fi
    tree="$_r_tree"
elif [[ -n "$_want_tree" ]]; then
    tree="$_want_tree"
else
    die "drgn: :$PORT has a live $_r_qbin (pid $_r_pid) but nothing recorded which
       tree it is.  Name it:  kbuildlab drgn <tree> --port $PORT"
fi

# --------------------------------------------------------------- the channel
# Read out of the run state, never reconstructed.
[[ -n "$_r_sf" && -r "$_r_sf" ]] || die "drgn: :$PORT has a live guest but no readable run state,
       so there is nothing that records where its QMP monitor is.  Only a guest
       started by 'kbuildlab run' carries one."
qmpsock="$(kbl_state_get "$_r_sf" KBL_QMP_SOCK)"
qmpport="$(kbl_state_get "$_r_sf" KBL_QMP_PORT)"
[[ -n "$qmpsock$qmpport" ]] || die "drgn: the guest on :$PORT was started without a QMP monitor
       (--no-qmp, or before this tool grew one), and drgn has no other way in.
       Restart it:  kbuildlab run $(basename "$tree")"

if [[ $TCP -eq 1 ]]; then
    [[ -n "$qmpport" ]] || die "drgn: --tcp, but that run recorded no QMP TCP port"
    addr="localhost:$qmpport"
else
    [[ -n "$qmpsock" && -S "$qmpsock" ]] || die "drgn: the QMP unix socket this run recorded is gone
       (${qmpsock:-none recorded}).  --tcp uses the TCP port instead, at the cost
       of automatic Linux-kernel identification."
    addr="$qmpsock"
fi

arch="$(kbl_tree_arch "$tree")"
src="$(kbl_tree_src "$tree")"
vmlinux="$src/vmlinux"
if [[ $SYMBOLS -eq 1 ]]; then
    [[ -f "$vmlinux" ]] || die "no vmlinux at $vmlinux
       build it first: kbuildlab build $(basename "$tree")"
fi

# Whether this guest is readable is asked, not inferred.  A drgn that only
# identifies -- no symbols -- answers in 0.1s, and it is the same question the
# session is about to ask.  QMP's runstate cannot answer it: measured on
# upstream-arm64, a guest held at start_kernel and one held at a late breakpoint
# both read `debug`, and only the second is readable.
_ident=""
if [[ $TCP -eq 0 && -S "$qmpsock" ]]; then
    _ident="$(timeout 30 drgn --qemu "$qmpsock" --no-default-symbols \
                -e 'import drgn; print(1 if prog.flags & drgn.ProgramFlags.IS_LINUX_KERNEL else 0)' \
              2>/dev/null | tail -1)"
fi

_qmp_status() {
    [[ -n "$qmpport" ]] && command -v python3 >/dev/null 2>&1 || return 0
    python3 - "$qmpport" <<'QMPPY' 2>/dev/null
import json, socket, sys
try:
    s = socket.create_connection(("127.0.0.1", int(sys.argv[1])), timeout=2)
    s.recv(65536); s.sendall(b'{"execute":"qmp_capabilities"}\n'); s.recv(65536)
    s.sendall(b'{"execute":"query-status"}\n')
    print(json.loads(s.recv(65536).splitlines()[0])["return"]["status"])
except Exception:
    pass
QMPPY
}

if [[ "$_ident" == 0 ]]; then
    # The note is written from qemu_fw_cfg's device initcall, so it does not
    # exist while the kernel is still inside start_kernel -- which is where an
    # early-boot session usually is.  Say how far to go, not just "continue".
    if [[ "$(_qmp_status)" == prelaunch ]]; then
        warn "drgn: the guest on :$PORT has never been started -- it is frozen at its
       reset vector, so there is no kernel in memory.  Every symbol lookup in this
       session raises ObjectNotFoundError, and it will keep doing so: drgn builds
       its Program when it connects, so continuing the guest afterwards changes
       nothing here.  Quit this session and run it again once the guest has booted."
    else
        warn "drgn: the guest on :$PORT has not published its vmcoreinfo note, so drgn
       cannot tell it is a Linux kernel and every symbol lookup raises
       ObjectNotFoundError.

       That note is written by qemu_fw_cfg's device initcall, which runs inside
       do_initcalls() -- after start_kernel has returned.  A guest stopped at
       start_kernel is too early: let it reach userspace first.

       This session will not recover on its own.  drgn builds its Program when it
       connects, so booting the guest further does not re-identify it: quit and
       run this again."
    fi
    _cfg="$src/.config"
    if [[ -r "$_cfg" ]]; then
        _missing=""
        grep -qx 'CONFIG_FW_CFG_SYSFS=[ym]' "$_cfg" || _missing="$_missing CONFIG_FW_CFG_SYSFS"
        grep -qxE 'CONFIG_(VMCORE_INFO|CRASH_CORE|KEXEC_CORE)=y' "$_cfg" \
            || _missing="$_missing CONFIG_VMCORE_INFO"
        [[ -n "$_missing" ]] && warn "       This tree is also built without$_missing, so it would not
       write the note at any point.  'kbuildlab config $(basename "$tree") --preset',
       then rebuild."
    fi
elif [[ "$_ident" == 1 ]]; then
    say "kernel       identified from vmcoreinfo"
fi

# drgn reads guest PHYSICAL memory on every architecture it knows; the kernel
# virtual translation on top of it is per-architecture, and riscv64 has none in
# drgn 0.2.0.  Measured: prog.read(0x80200000, 8, physical=True) returns the
# Image header while any kernel VA raises FaultError "could not find memory
# segment", identically under Sv39 and under Sv48/Sv57.  Only worth saying once
# the guest has been identified; before that the message above is the reason.
if [[ "$(kbl_tree_arch "$tree")" == riscv64 && "$_ident" != 0 ]]; then
    warn "drgn: $(drgn --version 2>/dev/null | head -1) translates no riscv64 kernel virtual
       address, so reads through symbols raise FaultError even though the guest is
       identified and physical reads work.  Use prog.read(PA, N, physical=True),
       or 'kbuildlab attach' for this tree."
fi

declare -a ARGS=(--qemu "$addr")
[[ $SYMBOLS -eq 1 ]] && ARGS+=(-s "$vmlinux")
ARGS+=("${PASS[@]+"${PASS[@]}"}")

say "guest        $(basename "$tree") ($arch) on gdb :$PORT"
# One client per -qmp chardev: a second session queues in the listen backlog and
# looks like a hang.  Worth saying where the address is already on screen.
say "qmp          $addr (one drgn at a time; a second waits for this one to quit)"
[[ $SYMBOLS -eq 1 ]] && say "symbols      $vmlinux" || say "symbols      none (--no-symbols)"
# No chdir, unlike `attach`: drgn resolves no path relatively, so a script path
# on this command line means what it says.
exec drgn "${ARGS[@]}"
