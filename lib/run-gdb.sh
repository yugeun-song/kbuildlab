#!/usr/bin/env bash
# Attach a debugger to a running guest, handing the debugger everything it
# needs to know about this tree.
#
# What this does NOT do is decide anything on the debugger's behalf.  It states
# what the tree says and stops if the tree does not say it.
set -uo pipefail
_self="$(readlink -f "${BASH_SOURCE[0]}")"
KBL_REPO="$(cd -P "$(dirname "$_self")/.." && pwd)"
# shellcheck source=/dev/null
source "${KBL_REPO}/lib/common.sh"

PORT=""; BOOTBREAK=1; STOP=""; STOP_AT=""
declare -a REST=() PASS=()
_usage() {
    cat <<USAGE
kbuildlab attach [TREE] [options] [-- GDB ARGS]
  attach gdb to a running guest, stopped at head.S _text by default

  --no-bootbreak|--raw   attach without running to _text (already-booted guest)
  --stop WHERE           text (default) | firmware | start_kernel
  --port|-p N            gdb port (default: the tree's GDB_PORT)
  -- GDB ARGS            pass the rest straight to gdb
  TREE is a name, directory, or kernel source root; omitted, the current tree.
USAGE
}
while [[ $# -gt 0 ]]; do
    case "$1" in
        -h|--help)            _usage; exit 0 ;;
        --port|-p)            PORT="${2:?--port needs a number}"; shift 2 ;;
        --no-bootbreak|--raw) BOOTBREAK=0; shift ;;   # attach raw (no run-to-_text)
        --bootbreak)          BOOTBREAK=1; shift ;;
        --stop)               STOP="${2:?--stop needs text|firmware|start_kernel}"; shift 2 ;;
        --) shift; PASS=("$@"); break ;;
        -*) die "attach: unknown option '$1' (pass gdb arguments after --)" ;;
        *)  REST+=("$1"); shift ;;
    esac
done
# Stop point: text (default, head.S _text) | firmware (raw, land at firmware
# _start) | start_kernel (past _text, run on to start_kernel).
case "$STOP" in
    ""|text|_text)             : ;;
    firmware|_start|raw|fw)    BOOTBREAK=0 ;;
    start_kernel|start-kernel) STOP_AT="start_kernel" ;;
    *) die "attach --stop: unknown '$STOP' (text|firmware|start_kernel)" ;;
esac

tree="$(kbl_tree "${REST[0]:-}")" || exit 1
arch="$(kbl_tree_arch "$tree")"
src="$(kbl_tree_src "$tree")"
vmlinux="$src/vmlinux"
[[ -f "$vmlinux" ]] || die "no vmlinux at $vmlinux
       build it first: kbuildlab build $(basename "$tree")"

gdb="$(kbl_gdb "$arch")"
[[ -n "$PORT" ]] || PORT="$(kbl_tree_get "$tree" GDB_PORT)"
[[ -n "$PORT" ]] || die "no gdb port: state GDB_PORT in $tree/tree.conf or pass --port"
[[ -n "$(ss -ltnH "sport = :${PORT}" 2>/dev/null)" ]] \
    || die "nothing is listening on :$PORT.  Start the guest first:
       kbuildlab run $(basename "$tree")"

# gdbtools, if it is installed, is what makes symbols work before the MMU is on.
# It is optional and found the way it documents; absent is a normal answer.
declare -a ARGS=(-q -iex "set pagination off")
tool=""
if [[ -n "${GDBTOOLS_PATH:-}" && -f "${GDBTOOLS_PATH}" ]]; then
    tool="$GDBTOOLS_PATH"
elif [[ -r "${XDG_CONFIG_HOME:-$HOME/.config}/gdbtools/root" ]]; then
    _r="$(<"${XDG_CONFIG_HOME:-$HOME/.config}/gdbtools/root")"
    [[ -f "$_r/gdbtools.py" ]] && tool="$_r/gdbtools.py"
