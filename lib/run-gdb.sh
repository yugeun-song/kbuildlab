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

PORT=""
declare -a REST=() PASS=()
while [[ $# -gt 0 ]]; do
    case "$1" in
        --port|-p) PORT="${2:?--port needs a number}"; shift 2 ;;
        --) shift; PASS=("$@"); break ;;
        -*) die "attach: unknown option '$1' (pass gdb arguments after --)" ;;
        *)  REST+=("$1"); shift ;;
    esac
done

tree="$(kbl_tree "${REST[0]:-}")" || exit 1
arch="$(kbl_tree_arch "$tree")"
vmlinux="$tree/kernel/vmlinux"
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
# in the environment wins, so a one-off override still works.
while IFS='=' read -r k v; do
    v="${v%%#*}"; v="${v%"${v##*[![:space:]]}"}"
    [[ -n "${!k:-}" ]] || export "$k=$v"
done < <(sed -n 's/^[[:space:]]*\(GDBTOOLS_[A-Z0-9_]*\)=\(.*\)/\1=\2/p' "$tree/tree.conf")
export GDBTOOLS_AUTO=1

ARGS+=("$vmlinux" -ex "target remote :${PORT}")
[[ -n "$tool" ]] && ARGS=(-q -iex "set pagination off" -ex "source $tool" "$vmlinux" -ex "target remote :${PORT}")
ARGS+=("${PASS[@]+"${PASS[@]}"}")

say "attaching    $gdb -> :$PORT"
[[ -n "$tool" ]] && say "gdbtools     $tool" || say "gdbtools     not installed (plain gdb; pre-MMU symbols will not resolve)"
exec "$gdb" "${ARGS[@]}"
