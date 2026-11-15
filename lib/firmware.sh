_FW_ACTIONS="show update build own global check"

_fw_describe() { git -C "$1" describe --tags --always --dirty 2>/dev/null || echo "-"; }
_fw_built() { if [[ -f "$1" ]]; then date -r "$1" '+%Y-%m-%d %H:%M'; else echo "-"; fi; }
_fw_uses() { [[ "$(kbl_tree_get "$1" BOOT)" == uboot ]]; }

_fw_fragments() { # sets _fw_frags
    local f d
    _fw_frags=()
    d="$(dirname "$_fw_conf")"
    for f in $(kbl_conf_get "$_fw_conf" FRAGMENTS); do
        case "$f" in "~"/*) f="$HOME/${f#\~/}" ;; /*) ;; *) f="$d/$f" ;; esac
        [[ -f "$f" ]] || die "firmware: fragment not found: $f (FRAGMENTS in $_fw_conf)"
        _fw_frags+=("$f")
    done
}

_fw_inputs() {
    local f c
    printf 'head %s\n' "$(git -C "$_fw_src" rev-parse --verify --quiet HEAD 2>/dev/null)"
    printf 'edits %s\n' "$(git -C "$_fw_src" diff HEAD 2>/dev/null | sha1sum | cut -c1-40)"
    printf 'conf %s\n' "$(sha1sum <"$_fw_conf" | cut -c1-40)"
    for f in "${_fw_frags[@]+"${_fw_frags[@]}"}"; do
        printf 'fragment %s %s\n' "$f" "$(sha1sum <"$f" | cut -c1-40)"
    done
    c="$(kbl_container)" || return 1
    printf 'image %s\n' "$("$c" image inspect --format '{{.Id}}' "$KBL_IMAGE" 2>/dev/null)"
    printf 'arch %s\n' "$_fw_arch"
}

_fw_current() {
    [[ -e "$_fw_src/.git" && -f "$_fw_image" && -s "$_fw_stamp" && "$(_fw_inputs)" == "$(cat "$_fw_stamp")" ]]
}

_fw_container() { # sets _KBL_RUN
    local f d
    local -a opts=(-v "${_fw_src}:${_fw_src}:ro")
    local -A seen=()
    [[ -d "$_fw_src" && "$(readlink -f -- "$_fw_src")" != / ]] || die "firmware: $_fw_src cannot be mounted into the build container"
    for f in "${_fw_frags[@]+"${_fw_frags[@]}"}"; do
        d="$(dirname "$f")"
        [[ "$d" == "$KBL_REPO" || "$d" == "$KBL_REPO"/* || -n "${seen[$d]+x}" ]] && continue
        seen["$d"]=1
        opts+=(-v "${d}:${d}:ro")
    done
    for d in "$_fw_src" "$_fw_build" "${!seen[@]}"; do
        [[ "$d" != *:* ]] || die "firmware: $d has a colon in its name, which a container mount cannot spell"
    done
    local _KBL_ACTIVE_TREE="" _KBL_ACTIVE_IMAGE="$KBL_IMAGE"
    _container_run "$_fw_build" "${opts[@]}"
}

_fw_make() { # _fw_make MAKE-ARG...
    local -a margs=()
    read -r -a margs <<<"$(kbl_conf_get "$_fw_conf" MAKE_ARGS)"
    "${_KBL_RUN[@]}" make -C "$_fw_src" "O=$_fw_build" "CROSS_COMPILE=$(_cross "$_fw_arch")" \
        "${margs[@]+"${margs[@]}"}" "$@"
}

_fw_fail() { # _fw_fail WHAT
    tail -25 "$_fw_log" >&2
    die "firmware: $1 (above; the whole log is $_fw_log)"
}

_fw_build_now() { # _fw_build_now FRESH
    local fresh="$1" dc inputs before after target
    _fw_fragments
    [[ -f "$_fw_src/Makefile" ]] || die "firmware: no source in $_fw_src
       kbuildlab firmware update clones a global firmware's; a tree's own states where its is (SRC)"
    target="${_fw_image#"$_fw_build"/}"
    mkdir -p "$_fw_build" "$(dirname "$_fw_log")" || die "firmware: cannot create $_fw_build"
    : >"$_fw_log"
    rm -f "$_fw_stamp"
    inputs="$(_fw_inputs)" || exit 1
    before="$(sha256sum "$_fw_image" 2>/dev/null | cut -c1-64)"
    _fw_container || exit 1
    if [[ $fresh -eq 1 && -f "$_fw_build/.config" ]]; then
        say "wiping       $_fw_build (its .config is kept)"
        cp -f "$_fw_build/.config" "$_fw_log.config" || die "firmware: cannot save $_fw_build/.config"
        _fw_make mrproper >>"$_fw_log" 2>&1 || _fw_fail "make mrproper failed"
        mv -f "$_fw_log.config" "$_fw_build/.config" || die "firmware: cannot put $_fw_build/.config back"
    fi
    if [[ ! -f "$_fw_build/.config" ]]; then
        dc="$(kbl_conf_get "$_fw_conf" DEFCONFIG)"
        [[ -n "$dc" ]] || die "firmware: $_fw_build has no .config and $_fw_conf states no DEFCONFIG to start from"
        say "defconfig    $dc"
        _fw_make "$dc" >>"$_fw_log" 2>&1 || _fw_fail "make $dc failed"
    else
        say "keeping      $_fw_build/.config"
    fi
    if [[ ${#_fw_frags[@]} -gt 0 ]]; then
        [[ -f "$_fw_src/scripts/kconfig/merge_config.sh" ]] \
            || die "firmware: $_fw_src has no scripts/kconfig/merge_config.sh to merge FRAGMENTS with"
        say "merging      ${#_fw_frags[@]} fragment(s)"
        "${_KBL_RUN[@]}" sh "$_fw_src/scripts/kconfig/merge_config.sh" -m .config "${_fw_frags[@]}" >>"$_fw_log" 2>&1 \
            || _fw_fail "merge_config failed"
    fi
    _fw_make olddefconfig >>"$_fw_log" 2>&1 || _fw_fail "make olddefconfig failed"
    if [[ ${#_fw_frags[@]} -gt 0 ]]; then
        "${KBL_REPO}/presets/verify.sh" "$_fw_build/.config" "${_fw_frags[@]}" \
            || die "firmware: $_fw_build/.config does not hold what the fragments ask for (above)"
    fi
    say "building     $target, $(_kbl_jobs) job(s)"
    _fw_make -j"$(_kbl_jobs)" "$target" >>"$_fw_log" 2>&1 || _fw_fail "the build failed"
    [[ -f "$_fw_image" ]] || die "firmware: the build made no $_fw_image"
    [[ -z "$_fw_symbols" || -f "$_fw_symbols" ]] || die "firmware: the build made no $_fw_symbols (SYMBOLS in $_fw_conf)"
    if [[ "$(_fw_inputs)" == "$inputs" ]]; then
        printf '%s\n' "$inputs" >"$_fw_stamp"
    else warn "firmware: $_fw_src or its settings changed during the build; it is built again next time"; fi
    after="$(sha256sum "$_fw_image" | cut -c1-64)"
    if [[ "$before" == "$after" ]]; then
        ok "unchanged    $_fw_image at $(_fw_describe "$_fw_src")"
    else ok "built        $_fw_image at $(_fw_describe "$_fw_src")"; fi
}

_fw_resolve() { # _fw_resolve REMOTE VERSION
    local out
    if [[ "$2" == *[\*\?\[]* ]]; then
        out="$(git ls-remote --tags --refs "$1" "refs/tags/$2")" || die "firmware: cannot read the tags of $1"
        out="$(sed 's|.*refs/tags/||' <<<"$out" | sort -V | tail -1)"
        [[ -n "$out" ]] || die "firmware: no tag of $1 matches $2"
        printf 'tag %s\n' "$out"
        return 0
    fi
    out="$(git ls-remote --refs "$1" "refs/tags/$2" "refs/heads/$2")" || die "firmware: cannot read the refs of $1"
    case "$out" in
        *"refs/tags/$2"*) printf 'tag %s\n' "$2" ;;
        *"refs/heads/$2"*) printf 'branch %s\n' "$2" ;;
        *) die "firmware: $1 has neither a tag nor a branch named $2" ;;
    esac
}

_fw_sync() {
    local url ver origin kind want cur
    local -a depth=()
    url="$(kbl_conf_get "$_fw_conf" URL)"
    ver="$(kbl_conf_get "$_fw_conf" VERSION)"
    command -v git >/dev/null 2>&1 || die "git is not installed$(kbl_pkg_hint git)"
    if [[ ! -e "$_fw_src/.git" && -f "$_fw_src/Makefile" && -z "$ver" ]]; then
        say "pinned       $_fw_src is no git checkout; it is built as it is"
        return 0
    fi
    if [[ ! -e "$_fw_src/.git" ]]; then
        [[ -n "$url" && -n "$ver" ]] || die "firmware: $_fw_src is no git checkout, and $_fw_conf
       does not state both URL and VERSION to clone one from"
        [[ ! -e "$_fw_src" || -z "$(ls -A "$_fw_src" 2>/dev/null)" ]] \
            || die "firmware: $_fw_src exists and is not a git checkout"
        read -r kind want < <(_fw_resolve "$url" "$ver") || exit 1
        [[ -n "$want" ]] || exit 1
        say "cloning      $want from $url"
        mkdir -p "$(dirname "$_fw_src")" || die "firmware: cannot create $(dirname "$_fw_src")"
        GIT_TERMINAL_PROMPT=0 git -c advice.detachedHead=false clone -q --depth 1 --branch "$want" "$url" "$_fw_src" 2>&1 \
            || die "firmware: cloning $url failed"
        say "source       $_fw_src at $(_fw_describe "$_fw_src")"
        return 0
    fi
    if [[ -z "$ver" ]]; then
        say "pinned       $_fw_src stays at $(_fw_describe "$_fw_src") ($_fw_conf states no VERSION)"
        return 0
    fi
    if [[ -n "$(git -C "$_fw_src" status --porcelain --untracked-files=no 2>/dev/null)" ]]; then
        say "edited       $_fw_src has local changes: it is built as it is, and not moved"
        return 0
    fi
    origin="$(git -C "$_fw_src" remote get-url origin 2>/dev/null)"
    [[ -n "$origin" ]] || die "firmware: $_fw_src has no origin to fetch from"
    [[ -z "$url" || "$url" == "$origin" ]] || die "firmware: $_fw_src is a checkout of $origin, and $_fw_conf names $url"
    [[ "$(git -C "$_fw_src" rev-parse --is-shallow-repository 2>/dev/null)" == true ]] && depth=(--depth 1)
    read -r kind want < <(_fw_resolve "$origin" "$ver") || exit 1
    [[ -n "$want" ]] || exit 1
    cur="$(_fw_describe "$_fw_src")"
    if [[ "$kind" == tag ]]; then
        if [[ "$(git -C "$_fw_src" rev-parse --verify --quiet "refs/tags/$want^{commit}")" == "$(git -C "$_fw_src" rev-parse HEAD)" ]]; then
            say "current      $_fw_src is at $want"
            return 0
        fi
        say "fetching     $want"
        GIT_TERMINAL_PROMPT=0 git -C "$_fw_src" fetch -q "${depth[@]+"${depth[@]}"}" origin tag "$want" \
            || die "firmware: fetching $want failed"
        git -C "$_fw_src" checkout -q --detach "refs/tags/$want" || die "firmware: cannot check out $want in $_fw_src"
    else
        say "fetching     $want"
        GIT_TERMINAL_PROMPT=0 git -C "$_fw_src" fetch -q "${depth[@]+"${depth[@]}"}" origin "$want" \
            || die "firmware: fetching $want failed"
        if git -C "$_fw_src" symbolic-ref -q HEAD >/dev/null; then
            git -C "$_fw_src" merge -q --ff-only FETCH_HEAD || die "firmware: $_fw_src does not fast-forward to $want"
        else
            git -C "$_fw_src" checkout -q --detach FETCH_HEAD || die "firmware: cannot check out $want in $_fw_src"
        fi
    fi
    if [[ "$cur" == "$(_fw_describe "$_fw_src")" ]]; then
        say "current      $_fw_src is at $cur"
    else say "advanced     $_fw_src: $cur -> $(_fw_describe "$_fw_src")"; fi
}

_fw_trees() { # _fw_trees ALL WORD...: sets _fw_list
    local all="$1" t key
    shift
    local -a cand=()
    local -A seen=()
    _fw_list=()
    if [[ $all -eq 1 ]]; then
        for t in $(kbl_trees); do _fw_uses "$KBL_TREES/$t" && cand+=("$KBL_TREES/$t"); done
        [[ ${#cand[@]} -gt 0 ]] || die "firmware: no registered tree boots through firmware (BOOT=uboot)"
    elif [[ $# -eq 0 ]]; then
        t="$(kbl_tree "")" || exit 1
        cand=("$t")
    else
        for t in "$@"; do
            t="$(kbl_tree "$t")" || exit 1
            cand+=("$t")
        done
    fi
    for t in "${cand[@]}"; do
        key="$( (
            kbl_tree_firmware "$t"
            printf '%s\n' "$_fw_build"
        ))" || exit 1
        [[ -n "${seen[$key]+x}" ]] && continue
        seen["$key"]=1
        _fw_list+=("$t")
    done
}

_fw_title() { # _fw_title TREE
    if [[ "$_fw_origin" == own ]]; then
        printf 'firmware of %s\n' "$(kbl_tree_label "$1")"
    else printf 'firmware %s\n' "$_fw_arch"; fi
}

_fw_show_one() { # _fw_show_one TREE
    local tree="$1" v t users=""
    kbl_tree_firmware "$tree"
    for t in $(kbl_trees); do
        _fw_uses "$KBL_TREES/$t" || continue
        [[ "$( (
            kbl_tree_firmware "$KBL_TREES/$t"
            printf '%s\n' "$_fw_build"
        ) 2>/dev/null)" == "$_fw_build" ]] \
            && users+="${users:+ }$(kbl_tree_label "$KBL_TREES/$t")"
    done
    head1 "$(kbl_tree_label "$tree")  firmware"
    if [[ "$_fw_origin" == own ]]; then
        say "boots        its own, registered from $_fw_conf"
    else say "boots        kbuildlab's global firmware for $_fw_arch ($_fw_conf)"; fi
    say "source       $_fw_src  $([[ -e "$_fw_src/.git" ]] && _fw_describe "$_fw_src" || echo '(not cloned: kbuildlab firmware update)')"
    v="$(kbl_conf_get "$_fw_conf" URL)"
    [[ -n "$v" ]] && say "upstream     $v"
    v="$(kbl_conf_get "$_fw_conf" VERSION)"
    say "follows      ${v:-nothing: update rebuilds and does not move the source}"
    say "build        $_fw_build"
    if [[ -f "$_fw_image" ]]; then
        _fw_fragments
        say "image        $_fw_image  $(stat -c%s "$_fw_image") bytes, built $(_fw_built "$_fw_image")$(_fw_current || echo '; its source or settings changed since')"
    else say "image        $_fw_image  (not built: kbuildlab firmware update $(kbl_tree_label "$tree"))"; fi
    [[ -n "$_fw_symbols" ]] && say "symbols      $_fw_symbols$([[ -f "$_fw_symbols" ]] || echo '  (missing)')"
    v="$(kbl_conf_get "$_fw_conf" DEFCONFIG)"
    [[ -n "$v" ]] && say "defconfig    $v"
    v="$(kbl_conf_get "$_fw_conf" FRAGMENTS)"
    [[ -n "$v" ]] && say "fragments    $v"
    v="$(kbl_conf_get "$_fw_conf" MAKE_ARGS)"
    [[ -n "$v" ]] && say "make         $v"
    say "shared by    ${users:-no tree whose BOOT is uboot}"
    [[ "$_fw_origin" == global && -f "$tree/firmware.conf" ]] \
        && say "own          $tree/firmware.conf is not registered (kbuildlab firmware own $(kbl_tree_label "$tree"))"
    return 0
}

_fw_show() {
    local t line
    if [[ $# -gt 0 ]]; then
        t="$(kbl_tree "$1")" || exit 1
        _fw_show_one "$t"
        return 0
    fi
    printf '%s%-14s %-8s %-7s %-18s %-17s %s%s\n' "$KBL_B" TREE ARCH USES VERSION BUILT IMAGE "$KBL_0"
    for t in $(kbl_trees); do
        t="$KBL_TREES/$t"
        if ! _fw_uses "$t"; then
            printf '%-14s %-8s %s-%s\n' "$(kbl_tree_label "$t")" "$(kbl_tree_get "$t" ARCH)" "$KBL_D" "$KBL_0"
            continue
        fi
        line="$( (
            kbl_tree_firmware "$t"
            printf '%-7s %-18s %-17s %s\n' "$_fw_origin" \
                "$([[ -e "$_fw_src/.git" ]] && _fw_describe "$_fw_src" || echo -)" "$(_fw_built "$_fw_image")" "$_fw_image"
        ) 2>&1)" \
            || line="${KBL_R}${line%%$'\n'*}${KBL_0}"
        printf '%-14s %-8s %s\n' "$(kbl_tree_label "$t")" "$(kbl_tree_get "$t" ARCH)" "$line"
    done
}

_fw_check() { # _fw_check TREE
    local tree="$1" rc=0 out
    (kbl_tree_firmware "$tree") || return 1
    kbl_tree_firmware "$tree"
    head1 "$(kbl_tree_label "$tree")  firmware ($_fw_origin)"
    _fw_fragments
    if [[ -e "$_fw_src/.git" ]]; then
        ok "source       $_fw_src at $(_fw_describe "$_fw_src")"
    else
        bad "source       $_fw_src is not cloned (kbuildlab firmware update)"
        rc=1
    fi
    if [[ -f "$_fw_image" ]]; then
        ok "image        $_fw_image"
    else
        bad "image        $_fw_image is not built"
        rc=1
    fi
    if [[ -f "$_fw_image" ]]; then
        if _fw_current; then
            ok "current      built from this source, these settings and this container image"
        else
            bad "current      the source, the settings or the container image changed since it was built (kbuildlab firmware build)"
            rc=1
        fi
    fi
    if [[ -n "$_fw_symbols" ]]; then
        if [[ -f "$_fw_symbols" ]]; then
            ok "symbols      $_fw_symbols"
        else
            bad "symbols      $_fw_symbols is missing"
            rc=1
        fi
    fi
    if [[ ${#_fw_frags[@]} -gt 0 && -f "$_fw_build/.config" ]]; then
        if out="$("${KBL_REPO}/presets/verify.sh" "$_fw_build/.config" "${_fw_frags[@]}" 2>&1)"; then
            ok "fragments    $(tail -1 <<<"$out")"
        else
            bad "fragments    the .config does not hold them:"
            printf '%s\n' "$out" >&2
            rc=1
        fi
    fi
    return $rc
}

_fw_own() { # _fw_own TREE
    local tree="$1" label conf reg was k
    label="$(kbl_tree_label "$tree")"
    conf="$tree/firmware.conf"
    reg="$(kbl_tree_data "$tree")/firmware.registered"
    [[ -f "$conf" ]] || die "firmware own: $label has no $conf
       write it in tree.conf's grammar: SRC, BUILD and IMAGE, and as needed SYMBOLS, URL, VERSION,
       DEFCONFIG, FRAGMENTS and MAKE_ARGS; then run this again"
    for k in SRC BUILD IMAGE; do
        [[ -n "$(kbl_conf_get "$conf" "$k")" ]] || die "firmware own: $conf states no $k"
    done
    [[ -z "$(kbl_conf_get "$conf" NAME)" ]] || die "firmware own: $conf states NAME, which places a global firmware; a tree's own states SRC and BUILD"
    was="$(cat "$reg" 2>/dev/null)"
    mkdir -p "$(dirname "$reg")" || die "firmware own: cannot write $(dirname "$reg")"
    {
        sha256sum <"$conf" | cut -c1-64
        date '+%Y-%m-%d %H:%M:%S'
    } >"$reg"
    if ! (
        kbl_tree_firmware "$tree"
        [[ "$(readlink -m -- "$_fw_build")" != "$(readlink -m -- "$_fw_src")" ]] || die "firmware own: BUILD is SRC; it is a directory of its own"
        _fw_sync
        _fw_build_now 0
    ); then
        if [[ -n "$was" ]]; then printf '%s\n' "$was" >"$reg"; else rm -f "$reg"; fi
        die "firmware own: $label's own firmware is not registered (above)"
    fi
    ok "registered   $label boots its own firmware from its next run"
    say "undo         kbuildlab firmware global $label"
}

_fw_global() { # _fw_global TREE
    local tree="$1" label reg
    label="$(kbl_tree_label "$tree")"
    reg="$(kbl_tree_data "$tree")/firmware.registered"
    if [[ -f "$reg" ]]; then
        rm -f "$reg" "$(kbl_tree_data "$tree")/firmware.stamp" "$(kbl_tree_data "$tree")/firmware.log" \
            || die "firmware global: cannot write $(dirname "$reg")"
        ok "global       $label boots kbuildlab's global firmware again"
        [[ -f "$tree/firmware.conf" ]] && say "kept         $tree/firmware.conf (kbuildlab firmware own $label registers it again)"
    else
        say "global       $label already boots the global firmware"
    fi
    return 0
}

_fw_update_one() { # _fw_update_one TREE
    kbl_tree_firmware "$1"
    head1 "== $(_fw_title "$1")"
    _fw_sync
    _fw_fragments
    if _fw_current; then
        ok "current      $_fw_image is built from $(_fw_describe "$_fw_src") with these settings"
        return 0
    fi
    _fw_build_now 0
}

cmd_firmware() {
    local all=0 fresh=0 action=show t rc=0
    local -a words=()
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --all)
                all=1
                shift
                ;;
            --fresh)
                fresh=1
                shift
                ;;
            -*) die "firmware: unknown option '$1'" ;;
            *)
                words+=("$1")
                shift
                ;;
        esac
    done
    if [[ ${#words[@]} -gt 0 && " $_FW_ACTIONS " == *" ${words[0]} "* ]]; then
        action="${words[0]}"
        words=("${words[@]:1}")
    fi
    [[ "$action" == build || $fresh -eq 0 ]] || die "firmware $action: --fresh belongs to firmware build"
    case "$action" in
        show)
            [[ $all -eq 0 && ${#words[@]} -le 1 ]] || die "firmware: kbuildlab firmware [TREE]"
            _fw_show "${words[@]+"${words[@]}"}"
            ;;
        own | global)
            [[ $all -eq 0 && ${#words[@]} -le 1 ]] || die "firmware $action: one tree at a time (kbuildlab firmware $action [TREE])"
            t="$(kbl_tree "${words[0]:-}")" || exit 1
            "_fw_$action" "$t"
            ;;
        update)
            _fw_trees "$all" "${words[@]+"${words[@]}"}"
            for t in "${_fw_list[@]}"; do (_fw_update_one "$t") || rc=1; done
            ;;
        build)
            _fw_trees "$all" "${words[@]+"${words[@]}"}"
            for t in "${_fw_list[@]}"; do
                (
                    kbl_tree_firmware "$t"
                    head1 "== $(_fw_title "$t")"
                    _fw_build_now "$fresh"
                ) || rc=1
            done
            ;;
        check)
            _fw_trees "$all" "${words[@]+"${words[@]}"}"
            for t in "${_fw_list[@]}"; do _fw_check "$t" || rc=1; done
            ;;
    esac
    return $rc
}
