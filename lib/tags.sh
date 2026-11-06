# Source indexes: ctags, cscope and GNU global over the tree as its build
# compiled it, and the clangd index (lib/index/).  Sourced by bin/kbuildlab
# after lib/common.sh.

# ---------------------------------------------------------------- tags
# The fourth index is clangd's, and it is the odd one out in three ways worth
# stating, because each is why it is done here rather than somewhere else.
#
# It is computed on the host.  ctags, cscope and gtags run inside the build
# image because they are the kernel's own make targets; clangd's input is the
# .cmd files kbuild has already written, and the clangd that will read the
# result is the one on the host.  The image is only asked what the build
# compiler answers where clang would answer differently.
#
# It writes nothing into the tree.  A compilation database in the source root
# would show up in `git status`, and `make mrproper` would take it away again;
# it lives in $XDG_CACHE_HOME/kbuildlab/clangd/<tree> instead, with clangd
# pointed there from the user's config by path.
#
# And it belongs on this command rather than on `build`.  ctags, cscope, gtags
# and clangd answer the same question -- where is this symbol -- and a tree
# reindexed for three of them and not the fourth is a tree whose editor is
# quietly describing the previous build.  `update` calls this, so a synced and
# rebuilt tree gets all four without being asked.
_clangd_index() {   # _clangd_index TREE [gen|warm]
    local tree="$1" phase="${2:-}"
    [[ "${KBL_CLANGD:-1}" == "0" ]] && { say "clangd       skipped (KBL_CLANGD=0)"; return 0; }
    command -v clangd  >/dev/null 2>&1 || { warn "clangd skipped: no clangd on the host"; return 0; }
    command -v python3 >/dev/null 2>&1 || { warn "clangd skipped: no python3 on the host"; return 0; }
    # Every tree in the workspace is named as a peer so a tree too old to ship
    # scripts/clang-tools/gen_compile_commands.py can borrow a newer tree's
    # copy.  The .cmd format that script reads has not changed since it was
    # added in v5.4, and copying it into the old tree would be writing into the
    # source -- which is the one thing this must not do.
    local ws t; local -a peers=() wsopt=()
    for t in $(kbl_trees); do peers+=(--peer "$KBL_TREES/$t"); done
    ws="$(kbl_workspace_opt)" && wsopt=(--workspace "$ws")
    # The build image answers what clangd's clang would answer differently --
    # __clang__, __GNUC__, the __has_* tests -- and later measures the result,
    # so the generator is told how to reach it.  -i: both read their questions
    # from stdin.
    local _KBL_ACTIVE_IMAGE _KBL_ACTIVE_TREE="$tree"; _KBL_ACTIVE_IMAGE="$(_tree_image "$tree")"
    _container_run "$(kbl_tree_src "$tree")" -i || return 0
    # KBL_CLANGD_JOBS caps the parallel work of both steps, for a machine that
    # has other things to do; unset, each sizes itself to what is free.
    local -a jobs=()
    [[ -n "${KBL_CLANGD_JOBS:-}" ]] && jobs=(-j "$KBL_CLANGD_JOBS")
    if [[ "$phase" != warm ]]; then
        local -a gencmd=(python3 "${KBL_REPO}/lib/index/clangd.py" --tree "$tree" "${wsopt[@]+"${wsopt[@]}"}"
                      "${peers[@]}" --build-shell "$(printf '%q ' "${_KBL_RUN[@]}")" gen --force
                      "${jobs[@]+"${jobs[@]}"}")
        say "indexing     clangd (compile_commands.json, out of tree)"
        if [[ "${KBL_TAGS_FRESH:-0}" == 1 ]]; then
            "${gencmd[@]}" --fresh || { warn "clangd index failed"; return 1; }
        elif ! "${gencmd[@]}"; then
            warn "clangd index failed (above); generating it again from scratch, keeping only clangd's own shards"
            "${gencmd[@]}" --rebuild || { warn "clangd index failed from scratch too"; return 1; }
        fi
    fi
    [[ "$phase" == gen ]] && return 0

    # And then build clangd's own index, here, rather than leaving it to
    # whichever clangd an editor starts later.  It is the same work; done here
    # it is finished before the editor opens, instead of competing with the
    # reading you opened the editor to do.  clangd keys its shards on file
    # contents, so a rebuild that touched forty files re-indexes forty files
    # and an already-warm tree costs only the minutes clangd takes to read the
    # project back.
    #
    # Synchronous on purpose.  A background daemon this command started and did
    # not mention would be a process the operator has to discover; `tags` is
    # already the slow command, and the wait is visible and interruptible.
    [[ "${KBL_CLANGD_WARM:-1}" == "0" ]] && { say "clangd       index left cold (KBL_CLANGD_WARM=0)"; return 0; }
    if [[ -n "${_KBL_WARM_QUEUE:-}" ]]; then
        printf '%s\n' "$tree" >> "$_KBL_WARM_QUEUE"
        say "clangd       background index warmed once every tree is built"
        return 0
    fi
    say "indexing     clangd background index"
    python3 "${KBL_REPO}/lib/index/clangd.py" --tree "$tree" warm "${jobs[@]+"${jobs[@]}"}" \
        || { warn "clangd index warming did not finish (the editor will resume it)"; return 1; }
}

