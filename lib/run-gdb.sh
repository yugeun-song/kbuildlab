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

PORT=""; PORT_SET=0; BOOTBREAK=1; STOP=""; STOP_AT=""; LIST=0; FIRST=0
declare -a REST=() PASS=()
_usage() {
    cat <<USAGE
kbuildlab attach [TREE] [options] [-- GDB ARGS]
  attach gdb to a running guest, stopped at head.S _text by default

  --no-bootbreak|--raw   attach without running to _text (already-booted guest)
  --stop WHERE           text (default) | firmware | start_kernel
  --port|-p N            attach by gdb port; the tree is recovered from the
                         guest's own run state, so TREE may be omitted
  --list|-l              list live guests and exit; never prompts
  --first                with several matches, take the lowest port instead of
                         asking (for scripts)
  -- GDB ARGS            pass the rest straight to gdb

  TREE is a name, directory, or kernel source root; omitted, the current tree.
  TREE and --port may be given together and must then agree.

  With one live guest this behaves exactly as it always has.  With several of the
  same tree -- which is normal, since 'run' advances to a free port rather than
  refusing -- it asks which.  The tree's GDB_PORT is 'run's default and not
  'attach's: once a second guest exists that number no longer identifies one.
USAGE
}
while [[ $# -gt 0 ]]; do
    case "$1" in
        -h|--help)            _usage; exit 0 ;;
        --port|-p)            PORT="${2:?--port needs a number}"; PORT_SET=1; shift 2 ;;
        --list|-l)            LIST=1; shift ;;
        --first)              FIRST=1; shift ;;
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

# --------------------------------------------------------------- which guest
# A tree name no longer names ONE guest.  `run` advances to a free port rather
# than refusing, so a second guest of the same tree is normal and its port is a
# number that appears nowhere in tree.conf.  Attach therefore discovers instead
# of assuming, and asks when the answer is genuinely ambiguous.
#
# What it must NOT do is probe the gdbstub to find out more.  A bare TCP connect
# -- not one RSP byte -- pauses a running guest and leaves it paused after the
# socket closes (measured on qemu 11.1.1 via QMP query-status).  Everything below
# reads /proc; nothing opens that port.
_uptime_of() { ps -o etime= -p "$1" 2>/dev/null | tr -d ' '; }

_row_fields() {   # sets the _r_* variables from one kbl_instances line
    # \x1f, not tab: see kbl_instances -- tab collapses empty middle fields.
    IFS=$'\x1f' read -r _r_port _r_pid _r_start _r_frozen _r_qbin _r_sf \
                       _r_tree _r_name _r_boot _r_kaslr _r_ssh <<<"$1"
}

_print_table() {   # _print_table ROW...
    printf '  %-3s %-6s %-6s %-16s %-7s %-6s %-8s %-20s %-8s %s\n' \
        '#' port ssh tree boot kaslr state dbg pid uptime
    local i=0 r
    for r in "$@"; do
        i=$((i + 1)); _row_fields "$r"
        printf '  %-3s %-6s %-6s %-16s %-7s %-6s %-8s %-20s %-8s %s\n' \
            "$i" "$_r_port" "${_r_ssh:--}" "${_r_name:-?}" "${_r_boot:-?}" \
            "$(case "$_r_kaslr" in 1) echo on ;; 0) echo off ;; *) echo '?' ;; esac)" \
            "$([[ "$_r_frozen" == 1 ]] && echo frozen || echo running)" \
            "$(kbl_gdb_attached "$_r_port" "$_r_pid" "$(kbl_qemu_gdb_bind "$_r_pid")")" \
            "$_r_pid" "$(_uptime_of "$_r_pid")"
    done
}

mapfile -t _rows < <(kbl_instances)

# A tree given on the command line filters; a row that recorded no tree is not
# claimed by any name, because guessing which tree an unlabelled guest belongs to
# is how a session comes up with the wrong symbols and no sign of it.
_want_tree=""
if [[ -n "${REST[0]:-}" ]]; then
    _want_tree="$(kbl_tree "${REST[0]}")" || exit 1
elif [[ $PORT_SET -eq 0 ]]; then
    # No tree named and no port: the current directory decides, exactly as before.
    _want_tree="$(kbl_tree "" 2>/dev/null)" || _want_tree=""
fi

# Two passes, because "the port matched but the tree did not" and "nothing is on
# that port" are different failures and deserve different messages.  Collapsing
# them -- which one filter does -- reports a live guest of another tree as an
# unreadable process, which sends the reader looking for a permissions problem
# that is not there.
declare -a _byport=() _cand=()
for _r in "${_rows[@]+"${_rows[@]}"}"; do
    _row_fields "$_r"
    [[ $PORT_SET -eq 1 && "$_r_port" != "$PORT" ]] && continue
    _byport+=("$_r")
