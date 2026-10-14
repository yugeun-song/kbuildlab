# The tree registry: register, list, inspect, edit, move and remove trees from
# any directory.  Sourced by bin/kbuildlab after lib/common.sh.
#
# Each tree is $KBL_TREES/<id>/tree.conf, its ID 16 random hex digits.  Nothing here caches what a tree
# is: `trees` and `inspect` read the source every time, and an edit made by
# hand to a tree.conf is what the next command sees.

_treeinfo() {
    KBL_TREES="$KBL_TREES" KBL_DATA_HOME="$KBL_DATA_HOME" KBL_STATE_DIR="${KBL_STATE_DIR:-/dev/shm}" \
        python3 "${KBL_REPO}/lib/treeinfo.py" "$@"
}

# Refuse to move or delete what a running guest is using.
_kbl_no_guests() {   # _kbl_no_guests TREE WHAT
    local r n=0 ports=""
    while IFS= read -r r; do
        [[ -n "$r" ]] || continue
        kbl_row_fields "$r"
        [[ -n "$_r_tree" && "$(readlink -f "$_r_tree")" == "$(readlink -f "$1")" ]] || continue
        n=$((n + 1)); ports="$ports :$_r_port"
    done < <(kbl_instances 2>/dev/null)
    [[ $n -eq 0 ]] || die "$2: $(kbl_tree_label "$1") has $n live guest(s) on$ports; stop them first"
}

_kbl_confirm() {   # _kbl_confirm YES PROMPT
    [[ "$1" == 1 ]] && return 0
    [[ -t 0 ]] || die "$2 -- not a terminal, so nothing was deleted; pass --yes to confirm"
    local a; read -r -p "  $2 [y/N] " a || a=""
    [[ "$a" == y || "$a" == Y || "$a" == yes ]]
}

# A registered tree given by its ID or a unique prefix of it.  Commands that
# move or delete take an ID and never a path, so a `.` typed in the wrong
# directory cannot select a tree.
_kbl_id_tree() {   # _kbl_id_tree CMD ID
    local -a hits=()
    mapfile -t hits < <(kbl_trees_by_id "$2")
    case ${#hits[@]} in
        1) printf '%s\n' "$KBL_TREES/${hits[0]}" ;;
        0) die "$1: '$2' is not the ID of a registered tree (it takes an ID, not a path; kbuildlab trees lists them)" ;;
        *) die "$1: the ID prefix '$2' matches several trees: $(_kbl_labels "${hits[@]}")" ;;
    esac
}

_kbl_src_owner() {   # _kbl_src_owner SRC
    local t s
    for t in $(kbl_trees); do
        s="$(kbl_norm_src "$(kbl_tree_get "$KBL_TREES/$t" SRC)")" || continue
        [[ "$s" == "$1" ]] && { printf '%s\n' "$KBL_TREES/$t"; return 0; }
    done
    return 1
}

# Write KEY=VALUE into a tree.conf, replacing the line kbl_tree_get reads (the
# first) and dropping later duplicates, or appending.  A value that would not
# survive the grammar unquoted is written quoted.
_kbl_conf_set() {   # _kbl_conf_set FILE KEY VALUE
    local f="$1" k="$2" v="$3"
    [[ "$k" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || die "set: '$k' is not a key name"
    case "$v" in *$'\n'*|*'"'*) die "set: $k's value may not contain a newline or a double quote" ;; esac
    [[ "$v" == *"#"* || "$v" =~ ^[[:space:]] || "$v" =~ [[:space:]]$ ]] && v="\"$v\""
    python3 - "$f" "$k" "$v" <<'EOF' || die "set: cannot write $1"
import os, re, sys
path, key, val = sys.argv[1:]
pat = re.compile(r"^[ \t\r\f\v]*" + re.escape(key) + r"[ \t\r\f\v]*=")
with open(path, encoding="utf-8") as f:
    lines = f.read().splitlines()
out, done = [], False
for line in lines:
    if pat.match(line):
        if not done:
            out.append(f"{key}={val}")
            done = True
        continue
    out.append(line)
if not done:
    out.append(f"{key}={val}")
tmp = path + ".kbl-tmp"
with open(tmp, "w", encoding="utf-8") as f:
    f.write("\n".join(out) + "\n")
os.replace(tmp, path)
EOF
}

_kbl_conf_unset() {   # _kbl_conf_unset FILE KEY
    python3 - "$1" "$2" <<'EOF' || die "unset: cannot write $1"
import os, re, sys
path, key = sys.argv[1:]
pat = re.compile(r"^[ \t\r\f\v]*" + re.escape(key) + r"[ \t\r\f\v]*=")
with open(path, encoding="utf-8") as f:
    lines = [l for l in f.read().splitlines() if not pat.match(l)]
tmp = path + ".kbl-tmp"
with open(tmp, "w", encoding="utf-8") as f:
    f.write("\n".join(lines) + "\n")
os.replace(tmp, path)
EOF
}

# The arch a kernel source was configured or built for, from its .config.
_kbl_src_arch() {   # _kbl_src_arch SRC
    local cfg="$1/.config"
    [[ -r "$cfg" ]] || return 1
    if grep -q '^CONFIG_ARM64=y' "$cfg"; then echo arm64
    elif grep -q '^CONFIG_X86_64=y' "$cfg"; then echo x86_64
    elif grep -q '^CONFIG_RISCV=y' "$cfg" && grep -q '^CONFIG_64BIT=y' "$cfg"; then echo riscv64
    else return 1; fi
}

# A new tree.conf from the templates: the header with its arch and version,
# then the arch's QEMU description, with SRC stated first.
_kbl_new_conf() {   # _kbl_new_conf FILE ARCH VERSION SRC
    local body tpl="${KBL_REPO}/templates/tree.conf.$2"
    [[ -r "$tpl" ]] || die "no QEMU template for $2"
    body="$(<"${KBL_REPO}/templates/tree.conf.in")"
    body="${body//@ARCH@/"$2"}"; body="${body//@VERSION@/"$3"}"; body="${body//@SRC@/"$4"}"
    { printf '%s\n' "$body"; cat "$tpl"; } > "$1"
}

cmd_trees() {
    [[ "${1:-}" == ls || "${1:-}" == list ]] && shift
    local a
    for a in "$@"; do
        case "$a" in
            -q|--json|--narrow) ;;
            *) die "trees: unknown option '$a' (trees [-q|--json|--narrow])" ;;
        esac
    done
    _treeinfo list "$@"
}