_part_inputs() {   # _part_inputs TREE tags|clangd|kmx
    local k
    _build_inputs "$1" || return 1
    case "$2" in
        tags) printf 'tags %s\n' "${KBL_TAGS_VIEW:-1}" ;;
        clangd) printf 'clangd %s %s warm %s\n' "${KBL_CLANGD:-1}" "$(clangd --version 2>/dev/null | head -1)" "${KBL_CLANGD_WARM:-1}" ;;
        kmx)
            k="$(command -v kmx 2>/dev/null)"
            printf 'kmx %s %s\n' "${KBL_KMX:-1}" "${k:+$(stat -L -c '%s %Y' "$k" 2>/dev/null)}" ;;
    esac
}

_part_current() {   # _part_current TREE tags|clangd|kmx
    local tree="$1" rec src
    rec="$(_stamp "$tree" "$2")"; [[ -s "$rec" ]] || return 1
    src="$(kbl_tree_src "$tree")" || return 1
    case "$2" in
        tags) [[ -s "$src/tags" && -s "$src/cscope.out" ]] || return 1 ;;
        clangd) [[ "${KBL_CLANGD:-1}" == 0 || -f "${XDG_CACHE_HOME:-$HOME/.cache}/kbuildlab/clangd/$(basename "$tree")/compile_commands.json" ]] || return 1 ;;
    esac
    [[ "$(_part_inputs "$tree" "$2")" == "$(cat "$rec")" ]]
}

_stamp_part() {   # _stamp_part TREE PART INPUTS [pending]
    local to
    to="$(_stamp "$1" "$2")${4:+.pending}"
    [[ -n "$3" && "$(_part_inputs "$1" "$2")" == "$3" ]] || return 0
    mkdir -p "$(kbl_tree_data "$1")" && printf '%s\n' "$3" > "$to"
}

_kmx_index() {   # _kmx_index TREE
    local tree="$1" src line
    [[ "${KBL_KMX:-1}" == "0" ]] && { say "kmx          skipped (KBL_KMX=0)"; return 0; }
    command -v kmx >/dev/null 2>&1 || { warn "kmx skipped: no kmx on the host"; return 0; }
    src="$(kbl_tree_src "$tree")" || return 0
    local -a threads=()
    [[ -n "${KBL_KMX_JOBS:-}" ]] && threads=(--threads "$KBL_KMX_JOBS")
    say "indexing     kmx (macros, and the C types in the build's DWARF)"
    if [[ "${KBL_TAGS_FRESH:-0}" == 1 || "$(kmx --help 2>/dev/null)" != *$'\n  refresh '* ]] \
       || ! line="$(kmx "$src" --quiet "${threads[@]+"${threads[@]}"}" refresh | sed -n 's/^  types *//p')"; then
        line="$(kmx "$src" --quiet "${threads[@]+"${threads[@]}"}" index | sed -n 's/^  types *//p')" \
            || { warn "kmx index failed (above)"; return 1; }
    fi
    [[ -n "$line" ]] && say "kmx          $line"
    return 0
}

