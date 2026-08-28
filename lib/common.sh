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
kbl_container() {
    local c
    for c in ${KBL_CONTAINER:-} docker podman nerdctl; do
        [[ -n "$c" ]] && command -v "$c" >/dev/null 2>&1 && { printf '%s\n' "$c"; return 0; }
    done
    die "no container runtime found. Need one of: docker, podman, nerdctl
       (or set \$KBL_CONTAINER to the one you use)"
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
kbl_tree_get() {
    local tree="$1" key="$2" v
    v="$(sed -n "s/^[[:space:]]*${key}=//p" "$tree/tree.conf" | head -1)"
    v="${v%%#*}"; v="${v%"${v##*[![:space:]]}"}"; v="${v%\"}"; v="${v#\"}"
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

# --- scratch -----------------------------------------------------------------
kbl_rundir() {
    local ws h base
    ws="$(kbl_workspace 2>/dev/null || echo "$PWD")"
    h="$(printf '%s' "$ws" | cksum | cut -d' ' -f1)"
    base="${XDG_RUNTIME_DIR:-/tmp}/kbuildlab/$(id -u)/$h"
    mkdir -p "$base" 2>/dev/null || base="/tmp/kbuildlab-$(id -u)-$h"
    mkdir -p "$base" 2>/dev/null || die "cannot create a scratch directory"
    printf '%s\n' "$base"
}
