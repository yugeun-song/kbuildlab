#!/usr/bin/env bash
# Boot a built tree under QEMU, frozen at the reset vector so a debugger can
# attach before the first instruction.
set -uo pipefail
_self="$(readlink -f "${BASH_SOURCE[0]}")"
KBL_REPO="$(cd -P "$(dirname "$_self")/.." && pwd)"
# shellcheck source=/dev/null
source "${KBL_REPO}/lib/common.sh"

RUN=0; PORT=""; MEM=1G; SMP=2; KVM=auto
declare -a EXTRA=()
declare -a REST=()
while [[ $# -gt 0 ]]; do
    case "$1" in
        --run)   RUN=1; shift ;;
        --port|-g) PORT="${2:?--port needs a number}"; shift 2 ;;
        --mem|-m)  MEM="${2:?}"; shift 2 ;;
        --smp)     SMP="${2:?}"; shift 2 ;;
        --no-kvm)  KVM=0; shift ;;
        --) shift; EXTRA=("$@"); break ;;
        -*) die "run: unknown option '$1' (pass QEMU arguments after --)" ;;
        *)  REST+=("$1"); shift ;;
    esac
done

tree="$(kbl_tree "${REST[0]:-}")" || exit 1
arch="$(kbl_tree_arch "$tree")"
src="$tree/kernel"

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

if [[ -n "$(ss -ltnH "sport = :${PORT}" 2>/dev/null)" ]]; then
    die "port $PORT is already in use.  A second guest there would take the
       port QEMU picks next, and a debugger aiming at $PORT would attach to
       whichever guest got it -- so this stops instead."
fi

declare -a CMD=("$qemu" -machine "$machine" -m "$MEM" -smp "$SMP"
                -kernel "$image" -nographic
                -append "console=$console nokaslr")
[[ -n "$cpu" ]] && CMD+=(-cpu "$cpu")
if [[ "$arch" == "x86_64" && "$KVM" != 0 && -r /dev/kvm && -w /dev/kvm ]]; then
    CMD+=(-enable-kvm)
fi
CMD+=(-gdb "tcp::${PORT}")
[[ $RUN -eq 1 ]] || CMD+=(-S)
CMD+=("${EXTRA[@]:-}")

say "guest        $(basename "$tree") ($arch) on :$PORT"
[[ $RUN -eq 1 ]] || say "frozen       attach with: kbuildlab attach $(basename "$tree")"
exec "${CMD[@]}"