# Every index holds what the image's compiler and tools answered, under keys that
# do not name them.  The image a part was last finished with outlives its stamp,
# which a run removes as it starts, so an interrupted run does not hide the change.
_index_unusable() {   # _index_unusable TREE PART
    local rec old now
    rec="$(_stamp "$1" "$2")"
    old="$(sed -n 's/^image //p' "$rec" 2>/dev/null)"
    if [[ -n "$old" ]]; then printf '%s\n' "$old" > "$rec.image"
    else old="$(cat "$rec.image" 2>/dev/null)"; fi
    now="$("$(kbl_container)" image inspect --format '{{.Id}}' "$(_tree_image "$1")" 2>/dev/null)"
    [[ -z "$old" || -z "$now" || "$old" == "$now" ]] || printf 'its build image changed\n'
}

# A part, from nothing when its reason says so.
_index_part() {   # _index_part WHY PART-COMMAND...
    local why="$1"; shift
    if [[ -n "$why" ]]; then
        say "discarding   what this index holds: $why"
        KBL_TAGS_FRESH=1 "$@"
    else
        "$@"
    fi
}

cmd_tags() {
    local clangd=1 tags=1 kmx=1 alone=0
    local -a rest=()
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --no-clangd) clangd=0; shift ;;
            --clangd-only) tags=0; kmx=0; alone=1; shift ;;
            --no-warm) export KBL_CLANGD_WARM=0; shift ;;
            --fresh) export KBL_TAGS_FRESH=1; shift ;;
            *) rest+=("$1"); shift ;;
        esac
    done
    if [[ -n "${_KBL_TAGS_PARTS:-}" ]]; then
        [[ " $_KBL_TAGS_PARTS " == *" tags "* ]] || tags=0
        [[ " $_KBL_TAGS_PARTS " == *" clangd "* ]] || clangd=0
        [[ " $_KBL_TAGS_PARTS " == *" kmx "* ]] || kmx=0
    fi
    local tree; tree="$(kbl_tree "${rest[0]:-}")" || exit 1
    local src; src="$(kbl_tree_src "$tree")" || exit 1
    [[ -d "$src" ]] || die "no kernel source in $tree"
    local gen=0 tin="" cin="" kin="" cs twhy="" cwhy="" kwhy=""
    [[ $tags -eq 0 ]] || twhy="$(_index_unusable "$tree" tags)"
    [[ $clangd -eq 0 ]] || cwhy="$(_index_unusable "$tree" clangd)"
    [[ $kmx -eq 0 ]] || kwhy="$(_index_unusable "$tree" kmx)"
    cs="$(_stamp "$tree" clangd)"
    if [[ $tags -eq 1 ]]; then rm -f "$(_stamp "$tree" tags)"; tin="$(_part_inputs "$tree" tags)"; fi
    if [[ $clangd -eq 1 ]]; then rm -f "$cs" "$cs.pending"; cin="$(_part_inputs "$tree" clangd)"; fi
    if [[ $kmx -eq 1 ]]; then rm -f "$(_stamp "$tree" kmx)"; kin="$(_part_inputs "$tree" kmx)"; fi
    # Asked for clangd alone, its failure is the command's.
    if [[ $clangd -eq 1 ]]; then
        _index_part "$cwhy" _clangd_index "$tree" gen || { gen=1; [[ $alone -eq 0 ]] || exit 1; }
    fi
    if [[ $tags -eq 1 ]]; then _index_part "$twhy" _index_tree "$tree" && _stamp_part "$tree" tags "$tin"; fi
    if [[ $kmx -eq 1 ]]; then _index_part "$kwhy" _kmx_index "$tree" && _stamp_part "$tree" kmx "$kin"; fi
    if [[ $clangd -eq 1 && $gen -eq 0 ]] && _clangd_index "$tree" warm; then
        _stamp_part "$tree" clangd "$cin" "${_KBL_WARM_QUEUE:+pending}"
    fi
    return 0
}

