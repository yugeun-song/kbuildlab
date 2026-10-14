# The tree registry: register, list, inspect, edit, move and remove trees from
# any directory.  Sourced by bin/kbuildlab after lib/common.sh.
#
# Each tree is $KBL_TREES/<name>/tree.conf.  Nothing here caches what a tree
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
    [[ $n -eq 0 ]] || die "$2: $(basename "$1") has $n live guest(s) on$ports; stop them first"
}

_kbl_confirm() {   # _kbl_confirm YES PROMPT
    [[ "$1" == 1 ]] && return 0
    [[ -t 0 ]] || die "$2 -- not a terminal, so nothing was deleted; pass --yes to confirm"
    local a; read -r -p "  $2 [y/N] " a || a=""
    [[ "$a" == y || "$a" == Y || "$a" == yes ]]
}

# A registered tree named exactly.  Commands that move or delete take a name and
# never a path, so a `.` typed in the wrong directory cannot select a tree.
_kbl_named_tree() {   # _kbl_named_tree CMD NAME
    [[ -n "$2" && "$2" != */* && "$2" != . && "$2" != .. ]] && kbl_is_tree "$KBL_TREES/$2" \
        || die "$1: '$2' is not a registered tree name (it takes a name, not a path; kbuildlab trees lists them)"
    printf '%s\n' "$KBL_TREES/$2"
}

# A name a new tree can take: usable, not registered, and with no data left
# under it by a tree that was removed without --purge.
_kbl_new_name() {   # _kbl_new_name CMD NAME
    kbl_valid_name "$2" || die "$1: '$2' is not a usable tree name (letters, digits, . _ + -)"
    kbl_is_tree "$KBL_TREES/$2" && die "$1: $2 is already registered ($KBL_TREES/$2)"
    [[ -e "$KBL_TREES/$2" ]] \
        && die "$1: $KBL_TREES/$2 exists but holds no tree.conf; remove it or pick another name"
    [[ -e "$KBL_DATA_HOME/trees/$2" ]] && die "$1: $KBL_DATA_HOME/trees/$2 is left from an earlier tree named $2
       (removed without --purge).  Delete it with kbuildlab prune, or remove the
       directory yourself, or pick another name"
    return 0
}

# A tree.conf from before the registry, rewritten for it: SRC stated first, a
# path into the old tree directory that `migrate` moves into the data directory
# kept relative (to the data directory), every other relative path made absolute
# against the old directory.  IN and OUT may be the same file.
_kbl_legacy_conf() {   # _kbl_legacy_conf IN OUT OLD-TREE-DIR SRC
    python3 - "$@" <<'EOF' || die "cannot write $2"
import os, re, sys
srcf, dstf, old, src = sys.argv[1:]
old = os.path.normpath(old)
path_keys = {"INITRD", "UBOOT", "BOOTDISK", "PERSIST_DISK", "OVMF_CODE", "OVMF_VARS"}
moved = {"boot", "disk", "modules.cpio.gz", "defconfig.snapshot"}
line_re = re.compile(r"^([ \t]*)([A-Za-z_][A-Za-z0-9_]*)([ \t]*=[ \t]*)(.*)$")
out, seen = [], set()
for line in open(srcf, encoding="utf-8").read().splitlines():
    m = line_re.match(line)
    if m and m.group(2) in ("SRC_DIR", "SRC"):
        continue
    if m and m.group(2) in path_keys and m.group(2) not in seen:
        seen.add(m.group(2))
        raw = m.group(4)
        quoted = raw.startswith('"')
        val = raw[1:].split('"', 1)[0] if quoted else raw.split("#", 1)[0].rstrip()
        rest = raw[len(val) + 2:] if quoted else raw[len(raw.split("#", 1)[0]):]
        if rest.strip() and not quoted:
            rest = "   " + rest.strip()
        if val and not val.startswith(("/", "~")):
            full = os.path.normpath(os.path.join(old, val))
            rel = os.path.relpath(full, old)
            inside = full.startswith(old + "/")
            val = rel if inside and rel.split("/", 1)[0] in moved else full
            line = m.group(1) + m.group(2) + m.group(3) + (f'"{val}"' if quoted else val) + \
                (rest if rest.strip() else "")
    out.append(line)
head = ("# The kernel source this tree builds and boots.\n"
        f"SRC={src}\n")
tmp = dstf + ".kbl-tmp"
with open(tmp, "w", encoding="utf-8") as f:
    f.write(head + "\n".join(out) + "\n")
os.replace(tmp, dstf)
EOF
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

# A new tree.conf from the templates: the header with its name and version, then
# the arch's QEMU description, with SRC stated first.
_kbl_new_conf() {   # _kbl_new_conf FILE NAME ARCH VERSION SRC
    local body tpl="${KBL_REPO}/templates/tree.conf.$3"
    [[ -r "$tpl" ]] || die "no QEMU template for $3"
    body="$(<"${KBL_REPO}/templates/tree.conf.in")"
    body="${body//@ARCH@/"$3"}"; body="${body//@VERSION@/"$4"}"; body="${body//@NAME@/"$2"}"
    body="${body//@SRC@/"$5"}"
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
    local name="" ; local -a opts=()
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --json) opts+=(--json); shift ;;
            --format|-f) opts+=(--format "${2:?--format needs a key}"); shift 2 ;;
            -*) die "inspect: unknown option '$1'" ;;
            *) name="$1"; shift ;;
        esac
    done
    local tree; tree="$(kbl_tree "$name")" || exit 1
    _treeinfo inspect "$(basename "$tree")" "${opts[@]+"${opts[@]}"}"
}

cmd_add() {
    local src="" name="" arch="" version="" upstream=""
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --name) name="${2:?--name needs a value}"; shift 2 ;;
            --arch) arch="${2:?--arch needs a value}"; shift 2 ;;
            --version) version="${2:?--version needs a value}"; shift 2 ;;
            --upstream) upstream="${2:?--upstream needs stable or mainline}"; shift 2 ;;
            -*) die "add: unknown option '$1'" ;;
            *) src="$1"; shift ;;
        esac
    done
    [[ -n "$src" ]] || die "add: name the kernel source directory: kbuildlab add DIR [--name NAME]"
    [[ -d "$src" ]] || die "add: $src is not a directory"
    src="$(cd -P "$src" && pwd)"
    kbl_is_ksrc "$src" || die "add: $src is not a kernel source (no top-level Makefile with VERSION, arch/, Kbuild)"
    if [[ -z "$name" ]]; then
        name="$(basename "$src")"
        case "$name" in kernel|linux|src|source) name="$(basename "$(dirname "$src")")" ;; esac
    fi
    _kbl_new_name add "$name"
    local t s
    for t in $(kbl_trees); do
        s="$(kbl_norm_src "$(kbl_tree_get "$KBL_TREES/$t" SRC)")" || continue
        [[ "$s" == "$src" ]] && die "add: $src is already registered as $t"
    done
    if [[ -z "$arch" ]]; then
        arch="$(_kbl_src_arch "$src")" || die "add: $src has no .config to read the arch from; pass --arch arm64|riscv64|x86_64"
        say "arch         $arch (from $src/.config)"
    fi
    case "$arch" in arm64|riscv64|x86_64) ;; aarch64) arch=arm64 ;; *) die "add: unknown arch '$arch'" ;; esac
    if [[ -z "$version" && -e "$src/.git" ]]; then
        version="$(git -C "$src" describe --exact-match --tags 2>/dev/null \
                   || git -C "$src" symbolic-ref --short -q HEAD 2>/dev/null)" || version=""
    fi
    mkdir -p "$KBL_TREES/$name" || die "add: cannot create $KBL_TREES/$name"
    _kbl_new_conf "$KBL_TREES/$name/tree.conf" "$name" "$arch" "${version:-}" "$src"
    [[ -n "$upstream" ]] && _kbl_conf_set "$KBL_TREES/$name/tree.conf" UPSTREAM "$upstream"
    [[ -n "$version" ]] || warn "add: no tag or branch is checked out in $src, so VERSION is empty; \`sync\` needs it:
       kbuildlab set $name VERSION=<tag or branch>"
    ok "registered $name -> $src"
    say "config       $KBL_TREES/$name/tree.conf"
}

cmd_rm() {
    local name="" purge=0 yes=0
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --purge) purge=1; shift ;;
            --yes|-y) yes=1; shift ;;
            -*) die "rm: unknown option '$1'" ;;
            *) name="$1"; shift ;;
        esac
    done
    [[ -n "$name" ]] || die "rm: name the tree: kbuildlab rm NAME [--purge]"
    local tree; tree="$(_kbl_named_tree rm "$name")" || exit 1
    _kbl_no_guests "$tree" rm
    local data src raw rc=0; data="$(kbl_tree_data "$tree")"; raw="$(kbl_tree_get "$tree" SRC)"
    src="$(kbl_norm_src "$raw")" || src=""
    if [[ $purge -eq 1 ]]; then
        local t s
        [[ -n "$src" && -d "$src" ]] && kbl_is_ksrc "$src" \
            || die "rm --purge: $name's SRC ($raw) is not a kernel source, so it is not deleted; drop --purge"
        for t in $(kbl_trees); do
            [[ "$t" == "$name" ]] && continue
            s="$(kbl_norm_src "$(kbl_tree_get "$KBL_TREES/$t" SRC)")" || continue
            [[ "$s" == "$src" ]] && die "rm --purge: $src is also the source of $t"
        done
        _kbl_confirm "$yes" "delete $name's source $src ($(du -sh "$src" 2>/dev/null | cut -f1)) and its data?" \
            || die "rm: cancelled"
    fi
    rm -rf -- "$tree" || die "rm: cannot remove $tree"
    ok "unregistered $name"
    local cache="${XDG_CACHE_HOME:-$HOME/.cache}/kbuildlab"
    if [[ $purge -eq 1 ]]; then
        python3 "${KBL_REPO}/lib/clangd.py" drop "$name" >/dev/null 2>&1 || true
        if rm -rf -- "$data" "$cache/clangd/$name" "$cache/view/$name"; then ok "deleted its data and indexes"
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
    local name="" dest="" only=0
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --no-move|--src-only) only=1; shift ;;
            -*) die "mv: unknown option '$1'" ;;
            *) if [[ -z "$name" ]]; then name="$1"; else dest="$1"; fi; shift ;;
        esac
    done
    [[ -n "$name" && -n "$dest" ]] || die "mv: kbuildlab mv NAME NEW-SOURCE-DIR [--no-move]"
    local tree src; tree="$(_kbl_named_tree mv "$name")" || exit 1; src="$(kbl_tree_src "$tree")" || exit 1
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
    ok "$(basename "$tree") SRC=$dest"
    say "next         kbuildlab tags $(basename "$tree")   (the indexes name the old paths)"
}

cmd_rename() {
    local old="${1:-}" new="${2:-}"
    [[ -n "$old" && -n "$new" ]] || die "rename: kbuildlab rename OLD NEW"
    local tree; tree="$(_kbl_named_tree rename "$old")" || exit 1
    _kbl_new_name rename "$new"
    [[ -e "$KBL_LAB/trees/$new" ]] && die "rename: $KBL_LAB/trees/$new records another tree named $new;
       remove that record or pick another name"
    _kbl_no_guests "$tree" rename
    local cache="${XDG_CACHE_HOME:-$HOME/.cache}/kbuildlab" d
    # The caches under the new name belong to a tree removed without --purge, and
    # a cache is only ever rebuilt; they go before anything moves.
    rm -rf -- "$cache/clangd/$new" "$cache/view/$new" || die "rename: cannot clear $cache's entries for $new"
    mv -T -- "$tree" "$KBL_TREES/$new" || die "rename: cannot rename $tree"
    for d in "$KBL_DATA_HOME/trees" "$cache/clangd" "$cache/view" "$KBL_LAB/trees"; do
        [[ -e "$d/$old" ]] || continue
        mv -T -- "$d/$old" "$d/$new" || warn "rename: could not move $d/$old to $d/$new"
    done
    python3 "${KBL_REPO}/lib/clangd.py" drop "$old" >/dev/null 2>&1 || true
    ok "renamed $old -> $new"
    say "next         kbuildlab tags $new   (clangd's config names trees by name)"
}

cmd_get() {
    local name="${1:-}"; shift || true
    local tree; tree="$(kbl_tree "$name")" || exit 1
    if [[ $# -eq 0 ]]; then cat "$tree/tree.conf"; return 0; fi
    local k
    for k in "$@"; do
        [[ "$k" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || die "get: '$k' is not a key name"
        if [[ $# -eq 1 ]]; then kbl_tree_get "$tree" "$k"; else printf '%s=%s\n' "$k" "$(kbl_tree_get "$tree" "$k")"; fi
    done
}

cmd_set() {
    local name="${1:-}"; shift || true
    [[ $# -gt 0 ]] || die "set: kbuildlab set NAME KEY=VALUE..."
    local tree kv k v; tree="$(kbl_tree "$name")" || exit 1
    for kv in "$@"; do
        [[ "$kv" == *=* ]] || die "set: '$kv' is not KEY=VALUE"
        k="${kv%%=*}"; v="${kv#*=}"
        if [[ "$k" == SRC ]]; then
            v="$(kbl_norm_src "$v")" || die "set: SRC is an absolute path (or ~/...), not '${kv#*=}'"
        fi
        _kbl_conf_set "$tree/tree.conf" "$k" "$v"
        ok "$(basename "$tree") $k=$v"
    done
}

cmd_unset() {
    local name="${1:-}"; shift || true
    [[ $# -gt 0 ]] || die "unset: kbuildlab unset NAME KEY..."
    local tree k; tree="$(kbl_tree "$name")" || exit 1
    for k in "$@"; do
        [[ "$k" == SRC || "$k" == ARCH ]] && die "unset: a tree cannot be without $k"
        _kbl_conf_unset "$tree/tree.conf" "$k"
        ok "$(basename "$tree") $k unset"
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
        if ! s="$(kbl_norm_src "$raw")"; then
            bad "$t: SRC=$raw is not an absolute path; fix it: kbuildlab set $t SRC=/path/to/linux"; continue
        fi
        [[ -d "$s" ]] && continue
        if [[ ! -d "$(dirname "$s")" ]]; then
            say "kept         $t: $(dirname "$s") is missing too (a drive that is not mounted?)"; continue
        fi
        d="$(kbl_tree_data "$KBL_TREES/$t")"
        if [[ -z "$(find "$d" -mindepth 1 -print -quit 2>/dev/null)" ]]; then
            say "kept         $t: never built or run here; kbuildlab sync $t clones its source"; continue
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
        bad "$t: its source $(kbl_norm_src "$(kbl_tree_get "$KBL_TREES/$t" SRC)") is gone"
        if _kbl_confirm "$yes" "unregister $t?"; then rm -rf -- "${KBL_TREES:?}/$t"; ok "unregistered $t"; n=$((n + 1)); fi
    done
    for d in "${orphans[@]+"${orphans[@]}"}"; do
        bad "$d belongs to no registered tree ($(du -sh "$d" 2>/dev/null | cut -f1))"
        if _kbl_confirm "$yes" "delete $d?"; then rm -rf -- "$d"; ok "deleted $d"; n=$((n + 1)); fi
    done
    [[ ${#gone[@]} -eq 0 && ${#orphans[@]} -eq 0 ]] && ok "nothing to prune"
    return 0
}

# Trees from before the registry kept tree.conf, qemu.conf and their run files
# beside the source, in <workspace>/<name>/.  This moves the description into the
# registry (rewritten by _kbl_legacy_conf) and the run files into the data
# directory, keeps a copy of what it replaced, and leaves the old directory
# holding only the kernel source.
cmd_migrate() {
    local dry=0 old name src stamp backup moved=0
    [[ "${1:-}" == --dry-run || "${1:-}" == -n ]] && dry=1
    local -a legacy=(); mapfile -t legacy < <(kbl_legacy_trees)
    [[ ${#legacy[@]} -gt 0 ]] || { ok "nothing to migrate"; return 0; }
    stamp="$(date +%Y%m%d-%H%M%S)"; backup="$KBL_DATA_HOME/migrate-$stamp"
    for old in "${legacy[@]}"; do
        name="$(basename "$old")"
        kbl_valid_name "$name" || { bad "$name: not a usable tree name; skipped"; continue; }
        local sd; sd="$(kbl_tree_get "$old" SRC_DIR)"
        if [[ -n "$sd" ]]; then src="$old/$sd"
        else
            src=""; local d k=0
            for d in "$old"/*/; do kbl_is_ksrc "${d%/}" && { src="${d%/}"; k=$((k + 1)); }; done
            [[ $k -le 1 ]] || { bad "$name: more than one kernel source in $old; skipped"; continue; }
            [[ -n "$src" ]] || src="$old/kernel"
        fi
        src="$(readlink -f "$src")"
        if [[ $dry -eq 1 ]]; then
            say "would move   $name: SRC=$src"; continue
        fi
        local r busy=0
        while IFS= read -r r; do
            [[ -n "$r" ]] || continue; kbl_row_fields "$r"
            [[ -n "$_r_tree" && "$(readlink -f "$_r_tree")" == "$(readlink -f "$old")" ]] && busy=1
        done < <(kbl_instances 2>/dev/null)
        [[ $busy -eq 0 ]] || { bad "$name: a guest started from $old is running; stop it and migrate again"; continue; }
        local cfgdir="$KBL_TREES/$name" data="$KBL_DATA_HOME/trees/$name"
        mkdir -p "$cfgdir" "$data" "$backup/$name" || die "migrate: cannot create $cfgdir"
        cp -a "$old/tree.conf" "$backup/$name/" || die "migrate: cannot back up $old/tree.conf"
        [[ -f "$old/config-fragment" ]] && cp -a "$old/config-fragment" "$backup/$name/"
        _kbl_legacy_conf "$old/tree.conf" "$cfgdir/tree.conf" "$old" "$src"
        [[ -f "$old/config-fragment" ]] && cp -a "$old/config-fragment" "$cfgdir/config-fragment"
        local f
        for f in boot disk modules.cpio.gz defconfig.snapshot; do
            if [[ -e "$old/$f" ]]; then
                if [[ -e "$data/$f" ]]; then bad "$name: $data/$f already exists; $old/$f left in place"
                else mv -- "$old/$f" "$data/$f" || die "migrate: cannot move $old/$f"; fi
            fi
        done
        rm -f -- "$old/tree.conf" "$old/config-fragment"
        [[ -L "$old/qemu.conf" ]] && rm -f -- "$old/qemu.conf"
        ok "$name -> $cfgdir (SRC=$src)"
        moved=$((moved + 1))
    done
    [[ $dry -eq 1 ]] && return 0
    [[ $moved -gt 0 ]] && say "backup       $backup (the tree.conf files as they were)"
}