cmd_inspect() {
    local want="" ; local -a opts=()
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --json) opts+=(--json); shift ;;
            --format|-f) opts+=(--format "${2:?--format needs a key}"); shift 2 ;;
            -*) die "inspect: unknown option '$1'" ;;
            *) want="$1"; shift ;;
        esac
    done
    local tree; tree="$(kbl_tree "$want")" || exit 1
    _treeinfo inspect "$(basename "$tree")" "${opts[@]+"${opts[@]}"}"
}

cmd_add() {
    local src="" arch="" version="" upstream=""
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --arch) arch="${2:?--arch needs a value}"; shift 2 ;;
            --version) version="${2:?--version needs a value}"; shift 2 ;;
            --upstream) upstream="${2:?--upstream needs stable or mainline}"; shift 2 ;;
            -*) die "add: unknown option '$1'" ;;
            *) src="$1"; shift ;;
        esac
    done
    [[ -n "$src" ]] || die "add: name the kernel source directory: kbuildlab add DIR"
    [[ -d "$src" ]] || die "add: $src is not a directory"
    src="$(cd -P "$src" && pwd)"
    kbl_is_ksrc "$src" || die "add: $src is not a kernel source (no top-level Makefile with VERSION, arch/, Kbuild)"
    local had; had="$(_kbl_src_owner "$src")" && die "add: $src is already registered as $(kbl_tree_label "$had")"
    if [[ -z "$arch" ]]; then
        arch="$(_kbl_src_arch "$src")" || die "add: $src has no .config to read the arch from; pass --arch arm64|riscv64|x86_64"
        say "arch         $arch (from $src/.config)"
    fi
    case "$arch" in arm64|riscv64|x86_64) ;; aarch64) arch=arm64 ;; *) die "add: unknown arch '$arch'" ;; esac
    if [[ -z "$version" && -e "$src/.git" ]]; then
        version="$(git -C "$src" describe --exact-match --tags 2>/dev/null \
                   || git -C "$src" symbolic-ref --short -q HEAD 2>/dev/null)" || version=""
    fi
    local id tree; id="$(kbl_new_id)" || exit 1; tree="$KBL_TREES/$id"
    mkdir -p "$tree" || die "add: cannot create $tree"
    _kbl_new_conf "$tree/tree.conf" "$arch" "${version:-}" "$src"
    [[ -n "$upstream" ]] && _kbl_conf_set "$tree/tree.conf" UPSTREAM "$upstream"
    [[ -n "$version" ]] || warn "add: no tag or branch is checked out in $src, so VERSION is empty; \`sync\` needs it:
       kbuildlab set $(kbl_tree_label "$tree") VERSION=<tag or branch>"
    ok "registered $(kbl_tree_label "$tree") -> $src"
    say "config       $tree/tree.conf"
}