done
_treeport=""
[[ -n "$_want_tree" ]] && _treeport="$(kbl_tree_get "$_want_tree" GDB_PORT)"
for _r in "${_byport[@]+"${_byport[@]}"}"; do
    _row_fields "$_r"
    if [[ -n "$_want_tree" ]]; then
        if [[ -n "$_r_tree" ]]; then
            [[ "$(readlink -f "$_r_tree")" == "$(readlink -f "$_want_tree")" ]] || continue
        else
            # A guest this tool did not start records nothing.  Two things can
            # still name it:
            #   - the user gave BOTH a tree and --port, which IS the assertion
            #     that the two go together.  Refusing there refuses the only
            #     instruction available, and it is a regression: before discovery
            #     existed, `attach TREE --port N` needed nothing but a listener.
            #   - the port is the tree's own GDB_PORT, the historical link.
            # Anything else is claimed by no name, because guessing which tree an
            # unlabelled guest belongs to is how a session comes up with
            # confident, wrong symbols.
            if [[ $PORT_SET -eq 1 && "$_r_port" == "$PORT" ]]; then :
            elif [[ -n "$_treeport" && "$_r_port" == "$_treeport" ]]; then :
            else continue
            fi
            _r_name="${_r_name:-$(basename "$_want_tree")}"
            _r="$(printf '%s\x1f%s\x1f%s\x1f%s\x1f%s\x1f%s\x1f%s\x1f%s\x1f%s\x1f%s\x1f%s' \
                  "$_r_port" "$_r_pid" "$_r_start" "$_r_frozen" "$_r_qbin" "$_r_sf" \
                  "$_want_tree" "$_r_name" "$_r_boot" "$_r_kaslr" "$_r_ssh")"
        fi
    fi
    _cand+=("$_r")
done

