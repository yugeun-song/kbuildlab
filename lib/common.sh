# Shared resolution and validation.  Source, do not run.
#
# The contract this file enforces: kbuildlab is handed a workspace and a tree
# name, and it validates them.  It does not search the host for something it was
# not given, and it does not substitute a default for a value that is missing --
# it says which value is missing and stops.  A guess here produces a build that
# looks right and is not.

set -uo pipefail

kbl_repo() {
    local self; self="$(readlink -f "${BASH_SOURCE[0]}")"
    (cd -P "$(dirname "$self")/.." && pwd)
}
KBL_REPO="${KBL_REPO:-$(kbl_repo)}"

die()  { printf 'kbuildlab: %s\n' "$*" >&2; exit 1; }
warn() { printf 'kbuildlab: %s\n' "$*" >&2; }
say()  { printf '  %s\n' "$*"; }

# --- host prerequisites ------------------------------------------------------
# Reported by name and never installed.  Changing a machine's packages or its
# sysctls is not this tool's business; saying precisely what is absent is.
# podman first, then docker.  Rootless podman needs no daemon at all, so a machine
# that keeps dockerd off -- a laptop, say -- builds without starting a service or
# asking for root.  $KBL_CONTAINER still wins over both when a machine wants one.
kbl_container() {
    local c
    for c in ${KBL_CONTAINER:-} podman docker nerdctl; do
        [[ -n "$c" ]] && command -v "$c" >/dev/null 2>&1 && { printf '%s\n' "$c"; return 0; }
    done
    die "no container runtime found. Need one of: podman, docker, nerdctl
       (or set \$KBL_CONTAINER to the one you use)"
}

