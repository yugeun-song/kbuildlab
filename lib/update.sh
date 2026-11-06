# `update`: one pass over a tree -- check, sync, keep .config, incremental
# build, modules, indexes -- then a summary.  Sourced by bin/kbuildlab after
# lib/common.sh.

# ---------------------------------------------------------------- update
# An update keeps the tree's answers: no --preset, because re-merging the
# fragments would overwrite answers the operator changed on purpose.  It runs
# olddefconfig itself, so new symbols take their defaults before anything is
# reported, and reports two things olddefconfig does not say: what the update
# changed in the .config, and any preset line the newer source dropped (renamed,
# moved under another `depends on`, deleted).  Neither is repaired.

# How one .config differs from another, one "new|gone|changed CONFIG_X" per line.
_config_delta() {   # _config_delta OLD NEW
    LC_ALL=C awk '
        function parse(line) {
            if (line ~ /^CONFIG_[A-Za-z0-9_]+=/) {
                name = line; sub(/=.*/, "", name); val = substr(line, length(name) + 2); return 1
            }
            if (line ~ /^# CONFIG_[A-Za-z0-9_]+ is not set$/) { name = line; sub(/^# /, "", name); sub(/ .*/, "", name); val = "n"; return 1 }
            return 0
        }
        FNR == NR { if (parse($0)) old[name] = val; next }
        parse($0) { now[name] = val }
        END {
            for (n in now) if (!(n in old)) print "new " n; else if (old[n] != now[n]) print "changed " n
            for (n in old) if (!(n in now)) print "gone " n
        }' "$1" "$2" | LC_ALL=C sort
}

_update_report() {   # _update_report TREE BEFORE
    local tree="$1" before="$2" src cfg arch
    src="$(kbl_tree_src "$tree")" || exit 1; cfg="$src/.config"
    [[ -r "$cfg" ]] || return 0

    # (1) what this update changed in the .config, against the copy taken before it.
    if [[ -s "$before" ]]; then
        local -a delta=() names=(); local kind
        mapfile -t delta < <(_config_delta "$before" "$cfg")
        [[ ${#delta[@]} -eq 0 ]] && say "config       unchanged by this update"
        for kind in new gone changed; do
            mapfile -t names < <(printf '%s\n' "${delta[@]+"${delta[@]}"}" | sed -n "s/^$kind //p")
            [[ ${#names[@]} -gt 0 ]] \
                && say "config       ${#names[@]} $kind: ${names[*]:0:12}${names[12]:+ ...}"
        done
    fi

    # (2) does the .config still say what the preset asked for?  A tree with its
    # own fragment is checked against that too, in the order cmd_config merges them.
    local -a frags=("${KBL_REPO}/presets/common.config")
    arch="$(kbl_tree_arch "$tree")" || exit 1
    local archpreset="${KBL_REPO}/presets/arch/${arch}.config"
    [[ "$(kbl_tree_get "$tree" UPSTREAM)" == "mainline" \
       && -f "${KBL_REPO}/presets/arch/${arch}-mainline.config" ]] \
        && archpreset="${KBL_REPO}/presets/arch/${arch}-mainline.config"
    [[ -f "$archpreset" ]] && frags+=("$archpreset")
    # A kernel older than the preset's reference took it softly; only the
    # tree's own fragment was a demand there.
    _older_than_preset "$src" && frags=()
    [[ -f "$tree/config-fragment" ]] && frags+=("$tree/config-fragment")
    local out
    if [[ ${#frags[@]} -eq 0 ]]; then
        say "preset       taken softly by this kernel; no line of it is a demand here"
    elif out="$("${KBL_REPO}/presets/verify.sh" "$cfg" "${frags[@]}" 2>&1)"; then
        say "preset       $(tail -1 <<<"$out")"
    else
        warn "preset drift in $(kbl_tree_label "$tree") -- the update did not change these, the newer
       source did.  Nothing was repaired; re-apply deliberately with
       'kbuildlab config $(kbl_tree_label "$tree") --preset' if that is what you want:"
        printf '%s\n' "$out" >&2
    fi
}

# One stage of an update, in a subshell so a `die` inside it ends only that
# stage, with its outcome and duration appended to the run's record.
_update_stage() {   # _update_stage RECORD NAME STAGE CMD...
    local rec="$1" name="$2" stage="$3" t0=$SECONDS rc; shift 3
    ( "$@" ); rc=$?
    printf '%s\t%s\t%s\t%s\n' "$name" "$stage" "$([[ $rc -eq 0 ]] && echo ok || echo fail)" \
        "$((SECONDS - t0))" >> "$rec"
    return $rc
}

# What a tree needs before anything runs, said all at once.
_update_check() {   # _update_check TREE
    local tree="$1" src v c img
    kbl_tree_arch "$tree" >/dev/null || exit 1
    src="$(kbl_tree_src "$tree")" || exit 1
    v="$(kbl_tree_get "$tree" VERSION)"
    [[ -n "$v" ]] || die "$(kbl_tree_label "$tree") states no VERSION to track: kbuildlab set $(kbl_tree_label "$tree") VERSION=<tag or branch>"
    if [[ -e "$src" ]]; then
        kbl_is_ksrc "$src" || die "$src is not a kernel source"
        [[ -d "$src/.git" ]] || die "$src is not a git checkout, so it cannot be synced"
    fi
    c="$(kbl_container)" || exit 1
    img="$(_tree_image "$tree")"
    "$c" image inspect "$img" >/dev/null 2>&1 || die "container image $img is missing: kbuildlab image"
    command -v git >/dev/null 2>&1 || die "git is not installed$(kbl_pkg_hint git)"
    ok "$(kbl_tree_label "$tree"): $(kbl_tree_arch "$tree"), tracks $v, source $src"
}

_update_fetch() {   # _update_fetch TREE DIR
    local tree="$1" d="$2" t0 rc
    t0=$SECONDS
    ( _update_check "$tree" ) > "$d/check.log" 2>&1; rc=$?
    printf '%s %s\n' "$rc" "$((SECONDS - t0))" > "$d/check.rc"
    [[ $rc -eq 0 ]] || return 0
    t0=$SECONDS
    ( export GIT_TERMINAL_PROMPT=0; cmd_sync "$tree" ) > "$d/sync.log" 2>&1; rc=$?
    printf '%s %s\n' "$rc" "$((SECONDS - t0))" > "$d/sync.rc"
}

_update_replay() {   # _update_replay RECORD NAME STAGE DIR
    local rc secs
    cat "$4/$3.log" 2>/dev/null
    if [[ -r "$4/$3.rc" ]]; then read -r rc secs < "$4/$3.rc"
    else bad "$3 did not finish"; rc=1 secs=0; fi
    printf '%s\t%s\t%s\t%s\n' "$2" "$3" "$([[ $rc -eq 0 ]] && echo ok || echo fail)" "$secs" >> "$1"
    return "$rc"
}

KBL_INDEX_ROOM_MB=3072

_update_one() {   # _update_one TREE RECORD NOTAGS DIR PID [TAGS-OPTION...]
    local tree="$1" rec="$2" notags="$3" d="$4" pid="$5" name src before synced=1; shift 5
    name="$(kbl_tree_label "$tree")"
    head1 "== $name"
    if [[ -n "$pid" ]]; then
        wait "$pid"
        _update_replay "$rec" "$name" check "$d" || return 1
        if ! _update_replay "$rec" "$name" sync "$d"; then
            warn "fetching $name failed (above); it is fetched again after the other trees"
            return 2
        fi
    elif ! _update_stage "$rec" "$name" sync cmd_sync "$tree"; then
        warn "fetching $name failed again (above); building it as it is checked out"
        synced=0
    fi
    src="$(kbl_tree_src "$tree")" || return 1
    if [[ $synced -eq 1 ]] && _build_current "$tree"; then
        local at p
        local -a stale=()
        at="$(_src_head "$src" | cut -c1-12)"
        printf '%s\t%s\t%s\t%s\n' "$name" config same 0 "$name" build same 0 >> "$rec"
        if [[ "$notags" == 1 ]]; then
            printf '%s\t%s\t%s\t%s\n' "$name" tags skip 0 >> "$rec"
            say "current      built at $at, the newest commit; config and build skipped"
            return 0
        fi
        for p in tags clangd kmx; do
            if [[ $# -gt 0 ]] || ! _part_current "$tree" "$p"; then stale+=("$p"); fi
        done
        if [[ ${#stale[@]} -eq 0 ]]; then
            printf '%s\t%s\t%s\t%s\n' "$name" tags same 0 >> "$rec"
            say "current      built and indexed at $at, the newest commit; skipped"
            return 0
        fi
        say "current      built at $at, the newest commit; config and build skipped, redoing ${stale[*]}"
        _KBL_INDEX_QUEUE+=("$tree"$'\t'"${stale[*]}")
        return 0
    fi
    # Taken before cmd_config, which merges a tree's own fragment.
    before="$(dirname "$rec")/config.before"; rm -f "$before"
    [[ -f "$src/.config" ]] && cp -p "$src/.config" "$before"
    _update_stage "$rec" "$name" config _update_config "$tree" "$before" || { rm -f "$before"; return 1; }
    rm -f "$before"
    _update_stage "$rec" "$name" build cmd_build "$tree" || return 1
    if [[ "$notags" == 1 ]]; then
        printf '%s\t%s\t%s\t%s\n' "$name" tags skip 0 >> "$rec"
    else
        _KBL_INDEX_QUEUE+=("$tree"$'\t')
    fi
    [[ $synced -eq 1 ]]
}

_update_index_one() {   # _update_index_one TREE DIR JOBS PARTS [TAGS-OPTION...]
    local tree="$1" d="$2" jobs="$3" parts="$4" t0=$SECONDS rc; shift 4
    ( [[ -n "${KBL_CLANGD_JOBS:-}" ]] || export KBL_CLANGD_JOBS="$jobs"
      _KBL_TAGS_PARTS="$parts"
      cmd_tags "$tree" "$@" ) > "$d/tags.log" 2>&1; rc=$?
    printf '%s %s\n' "$rc" "$((SECONDS - t0))" > "$d/tags.rc"
}

# Up to half the cores' worth of trees index at once, on a share of the cores each
# that still leaves none idle while another tree is in a stretch of its own that
# uses one.  Another starts only with memory to begin on; each stage then sizes
# itself to what is free.  Each tree's log is shown whole when it ends.
_update_index() {   # _update_index RECORD [TAGS-OPTION...]
    local rec="$1" tmp entry tree parts t rc=0 width cpus; shift
    [[ ${#_KBL_INDEX_QUEUE[@]} -gt 0 ]] || return 0
    tmp="$(dirname "$rec")"
    cpus="$(nproc)"
    width=$(( cpus / 2 ))
    (( width > ${#_KBL_INDEX_QUEUE[@]} )) && width=${#_KBL_INDEX_QUEUE[@]}
    (( width < 1 )) && width=1
    local jobs=$(( cpus * 2 / width )); (( jobs > cpus )) && jobs=$cpus
    head1 "== indexes"
    say "indexing     ${#_KBL_INDEX_QUEUE[@]} tree(s), up to $width at a time, $jobs job(s) each"
    local -a active=()
    _update_index_collect() {
        local -a still=(); local t
        for t in "${active[@]+"${active[@]}"}"; do
            if [[ -e "$tmp/$(kbl_tree_id "$t")/tags.rc" ]]; then
                head1 "== $(kbl_tree_label "$t")"
                _update_replay "$rec" "$(kbl_tree_label "$t")" tags "$tmp/$(kbl_tree_id "$t")" || rc=1
            else
                still+=("$t")
            fi
        done
        active=("${still[@]+"${still[@]}"}")
    }
    for entry in "${_KBL_INDEX_QUEUE[@]}"; do
        tree="${entry%%$'\t'*}"; parts="${entry#*$'\t'}"
        while (( ${#active[@]} >= width )) || { (( ${#active[@]} > 0 )) \
              && (( $(awk '/^MemAvailable:/ { print int($2 / 1024) }' /proc/meminfo) < KBL_INDEX_ROOM_MB )); }; do
            wait -n
            _update_index_collect
        done
        rm -f "$tmp/$(kbl_tree_id "$tree")/tags.rc"
        _update_index_one "$tree" "$tmp/$(kbl_tree_id "$tree")" "$jobs" "$parts" "$@" &
        active+=("$tree")
    done
    while (( ${#active[@]} > 0 )); do
        wait -n
        _update_index_collect
    done
    return $rc
}

_update_warm() {   # _update_warm RECORD QUEUE
    local rec="$1" res id res_ok secs t
    local -a trees=() args=()
    mapfile -t trees < <(awk '!seen[$0]++' "$2")
    [[ ${#trees[@]} -gt 0 ]] || return 0
    res="$(dirname "$rec")/warm"; : > "$res"
    for t in "${trees[@]}"; do args+=(--tree "$t"); done
    args+=(warm --results "$res")
    [[ -n "${KBL_CLANGD_JOBS:-}" ]] && args+=(-j "$KBL_CLANGD_JOBS")
    head1 "== clangd"
    say "indexing     clangd background index of $(_kbl_labels "${trees[@]##*/}")"
    python3 "${KBL_REPO}/lib/index/clangd.py" "${args[@]}" \
        || warn "clangd index warming did not finish everywhere (the editor will resume it)"
    while IFS=$'\t' read -r id res_ok secs; do
        printf '%s\t%s\t%s\t%s\n' "$(kbl_tree_label "$KBL_TREES/$id")" warm "$res_ok" "$secs" >> "$rec"
        t="$(_stamp "$KBL_TREES/$id" clangd)"
        if [[ "$res_ok" == ok && -f "$t.pending" ]]; then mv -f "$t.pending" "$t"; else rm -f "$t.pending"; fi
    done < "$res"
}

_update_config() {   # _update_config TREE BEFORE
    local src
    cmd_config "$1"
    _kmake "$1" olddefconfig || die "olddefconfig failed"
    src="$(kbl_tree_src "$1")" || exit 1
    [[ -s "$2" ]] && cmp -s "$2" "$src/.config" && touch -r "$2" "$src/.config"
    _update_report "$1" "$2"
}

_update_summary() {   # _update_summary RECORD
    local rec="$1" name stage res secs t same=0
    local -a order=() stages=(check sync config build tags)
    local -A cell=() total=()
    while IFS=$'\t' read -r name stage res secs; do
        [[ -n "${total[$name]+x}" ]] || { order+=("$name"); total[$name]=0; }
        cell["$name:$stage"]="$res"; total[$name]=$(( ${total[$name]} + secs ))
    done < "$rec"
    [[ ${#order[@]} -gt 0 ]] || return 0
    local w=4; for name in "${order[@]}"; do (( ${#name} > w )) && w=${#name}; done
    echo
    printf '%s%-*s  %-6s %-6s %-6s %-6s %-6s %s%s\n' "$KBL_B" "$w" TREE CHECK SYNC CONFIG BUILD TAGS TIME "$KBL_0"
    # Each mark is one column wide but ✓ and ✗ are three bytes, and printf pads
    # by bytes, so the mark is printed alone and the column padded after it.
    for name in "${order[@]}"; do
        printf '%-*s ' "$w" "$name"
        for stage in "${stages[@]}"; do
            case "${cell[$name:$stage]:-}" in
                ok)   printf ' %s%s%s     ' "$KBL_G" "$KBL_OK" "$KBL_0" ;;
                fail) printf ' %s%s%s     ' "$KBL_R" "$KBL_NO" "$KBL_0" ;;
                same) printf ' %s=%s     ' "$KBL_G" "$KBL_0"; same=1 ;;
                *)    printf ' %s-%s     ' "$KBL_D" "$KBL_0" ;;
            esac
        done
        t=${total[$name]}
        printf ' %dm%02ds\n' $((t / 60)) $((t % 60))
    done
    [[ $same -eq 1 ]] && printf '%s= already done at the newest commit, nothing changed since; skipped%s\n' "$KBL_D" "$KBL_0"
    return 0
}

_update_cleanup() {
    local p
    for p in $(jobs -p); do _kbl_kill_tree "$p"; done
    rm -rf -- "$_KBL_UPDATE_TMP"
}

_kbl_kill_tree() {   # _kbl_kill_tree PID
    local c
    for c in $(pgrep -P "$1" 2>/dev/null); do _kbl_kill_tree "$c"; done
    kill "$1" 2>/dev/null
    return 0
}

cmd_update() {
    local all=0 notags=0 t rc=0
    local -a names=() trees=() tagsopt=() later=()
    local -A pid=() seen=()
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --all) all=1; shift ;;
            --no-tags) notags=1; shift ;;
            --no-clangd|--no-warm|--fresh) tagsopt+=("$1"); shift ;;
            -*) die "update: unknown option '$1'" ;;
            *) names+=("$1"); shift ;;
        esac
    done
    if [[ $all -eq 1 ]]; then
        mapfile -t names < <(kbl_trees)
        [[ ${#names[@]} -gt 0 ]] || die "update: no trees are registered"
    fi
    if [[ ${#names[@]} -eq 0 ]]; then
        t="$(kbl_tree "")" || exit 1; trees=("$t")
    else
        for t in "${names[@]}"; do
            t="$(kbl_tree "$t")" || exit 1
            [[ -n "${seen[$t]+x}" ]] || { seen[$t]=1; trees+=("$t"); }
        done
    fi
    local tmp rec; tmp="$(mktemp -d)" || die "cannot create a temporary directory"
    _KBL_UPDATE_TMP="$tmp"; rec="$tmp/record"; : > "$rec"
    trap _update_cleanup EXIT
    _KBL_WARM_QUEUE="$tmp/warm.queue"; : > "$_KBL_WARM_QUEUE"
    _KBL_INDEX_QUEUE=()
    for t in "${trees[@]}"; do
        mkdir -p "$tmp/$(kbl_tree_id "$t")" || die "cannot write $tmp"
        _update_fetch "$t" "$tmp/$(kbl_tree_id "$t")" &
        pid[$t]=$!
    done
    for t in "${trees[@]}"; do
        _update_one "$t" "$rec" "$notags" "$tmp/$(kbl_tree_id "$t")" "${pid[$t]}" "${tagsopt[@]+"${tagsopt[@]}"}"
        case $? in 0) ;; 2) later+=("$t") ;; *) rc=1 ;; esac
    done
    for t in "${later[@]+"${later[@]}"}"; do
        _update_one "$t" "$rec" "$notags" "$tmp/$(kbl_tree_id "$t")" "" "${tagsopt[@]+"${tagsopt[@]}"}" || rc=1
    done
    _update_index "$rec" "${tagsopt[@]+"${tagsopt[@]}"}" || rc=1
    _update_warm "$rec" "$_KBL_WARM_QUEUE"
    _update_summary "$rec"
    return $rc
}