if [[ $LIST -eq 1 ]]; then
    if [[ ${#_cand[@]} -eq 0 ]]; then
        echo "no live guest${_want_tree:+ of $(basename "$_want_tree")}"
    else
        echo "live guests${_want_tree:+ of $(basename "$_want_tree")}:"
        _print_table "${_cand[@]}"
        # `state` is read from qemu's own -S flag, which says how the guest was
        # STARTED.  Whether it is stopped right now is a different question, and
        # the only way to ask it is through the monitor -- i.e. by touching the
        # stub, which pauses a running guest.  Saying what can be known beats
        # guessing at what cannot.
        echo "  state is how the guest was started (-S); whether it is stopped NOW"
        echo "  cannot be read without touching the stub, which would pause it."
    fi
    exit 0
fi

_chosen=""
case ${#_cand[@]} in
  0)
    # A guest IS on the port that was NAMED; it is just not the tree that was
    # named with it.  Only reachable when --port was given: without it _byport is
    # every live guest on the host, and taking its first element blamed an
    # unrelated tree's guest and quoted a --port the caller never typed.
    if [[ $PORT_SET -eq 1 && ${#_byport[@]} -gt 0 ]]; then
        _row_fields "${_byport[0]}"
        if [[ -n "$_r_tree" ]]; then
            die "attach: --port $_r_port is running $(basename "$_r_tree"), not $(basename "$_want_tree")
       (recorded in $_r_sf when that port was started)"
        fi
        die "attach: :$_r_port has a live $_r_qbin (pid $_r_pid) but nothing recorded which
       tree it is, so it cannot be matched against $(basename "$_want_tree").  Name it:
       kbuildlab attach <tree> --port $_r_port"
    fi
    if [[ $PORT_SET -eq 1 ]]; then
        if [[ -n "$(ss -ltnH "sport = :${PORT}" 2>/dev/null)" ]]; then
            die "attach: :$PORT is listening but no readable qemu-system process holds it
       (another user's, or not a guest).  Refusing to guess which tree that is."
        fi
        die "attach: nothing on :$PORT -- no qemu-system process carries -gdb for it,
       and nothing is listening there"
    fi
    die "attach: no live guest${_want_tree:+ of $(basename "$_want_tree")} -- no qemu-system
       process carries a gdb port recorded as this tree.  Start one:
       kbuildlab run ${_want_tree:+$(basename "$_want_tree")}" ;;
  1) _chosen="${_cand[0]}" ;;
  *)
    if [[ $FIRST -eq 1 ]]; then
        _chosen="${_cand[0]}"                       # kbl_instances sorts by port
    elif [[ -t 0 && -t 1 ]]; then
        echo "several live guests${_want_tree:+ of $(basename "$_want_tree")}:"
        _print_table "${_cand[@]}"
        if command -v fzf >/dev/null 2>&1; then
            _pick="$( { _print_table "${_cand[@]}"; } | fzf --header-lines=1 --no-multi \
                        --prompt='attach which? ' --height=40% --reverse )" || _pick=""
            [[ -n "$_pick" ]] || die "attach: cancelled"
            _n="$(awk '{print $1}' <<<"$_pick")"
        else
            _n=""
            for _try in 1 2 3; do
                read -r -p "  attach which? [1-${#_cand[@]}, q to cancel] " _n || _n=q
                [[ "$_n" == q || -z "$_n" ]] && die "attach: cancelled"
                [[ "$_n" =~ ^[0-9]+$ && "$_n" -ge 1 && "$_n" -le ${#_cand[@]} ]] && break
                _n=""
                echo "  not one of 1-${#_cand[@]}"
            done
        fi
        [[ -n "$_n" ]] || die "attach: no choice made"
        _chosen="${_cand[$((_n - 1))]}"
    else
        _ports=""
        for _r in "${_cand[@]}"; do _row_fields "$_r"; _ports="$_ports --port $_r_port"; done
        die "attach: ${#_cand[@]} live instances${_want_tree:+ of $(basename "$_want_tree")} and stdin
       is not a terminal, so the chooser cannot run.  Name one:$_ports"
    fi ;;
esac

_row_fields "$_chosen"
PORT="$_r_port"
if [[ -n "$_r_tree" ]]; then
    if [[ -n "$_want_tree" && "$(readlink -f "$_r_tree")" != "$(readlink -f "$_want_tree")" ]]; then
        die "attach: --port $PORT is running $(basename "$_r_tree"), not $(basename "$_want_tree")
       (recorded in $_r_sf when that port was started)"
    fi
    tree="$_r_tree"
elif [[ -n "$_want_tree" ]]; then
    tree="$_want_tree"
else
    die "attach: :$PORT has a live $_r_qbin (pid $_r_pid) but nothing recorded which
       tree it is.  Name it:  kbuildlab attach <tree> --port $PORT"
fi

# The stub serves one client; a second waits in the accept queue rather than
# being refused.  Say so instead of appearing to hang.
_dbg="$(kbl_gdb_attached "$PORT" "$_r_pid" "$(kbl_qemu_gdb_bind "$_r_pid")")"
case "$_dbg" in
    attached*) warn "attach: :$PORT already has a debugger attached, and the gdbstub serves
       exactly one client.  This gdb will NOT queue usefully: the kernel completes
       the handshake, qemu never accepts the socket, and the packets this gdb sends
       sit unread until its timeout -- after which, if the first client leaves, the
       late replies arrive one packet out of step and the session fails with a
       protocol error rather than attaching.  Detach the other client first, or
       start a second guest:  kbuildlab run $(basename "$tree")" ;;
esac

arch="$(kbl_tree_arch "$tree")"
src="$(kbl_tree_src "$tree")"
vmlinux="$src/vmlinux"
[[ -f "$vmlinux" ]] || die "no vmlinux at $vmlinux
       build it first: kbuildlab build $(basename "$tree")"

gdb="$(kbl_gdb "$arch")"

# gdbtools, if it is installed, is what makes symbols work before the MMU is on.
# It is optional and found the way it documents; absent is a normal answer.
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
# The qemu pid is the one discovery already established for this row, not a fresh
# search.  A command-line pattern broad enough to catch qemu also catches any
# shell holding that pattern as an argument -- including this script's own -- and
# `head -1` then picks whichever has the lower pid.  Discovery found this process
# by argv[0], which a shell cannot fake, so re-finding it here can only be worse.
if [[ -z "$_kaslr" ]]; then
    # Only the -append VALUE decides, as discover.lua does, and it is read from
    # /proc so argument boundaries survive: matching the whole command line would
    # let a path like /srv/vm/nokaslr-disk.img answer a question it has nothing to
    # do with.  A firmware chain has no -append at all, which is why the run state
    # records the command line and is consulted first, above.
    _qpid="${_r_pid:-}"; _app=""
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

# gdb's own working directory is the kernel source tree.  The kernel's
# scripts/gdb commands expect it: lx-symbols reloads the image with a bare
# `symbol-file vmlinux`, and lx-dmesg / lx-lsmod resolve module .ko paths the same
# relative way, so from anywhere else they fail with "vmlinux: No such file or
# directory" -- which is what happens today.  Documentation/dev-tools/gdb-kernel-
# debugging.rst says to run gdb from the build directory for exactly this reason.
# Every path this launcher passes is already absolute, so nothing else moves.
# -iex, so both are in force before ANY command runs -- including the kernel's
# own scripts/gdb, which resolve `vmlinux` and module paths relative to gdb's
# working directory.  The log file is pinned to the directory the command was
# typed in, since the chdir would otherwise put gdb.txt inside the kernel tree.
declare -a ARGS=(-q -iex "set pagination off" -iex "cd $src"
                 -iex "set logging file $PWD/gdb.txt")
[[ -n "$tool" ]] && ARGS+=(-ex "source $tool")
# Connect to the address the stub was BOUND to, not to an assumed loopback.  The
# launcher records it (KBL_GDB_BIND) and qemu's own `-gdb tcp:HOST:PORT` carries
# it; without this a tree that set GDB_BIND to be reachable from another machine
# is reported as listening by everything here and connectable by nothing.  A
# wildcard bind means every interface, and loopback is then the way in.
_gbind=""
[[ -n "$_st" && -r "$_st" ]] && _gbind="$(sed -n 's/^KBL_GDB_BIND=//p' "$_st" | head -1)"
if [[ -z "$_gbind" && -n "${_r_pid:-}" ]]; then
    _gdev="$(kbl_qemu_arg "$_r_pid" -gdb 2>/dev/null)"
    [[ "$_gdev" == tcp:* ]] && { _gbind="${_gdev#tcp:}"; _gbind="${_gbind%:*}"; }
fi
case "$_gbind" in ""|"0.0.0.0"|"::"|"[::]") _gbind="localhost" ;; esac
ARGS+=("$vmlinux" -ex "target remote ${_gbind}:${PORT}")
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
# gdb's working directory has to BE the kernel source, and it has to be that
# BEFORE anything runs: lx-symbols reloads the image with a bare
# `symbol-file vmlinux`, and lx-dmesg / lx-lsmod resolve module paths the same
# relative way, so from anywhere else they fail with "vmlinux: No such file or
# directory".  Documentation/dev-tools/gdb-kernel-debugging.rst says to run gdb
# from the build directory for exactly this reason.
#
# That silently re-rooted every relative path the caller passed after `--`.  So
# the caller's own file arguments are made absolute FIRST -- and not by guessing
# which ones are paths, but by asking: a relative argument that names a file
# existing here, in the directory the command was typed in, is that file.  One
# that does not is left exactly as written, because then it is not a path this
# code has any business rewriting.
# Only the positions gdb's own syntax says are paths.  A catch-all "if a file by
# that name exists here, it is a path" is a guess, and a wrong one: `-ex version`
# in a directory that happens to contain a file called `version` would be
# rewritten into an absolute path and stop being a gdb command.
_relabs() {   # _relabs VALUE -> absolute if it names a file here, else unchanged
    [[ "$1" != /* && -e "$PWD/$1" ]] && printf '%s\n' "$PWD/$1" || printf '%s\n' "$1"
}
for ((_i = 0; _i < ${#PASS[@]}; _i++)); do
    case "${PASS[_i]}" in
        # OPTION VALUE forms
        -x|--command|-s|--symbols|-e|--exec|-c|--core|-d|--directory|--se|--se=*)
            [[ "${PASS[_i]}" == --se=* ]] \
                && PASS[_i]="--se=$(_relabs "${PASS[_i]#--se=}")" \
                || { [[ -n "${PASS[_i+1]:-}" ]] && PASS[_i+1]="$(_relabs "${PASS[_i+1]}")"; } ;;
        # OPTION=VALUE forms
        --command=*|--symbols=*|--exec=*|--core=*|--directory=*)
            _o="${PASS[_i]%%=*}"; PASS[_i]="$_o=$(_relabs "${PASS[_i]#*=}")" ;;
        # A bare first argument is the symbol file; every later one is gdb's own
        # positional grammar and is left alone.
        -*) : ;;
        *)  [[ $_i -eq 0 ]] && PASS[_i]="$(_relabs "${PASS[_i]}")" ;;
    esac
done
ARGS+=("${PASS[@]+"${PASS[@]}"}")

say "attaching    $gdb -> :$PORT"
[[ -n "$tool" ]] && say "gdbtools     $tool" || say "gdbtools     not installed (plain gdb; pre-MMU symbols will not resolve)"
[[ $_bootbreak -eq 1 ]] && say "bootbreak    on -> stops at head.S _text (pass --no-bootbreak to attach raw)"
exec "$gdb" "${ARGS[@]}"