# The workspace is bind-mounted into the build container at the SAME path it has
# here, so a workspace that resolved to a system directory would hand an Ubuntu
# container write access to this host's package-managed files -- and the package
# manager would then find them changed underneath it.  Refuse before mounting;
# print the resolved path so the caller mounts exactly what was checked.
#
# The list is what a distribution actually owns, not everything outside $HOME:
# /run is tmpfs and /run/media is where removable drives land, /var is mostly
# state, so neither is banned wholesale -- only the package databases inside /var
# are.  $KBL_ALLOW_SYSTEM_MOUNT=1 overrides the whole check for the case this
# judgement gets wrong; it says what it is allowing.
kbl_assert_mountable() {
    local p sys
    p="$(readlink -f "${1:-}" 2>/dev/null)"
    [[ -n "$p" ]] || die "workspace '${1:-}' does not resolve to a path"
    [[ "$p" != "/" ]] \
        || die "workspace '${1:-}' resolves to the filesystem root; refusing to mount it into a container"
    if [[ "${KBL_ALLOW_SYSTEM_MOUNT:-}" == 1 ]]; then
        warn "mounting '$p' into a container because \$KBL_ALLOW_SYSTEM_MOUNT=1"
        printf '%s\n' "$p"; return 0
    fi
    for sys in /usr /etc /boot /opt /bin /sbin /lib /lib64 /proc /sys /dev \
               /var/lib/pacman /var/cache/pacman /var/lib/dpkg /var/lib/rpm /var/db; do
        [[ "$p" == "$sys" || "$p" == "$sys"/* ]] \
            && die "workspace '$p' is inside $sys, which the system owns; refusing to mount it into a container (set \$KBL_ALLOW_SYSTEM_MOUNT=1 if you mean it)"
    done
    printf '%s\n' "$p"
}

kbl_qemu() {
    local arch="$1" bin
    case "$arch" in
        arm64|aarch64) bin=qemu-system-aarch64 ;;
        riscv64)       bin=qemu-system-riscv64 ;;
        x86_64|amd64)  bin=qemu-system-x86_64 ;;
        *) die "unknown architecture '$arch'" ;;
    esac
    command -v "$bin" >/dev/null 2>&1 || die "$bin not found; install QEMU for $arch"
    printf '%s\n' "$bin"
}

# A cross gdb, or the host one if it was built for every target.  Names differ
# between distributions, so the candidates are probed rather than assumed.
kbl_gdb() {
    local arch="$1" c
    local -a cands
    case "$arch" in
        arm64|aarch64) cands=(aarch64-linux-gnu-gdb aarch64-none-linux-gnu-gdb
                              aarch64-none-elf-gdb gdb-multiarch) ;;
        riscv64)       cands=(riscv64-linux-gnu-gdb riscv64-unknown-linux-gnu-gdb
                              riscv64-unknown-elf-gdb gdb-multiarch) ;;
        x86_64|amd64)  cands=(gdb-multiarch gdb) ;;
        *) die "unknown architecture '$arch'" ;;
    esac
    for c in "${cands[@]}"; do
        command -v "$c" >/dev/null 2>&1 && { printf '%s\n' "$c"; return 0; }
    done
    if command -v gdb >/dev/null 2>&1 &&
       gdb --configuration 2>/dev/null | grep -q -- '--enable-targets=all'; then
        printf '%s\n' gdb; return 0
    fi
    die "no gdb that can debug $arch. Tried: ${cands[*]}
       The host gdb is only usable if it was built --enable-targets=all"
}

# --- the workspace -----------------------------------------------------------
# Handed in, or recorded once.  Never searched for: picking the wrong workspace
# attaches correct-looking symbols to the wrong machine, which is the one
# failure this tool must not produce quietly.
KBL_CONFIG_HOME="${XDG_CONFIG_HOME:-$HOME/.config}/kbuildlab"

kbl_workspace() {
    local d
    if [[ -n "${KBL_WORKSPACE:-}" ]]; then
        d="$KBL_WORKSPACE"
    elif [[ -r "$KBL_CONFIG_HOME/workspace" ]]; then
        d="$(<"$KBL_CONFIG_HOME/workspace")"
    else
        die "no workspace. Set \$KBL_WORKSPACE, or record one:
       kbuildlab init --workspace DIR"
    fi
    [[ -d "$d" ]] || die "workspace does not exist: $d"
    (cd -P "$d" && pwd)
}

# A directory is a tree when it holds a tree.conf.  One rule, so every command
# answers the same way.
kbl_is_tree() { [[ -f "$1/tree.conf" ]]; }

# A kernel source root, recognised by content and not by name: the clone may sit
# in a directory called anything ('kernel', 'linux', 'dummy').  The top-level
# kernel Makefile carries the VERSION/PATCHLEVEL/SUBLEVEL block, and a real tree
# also has arch/ and a top-level Kbuild -- together an unambiguous fingerprint.
kbl_is_ksrc() {
    local d="$1"
    [[ -d "$d/arch" && -f "$d/Kbuild" && -f "$d/Makefile" ]] || return 1
    grep -qE '^[[:space:]]*VERSION[[:space:]]*=' "$d/Makefile" 2>/dev/null \
        && grep -qE '^[[:space:]]*PATCHLEVEL[[:space:]]*=' "$d/Makefile" 2>/dev/null \
        && grep -qE '^[[:space:]]*SUBLEVEL[[:space:]]*=' "$d/Makefile" 2>/dev/null
}

# Map any path the user might name -- a tree directory, or the kernel source root
# inside one -- to the tree that owns it.  Prints the tree dir; empty on no match.
_kbl_owning_tree() {
    local d="$1" p
    [[ -d "$d" ]] || return 1
    d="$(cd -P "$d" && pwd)" || return 1
    kbl_is_tree "$d" && { printf '%s\n' "$d"; return 0; }
    if kbl_is_ksrc "$d"; then
        p="$(cd -P "$d/.." && pwd)"
        kbl_is_tree "$p" && { printf '%s\n' "$p"; return 0; }
    fi
    return 1
}

kbl_trees() {
    local ws d; ws="$(kbl_workspace)" || exit 1
    for d in "$ws"/*/; do kbl_is_tree "${d%/}" && basename "${d%/}"; done
}

kbl_tree() {
    local want="${1:-}" ws d n t
    ws="$(kbl_workspace)" || exit 1
    if [[ -n "$want" ]]; then
        # A path (absolute, or relative with a slash) is tried as given and
        # relative to CWD; a bare name is a tree under the workspace.  Either the
        # tree directory or the kernel source root inside it may be named.
        local -a cand=()
        if   [[ "$want" == /* ]];  then cand=("$want")
        elif [[ "$want" == */* ]]; then cand=("$PWD/$want" "$ws/$want")
        else                            cand=("$ws/$want"); fi
        for d in "${cand[@]}"; do
            t="$(_kbl_owning_tree "$d")" && { printf '%s\n' "$t"; return 0; }
        done
        die "'$want' is not a tree or a kernel source (no tree.conf, no kernel Makefile)
       have: $(kbl_trees | tr '\n' ' ')"
    fi
    t="$(_kbl_owning_tree "$PWD")" && { printf '%s\n' "$t"; return 0; }
    mapfile -t n < <(kbl_trees)
    case ${#n[@]} in
        0) die "no trees in $ws. Create one: kbuildlab init <name> --arch ARCH" ;;
        1) (cd -P "$ws/${n[0]}" && pwd) ;;
        *) die "several trees in $ws -- name one: ${n[*]}" ;;
    esac
}