# Where `tags` keeps a tree's view: its copies, its state, and the overlay's
# work directory.  The one place that decides it; tagview.py is told.
_view_dir() { printf '%s\n' "${XDG_CACHE_HOME:-$HOME/.cache}/kbuildlab/view/$(basename "$1")"; }

_index_plain() {   # _index_plain TREE
    local tree="$1" arch karch src
    arch="$(kbl_tree_arch "$tree")" || exit 1; karch="$(_karch "$arch")"
    src="$(kbl_tree_src "$tree")" || exit 1
    if command -v python3 >/dev/null 2>&1; then
        python3 "${KBL_REPO}/lib/index/tagview.py" --tree "$tree" --view "$(_view_dir "$tree")" forget \
            || die "cannot remove the view's indexes"
        if [[ "${KBL_TAGS_FRESH:-0}" == 1 ]]; then
            local -a outs=(); mapfile -t outs < <(python3 "${KBL_REPO}/lib/index/tagview.py" outputs)
            ( cd "$src" && rm -f -- "${outs[@]}" ) || die "cannot remove the indexes of $src"
        fi
    fi
    local _KBL_ACTIVE_IMAGE _KBL_ACTIVE_TREE="$tree"; _KBL_ACTIVE_IMAGE="$(_tree_image "$tree")"
    # Kernel's own targets: index only the built arch. gtags (GNU GLOBAL) is
    # optional -- not every image ships `global`, so absent is a warn, not fatal.
    say "indexing     ctags + cscope for $karch"
    _in_container "$src" bash -c "PATH=${KBL_REPO}/lib/index/ctags-shim:\$PATH KBL_TAGS_PASS=1 exec make ARCH=$karch tags cscope" \
        || die "tags/cscope failed"
    if _in_container "$src" sh -c 'command -v gtags >/dev/null 2>&1'; then
        say "indexing     gtags (GNU GLOBAL) for $karch"
        _in_container "$src" make ARCH="$karch" gtags \
            || warn "gtags failed (ctags/cscope are still current)"
    else
        warn "gtags skipped: no 'global' in the build image (rebuild image to add it)"
    fi
    _in_container "$src" sh -c 'ls -la tags cscope.out GTAGS 2>/dev/null || true'
}

_index_tree() {   # _index_tree TREE
    local tree="$1" name
    name="$(kbl_tree_label "$tree")"
    if [[ "${KBL_TAGS_VIEW:-1}" == "0" ]]; then _index_plain "$tree"; return; fi
    if [[ "$(basename "$(kbl_container)")" != podman ]] || ! command -v python3 >/dev/null 2>&1; then
        warn "the config view needs podman's overlay mounts and python3 on the host; indexing $name as it is on disk"
        _index_plain "$tree"; return
    fi
    if [[ "${KBL_TAGS_FRESH:-0}" != 1 ]]; then
        ( _index_view "$tree" ) && return 0
        warn "indexing $name failed (above); discarding its view and indexing it again from scratch"
    fi
    ( _index_view "$tree" fresh ) \
        || die "indexing $name from scratch failed (above); the tree keeps its previous indexes"
}

