# The lab/ record: init, and the tree and workspace records this repository
# keeps so a lost workspace or a new machine is rebuilt from them.  Sourced by
# bin/kbuildlab after lib/common.sh.

# ---------------------------------------------------------------- init
cmd_init() {
    local arch="" version="$KBL_DEFAULT_VERSION" upstream="" ws="" restore=0 srcdir=""
    local -a ids=()
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --workspace) ws="${2:?--workspace needs a directory}"; shift 2 ;;
            --restore)   restore=1; shift ;;
            --arch)      arch="${2:?--arch needs a value}"; shift 2 ;;
            --version)   version="${2:?--version needs a value}"; shift 2 ;;
            --upstream)  upstream="${2:?--upstream needs stable or mainline}"; shift 2 ;;
            -*) die "init: unknown option '$1'" ;;
            *)  if [[ $restore -eq 1 ]]; then ids+=("$1"); else srcdir="$1"; fi; shift ;;
        esac
    done

    if [[ -n "$ws" ]]; then
        mkdir -p "$ws" || die "cannot create $ws"
        ws="$(cd -P "$ws" && pwd)"
        mkdir -p "$KBL_CONFIG_HOME"
        printf '%s\n' "$ws" > "$KBL_CONFIG_HOME/workspace"
        say "workspace    $ws"
        [[ -z "$srcdir" && $restore -eq 0 ]] && return 0
    fi
    if [[ $restore -eq 1 ]]; then
        _restore_workspace "${ids[@]+"${ids[@]}"}"
        return 0
    fi

    [[ -n "$srcdir" ]] || die "init: give the directory the kernel source goes into:
       kbuildlab init DIR --arch ARCH [--version V], or pass --workspace DIR or --restore"
    [[ -n "$arch" ]] || die "init: --arch is required (arm64, riscv64, x86_64)"
    case "$arch" in arm64|riscv64|x86_64) ;; aarch64) arch=arm64 ;; *) die "init: unknown arch '$arch'" ;; esac
    [[ "$srcdir" == /* || "$srcdir" == "~" || "$srcdir" == "~/"* ]] || srcdir="$PWD/$srcdir"
    srcdir="$(kbl_norm_src "$srcdir")" || die "init: $srcdir is not a usable directory path"
    [[ -e "$srcdir" && ! -d "$srcdir" ]] && die "init: $srcdir exists and is not a directory"
    [[ -d "$srcdir" && ! -d "$srcdir/.git" && -n "$(ls -A "$srcdir" 2>/dev/null)" ]] \
        && die "init: $srcdir exists and is not empty; register an existing source with: kbuildlab add $srcdir"
    local had; had="$(_kbl_src_owner "$srcdir")" \
        && die "init: $srcdir is already the source of $(kbl_tree_label "$had")"
    local id root; id="$(kbl_new_id)" || exit 1; root="$KBL_TREES/$id"
    mkdir -p "$root" || die "init: cannot create $root"
    _kbl_new_conf "$root/tree.conf" "$arch" "$version" "$srcdir"
    [[ -n "$upstream" ]] && _kbl_conf_set "$root/tree.conf" UPSTREAM "$upstream"
    ok "registered $(kbl_tree_label "$root")"
    if [[ -d "$srcdir/.git" ]]; then say "source       $srcdir (a clone is already there; sync fetches into it)"
    else say "source       $srcdir (cloned by sync)"; fi
    say "next         kbuildlab sync $(kbl_tree_label "$root")"
}

_restore_tree() {   # _restore_tree ID
    local id="$1" rec="${KBL_LAB}/trees/$1" root ws_now
    kbl_valid_id "$id" || { warn "not restored: lab/trees/$id is not named by a tree ID"; return 1; }
    [[ -f "$rec/tree.conf" ]] || { warn "not restored: nothing is recorded as $id"; return 1; }
    [[ -n "$(kbl_tree_get "$rec" SRC)" ]] \
        || { warn "not restored: lab/trees/$id predates the registry (no SRC); register its source with kbuildlab add"; return 1; }
    ws_now="$(kbl_workspace)" || return 1
    root="$KBL_TREES/$id"
    kbl_is_tree "$root" && { say "kept         $(kbl_tree_label "$root") (registered)"; return 0; }
    mkdir -p "$root" || die "init: cannot create $root"
    _lab_copy "$rec/tree.conf" "$root/tree.conf" @KBL_WORKSPACE@ "$ws_now" \
        || die "init: cannot write $root/tree.conf"
    [[ -f "$rec/config-fragment" ]] \
        && _lab_copy "$rec/config-fragment" "$root/config-fragment" @KBL_WORKSPACE@ "$ws_now"
    ok "restored $(kbl_tree_label "$root") from $rec"
    say "source       $(kbl_tree_get "$root" SRC)"
    say "next         kbuildlab sync $(kbl_tree_label "$root")   (config then starts from the recorded .config)"
}

# A record spells the workspace's own path as @KBL_WORKSPACE@: it then reads
# the same from any machine, and names wherever the workspace is when it comes
# back.  Only the path itself is replaced, not a longer name it begins.
_lab_copy() {   # _lab_copy FROM TO OLD NEW
    [[ -n "$3" && -n "$4" ]] || die "record: nothing to spell the workspace with"
    python3 - "$@" <<'EOF' && chmod --reference="$1" "$2"
import re, sys
src, dst, old, new = sys.argv[1:]
with open(src, "rb") as f:
    data = f.read()
data = re.sub(re.escape(old.encode()) + rb"(?![\w.-])", lambda m: new.encode(), data)
with open(dst, "wb") as f:
    f.write(data)
EOF
}

# The workspace's own files -- the text files at its top and at the top of each
# directory a tree.conf points into, where the rootfs and firmware it boots are
# built -- and the source checkouts it builds them from, as path, URL and
# commit read off each checkout.  Dot files and build output are not recipes.
_record_workspace() {
    local ws dst d f rel v t
    ws="$(kbl_workspace_opt)" || return 0
    dst="${KBL_LAB}/workspace"
    local -A tops=(["."]=1)
    for t in $(kbl_trees); do
        d="$KBL_TREES/$t"
        while IFS= read -r k; do
            [[ "$k" == SRC ]] && continue
            v="$(kbl_tree_get "$d" "$k")"
            [[ "$v" == "$ws"/* ]] || continue
            rel="${v#"$ws"/}"
            tops["${rel%%/*}"]=1
        done < <(kbl_tree_keys "$d")
    done
    for d in "${!tops[@]}"; do
        for f in "$ws/$d"/*; do
            [[ -f "$f" && ! -L "$f" ]] && grep -Iq . "$f" 2>/dev/null || continue
            rel="$(realpath -m --relative-to="$ws" "$f")"
            if [[ $(stat -c %s "$f") -gt $KBL_GITHUB_FILE_LIMIT ]]; then
                say "not recorded $rel ($(du -h "$f" | cut -f1), over GitHub's file limit)"
                rm -f "$dst/$rel"
                continue
            fi
            mkdir -p "$dst/$(dirname "$rel")" && _lab_copy "$f" "$dst/$rel" "$ws" @KBL_WORKSPACE@
        done
    done
    # Checkouts at the top of the workspace or one level down that are not a
    # tree's kernel: buildroot, u-boot and the like, pinned at what is there.
    local list; list="$(mktemp)"
    local -A srcs=()
    for t in $(kbl_trees); do
        v="$(kbl_norm_src "$(kbl_tree_get "$KBL_TREES/$t" SRC)")" && srcs["$v"]=1
    done
    for d in "$ws"/*/ "$ws"/.*/ "$ws"/*/*/ "$ws"/.*/*/; do
        d="${d%/}"
        [[ "$(basename "$d")" == . || "$(basename "$d")" == .. || ! -d "$d/.git" ]] && continue
        [[ -n "${srcs["$(readlink -f "$d")"]:-}" ]] && continue
        # A bash without globskipdots expands .*/ to ../ too: never record outside.
        rel="$(realpath -m --relative-to="$ws" "$d")"
        [[ "$rel" == ..* ]] && continue
        printf '%s\t%s\t%s\n' "$rel" \
            "$(git -C "$d" remote get-url origin 2>/dev/null)" "$(git -C "$d" rev-parse HEAD 2>/dev/null)"
    done | sort -u > "$list"
    mkdir -p "$dst" && mv -f "$list" "$dst/sources"
}

# Inverse of the records: the workspace's own files where they are missing (an
# existing one is never overwritten), each recorded checkout cloned at its
# commit, and every recorded tree, or the ones whose IDs are given.
_restore_workspace() {   # _restore_workspace [ID...]
    local ws rel url commit rec
    ws="$(kbl_workspace)" || exit 1
    [[ -d "$KBL_LAB" ]] || die "init: nothing is recorded in $KBL_LAB"
    if [[ -d "$KBL_LAB/workspace" ]]; then
        while IFS= read -r rel; do
            [[ "$rel" == sources || -e "$ws/$rel" ]] && continue
            mkdir -p "$ws/$(dirname "$rel")" \
                && _lab_copy "$KBL_LAB/workspace/$rel" "$ws/$rel" @KBL_WORKSPACE@ "$ws" \
                && say "restored     $ws/$rel"
        done < <(cd "$KBL_LAB/workspace" && find . -type f -printf '%P\n')
    fi
    if [[ -f "$KBL_LAB/workspace/sources" ]]; then
        while IFS=$'\t' read -r rel url commit; do
            [[ -n "$rel" && -n "$url" && -n "$commit" ]] || continue
            [[ "$rel" == ..* || "$rel" == /* ]] && { warn "not restored: $rel lies outside $ws"; continue; }
            [[ -e "$ws/$rel" ]] && { say "kept         $ws/$rel"; continue; }
            say "cloning      $url into $ws/$rel at ${commit:0:12}"
            git clone -q "$url" "$ws/$rel" && git -C "$ws/$rel" checkout -q "$commit" \
                || warn "could not restore $ws/$rel from $url at $commit"
        done < "$KBL_LAB/workspace/sources"
    fi
    local -a want=("$@")
    if [[ ${#want[@]} -eq 0 ]]; then
        for rec in "$KBL_LAB"/trees/*/; do [[ -f "$rec/tree.conf" ]] && want+=("$(basename "$rec")"); done
    fi
    for rec in "${want[@]+"${want[@]}"}"; do
        ( _restore_tree "$rec" ) || warn "not restored: $rec (above)"
    done
}

# The tree's definition, kept where the workspace is not: in this repository.
# tree.conf, the tree's own config-fragment, and the .config as savedefconfig
# writes it -- only what differs from the arch defconfig, which is how the
# kernel itself stores a configuration and the form a config change is readable
# in.  `init --restore` and `config` put them back on any machine.
_record() {   # _record TREE
    local tree="$1" dst src ws
    dst="${KBL_LAB}/trees/$(kbl_tree_id "$tree")"
    src="$(kbl_tree_src "$tree")" || exit 1; kbl_tree_arch "$tree" >/dev/null || exit 1
    ws="$(kbl_workspace_opt)" || ws="/nonexistent-kbuildlab-workspace"
    mkdir -p "$dst" || { warn "record: cannot write $dst"; return 0; }
    _lab_copy "$tree/tree.conf" "$dst/tree.conf" "$ws" @KBL_WORKSPACE@
    if [[ -f "$tree/config-fragment" ]]; then
        _lab_copy "$tree/config-fragment" "$dst/config-fragment" "$ws" @KBL_WORKSPACE@
    else rm -f "$dst/config-fragment"; fi
    [[ -f "$src/.config" ]] || return 0
    if _kmake "$tree" savedefconfig >/dev/null 2>&1 && [[ -f "$src/defconfig" ]]; then
        mkdir -p "$(kbl_tree_data "$tree")" && cp -f "$src/defconfig" "$(kbl_tree_data "$tree")/defconfig.snapshot"
        _lab_copy "$src/defconfig" "$dst/defconfig" "$ws" @KBL_WORKSPACE@ && rm -f "$src/defconfig"
        say "recorded     $dst ($(grep -c . "$dst/defconfig") config lines)"
    else
        warn "record: savedefconfig failed; $dst keeps the config it had"
    fi
    _record_workspace
}