# --- the tree's own description ----------------------------------------------
# Read from the tree, never inferred from its directory name: renaming a
# directory must not change what gets built.
# tree.conf grammar, and it is the SAME one in all five parsers that read this
# file (kbuildlab's kbl_tree_get and its GDBTOOLS_ passthrough, the two firmware
# scripts, and the editor adapter's discover.lua):
#
#     ^\s* KEY \s*=\s* VALUE   with a trailing  # comment  and trailing space
#     stripped, and one layer of surrounding double quotes removed.
#
# They used to differ: three demanded KEY= with no space while the Lua one
# accepted spaces, and only one stripped quotes.  A line written as
# `GDBTOOLS_ENTRY_PA = 0x40200000` was then honoured by the editor and dropped in
# silence by the terminal, so the same guest calibrated to two different addresses
# with nothing to say which was which.
kbl_tree_get() {
    local tree="$1" key="$2" v
    v="$(sed -n "s/^[[:space:]]*${key}[[:space:]]*=[[:space:]]*//p" "$tree/tree.conf" | head -1)"
    if [[ "$v" == \"* ]]; then v="${v#\"}"; v="${v%%\"*}"
    else v="${v%%#*}"; v="${v%"${v##*[![:space:]]}"}"; fi
    printf '%s\n' "$v"
}

kbl_tree_arch() {
    local a; a="$(kbl_tree_get "$1" ARCH)"
    [[ -n "$a" ]] || die "$1/tree.conf states no ARCH"
    case "$a" in
        arm64|aarch64|riscv64|x86_64) printf '%s\n' "$a" ;;
        *) die "$1/tree.conf states ARCH=$a, which is not one of arm64, riscv64, x86_64" ;;
    esac
}

# The kernel source directory inside a tree.  Named by SRC_DIR in tree.conf when
# set; else the single kernel source root among the tree's immediate children;
# else the historical default 'kernel' (also the clone target before any source
# exists).  'kernel' is a default, never a requirement -- the clone may be named
# anything and is still found by content.
kbl_tree_src() {
    local tree="$1" d n=0 found="" sd
    sd="$(kbl_tree_get "$tree" SRC_DIR)"
    [[ -n "$sd" ]] && { printf '%s\n' "$tree/$sd"; return 0; }
    for d in "$tree"/*/; do
        d="${d%/}"
        kbl_is_ksrc "$d" && { found="$d"; n=$((n + 1)); }
    done
    case $n in
        1) printf '%s\n' "$found" ;;
        0) printf '%s\n' "$tree/kernel" ;;
        *) die "$tree holds more than one kernel source; name one in tree.conf: SRC_DIR=<dir>" ;;
    esac
}

# --- per-run state -----------------------------------------------------------
# Where `run` records what it started and `attach` reads it back.  A path, not a
# constant, because it is a contract with three other readers (the nvim adapter,
# and both halves of this tool) and moving it must be one coordinated change.
#
# The default stays /dev/shm rather than $XDG_RUNTIME_DIR, and that is a
# measured choice: /run/user/<uid> is 1.55 GB on this host while /dev/shm is
# 7.6 GB, and a run copies an 88-139 MB boot image beside the state file.  A
# dozen guests would fill XDG_RUNTIME_DIR, and the damage would land on the
# user's other services rather than on this tool.  XDG_RUNTIME_DIR is also
# removed at last logout, which would delete the state out from under a guest
# started with nohup.
kbl_statedir() {
    local d="${KBL_STATE_DIR:-/dev/shm}"
    [[ -d "$d" ]] || die "state directory does not exist: $d (set \$KBL_STATE_DIR)"
    printf '%s\n' "$d"
}

# The gdb port a running qemu-system process carries, or nothing.  Read from
# /proc, never from a state file: a process is the only first-hand evidence that
# a guest is alive, and the state file can outlive it.
#
# Both spellings qemu accepts: `-gdb tcp::PORT`, `-gdb tcp:HOST:PORT`, and on
# 1234 the `-s` shorthand.  argv is read NUL-separated so an argument boundary is
# never guessed from spacing.
kbl_qemu_gdb_port() {
    local pid="$1" f="/proc/$1/cmdline"
    [[ -r "$f" ]] || return 1
    local -a argv=(); mapfile -d '' -t argv < "$f" 2>/dev/null || return 1
    [[ ${#argv[@]} -gt 0 ]] || return 1
    [[ "$(basename -- "${argv[0]}")" == qemu-system-* ]] || return 1
    local i
    for ((i = 0; i < ${#argv[@]}; i++)); do
        case "${argv[i]}" in
            -gdb) local v="${argv[i+1]:-}"
                  [[ "$v" == tcp:* ]] && { printf '%s\n' "${v##*:}"; return 0; } ;;
            -s)   printf '1234\n'; return 0 ;;
        esac
    done
    return 1
}

# The ADDRESS a live qemu's gdbstub is bound to, from its own `-gdb tcp:HOST:PORT`.
# Empty means it was spelled without a host, which binds every interface.
kbl_qemu_gdb_bind() {
    local v; v="$(kbl_qemu_arg "$1" -gdb 2>/dev/null)" || return 0
    [[ "$v" == tcp:* ]] || return 0
    v="${v#tcp:}"; printf '%s\n' "${v%:*}"
}

# One argv value by option name, for a live qemu (e.g. -machine, -smp).
kbl_qemu_arg() {
    local pid="$1" want="$2" f="/proc/$1/cmdline"
    [[ -r "$f" ]] || return 1
    local -a argv=(); mapfile -d '' -t argv < "$f" 2>/dev/null || return 1
    local i
    for ((i = 0; i < ${#argv[@]}; i++)); do
        [[ "${argv[i]}" == "$want" ]] && { printf '%s\n' "${argv[i+1]:-}"; return 0; }
    done
    return 1
}

kbl_qemu_has_flag() {
    local pid="$1" want="$2" f="/proc/$1/cmdline" a
    [[ -r "$f" ]] || return 1
    local -a argv=(); mapfile -d '' -t argv < "$f" 2>/dev/null || return 1
    for a in "${argv[@]}"; do [[ "$a" == "$want" ]] && return 0; done
    return 1
}

# /proc/PID/stat field 22 (starttime).  Together with the pid this is a key that
# survives pid reuse, which "is that pid still alive?" alone does not.
#
# Parsed from after the LAST ') ' rather than by column number: comm sits in
# parentheses and may contain BOTH spaces and parentheses, so a shortest-match
# cut lands inside comm and shifts every field after it -- which this did, and
# then returned 0 for the very key that exists to survive pid reuse.  qemu's
# comm is truncated to 15 characters ("qemu-system-aar") and has none today, but
# a rule that happens to work is not a rule.
kbl_proc_starttime() {
    local f="/proc/$1/stat" line rest
    [[ -r "$f" ]] || return 1
    read -r line < "$f" || return 1
    rest="${line##*) }"                                   # ## = LAST ') ', not the first
    printf '%s\n' "$(awk '{print $20}' <<<"$rest")"      # 22nd overall = 20th after comm
}

# Is a debugger already on this gdbstub?  Answered from /proc alone.
#
# NOT by connecting.  A bare TCP connect -- not one RSP byte sent -- PAUSES a
# running guest: measured on qemu 11.1.1, QMP query-status goes from
# running:true to status:paused and stays paused after the socket is closed.  A
# probe that asks "is anyone attached?" would therefore stop the user's guest
# and leave it stopped.  Nothing in this file may open that port.
#
# The evidence is an intersection.  Take the socket inodes qemu's own fds hold,
# and the inodes of ESTABLISHED rows on this port in /proc/net/tcp{,6}: an inode
# in both means qemu accepted that connection.  Ownership matters because the
# gdbstub listens with a backlog and keeps listening after it accepts, so a
# SECOND client completes its handshake and shows as ESTABLISHED while qemu does
# not hold it -- it is queued, waiting for the first to leave.  Counting bare
# ESTABLISHED rows reports that queued client as attached.
#
# Both address families are read: `-gdb tcp::N` binds 0.0.0.0 and [::], so a
# client from ::1 appears only in /proc/net/tcp6.
#
# Prints one of: attached | attached +N waiting | waiting | - | unknown
# `bind` is the address the stub is on ("" or a wildcard means any).  It matters:
# the stub now binds 127.0.0.1 by default, so an ESTABLISHED socket whose LOCAL
# port happens to be the same number on a different address has nothing to do
# with it -- and counting it reported a waiting client on a guest nobody had
# touched.
# Is the run that claimed this port still setting it up?  pid AND start time,
# because a pid alone is not an identity: a reused one would make a dead run's
# claim look live for ever, and its 88-139 MB boot image would never be reclaimed.
kbl_launcher_alive() {
    local f="$1" lp ls now
    [[ -r "$f" ]] || return 1
    lp="$(sed -n 's/^KBL_LAUNCHER_PID=//p' "$f" | head -1)"
    [[ -n "$lp" ]] || return 1
    kill -0 "$lp" 2>/dev/null || return 1
    ls="$(sed -n 's/^KBL_LAUNCHER_START=//p' "$f" | head -1)"
    [[ -n "$ls" ]] || return 0                     # older record: pid is all there is
    now="$(kbl_proc_starttime "$lp")" || return 0
    [[ "$now" == "$ls" ]]
}

kbl_gdb_attached() {
    local port="$1" pid="${2:-}" bind="${3:-}" hexport hexaddr=""
    hexport="$(printf '%04X' "$port")"
    case "$bind" in
        ""|"0.0.0.0"|"::"|"[::]"|"*") hexaddr="" ;;                 # any address
        "127.0.0.1"|"localhost")      hexaddr="0100007F" ;;         # LE, as /proc prints it
        *) if [[ "$bind" =~ ^([0-9]+)\.([0-9]+)\.([0-9]+)\.([0-9]+)$ ]]; then
               hexaddr="$(printf '%02X%02X%02X%02X' "${BASH_REMATCH[4]}" "${BASH_REMATCH[3]}" \
                                                    "${BASH_REMATCH[2]}" "${BASH_REMATCH[1]}")"
           fi ;;
    esac
    local -A own=()
    if [[ -n "$pid" && -r "/proc/$pid/fd" ]]; then
        local l t
        for l in /proc/$pid/fd/*; do
            t="$(readlink -- "$l" 2>/dev/null)" || continue
            [[ "$t" == socket:\[*\] ]] || continue
            t="${t#socket:[}"; t="${t%]}"
            own["$t"]=1
        done
    elif [[ -n "$pid" ]]; then
        printf 'unknown\n'; return 0      # another uid's process; do not guess
    else
        printf 'unknown\n'; return 0      # no owning qemu found for this port
    fi
    local mine=0 queued=0 f line local_a st ino rest
    for f in /proc/net/tcp /proc/net/tcp6; do
        [[ -r "$f" ]] || continue
        while read -r _sl local_a _rem st rest; do
            [[ "$st" == "01" ]] || continue                 # 01 = ESTABLISHED
            [[ "${local_a##*:}" == "$hexport" ]] || continue
            # tcp6 rows print an IPv4-mapped address as a 32-hex-digit value, so
            # only compare when the widths match; a mismatch there is not evidence
            # of anything and the row is kept rather than silently dropped.
            if [[ -n "$hexaddr" && ${#local_a} -eq 13 && "${local_a%%:*}" != "$hexaddr" ]]; then
                continue
            fi
            # inode is the 10th field overall, i.e. the 6th of what is left
            # after sl/local/rem/st have been consumed above.
            set -- $rest; ino="${6:-}"
            [[ -n "$ino" ]] || continue
            if [[ -n "${own[$ino]:-}" ]]; then mine=$((mine + 1)); else queued=$((queued + 1)); fi
        done < <(tail -n +2 "$f")
    done
    if   [[ $mine -gt 0 && $queued -gt 0 ]]; then printf 'attached +%d waiting\n' "$queued"
    elif [[ $mine -gt 0 ]];               then printf 'attached\n'
    elif [[ $queued -gt 0 ]];             then printf 'waiting\n'
    else                                       printf -- '-\n'; fi
}

# Every live guest that exposes a gdbstub, one record per line, fields separated
# by \x1f (ASCII US):  port pid starttime frozen qbin statefile tree name boot kaslr ssh
#
# Not tab.  Tab is an IFS *whitespace* character, so `read` collapses a run of
# them into one delimiter -- and a record with an empty middle field (a guest
# with no ssh port, or one this tool did not start and so has no boot mode for)
# would silently shift every field after it.  The symptom is a table that reads
# plausibly with the wrong values in the wrong columns.  US is not whitespace,
# and cannot occur in a path or a tree name.
#
# Liveness comes from /proc and only from /proc.  A state file is then joined to
# a live row and used for the facts /proc cannot carry -- which tree this is, and
# what the firmware chain was told -- but it never creates a row of its own, so a
# file left behind by a dead guest cannot invent an instance.  A file whose
# recorded pid+starttime disagrees with the process actually on that port
# describes a previous run and is ignored rather than trusted.
# A value that would not survive the line-oriented state file.  The record format
# is one KEY=VALUE per line, so a newline inside a value ends the value early and
# the reader takes the first line as the whole thing -- a tree path silently
# truncated to its first line, which then matches nothing and leaves the operator
# unable to attach to their own guest by any name.  Refused where it is written,
# not discovered where it is read.
kbl_state_safe() {   # kbl_state_safe WHAT VALUE
    case "$2" in *$'\n'*|*$'\r'*)
        die "$1 contains a newline, which the run-state file cannot carry: ${2//$'\n'/\\n}" ;;
    esac
}

kbl_instances() {
    local sd; sd="$(kbl_statedir)" || return 1
    local d pid port start frozen qbin sf tree name boot kaslr ssh
    for d in /proc/[0-9]*; do
        pid="${d#/proc/}"
        port="$(kbl_qemu_gdb_port "$pid")" || continue
        start="$(kbl_proc_starttime "$pid")" || start=""
        qbin="$(basename -- "$(head -c 4096 "/proc/$pid/cmdline" 2>/dev/null | tr '\0' '\n' | head -1)")"
        kbl_qemu_has_flag "$pid" -S && frozen=1 || frozen=0
        sf="$sd/kbl-run-${port}.env"
        tree=""; name=""; boot=""; kaslr=""; ssh=""
        if [[ -r "$sf" ]]; then
            local spid sstart
            spid="$(sed -n 's/^KBL_QEMU_PID=//p' "$sf" | head -1)"
            sstart="$(sed -n 's/^KBL_QEMU_START=//p' "$sf" | head -1)"
            # A file that names a different process is about a run that has ended.
            if [[ -z "$spid" || ( "$spid" == "$pid" && ( -z "$sstart" || "$sstart" == "$start" ) ) ]]; then
                tree="$(sed -n 's/^KBL_TREE=//p'     "$sf" | head -1)"
                name="$(sed -n 's/^KBL_NAME=//p'     "$sf" | head -1)"
                boot="$(sed -n 's/^KBL_BOOT=//p'     "$sf" | head -1)"
                kaslr="$(sed -n 's/^KBL_KASLR=//p'   "$sf" | head -1)"
                ssh="$(sed -n 's/^KBL_SSH_PORT=//p'  "$sf" | head -1)"
            else
                sf=""
            fi
        else
            sf=""
        fi
        [[ -n "$name" ]] || name="$([[ -n "$tree" ]] && basename "$tree")"
        printf '%s\x1f%s\x1f%s\x1f%s\x1f%s\x1f%s\x1f%s\x1f%s\x1f%s\x1f%s\x1f%s\n' \
            "$port" "$pid" "$start" "$frozen" "$qbin" "$sf" "$tree" "$name" \
            "$boot" "$kaslr" "$ssh"
    done | sort -t"$(printf '\x1f')" -k1,1n
}

# Remove per-run scratch whose guest is gone.  Two conditions, both required: no
# live qemu carries that gdb port, AND nothing is listening on it.  The listen
# check alone (what this used to be) leaves a file forever once an unrelated
# process takes the port, and removes one that a guest is still using if the
# listen check races.
kbl_prune_state() {
    local sd; sd="$(kbl_statedir)" || return 0
    local -A live=()
    local rec p
    while IFS=$'\x1f' read -r p _; do [[ -n "$p" ]] && live["$p"]=1; done < <(kbl_instances)
    local f fp
    # All per-run scratch lives in the state directory, so the glob follows it
    # too: half of it under $KBL_STATE_DIR and half hardcoded in /dev/shm meant a
    # moved state directory left the 90-128 MB boot images unreclaimed for ever.
    for f in "$sd"/kbl-run-*.env "$sd"/kbl-boot-*.img "$sd"/kbl-vars-*.fd \
             "$sd"/kbl-qmp-*.sock; do
        [[ -e "$f" ]] || continue
        fp="${f##*-}"; fp="${fp%.env}"; fp="${fp%.img}"; fp="${fp%.fd}"; fp="${fp%.sock}"
        [[ "$fp" =~ ^[0-9]+$ ]] || continue
        # Two owners, and only these two.  A live qemu carrying that gdb port, or
        # a `run` that has claimed the port and not started qemu yet (its
        # launcher pid is in the state file, and O_EXCL made that claim atomic).
        #
        # NOT "something is listening".  That was the old rule and it is the
        # failure it was supposed to fix: one unrelated process taking the port
        # number pinned a dead run's 88-139 MB boot image in tmpfs for ever, and
        # left its state file for `attach` to read against a later guest.  Who is
        # listening now says nothing about whose these files are.
        [[ -n "${live[$fp]:-}" ]] && continue
        kbl_launcher_alive "$sd/kbl-run-${fp}.env" && continue
        rm -f "$f"
    done
}

# --- which guest ------------------------------------------------------------
# `attach` and `drgn` ask the same question in the same terms, so it is answered
# once, here.  Nothing below opens the gdb port: a bare TCP connect pauses a
# running guest and leaves it paused (qemu 11.1.1, via QMP query-status).

kbl_row_fields() {   # sets the _r_* variables from one kbl_instances line
    # \x1f, not tab: see kbl_instances -- tab collapses empty middle fields.
    IFS=$'\x1f' read -r _r_port _r_pid _r_start _r_frozen _r_qbin _r_sf \
                       _r_tree _r_name _r_boot _r_kaslr _r_ssh <<<"$1"
}

kbl_uptime_of() { ps -o etime= -p "$1" 2>/dev/null | tr -d ' '; }

# The guest's own runstate, read from QMP.  This is the question the -S flag
# cannot answer -- how the guest was STARTED is not how it is now -- and until
# there was a QMP monitor the only way to ask was to touch the gdbstub, which
# pauses a running guest.  Empty when the run recorded no QMP port.
kbl_qmp_runstate() {   # kbl_qmp_runstate STATEFILE
    local port; port="$(kbl_state_get "$1" KBL_QMP_PORT 2>/dev/null)"
    [[ -n "$port" ]] && command -v python3 >/dev/null 2>&1 || return 0
    python3 - "$port" <<'KBLQMP' 2>/dev/null
import json, socket, sys
try:
    s = socket.create_connection(("127.0.0.1", int(sys.argv[1])), timeout=2)
    s.recv(65536); s.sendall(b'{"execute":"qmp_capabilities"}\n'); s.recv(65536)
    s.sendall(b'{"execute":"query-status"}\n')
    print(json.loads(s.recv(65536).splitlines()[0])["return"]["status"])
except Exception:
    pass
KBLQMP
}

kbl_guest_table() {   # kbl_guest_table ROW...
    printf '  %-3s %-6s %-6s %-16s %-7s %-6s %-8s %-10s %-20s %-8s %s\n' \
        '#' port ssh tree boot kaslr started now dbg pid uptime
    local i=0 r
    for r in "$@"; do
        i=$((i + 1)); kbl_row_fields "$r"
        printf '  %-3s %-6s %-6s %-16s %-7s %-6s %-8s %-10s %-20s %-8s %s\n' \
            "$i" "$_r_port" "${_r_ssh:--}" "${_r_name:-?}" "${_r_boot:-?}" \
            "$(case "$_r_kaslr" in 1) echo on ;; 0) echo off ;; *) echo '?' ;; esac)" \
            "$([[ "$_r_frozen" == 1 ]] && echo frozen || echo running)" \
            "$(kbl_qmp_runstate "$_r_sf" || echo)" \
            "$(kbl_gdb_attached "$_r_port" "$_r_pid" "$(kbl_qemu_gdb_bind "$_r_pid")")" \
            "$_r_pid" "$(kbl_uptime_of "$_r_pid")"
    done
}

# The chosen kbl_instances row lands in KBL_GUEST_ROW; returns 2 when --list has
# printed its table instead.
#
# A global rather than stdout: under command substitution the function would run
# in a subshell with stdout on a pipe, so `[[ -t 1 ]]` goes false and the
# interactive chooser never runs, and `die` would exit the subshell only.
#
#   CMD        subcommand name, for messages
#   WANT_TREE  tree directory to filter by, or ""
#   PORT       gdb port to filter by, or ""
#   PORT_SET   1 if the caller was GIVEN that port rather than defaulting to it
#   FIRST      1 to take the lowest port instead of asking
#   LIST       1 to list and return 2 without choosing
kbl_pick_guest() {
    local cmd="$1" _want_tree="$2" PORT="$3" PORT_SET="$4" FIRST="$5" LIST="$6"
    KBL_GUEST_ROW=""
    local _r _treeport _n _try _pick _ports
    local -a _rows=() _byport=() _cand=()
    mapfile -t _rows < <(kbl_instances)

    # Two passes: "the port matched but the tree did not" and "nothing is on that
    # port" are different failures and get different messages.
    for _r in "${_rows[@]+"${_rows[@]}"}"; do
        kbl_row_fields "$_r"
        [[ $PORT_SET -eq 1 && "$_r_port" != "$PORT" ]] && continue
        _byport+=("$_r")
    done
    _treeport=""
    [[ -n "$_want_tree" ]] && _treeport="$(kbl_tree_get "$_want_tree" GDB_PORT)"
    for _r in "${_byport[@]+"${_byport[@]}"}"; do
        kbl_row_fields "$_r"
        if [[ -n "$_want_tree" ]]; then
            if [[ -n "$_r_tree" ]]; then
                [[ "$(readlink -f "$_r_tree")" == "$(readlink -f "$_want_tree")" ]] || continue
            else
                # A guest this tool did not start records no tree.  Only two
                # things name it: a tree AND --port given together, which is the
                # assertion that they go together, or the tree's own GDB_PORT.
                # Anything else is claimed by no name.
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
            kbl_guest_table "${_cand[@]}"
            # `started` is qemu's own -S flag; `now` is the live runstate, read
            # from QMP, which reaches it without touching the stub.  A guest that
            # is `paused` with no debugger on it is one a dead gdb left behind:
            # kbuildlab resume gets it back.
            echo "  started = how it was launched (-S); now = live runstate from QMP."
            echo "  paused with no dbg means a debugger died holding it: kbuildlab resume"
        fi
        return 2
    fi

    case ${#_cand[@]} in
      0)
        # A guest IS on the port that was NAMED; it is just not the tree that was
        # named with it.  Only reachable when --port was given: without it _byport
        # is every live guest on the host, and taking its first element blamed an
        # unrelated tree's guest and quoted a --port the caller never typed.
        if [[ $PORT_SET -eq 1 && ${#_byport[@]} -gt 0 ]]; then
            kbl_row_fields "${_byport[0]}"
            if [[ -n "$_r_tree" ]]; then
                die "$cmd: --port $_r_port is running $(basename "$_r_tree"), not $(basename "$_want_tree")
       (recorded in $_r_sf when that port was started)"
            fi
            die "$cmd: :$_r_port has a live $_r_qbin (pid $_r_pid) but nothing recorded which
       tree it is, so it cannot be matched against $(basename "$_want_tree").  Name it:
       kbuildlab $cmd <tree> --port $_r_port"
        fi
        if [[ $PORT_SET -eq 1 ]]; then
            if [[ -n "$(ss -ltnH "sport = :${PORT}" 2>/dev/null)" ]]; then
                die "$cmd: :$PORT is listening but no readable qemu-system process holds it
       (another user's, or not a guest).  Refusing to guess which tree that is."
            fi
            die "$cmd: nothing on :$PORT -- no qemu-system process carries -gdb for it,
       and nothing is listening there"
        fi
        die "$cmd: no live guest${_want_tree:+ of $(basename "$_want_tree")} -- no qemu-system
       process carries a gdb port recorded as this tree.  Start one:
       kbuildlab run ${_want_tree:+$(basename "$_want_tree")}" ;;
      1) KBL_GUEST_ROW="${_cand[0]}"; return 0 ;;
      *)
        if [[ $FIRST -eq 1 ]]; then
            KBL_GUEST_ROW="${_cand[0]}"; return 0          # kbl_instances sorts by port
        elif [[ -t 0 && -t 1 ]]; then
            echo "several live guests${_want_tree:+ of $(basename "$_want_tree")}:"
            kbl_guest_table "${_cand[@]}"
            if command -v fzf >/dev/null 2>&1; then
                _pick="$( { kbl_guest_table "${_cand[@]}"; } | fzf --header-lines=1 --no-multi \
                            --prompt="$cmd which? " --height=40% --reverse )" || _pick=""
                [[ -n "$_pick" ]] || die "$cmd: cancelled"
                _n="$(awk '{print $1}' <<<"$_pick")"
            else
                _n=""
                for _try in 1 2 3; do
                    read -r -p "  $cmd which? [1-${#_cand[@]}, q to cancel] " _n || _n=q
                    [[ "$_n" == q || -z "$_n" ]] && die "$cmd: cancelled"
                    [[ "$_n" =~ ^[0-9]+$ && "$_n" -ge 1 && "$_n" -le ${#_cand[@]} ]] && break
                    _n=""
                    echo "  not one of 1-${#_cand[@]}"
                done
            fi
            [[ -n "$_n" ]] || die "$cmd: no choice made"
            KBL_GUEST_ROW="${_cand[$((_n - 1))]}"; return 0
        else
            _ports=""
            for _r in "${_cand[@]}"; do kbl_row_fields "$_r"; _ports="$_ports --port $_r_port"; done
            die "$cmd: ${#_cand[@]} live instances${_want_tree:+ of $(basename "$_want_tree")} and stdin
       is not a terminal, so the chooser cannot run.  Name one:$_ports"
        fi ;;
    esac
}

# One KBL_ key out of a run-state file.  Read, never sourced.
kbl_state_get() {   # kbl_state_get FILE KEY
    [[ -r "$1" ]] || return 1
    sed -n "s/^$2=//p" "$1" | head -1
}