_index_view() {
    local tree="$1" fresh="${2:-}" name arch karch src view st up wk mode=full rc
    name="$(kbl_tree_label "$tree")"
    arch="$(kbl_tree_arch "$tree")" || exit 1; karch="$(_karch "$arch")"
    src="$(kbl_tree_src "$tree")" || exit 1
    local _KBL_ACTIVE_IMAGE _KBL_ACTIVE_TREE="$tree"; _KBL_ACTIVE_IMAGE="$(_tree_image "$tree")"
    view="$(_view_dir "$tree")"
    st="$view/state"; up="$view/upper"; wk="$view/work"
    if [[ -n "$fresh" ]]; then
        rm -rf -- "${view:?}/upper" "$view/state" "$view/readings" "$view/view.json" "$view/published.json" \
            || die "cannot discard the view in $view"
    fi
    mkdir -p "$st" "$up" "$wk" || die "cannot write $view"
    rm -f "$st"/keys "$st"/opts.* "$st"/files.* "$st"/now.* "$st"/replay.* "$st"/tags.new "$st"/tags.prev \
          "$st"/emitted
    if [[ -z "$fresh" && -s "$src/tags" ]] \
       && { ln -f "$src/tags" "$st/tags.prev" 2>/dev/null || cp --reflink=auto "$src/tags" "$st/tags.prev"; }; then
        mode=merge
    fi
    [[ -n "$fresh" ]] && mode=fresh
    local -a py=(python3 "${KBL_REPO}/lib/index/tagview.py" --tree "$tree" --view "$view")
    "${py[@]}" harvest --drop || die "cannot clear $up"
    _container_run "$src" -v "$src:$src:O,upperdir=$up,workdir=$wk" -v "$st:$st" -e "KBL_TAGS_STATE=$st" \
        -e "KBL_JOBS=$(_kbl_jobs)" || exit 1
    local -a over=("${_KBL_RUN[@]}")
    say "indexing     the sources scripts/tags.sh lists for $karch"
    "${over[@]}" bash -c "PATH=${KBL_REPO}/lib/index/ctags-shim:\$PATH; exec make ARCH=$karch tags" >/dev/null \
        || die "scripts/tags.sh could not list the sources of $name"
    [[ -s "$st/keys" ]] || die "scripts/tags.sh ran no ctags in $_KBL_ACTIVE_IMAGE; is ctags installed there?"
    "${py[@]}" harvest --drop || die "cannot clear $up"
    _container_run "$src" -i || exit 1
    local -a jobs=()
    [[ -n "${KBL_CLANGD_JOBS:-}" ]] && jobs=(-j "$KBL_CLANGD_JOBS")
    say "indexing     $name as its build compiled it (out of tree, $up)"
    "${py[@]}" build --build-shell "$(printf '%q ' "${_KBL_RUN[@]}")" "${jobs[@]+"${jobs[@]}"}" ${fresh:+--fresh}
    rc=$?
    if [[ $rc -eq 2 ]]; then
        rm -f "$st/tags.prev"; warn "$name records no compile; indexing it as it is on disk"
        _index_plain "$tree"; return
    fi
    [[ $rc -eq 0 ]] || die "the view of $name could not be built"
    say "indexing     ctags ($mode), cscope and gtags over that view"
    [[ -n "$fresh" ]] || "${py[@]}" stage || die "cannot stage the indexes of $name in $up"
    "${over[@]}" bash "${KBL_REPO}/lib/index/tags-index.sh" "$karch" "$mode"; rc=$?
    # 1 means an indexer failed and tags-index.sh withdrew what it wrote; any other
    # failure is the script's own, and nothing it left is trusted.
    if [[ $rc -le 1 ]]; then
        "${py[@]}" harvest --keep || die "cannot move the indexes of $name out of $up"
    else
        "${py[@]}" harvest --drop || die "cannot clear $up"
    fi
    rm -f "$st/tags.prev" "$st/tags.new" "$st/emitted"
    [[ $rc -eq 0 ]] || die "indexing $name failed (above); what failed keeps its previous index"
    ls -la "$src/tags" "$src/cscope.out" "$src/GTAGS" 2>/dev/null || true
}