cmd_rm() {
    local want="" purge=0 yes=0
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --purge) purge=1; shift ;;
            --yes|-y) yes=1; shift ;;
            -*) die "rm: unknown option '$1'" ;;
            *) want="$1"; shift ;;
        esac
    done
    [[ -n "$want" ]] || die "rm: give the tree's ID: kbuildlab rm ID [--purge]"
    local tree; tree="$(_kbl_id_tree rm "$want")" || exit 1
    local id label; id="$(kbl_tree_id "$tree")"; label="$(kbl_tree_label "$tree")"
    _kbl_no_guests "$tree" rm
    local data src raw rc=0; data="$(kbl_tree_data "$tree")"; raw="$(kbl_tree_get "$tree" SRC)"
    src="$(kbl_norm_src "$raw")" || src=""
    if [[ $purge -eq 1 ]]; then
        local t s
        [[ -n "$src" && -d "$src" ]] && kbl_is_ksrc "$src" \
            || die "rm --purge: $label's SRC ($raw) is not a kernel source, so it is not deleted; drop --purge"
        for t in $(kbl_trees); do
            [[ "$t" == "$id" ]] && continue
            s="$(kbl_norm_src "$(kbl_tree_get "$KBL_TREES/$t" SRC)")" || continue
            [[ "$s" == "$src" ]] && die "rm --purge: $src is also the source of $(kbl_tree_label "$KBL_TREES/$t")"
        done
        _kbl_confirm "$yes" "delete $label's source $src ($(du -sh "$src" 2>/dev/null | cut -f1)) and its data?" \
            || die "rm: cancelled"
    fi
    rm -rf -- "$tree" || die "rm: cannot remove $tree"
    ok "unregistered $label"
    local cache="${XDG_CACHE_HOME:-$HOME/.cache}/kbuildlab"
    if [[ $purge -eq 1 ]]; then
        python3 "${KBL_REPO}/lib/index/clangd.py" drop "$id" >/dev/null 2>&1 || true
        [[ -d "$cache/view/$id" ]] && chmod -R u+rwX -- "$cache/view/$id" 2>/dev/null
        if rm -rf -- "$data" "$cache/clangd/$id" "$cache/view/$id"; then ok "deleted its data and indexes"
        else bad "could not delete all of $data and its indexes"; rc=1; fi
        if rm -rf -- "$src"; then ok "deleted $src"
        else bad "could not delete all of $src"; rc=1; fi
    else
        [[ -d "$data" ]] && say "kept         $data ($(du -sh "$data" 2>/dev/null | cut -f1)); \`kbuildlab prune\` removes it"
        say "kept         ${src:-$raw}"
    fi
    return $rc
}

