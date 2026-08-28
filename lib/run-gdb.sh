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
while [[ $# -gt 0 ]]; do
    case "$1" in
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
# in the environment wins, so a one-off override still works.  GDBTOOLS_ENTRY_PA
# is the exception: the tree states it for editor adapters that only read
# tree.conf (they cannot see the per-run state file), but here the per-port state
# file below is authoritative -- it knows this port's boot mode, so a direct-boot
# port still reaches x86 decompressor recovery instead of a uefi image base.
while IFS='=' read -r k v; do
    [[ "$k" == GDBTOOLS_ENTRY_PA ]] && continue
    v="${v%%#*}"; v="${v%"${v##*[![:space:]]}"}"
    [[ -n "${!k:-}" ]] || export "$k=$v"
done < <(sed -n 's/^[[:space:]]*\(GDBTOOLS_[A-Z0-9_]*\)=\(.*\)/\1=\2/p' "$tree/tree.conf")
export GDBTOOLS_AUTO=1

# Match the exact boot combination `run` recorded for this gdb port (mode + load
# address). In a firmware chain the bootloader lands the kernel entry at a known
# address; point gdbtools' bootbreak HW breakpoint there. Parsed, never sourced.
_st="/dev/shm/kbl-run-${PORT}.env"
FWSYM=""   # firmware ELF symbols (u-boot) for the pre-kernel stages
if [[ -r "$_st" ]]; then
    _bmode="$(sed -n 's/^KBL_BOOT=//p' "$_st" | head -1)"
    _bla="$(sed -n 's/^KBL_LOADADDR=//p' "$_st" | head -1)"
    if [[ ( "$_bmode" == uboot || "$_bmode" == uefi ) && -n "$_bla" ]]; then
        # `_bla` is the image base -- where the firmware lands _text.  gdbtools shifts
        # it to the ELF entry (startup_64) itself when the two differ (newer x86), so
        # hand over the image base as-is and let the symbol offset be applied there,
        # where the vmlinux is loaded.
        [[ -n "${GDBTOOLS_ENTRY_PA:-}" ]]   || export GDBTOOLS_ENTRY_PA="$_bla"
        [[ -n "${GDBTOOLS_BREAK_KIND:-}" ]] || export GDBTOOLS_BREAK_KIND=hw
        say "combo        $_bmode: kernel image base $_bla (firmware bootbreak via hw-bp)"
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

# x86 KASLR recovery reads the decompressor's vmlinux; hand its path over so
# `kearly kaslr` works in terminal mode too (parity with the nvim-dap adapter).
if [[ "$arch" == "x86_64" ]]; then
    _decomp="$src/arch/x86/boot/compressed/vmlinux"
    if [[ -f "$_decomp" ]]; then
        [[ -z "${GDBTOOLS_X86_DECOMP_VMLINUX:-}" ]] && export GDBTOOLS_X86_DECOMP_VMLINUX="$_decomp"
        # QEMU loads the bzImage decompressor at the fixed 1MB PA; gdbtools needs
        # it to recover the KASLR-randomized main-kernel base.
        [[ -z "${GDBTOOLS_X86_DECOMP_PA:-}" ]] && export GDBTOOLS_X86_DECOMP_PA=0x100000
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