fi

# The machine description this tree states, handed over as-is.  Anything already
# in the environment wins, so a one-off override still works.  GDBTOOLS_ENTRY_PA is
# the exception: the tree states the address its DEFAULT boot mode lands the kernel
# at, and this port may be running another one, so it is held back here and applied
# further down only if nothing better is recorded.  Both callers of gdbtools resolve
# it the same way -- the nvim adapter reads the per-port state file too.
_op_entry="${GDBTOOLS_ENTRY_PA:-}"   # an operator pin; it outranks everything below
_tree_entry=""                       # what tree.conf states, applied only as a fallback
while IFS='=' read -r k v; do
    # The same grammar as kbl_tree_get, including the quote strip that this one
    # was missing -- and this is the parser whose output goes straight into gdb's
    # environment, so `GDBTOOLS_ENTRY_PA="0x40080000"` reached the debugger with
    # its quotes attached and did not parse as a number.
    v="${v%%#*}"; v="${v%"${v##*[![:space:]]}"}"; v="${v%\"}"; v="${v#\"}"
    [[ "$k" == GDBTOOLS_ENTRY_PA ]] && { _tree_entry="$v"; continue; }
    [[ -n "${!k:-}" ]] || export "$k=$v"
done < <(sed -n 's/^[[:space:]]*\(GDBTOOLS_[A-Z0-9_]*\)[[:space:]]*=[[:space:]]*\(.*\)/\1=\2/p' "$tree/tree.conf")
export GDBTOOLS_AUTO=1

# Match the exact boot combination `run` recorded for this gdb port (mode + load
# address). In a firmware chain the bootloader lands the kernel entry at a known
# address; point gdbtools' bootbreak HW breakpoint there. Parsed, never sourced.
# The state file kbl_instances ACCEPTED for this row, and nothing else.  The `:-`
# default used to point at the same path kbl_instances had just blanked after
# finding its recorded pid/starttime did not match the process on that port -- so
# `--list` called the file stale while `attach` calibrated to it, which is how a
# session ends up loading u-boot symbols into a guest that never ran u-boot.
# Blank means there is no usable state, exactly as for a guest started elsewhere.
_st="${_r_sf:-}"
FWSYM=""   # firmware ELF symbols (u-boot) for the pre-kernel stages
_bmode=""  # boot mode this port is actually running; empty when nothing recorded it
_kaslr=""  # whether it was booted with KASLR on; only the run state knows
if [[ -r "$_st" ]]; then
    _bmode="$(sed -n 's/^KBL_BOOT=//p' "$_st" | head -1)"
    _bla="$(sed -n 's/^KBL_LOADADDR=//p' "$_st" | head -1)"
    _kaslr="$(sed -n 's/^KBL_KASLR=//p' "$_st" | head -1)"
    if [[ ( "$_bmode" == uboot || "$_bmode" == uefi ) && -n "$_bla" ]]; then
        # `_bla` is the image base -- where the firmware lands _text.  gdbtools shifts
        # it to the ELF entry (startup_64) itself when the two differ (newer x86), so
        # hand over the image base as-is and let the symbol offset be applied there,
        # where the vmlinux is loaded.  x86 KASLR takes it away again below.
        [[ -n "${GDBTOOLS_ENTRY_PA:-}" ]] || export GDBTOOLS_ENTRY_PA="$_bla"
        say "combo        $_bmode: kernel image base $_bla (firmware bootbreak via hw-bp)"
        [[ -n "${GDBTOOLS_BREAK_KIND:-}" ]] || export GDBTOOLS_BREAK_KIND=hw
        # The u-boot ELF (u-boot.bin -> u-boot) symbolizes the firmware stages
        # (reset _start, pre-relocation). Kernel symbols come from vmlinux; their
        # address ranges do not overlap, so both resolve.
        if [[ "$_bmode" == uboot ]]; then
            _ub="$(kbl_tree_get "$tree" UBOOT)"
            [[ -n "$_ub" && "$_ub" != /* ]] && _ub="$tree/$_ub"
            [[ -n "$_ub" && -f "${_ub%.bin}" ]] && FWSYM="${_ub%.bin}"
        fi
    fi
fi

# Nothing recorded for this port, or a direct boot: the tree's own address is the
# only evidence left.  It describes the tree's DEFAULT mode, so it holds only while
# that is what is running -- the same rule the nvim adapter applies.
if [[ -z "${GDBTOOLS_ENTRY_PA:-}" && -n "$_tree_entry" ]]; then
    _sboot="$(kbl_tree_get "$tree" BOOT)"; [[ -z "$_sboot" ]] && _sboot=direct
    [[ "${_bmode:-$_sboot}" == "$_sboot" ]] && export GDBTOOLS_ENTRY_PA="$_tree_entry"
fi

# With no run state -- a guest something else started -- score KASLR from the same
# evidence discover.lua scores, in the same order, so the terminal and the editor
# cannot reach opposite verdicts about one guest: the guest's own command line
# first, the build's .config next, and anything still undecided counts as ON.  The
# asymmetry is deliberate: a wrong "off" silently skips the base recovery and the
# session breaks at an address the kernel never uses, while a wrong "on" only runs
# a recovery that was not needed.
#
# pgrep, anchored to a qemu-system process: `ps -eo args | grep <pattern>` matches
# the grep's OWN command line, so it never returns empty and every branch below it
# would be dead code.  Measured on this host with no guest running.
if [[ -z "$_kaslr" ]]; then
    # Both spellings discover.lua accepts: `-gdb tcp::PORT`, `-gdb tcp:HOST:PORT`,
    # and on 1234 the `-s` shorthand, which is what a hand-started guest usually
    # carries.  Anchored so -smp/-serial/-snapshot cannot stand in for -s.
    _pat="qemu-system.*-gdb tcp:[^[:space:]]*:${PORT}([^0-9]|\$)"
    [[ "$PORT" == 1234 ]] && _pat="qemu-system.*(-gdb tcp:[^[:space:]]*:1234([^0-9]|\$)|-s([[:space:]]|\$))"
    _qcmd="$(pgrep -af "$_pat" 2>/dev/null | head -1)"
    # Only the -append VALUE decides, as discover.lua does, and read from /proc so
    # argument boundaries survive: matching the whole command line would let a path
    # like /srv/vm/nokaslr-disk.img answer a question it has nothing to do with.
    _qpid="${_qcmd%%[[:space:]]*}"; _app=""
    if [[ -n "$_qpid" && -r "/proc/$_qpid/cmdline" ]]; then
        mapfile -d '' -t _argv < "/proc/$_qpid/cmdline" 2>/dev/null || _argv=()
        for ((_i = 0; _i < ${#_argv[@]}; _i++)); do
            [[ "${_argv[_i]}" == "-append" ]] && { _app="${_argv[_i+1]:-}"; break; }
        done
    fi
    if   [[ " $_app " == *" nokaslr "* ]]; then _kaslr=0
    elif [[ " $_app " == *" kaslr "*   ]]; then _kaslr=1
    elif grep -qx '# CONFIG_RANDOMIZE_BASE is not set' "$src/.config" 2>/dev/null; then _kaslr=0
    else _kaslr=1
    fi
fi

# x86 randomizes the PHYSICAL base (arm64 and riscv randomize only the virtual one),
# so every address derived above describes a boot that is not this one -- and stating
# one SUPPRESSES the recovery, which gdbtools runs only while ENTRY_PA is unset.  Take
# it back, whichever source produced it.  An operator pin is left alone: that is the
# documented way to override the recovery.
if [[ "$arch" == "x86_64" && "$_kaslr" == 1 && -z "$_op_entry" && -n "${GDBTOOLS_ENTRY_PA:-}" ]]; then
    say "combo        KASLR on: base measured per run (the stated $GDBTOOLS_ENTRY_PA is a no-KASLR boot)"
    unset GDBTOOLS_ENTRY_PA
fi

# x86 KASLR recovery reads the decompressor's vmlinux; hand its path over so
# `kearly kaslr` works in terminal mode too (parity with the nvim-dap adapter).
if [[ "$arch" == "x86_64" ]]; then
    # The recovery is opt-in, and only this launcher knows how the guest was booted:
    # a firmware chain passes the kernel command line itself, so it is not on QEMU's
    # own command line to be read back.  Stated for every x86 attach, exactly as the
    # editor's adapter does -- whether the decompressor's vmlinux happens to be on
    # disk is a separate fact, carried separately below.
    if [[ "${_kaslr:-}" == 1 ]]; then
        [[ -z "${GDBTOOLS_X86_KASLR:-}" ]] && export GDBTOOLS_X86_KASLR=1
    fi
    _decomp="$src/arch/x86/boot/compressed/vmlinux"
    if [[ -f "$_decomp" ]]; then
        [[ -z "${GDBTOOLS_X86_DECOMP_VMLINUX:-}" ]] && export GDBTOOLS_X86_DECOMP_VMLINUX="$_decomp"
        # DECOMP_PA is the switch between gdbtools' two x86 recoveries, so state it
        # only for the boot that has a decompressor at a fixed address: `-kernel`
        # lands the bzImage at the 1MB PA the boot protocol fixes.  A firmware chain
        # enters the EFI stub instead and never runs those stages, so setting it
        # there sends the recovery down a walk that cannot fire.  The port's own run
        # state answers; with none recorded, the tree's stated default does.
        _dboot="${_bmode:-$(kbl_tree_get "$tree" BOOT)}"
        [[ -z "$_dboot" ]] && _dboot=direct
        if [[ "$_dboot" == direct ]]; then
            [[ -z "${GDBTOOLS_X86_DECOMP_PA:-}" ]] && export GDBTOOLS_X86_DECOMP_PA=0x100000
        fi
    fi
fi

declare -a ARGS=(-q -iex "set pagination off")
[[ -n "$tool" ]] && ARGS+=(-ex "source $tool")
ARGS+=("$vmlinux" -ex "target remote :${PORT}")
[[ -n "$FWSYM" ]] && ARGS+=(-ex "add-symbol-file $FWSYM -o 0")
# Default: run straight to the kernel's very first instruction (head.S _text),
# past the QEMU reset shim -- where early-boot debugging begins, and where the
# pre-MMU physical regime is symbolized so pwndbg's context stops erroring.
# Needs gdbtools (kearly) and a guest frozen at reset; --no-bootbreak skips it
# (e.g. attaching to an already-booted guest, where running to _text never
# returns). x86 lands at the decompressor entry; add `kearly kaslr auto` there.
_bootbreak=0
[[ $BOOTBREAK -eq 1 && -n "$tool" ]] && { ARGS+=(-ex "kearly bootbreak"); _bootbreak=1; }
# --stop start_kernel: from _text, run on to start_kernel (gdbtools applies the
# KASLR slide as the high-VA symbol is reached).
[[ -n "$STOP_AT" ]] && ARGS+=(-ex "break $STOP_AT" -ex "continue")
ARGS+=("${PASS[@]+"${PASS[@]}"}")

say "attaching    $gdb -> :$PORT"
[[ -n "$tool" ]] && say "gdbtools     $tool" || say "gdbtools     not installed (plain gdb; pre-MMU symbols will not resolve)"
[[ $_bootbreak -eq 1 ]] && say "bootbreak    on -> stops at head.S _text (pass --no-bootbreak to attach raw)"
exec "$gdb" "${ARGS[@]}"