cmd_mv() {
    local want="" dest="" only=0
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --no-move|--src-only) only=1; shift ;;
            -*) die "mv: unknown option '$1'" ;;
            *) if [[ -z "$want" ]]; then want="$1"; else dest="$1"; fi; shift ;;
        esac
    done
    [[ -n "$want" && -n "$dest" ]] || die "mv: kbuildlab mv ID NEW-SOURCE-DIR [--no-move]"
    local tree src; tree="$(_kbl_id_tree mv "$want")" || exit 1; src="$(kbl_tree_src "$tree")" || exit 1
    _kbl_no_guests "$tree" mv
    if [[ $only -eq 1 ]]; then
        dest="$(cd -P "$dest" 2>/dev/null && pwd)" || die "mv: $dest does not exist"
        kbl_is_ksrc "$dest" || die "mv: $dest is not a kernel source"
    else
        [[ -e "$dest" ]] && die "mv: $dest already exists (to repoint SRC at a source you moved yourself: --no-move)"
        [[ -d "$src" ]] || die "mv: $src does not exist; to repoint SRC at where it is now: --no-move"
        kbl_is_ksrc "$src" || die "mv: $src is not a kernel source, so it is not moved; to repoint SRC: --no-move"
        [[ "$dest" == /* ]] || dest="$PWD/$dest"
        mkdir -p "$(dirname "$dest")" || die "mv: cannot create $(dirname "$dest")"
        say "moving       $src -> $dest"
        mv -T -- "$src" "$dest" || die "mv: could not move $src"
        dest="$(cd -P "$dest" && pwd)"
    fi
    _kbl_conf_set "$tree/tree.conf" SRC "$dest"
    ok "$(kbl_tree_label "$tree") SRC=$dest"
    say "next         kbuildlab tags $(kbl_tree_label "$tree")   (the indexes name the old paths)"
}

cmd_get() {
    local want="${1:-}"; shift || true
    local tree; tree="$(kbl_tree "$want")" || exit 1
    if [[ $# -eq 0 ]]; then cat "$tree/tree.conf"; return 0; fi
    local k
    for k in "$@"; do
        [[ "$k" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || die "get: '$k' is not a key name"
        if [[ $# -eq 1 ]]; then kbl_tree_get "$tree" "$k"; else printf '%s=%s\n' "$k" "$(kbl_tree_get "$tree" "$k")"; fi
    done
}

cmd_set() {
    local want="${1:-}"; shift || true
    [[ $# -gt 0 ]] || die "set: kbuildlab set TREE KEY=VALUE..."
    local tree kv k v; tree="$(kbl_tree "$want")" || exit 1
    for kv in "$@"; do
        [[ "$kv" == *=* ]] || die "set: '$kv' is not KEY=VALUE"
        k="${kv%%=*}"; v="${kv#*=}"
        if [[ "$k" == SRC ]]; then
            v="$(kbl_norm_src "$v")" || die "set: SRC is an absolute path (or ~/...), not '${kv#*=}'"
        fi
        _kbl_conf_set "$tree/tree.conf" "$k" "$v"
        ok "$(kbl_tree_label "$tree") $k=$v"
    done
}

cmd_unset() {
    local want="${1:-}"; shift || true
    [[ $# -gt 0 ]] || die "unset: kbuildlab unset TREE KEY..."
    local tree k; tree="$(kbl_tree "$want")" || exit 1
    for k in "$@"; do
        [[ "$k" == SRC || "$k" == ARCH ]] && die "unset: a tree cannot be without $k"
        _kbl_conf_unset "$tree/tree.conf" "$k"
        ok "$(kbl_tree_label "$tree") $k unset"
    done
}

cmd_edit() {
    local tree; tree="$(kbl_tree "${1:-}")" || exit 1
    local ed="${VISUAL:-${EDITOR:-}}"
    [[ -n "$ed" ]] || die "edit: neither \$VISUAL nor \$EDITOR is set; the file is $tree/tree.conf"
    # shellcheck disable=SC2086
    exec $ed "$tree/tree.conf"
}

# A tree is gone when its source is, the directory that held the source is
# still there (a drive that is not mounted is not a deleted source), and it was
# built or run here before (a tree `init` registered is not cloned yet).  A data
# directory no tree owns is deleted, except one holding a persistent disk.
cmd_prune() {
    local yes=0 t s raw d n=0
    [[ "${1:-}" == --yes || "${1:-}" == -y ]] && yes=1
    local -a gone=() orphans=()
    for t in $(kbl_trees); do
        raw="$(kbl_tree_get "$KBL_TREES/$t" SRC)"
        local l; l="$(kbl_tree_label "$KBL_TREES/$t")"
        if ! s="$(kbl_norm_src "$raw")"; then
            bad "$l: SRC=$raw is not an absolute path; fix it: kbuildlab set $l SRC=/path/to/linux"; continue
        fi
        [[ -d "$s" ]] && continue
        if [[ ! -d "$(dirname "$s")" ]]; then
            say "kept         $l: $(dirname "$s") is missing too (a drive that is not mounted?)"; continue
        fi
        d="$(kbl_tree_data "$KBL_TREES/$t")"
        if [[ -z "$(find "$d" -mindepth 1 -print -quit 2>/dev/null)" ]]; then
            say "kept         $l: never built or run here; kbuildlab sync $l clones its source"; continue
        fi
        gone+=("$t")
    done
    if [[ -d "$KBL_DATA_HOME/trees" ]]; then
        for d in "$KBL_DATA_HOME/trees"/*/; do
            d="${d%/}"; [[ -d "$d" ]] || continue
            kbl_is_tree "$KBL_TREES/$(basename "$d")" && continue
            if [[ -n "$(find "$d/disk" -type f -print -quit 2>/dev/null)" ]]; then
                say "kept         $d: it holds a persistent disk ($(du -sh "$d/disk" 2>/dev/null | cut -f1)); remove it yourself if it is not wanted"
                continue
            fi
            orphans+=("$d")
        done
    fi
    for t in "${gone[@]+"${gone[@]}"}"; do
        local l; l="$(kbl_tree_label "$KBL_TREES/$t")"
        bad "$l: its source $(kbl_norm_src "$(kbl_tree_get "$KBL_TREES/$t" SRC)") is gone"
        if _kbl_confirm "$yes" "unregister $l?"; then rm -rf -- "${KBL_TREES:?}/$t"; ok "unregistered $l"; n=$((n + 1)); fi
    done
    for d in "${orphans[@]+"${orphans[@]}"}"; do
        bad "$d belongs to no registered tree ($(du -sh "$d" 2>/dev/null | cut -f1))"
        if _kbl_confirm "$yes" "delete $d?"; then rm -rf -- "$d"; ok "deleted $d"; n=$((n + 1)); fi
    done
    [[ ${#gone[@]} -eq 0 && ${#orphans[@]} -eq 0 ]] && ok "nothing to prune"
    return 0
}
