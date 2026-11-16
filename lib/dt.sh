_DT_ACTIONS="show edit apply rebase detach reset diff probe check"
_DT_STAGES="machine firmware base custom booted"
_DT_OWN="the machine takes the size of its memory and the number of its CPUs from the tree, and qemu is asked
       for the tree it then makes: whatever else the two state differently is hardware qemu has no machine
       for; a tree of its own adds to /chosen, /aliases and /reserved-memory"
_DT_ROOM="what a tree sets aside lies in the machine's memory, clear of what is set aside or loaded there already"

_dt_rule() { # _dt_rule LINES
    grep -q '^[^!]' <<<"$1" && printf '       %s\n' "$_DT_OWN"
    grep -q '^!' <<<"$1" && printf '       %s\n' "$_DT_ROOM"
    return 0
}

_dt_fdt() { python3 "${KBL_REPO}/lib/fdt.py" "$@"; }

_dt_need() { # _dt_need PROGRAM...
    local p
    for p in "$@"; do
        command -v "$p" >/dev/null 2>&1 || die "dt: $p not found$(kbl_pkg_hint dtc)"
    done
}

_dt_is_tree() { # _dt_is_tree WORD
    [[ -d "$1" ]] && return 0
    [[ ! -e "$1" && -n "$(kbl_trees_by_id "$1")" ]]
}

_dt_context() { # _dt_context TREE BOOT BOARD
    local tree="$1" boot="$2" board="$3" arch bconf
    arch="$(kbl_tree_arch "$tree")" || exit 1
    if [[ -n "$board" ]]; then
        [[ -z "$boot" || "$boot" == direct ]] || die "dt: --boot $boot contradicts --board $board (a board boots directly)"
        bconf="$KBL_REPO/boards/$board.conf"
        [[ "$board" =~ ^[A-Za-z0-9._-]+$ && -f "$bconf" ]] || die "dt: no board '$board' in $KBL_REPO/boards"
        [[ "$(kbl_conf_get "$bconf" ARCH)" == "$arch" ]] \
            || die "dt: board $board is for $(kbl_conf_get "$bconf" ARCH), and $(kbl_tree_label "$tree") is $arch"
        boot=direct
        _dt_ctx="board-$board"
    else
        [[ -n "$boot" ]] || boot="$(kbl_tree_get "$tree" BOOT)"
        boot="${boot:-direct}"
        case "$boot" in
            direct | uboot | uefi) ;;
            *) die "dt: unknown boot mode '$boot' (direct, uboot, uefi)" ;;
        esac
        _dt_ctx="$boot"
    fi
    _dt_tree="$tree"
    _dt_boot="$boot"
    _dt_board="$board"
    _dt_dir="$(kbl_tree_data "$tree")/dt/$_dt_ctx"
    _dt_src="$(kbl_tree_get "$tree" DT_DIR)"
    if [[ -n "$_dt_src" ]]; then
        _dt_src="$(kbl_tree_path "$tree" "$_dt_src")/$_dt_ctx.dts"
    else _dt_src="$_dt_dir/custom.dts"; fi
    _dt_base=machine
    [[ "$boot" == uboot ]] && _dt_base=firmware
    return 0
}

_dt_lock() {
    [[ "${_dt_locked:-}" == "$_dt_dir" ]] && return 0
    command -v flock >/dev/null 2>&1 || die "flock (util-linux) not found; it keeps dt commands apart"
    { mkdir -p "$_dt_dir" && exec {_dt_lock_fd}>>"$_dt_dir/.lock"; } || die "dt: cannot write $_dt_dir"
    flock "$_dt_lock_fd" || die "dt: cannot lock $_dt_dir/.lock"
    _dt_locked="$_dt_dir"
}

_dt_unlock() {
    [[ -n "${_dt_locked:-}" ]] || return 0
    exec {_dt_lock_fd}>&-
    _dt_locked=""
}

_dt_machine() {
    if [[ -n "${_dt_want+x}" ]]; then
        printf '%s' "$_dt_want"
    elif [[ -f "$_dt_dir/attached" && -f "$_dt_dir/custom.machine" ]]; then
        cat "$_dt_dir/custom.machine"
    fi
}

_dt_inputs() {
    local q ub
    q="$(kbl_tree_get "$_dt_tree" QEMU_BIN)"
    q="${q:-$(kbl_qemu "$(kbl_tree_arch "$_dt_tree")")}"
    printf 'conf %s\n' "$(cat "$_dt_tree/tree.conf" ${_dt_board:+"$KBL_REPO/boards/$_dt_board.conf"} 2>/dev/null \
        | sha1sum | cut -c1-40)"
    printf 'qemu %s %s\n' "$(command -v "$q")" "$("$q" --version 2>/dev/null | head -1)"
    if [[ "$_dt_boot" == uboot ]]; then
        ub="$( (
            kbl_tree_firmware "$_dt_tree"
            printf '%s\n' "$_fw_image"
        ))" || return 1
        printf 'firmware %s %s\n' "$ub" "$(stat -L -c '%s %Y' "$ub" 2>/dev/null)"
    fi
    printf 'launcher %s\n' "$(sha1sum <"$KBL_REPO/lib/run-qemu.sh" | cut -c1-40)"
    printf 'machine %s\n' "$(_dt_machine)"
}

_dt_decompile() { # _dt_decompile BLOB SOURCE
    local err
    err="$(dtc -I dtb -O dts -o "$2.new" "$1" 2>&1 >/dev/null)" \
        || {
            rm -f "$2.new"
            die "dt: dtc cannot read $1: $(tail -1 <<<"$err")"
        }
    mv -f "$2.new" "$2" || die "dt: cannot write $2"
}

_dt_probe() {
    local tmp rc s inputs
    local -a args=("$_dt_tree")
    _dt_need dtc
    inputs="$(_dt_inputs)" || exit 1
    mkdir -p "$_dt_dir" || die "dt: cannot create $_dt_dir"
    tmp="$(mktemp -d -p "$_dt_dir" .probe.XXXXXX)" || die "dt: cannot write $_dt_dir"
    if [[ -n "$_dt_board" ]]; then args+=(--board "$_dt_board"); else args+=(--boot "$_dt_boot"); fi
    s="$(_dt_machine | sed -n 's/^mem //p' | head -1)"
    [[ -z "$s" ]] || args+=(--mem "$s")
    s="$(_dt_machine | sed -n 's/^smp //p' | head -1)"
    [[ -z "$s" ]] || args+=(--smp "$s")
    say "asking       the machine$([[ "$_dt_boot" == uboot ]] && echo ' and its firmware') for the device tree" >&2
    KBL_DT_PROBE="$tmp" "$KBL_REPO/lib/run-qemu.sh" "${args[@]}" </dev/null >"$tmp/log" 2>&1 {_dt_lock_fd}>&-
    rc=$?
    if [[ $rc -eq 3 ]]; then
        rm -f "$_dt_dir"/machine.dt[bs] "$_dt_dir"/firmware.dt[bs]
        sed 's/\x1b\[[0-9;]*[A-Za-z]//g' "$tmp/log" | grep . | tail -2 >"$_dt_dir/none.reason"
        rm -rf -- "$tmp"
        printf 'none\n%s\n' "$inputs" >"$_dt_dir/base.inputs"
        return 3
    fi
    if [[ $rc -ne 0 || ! -s "$tmp/machine.dtb" || ("$_dt_boot" == uboot && ! -s "$tmp/firmware.dtb") ]]; then
        sed 's/\x1b\[[0-9;]*[A-Za-z]//g; s/^/       /' "$tmp/log" | tail -15 >&2
        if [[ $rc -eq 4 ]]; then
            cp -f "$tmp/console.log" "$_dt_dir/probe.console.log" 2>/dev/null
            rm -rf -- "$tmp"
            die "dt: the firmware did not hand over its device tree in time (above)
       its console is kept in $_dt_dir/probe.console.log"
        fi
        rm -rf -- "$tmp"
        die "dt: the machine did not hand over its device tree (above)"
    fi
    for s in machine firmware; do
        [[ -s "$tmp/$s.dtb" ]] || continue
        _dt_fdt check "$tmp/$s.dtb" || {
            rm -rf -- "$tmp"
            die "dt: what the $s handed over is not a device tree"
        }
        mv -f "$tmp/$s.dtb" "$_dt_dir/$s.dtb" || die "dt: cannot write $_dt_dir/$s.dtb"
        _dt_decompile "$_dt_dir/$s.dtb" "$_dt_dir/$s.dts"
    done
    rm -rf -- "$tmp"
    printf 'tree\n%s\n' "$inputs" >"$_dt_dir/base.inputs"
    return 0
}

_dt_ensure_base() {
    local have
    if [[ "$_dt_boot" == uefi ]]; then
        mkdir -p "$_dt_dir" && echo "kbuildlab hands a uefi boot no device tree; its firmware describes the machine" >"$_dt_dir/none.reason"
        return 3
    fi
    local now
    now="$(_dt_inputs)" || exit 1
    have="$(cat "$_dt_dir/base.inputs" 2>/dev/null)"
    if [[ -n "$have" && "$(tail -n +2 <<<"$have")" == "$now" ]]; then
        [[ "$(head -1 <<<"$have")" == none ]] && return 3
        [[ -f "$_dt_dir/$_dt_base.dtb" ]] && return 0
    fi
    _dt_lock
    _dt_probe
}

_dt_why_none() { tr '\n' ' ' <"$_dt_dir/none.reason" 2>/dev/null; }

_dt_no_tree() {
    die "dt: $(kbl_tree_label "$_dt_tree") has no device tree in a $_dt_ctx boot: $(_dt_why_none)"
}

_dt_file() { # _dt_file STAGE-OR-FILE
    case "$1" in
        base) printf '%s\n' "$_dt_dir/$_dt_base.dtb" ;;
        machine | firmware | custom | booted) printf '%s\n' "$_dt_dir/$1.dtb" ;;
        *)
            [[ -f "$1" ]] || die "dt: '$1' is neither a stage ($_DT_STAGES) nor a file"
            _dt_fdt check "$1" 2>/dev/null || die "dt: $1 is not a device tree blob"
            realpath -- "$1"
            ;;
    esac
}

_dt_have() { # _dt_have STAGE-OR-FILE
    local f
    f="$(_dt_file "$1")" || exit 1
    [[ -f "$f" ]] && {
        printf '%s\n' "$f"
        return 0
    }
    case "$1" in
        custom) die "dt: $(kbl_tree_label "$_dt_tree") has no device tree of its own for the $_dt_ctx boot
       kbuildlab dt edit $(kbl_tree_label "$_dt_tree") makes one" ;;
        booted) die "dt: no boot of $(kbl_tree_label "$_dt_tree") with its own device tree has been captured yet" ;;
        firmware) die "dt: a $_dt_ctx boot has no firmware stage; its base is the machine's tree" ;;
        *) die "dt: no $1 tree in $_dt_dir (kbuildlab dt probe asks for it)" ;;
    esac
}

_dt_in_force() {
    if [[ -f "$_dt_dir/attached" && -f "$_dt_dir/custom.dtb" ]]; then echo custom; else echo "$_dt_base"; fi
}

_dt_kernel_include_dirs() { # _dt_kernel_include_dirs SRC
    local src="$1" rec d
    rec="$(find "$src/arch" -name '.*.dtb.cmd' -print -quit 2>/dev/null)"
    [[ -n "$rec" ]] || return 0
    grep -oE -- '-I ?[^ ]+' "$rec" | sed -E 's/^-I ?//' | awk '!seen[$0]++' | while IFS= read -r d; do
        [[ "$d" == /* ]] || d="$src/$d"
        [[ -d "$d" ]] && printf '%s\n' "$d"
    done
    return 0
}

_dt_compile() { # _dt_compile SOURCE OUT LOG INCLUDE-DIR [DTC-OPTION...]
    local src="$1" out="$2" log="$3" inc="$4" in="$1" d rc
    shift 4
    local -a cppi=() dtci=()
    : >"$log"
    [[ -n "$inc" ]] && {
        cppi+=(-I "$inc")
        dtci+=(-i "$inc")
    }
    if grep -Eq '^[[:space:]]*#[[:space:]]*(include|define|if|ifdef|ifndef)([[:space:]]|$)' "$src"; then
        command -v cpp >/dev/null 2>&1 || die "dt: $src uses the C preprocessor and cpp is not installed"
        while IFS= read -r d; do
            cppi+=(-I "$d")
            dtci+=(-i "$d")
        done \
            < <(_dt_kernel_include_dirs "$(kbl_tree_src "$_dt_tree")")
        in="$out.pp"
        cpp -nostdinc -undef -D__DTS__ -x assembler-with-cpp "${cppi[@]+"${cppi[@]}"}" -o "$in" "$src" 2>>"$log" \
            || {
                rm -f "$in"
                return 1
            }
    fi
    dtc -I dts -O dtb "$@" "${dtci[@]+"${dtci[@]}"}" -o "$out" "$in" 2>>"$log"
    rc=$?
    [[ "$in" == "$src" ]] || rm -f "$in"
    [[ $rc -eq 0 ]] || rm -f "$out"
    return $rc
}

_dt_kind() { # _dt_kind FILE
    local k
    if k="$(_dt_fdt kind "$1" 2>/dev/null)"; then
        printf '%s\n' "$k"
        return 0
    fi
    if [[ "$(head -c 4 "$1" | od -An -tx1 | tr -d ' \n')" == d00dfeed ]]; then
        die "dt apply: $1 begins like a device tree blob and is not a well-formed one"
    fi
    grep -Iq . "$1" || die "dt apply: $1 is neither a device tree blob nor a source text"
    if grep -Eq '^[[:space:]]*/plugin/[[:space:]]*;' "$1"; then echo overlay-source; else echo source; fi
}

_dt_failed() { # _dt_failed LOG WHAT
    sed 's/^/       /' "$1" >&2
    die "dt: $2 (above); nothing was attached"
}

_dt_backup_source() {
    [[ -f "$_dt_src" ]] || return 0
    cp -f "$_dt_src" "$_dt_src~" || die "dt: cannot write $_dt_src~"
}

_dt_apply() { # _dt_apply [FILE]
    local file="${1:-}" src="$_dt_src" out="$_dt_dir/custom.dtb" new="$_dt_dir/.custom.new.dtb"
    local log="$_dt_dir/custom.log" kind="" inc="" onto ovl n label lost take="" want hint
    label="$(kbl_tree_label "$_dt_tree")"
    _dt_need dtc
    [[ "$_dt_boot" != uefi ]] || {
        _dt_ensure_base
        _dt_no_tree
    }
    _dt_lock
    mkdir -p "$(dirname "$src")" || die "dt: cannot create $(dirname "$src") (DT_DIR in $_dt_tree/tree.conf)"
    rm -f "$new"
    if [[ -n "$file" ]]; then
        [[ -f "$file" && -r "$file" ]] || die "dt apply: no readable file '$file'"
        [[ -s "$file" ]] || die "dt apply: $file is empty"
        file="$(realpath -- "$file")"
        kind="$(_dt_kind "$file")" || exit 1
        case "$kind" in
            tree)
                cp -f "$file" "$new" || die "dt: cannot write $new"
                take=decompile
                ;;
            overlay | overlay-source)
                _dt_need fdtoverlay
                _dt_ensure_base || _dt_no_tree
                onto="$_dt_dir/$_dt_base.dtb"
                if [[ -f "$src" ]]; then
                    [[ -f "$out" && "$(sed -n 's/^source //p' "$_dt_dir/custom.id" 2>/dev/null)" == "$(sha256sum <"$src" | cut -c1-64)" ]] \
                        || die "dt apply: $src is not what $out was built from, so an overlay has nothing sound to go on
       kbuildlab dt apply $label builds the source first; kbuildlab dt reset $label starts from the $_dt_base's tree"
                    onto="$out"
                fi
                ovl="$file"
                if [[ "$kind" == overlay-source ]]; then
                    ovl="$_dt_dir/.overlay.dtbo"
                    _dt_compile "$file" "$ovl" "$log" "$(dirname "$file")" -@ \
                        || {
                            rm -f "$ovl"
                            _dt_failed "$log" "$file does not compile"
                        }
                fi
                fdtoverlay -i "$onto" -o "$new" "$ovl" >"$log" 2>&1 \
                    || {
                        rm -f "$new" "$_dt_dir/.overlay.dtbo"
                        _dt_failed "$log" "the overlay $file does not apply to $onto"
                    }
                rm -f "$_dt_dir/.overlay.dtbo"
                _dt_fdt check "$new" || {
                    rm -f "$new"
                    die "dt: applying $file left no well-formed tree"
                }
                take=decompile
                ;;
            source)
                if [[ "$file" != "$(realpath -m -- "$src")" ]]; then
                    inc="$(dirname "$file")"
                    _dt_compile "$file" "$new" "$log" "$inc" || _dt_failed "$log" "$file does not compile"
                    take="$file"
                fi
                ;;
        esac
    fi
    if [[ ! -f "$new" ]]; then
        [[ -f "$src" ]] || die "dt apply: $label has no device tree source of its own for the $_dt_ctx boot
       kbuildlab dt edit $label makes one from the $_dt_base's, or name a file: kbuildlab dt apply $label FILE"
        inc="$(sed -n 's/^include //p' "$_dt_dir/custom.id" 2>/dev/null | head -1)"
        _dt_compile "$src" "$new" "$log" "$inc" || _dt_failed "$log" "$src does not compile"
    fi
    _dt_fdt check "$new" || {
        rm -f "$new"
        die "dt: the build left no well-formed tree"
    }
    want="$(_dt_asks "$new")" || {
        rm -f "$new"
        die "dt: ${file:-$src} cannot be held against a machine of qemu's (above)"
    }
    _dt_want="$want"
    (_dt_ensure_base)
    case $? in
        0) ;;
        3)
            rm -f "$new"
            _dt_no_tree
            ;;
        *)
            rm -f "$new"
            exit 1
            ;;
    esac
    lost="$(_dt_fdt agrees "$_dt_dir/$_dt_base.dtb" "$new")"
    case $? in
        0) ;;
        1)
            rm -f "$new"
            hint="$(_dt_rebase_hint "$label")"
            unset _dt_want
            die "dt: ${file:-$src} does not agree with the machine qemu makes${want:+ for $(tr '\n' ' ' <<<"$want" | sed 's/ $//')}, in $(wc -l <<<"$lost") place(s):
$(head -12 <<<"$lost" | sed 's/^/         /')
$(_dt_rule "$lost")
${hint:+$hint$'\n'}       nothing was built or attached"
            ;;
        *)
            rm -f "$new"
            unset _dt_want
            die "dt: cannot hold ${file:-$src} against the $_dt_base's tree"
            ;;
    esac
    case "$take" in
        "") ;;
        decompile)
            _dt_backup_source
            _dt_decompile "$new" "$src"
            ;;
        *)
            _dt_backup_source
            cp -f "$take" "$src" || die "dt: cannot write $src"
            ;;
    esac
    mv -f "$new" "$out" || die "dt: cannot write $out"
    {
        printf 'built %s\n' "$(date '+%Y-%m-%d %H:%M:%S')"
        printf 'source %s\n' "$(sha256sum <"$src" | cut -c1-64)"
        [[ -n "$file" ]] && printf 'from %s\n' "${_dt_from:-$kind $file}"
        [[ -n "$inc" ]] && printf 'include %s\n' "$inc"
        printf 'base %s\n' "$_dt_base"
    } >"$_dt_dir/custom.id"
    printf '%s' "${want:+$want$'\n'}" >"$_dt_dir/custom.machine"
    cp -f "$_dt_dir/$_dt_base.dtb" "$_dt_dir/custom.base.dtb" || die "dt: cannot write $_dt_dir/custom.base.dtb"
    date '+%Y-%m-%d %H:%M:%S' >"$_dt_dir/attached"
    unset _dt_want
    n="$(grep -c 'Warning' "$log" 2>/dev/null)" || n=0
    ok "built        $out ($(stat -c%s "$out") bytes$([[ $n -gt 0 ]] && echo ", $n dtc warning(s) in $log"))"
    say "source       $src"
    [[ -z "$want" ]] || say "machine      $(tr '\n' ' ' <<<"$want" | sed 's/ $//'), as the tree states and tree.conf does not: qemu is started so"
    n="$(_dt_fdt diff "$_dt_dir/$_dt_base.dtb" "$out" | wc -l)"
    say "differs      in $n place(s) from the $_dt_base's (kbuildlab dt diff $label)"
    say "attached     $label boots on it from its next $_dt_ctx boot (kbuildlab dt detach $label undoes that)"
}

_dt_asks() { # _dt_asks BLOB: what of the machine BLOB states otherwise than tree.conf does
    local asked mem smp stated
    asked="$(_dt_fdt machine "$1")" || return 1
    mem="$(sed -n 's/^mem //p' <<<"$asked")"
    smp="$(sed -n 's/^smp //p' <<<"$asked")"
    stated="$(kbl_tree_get "$_dt_tree" MEM)"
    [[ "$(kbl_mem_bytes "$mem")" == "$(kbl_mem_bytes "${stated:-$KBL_DEFAULT_MEM}")" ]] || printf 'mem %s\n' "$mem"
    stated="$(kbl_tree_get "$_dt_tree" SMP)"
    stated="${stated:-$KBL_DEFAULT_SMP}"
    [[ "$smp" == "$(kbl_smp_count "$stated")" ]] || {
        kbl_smp_count "$stated" >/dev/null || die "dt: SMP in $_dt_tree/tree.conf is '$stated', which does not begin with the number of CPUs
       a device tree of the tree's own is held against that number: write it first, as in 4,sockets=2"
        printf 'smp %s\n' "$smp"
    }
    return 0
}

_dt_rebase_hint() { # _dt_rebase_hint LABEL
    [[ -f "$_dt_dir/custom.base.dtb" ]] || return 0
    [[ "$(_dt_fdt id "$_dt_dir/custom.base.dtb")" != "$(_dt_fdt id "$_dt_dir/$_dt_base.dtb")" ]] || return 0
    printf '       that machine is not the one the tree was written on: kbuildlab dt rebase %s writes the tree\n' "$1"
    printf '       again onto it, keeping what is the tree'"'"'s own\n'
}

_dt_rebase() { # _dt_rebase [FILE]
    local file="${1:-}" label cand="$_dt_dir/.rebase.own.dtb" old="$_dt_dir/.rebase.old.dtb" made="$_dt_dir/.rebase.made.dtb"
    local log="$_dt_dir/custom.log" kind want lost
    label="$(kbl_tree_label "$_dt_tree")"
    _dt_need dtc
    if [[ -n "$file" ]]; then
        [[ -f "$file" && -r "$file" && -s "$file" ]] || die "dt rebase: no readable file '$file'"
        file="$(realpath -- "$file")"
        kind="$(_dt_kind "$file")" || exit 1
    else
        file="$_dt_src"
        [[ -f "$file" ]] || die "dt rebase: $label has no device tree source of its own for the $_dt_ctx boot"
        kind=source
    fi
    [[ -f "$_dt_dir/custom.base.dtb" ]] || _dt_ensure_base || _dt_no_tree
    _dt_lock
    rm -f "$cand" "$old" "$made"
    case "$kind" in
        tree) cp -f "$file" "$cand" || die "dt: cannot write $cand" ;;
        source) _dt_compile "$file" "$cand" "$log" "$(dirname "$file")" || _dt_failed "$log" "$file does not compile" ;;
        *) die "dt rebase: $file is an overlay, which is applied onto a tree (kbuildlab dt apply $label $file)" ;;
    esac
    if [[ -f "$_dt_dir/custom.base.dtb" ]]; then
        cp -f "$_dt_dir/custom.base.dtb" "$old" || die "dt: cannot write $old"
    else
        cp -f "$_dt_dir/$_dt_base.dtb" "$old" || die "dt: cannot write $old"
    fi
    want="$(_dt_asks "$cand")" || {
        rm -f "$cand" "$old"
        die "dt: $file cannot be held against a machine of qemu's (above)"
    }
    _dt_want="$want"
    (_dt_ensure_base)
    case $? in
        0) ;;
        3)
            rm -f "$cand" "$old"
            _dt_no_tree
            ;;
        *)
            rm -f "$cand" "$old"
            exit 1
            ;;
    esac
    lost="$(_dt_fdt rebase "$old" "$cand" "$_dt_dir/$_dt_base.dtb" "$made")"
    case $? in
        0) ;;
        1)
            rm -f "$cand" "$old" "$made"
            unset _dt_want
            die "dt: $file cannot be written onto the machine qemu makes${want:+ for $(tr '\n' ' ' <<<"$want" | sed 's/ $//')}, for $(wc -l <<<"$lost") reason(s):
$(head -12 <<<"$lost" | sed 's/^/         /')
$(_dt_rule "$lost")
       nothing was written, built or attached"
            ;;
        *)
            rm -f "$cand" "$old" "$made"
            unset _dt_want
            die "dt: cannot write $file onto $_dt_dir/$_dt_base.dtb"
            ;;
    esac
    unset _dt_want
    say "rebased      $file onto the machine qemu makes${want:+ for $(tr '\n' ' ' <<<"$want" | sed 's/ $//')}: its hardware reads as that machine states it"
    rm -f "$cand" "$old"
    _dt_from="rebase $file"
    _dt_apply "$made"
    unset _dt_from
    rm -f "$made"
}

_dt_edit() {
    local src="$_dt_src" ed before label
    label="$(kbl_tree_label "$_dt_tree")"
    _dt_need dtc
    _dt_ensure_base || _dt_no_tree
    if [[ ! -f "$src" ]]; then
        mkdir -p "$(dirname "$src")" || die "dt: cannot create $(dirname "$src") (DT_DIR in $_dt_tree/tree.conf)"
        _dt_decompile "$_dt_dir/$_dt_base.dtb" "$src"
        cp -f "$_dt_dir/$_dt_base.dtb" "$_dt_dir/custom.base.dtb" || die "dt: cannot write $_dt_dir/custom.base.dtb"
        say "source       $src, made from the $_dt_base's tree"
    fi
    ed="${VISUAL:-${EDITOR:-}}"
    [[ -n "$ed" ]] || die "dt edit: neither \$VISUAL nor \$EDITOR is set; the source is $src
       kbuildlab dt apply $label builds and attaches it"
    before="$(sha256sum <"$src")"
    # shellcheck disable=SC2086
    $ed "$src" || die "dt edit: the editor failed; nothing was built"
    if [[ "$before" == "$(sha256sum <"$src")" && -f "$_dt_dir/attached" && -f "$_dt_dir/custom.dtb" &&
    "$(sed -n 's/^source //p' "$_dt_dir/custom.id" 2>/dev/null)" == "$(sha256sum <"$src" | cut -c1-64)" ]]; then
        say "unchanged    $src; $label still boots on $_dt_dir/custom.dtb"
        return 0
    fi
    _dt_apply
}

_dt_detach() {
    local label
    label="$(kbl_tree_label "$_dt_tree")"
    _dt_lock
    if [[ -f "$_dt_dir/attached" ]]; then
        rm -f "$_dt_dir/attached" || die "dt: cannot write $_dt_dir"
        ok "detached     $label boots on the $_dt_base's own tree again ($_dt_ctx boot)"
        [[ -f "$_dt_src" ]] && say "kept         $_dt_src and its build; kbuildlab dt apply $label attaches them again"
    else
        say "detached     nothing was attached to $label's $_dt_ctx boot"
    fi
    return 0
}

_dt_reset() { # _dt_reset YES
    local label f n=0
    label="$(kbl_tree_label "$_dt_tree")"
    _dt_lock
    for f in "$_dt_src" "$_dt_src~" "$_dt_dir"/custom.* "$_dt_dir"/booted.* "$_dt_dir/attached"; do
        [[ -e "$f" ]] && n=$((n + 1))
    done
    [[ $n -gt 0 ]] || {
        say "reset        $label has no device tree of its own for the $_dt_ctx boot"
        return 0
    }
    _kbl_confirm "$1" "delete $label's own device tree source, $_dt_src, and its builds in $_dt_dir?" || die "dt reset: cancelled"
    rm -f "$_dt_src" "$_dt_src~" "$_dt_dir"/custom.* "$_dt_dir"/booted.* "$_dt_dir/attached" \
        || die "dt: cannot write $_dt_dir or $_dt_src"
    ok "reset        $label boots on the $_dt_base's own tree; the next dt edit starts from it"
}

_dt_diff() { # _dt_diff [A [B]]
    local a="${1:-base}" b="${2:-custom}" fa fb rc
    _dt_ensure_base || _dt_no_tree
    fa="$(_dt_have "$a")" || exit 1
    fb="$(_dt_have "$b")" || exit 1
    _dt_fdt diff "$fa" "$fb"
    rc=$?
    [[ $rc -le 1 ]] || die "dt diff: cannot compare $fa and $fb"
    [[ $rc -eq 1 ]] || say "same         $a and $b describe the same tree"
    return 0
}

_dt_stage_line() { # _dt_stage_line STAGE NOTE
    local f="$_dt_dir/$1.dtb"
    [[ -f "$f" ]] || return 0
    say "$(printf '%-12s' "$1") ${f%.dtb}.dts, .dtb  $(date -r "$f" '+%Y-%m-%d %H:%M')${2:+  $2}"
}

_dt_show() { # _dt_show STAGE SOURCE JSON
    local stage="$1" source="$2" json="$3" label force f rc=0 note fa src bdtb
    label="$(kbl_tree_label "$_dt_tree")"
    _dt_ensure_base || rc=$?
    if [[ $rc -eq 3 ]]; then
        if [[ $json -eq 1 ]]; then
            printf '{"tree": "%s", "boot": "%s", "device_tree": null}\n' "$label" "$_dt_ctx"
        else say "device tree  none for $label in a $_dt_ctx boot: $(_dt_why_none)"; fi
        return 0
    fi
    force="$(_dt_in_force)"
    f="$(_dt_have "${stage:-$force}")" || exit 1
    if [[ $source -eq 1 ]]; then
        [[ -f "${f%.dtb}.dts" && ! "$f" -nt "${f%.dtb}.dts" ]] || _dt_decompile "$f" "${f%.dtb}.dts"
        cat "${f%.dtb}.dts"
        return 0
    fi
    if [[ $json -eq 1 ]]; then
        python3 - "$label" "$_dt_ctx" "$force" "$_dt_dir" "$f" "${KBL_REPO}/lib" "$_dt_src" <<'EOF'
import json, os, sys
label, ctx, force, d, f, lib, source = sys.argv[1:]
sys.path.insert(0, lib)
import fdt
stages = {s: os.path.join(d, s + ".dtb") for s in ("machine", "firmware", "custom", "booted")
          if os.path.isfile(os.path.join(d, s + ".dtb"))}
print(json.dumps({"tree": label, "boot": ctx, "in_force": force, "directory": d, "source": source,
                  "stages": stages, "shown": f, "structure": fdt.summary(fdt.load(f))}, indent=1))
EOF
        return 0
    fi
    head1 "$label  device tree, $_dt_ctx boot"
    if [[ "$force" == custom ]]; then
        if [[ "$_dt_boot" == uboot ]]; then
            fa="$(kbl_tree_get "$_dt_tree" UBOOT_FDTADDR)"
            say "in force     its own: the firmware loads $_dt_dir/custom.dtb from the boot partition${fa:+ to $fa (UBOOT_FDTADDR)}"
        else say "in force     its own: QEMU hands the kernel $_dt_dir/custom.dtb"; fi
    elif [[ "$_dt_boot" == uboot ]]; then
        say "in force     the firmware's: it hands the kernel the tree QEMU generated, as it holds it; nothing is attached"
    else
        say "in force     the machine's: QEMU hands the kernel the tree it holds; nothing is attached"
    fi
    _dt_stage_line machine "as QEMU holds it"
    _dt_stage_line firmware "as the firmware holds it when it starts the kernel"
    if [[ -f "$_dt_src" ]]; then
        note="not attached"
        [[ -f "$_dt_dir/attached" ]] && note="attached $(cat "$_dt_dir/attached")"
        if [[ ! -f "$_dt_dir/custom.dtb" ]]; then
            say "custom       $_dt_src  not built (kbuildlab dt apply $label)"
        else
            [[ "$(sed -n 's/^source //p' "$_dt_dir/custom.id" 2>/dev/null)" == "$(sha256sum <"$_dt_src" | cut -c1-64)" ]] \
                || note="$note; the source changed since this build (kbuildlab dt apply $label)"
            _dt_fdt agrees "$_dt_dir/$_dt_base.dtb" "$_dt_dir/custom.dtb" >/dev/null 2>&1 \
                || note="$note; it does not agree with the machine as it is now (kbuildlab dt check $label)"
            say "custom       $_dt_src, built as $_dt_dir/custom.dtb  $(date -r "$_dt_dir/custom.dtb" '+%Y-%m-%d %H:%M')  $note"
            note="$(tr '\n' ' ' <"$_dt_dir/custom.machine" 2>/dev/null | sed 's/ $//')"
            [[ -z "$note" ]] || say "asks for     $note, as the tree states and tree.conf does not: qemu is started so while it is attached"
        fi
    fi
    if [[ -f "$_dt_dir/booted.dtb" ]]; then
        [[ -f "$_dt_dir/booted.dts" && ! "$_dt_dir/booted.dtb" -nt "$_dt_dir/booted.dts" ]] \
            || _dt_decompile "$_dt_dir/booted.dtb" "$_dt_dir/booted.dts"
        note="as the kernel was handed it"
        [[ -f "$_dt_dir/custom.dtb" ]] \
            && note="$note; $(_dt_fdt diff "$_dt_dir/custom.dtb" "$_dt_dir/booted.dtb" | wc -l) place(s) differ from custom (kbuildlab dt diff $label custom booted)"
        _dt_stage_line booted "$note"
    fi
    if [[ -n "$_dt_board" ]]; then
        bdtb="$(kbl_conf_get "$KBL_REPO/boards/$_dt_board.conf" DTB)"
        src="$(kbl_tree_src "$_dt_tree")"
        [[ -n "$bdtb" && "$bdtb" != /* ]] && bdtb="$src/$bdtb"
        [[ -n "$bdtb" && -f "${bdtb%.dtb}.dts" ]] && say "kernel       ${bdtb%.dtb}.dts, which the kernel build compiles for $_dt_board"
    fi
    say "structure    of ${stage:-$force}, $f"
    _dt_fdt summary "$f"
}

_dt_check() {
    local label rc=0 n f="$_dt_dir/custom.dtb"
    label="$(kbl_tree_label "$_dt_tree")"
    head1 "$label  device tree, $_dt_ctx boot"
    _dt_ensure_base || {
        say "device tree  none: $(_dt_why_none)"
        return 0
    }
    for n in machine firmware; do
        [[ -f "$_dt_dir/$n.dtb" ]] || continue
        if _dt_fdt check "$_dt_dir/$n.dtb" 2>/dev/null; then
            ok "$(printf '%-12s' "$n") $_dt_dir/$n.dtb is well formed and current"
        else
            bad "$(printf '%-12s' "$n") $_dt_dir/$n.dtb is not a well-formed tree (kbuildlab dt probe $label)"
            rc=1
        fi
    done
    if [[ ! -f "$_dt_src" && ! -f "$f" ]]; then
        ok "custom       none: $label boots on the $_dt_base's own tree"
        [[ ! -f "$_dt_dir/attached" ]] || {
            bad "attached     a tree is marked attached and there is none (kbuildlab dt detach $label)"
            rc=1
        }
        return $rc
    fi
    if [[ ! -f "$f" ]]; then
        bad "custom       $_dt_src is not built (kbuildlab dt apply $label)"
        return 1
    fi
    if _dt_fdt check "$f" 2>/dev/null; then
        ok "custom       $f is well formed"
    else
        bad "custom       $f is not a well-formed tree (kbuildlab dt apply $label)"
        rc=1
    fi
    if [[ "$(sed -n 's/^source //p' "$_dt_dir/custom.id" 2>/dev/null)" == "$(sha256sum <"$_dt_src" 2>/dev/null | cut -c1-64)" ]]; then
        ok "source       $_dt_src is what custom.dtb was built from"
    else
        bad "source       $_dt_src changed since custom.dtb was built (kbuildlab dt apply $label)"
        rc=1
    fi
    if n="$(_dt_fdt agrees "$_dt_dir/$_dt_base.dtb" "$f")"; then
        ok "agrees       it states the machine's hardware as the $_dt_base hands it on, and sets aside only memory it has"
    else
        bad "agrees       it does not agree with the machine as it is now:"
        head -8 <<<"$n" | sed 's/^/       /'
        rc=1
    fi
    n="$(_dt_fdt machine "$f" 2>/dev/null)"
    if [[ -n "$n" && "$n" == "$(_dt_fdt machine "$_dt_dir/machine.dtb" 2>/dev/null)" && "$(_dt_asks "$f")" == "$(cat "$_dt_dir/custom.machine" 2>/dev/null)" ]]; then
        ok "qemu         is started with what the tree states: $(tr '\n' ' ' <<<"$n" | sed 's/ $//; s/mem \([^ ]*\)/\1 of memory,/; s/smp \([^ ]*\)/\1 CPU(s)/')"
    else
        bad "qemu         is started with another machine than the tree states (kbuildlab dt apply $label)"
        rc=1
    fi
    if [[ -f "$_dt_dir/attached" ]]; then
        ok "attached     since $(cat "$_dt_dir/attached")"
    else say "attached     no: $label boots on the $_dt_base's own tree"; fi
    if [[ -s "$_dt_dir/booted.lost" ]]; then
        bad "booted       the last boot did not hand the tree on as it is:"
        head -8 "$_dt_dir/booted.lost" | sed 's/^/       /'
        rc=1
    elif [[ -f "$_dt_dir/booted.dtb" ]]; then
        if n="$(_dt_fdt carried "$f" "$_dt_dir/booted.dtb")"; then
            ok "booted       the last boot handed the kernel this tree as it is ($(sed -n 's/^captured //p' "$_dt_dir/booted.id" 2>/dev/null))"
        else
            bad "booted       what the last boot handed the kernel is not this build:"
            head -8 <<<"$n" | sed 's/^/       /'
            rc=1
        fi
    else
        say "booted       not yet: no boot with this tree has been captured"
    fi
    return $rc
}

cmd_dt() {
    local action=show boot="" board="" stage="" source=0 json=0 yes=0 tree=""
    local -a words=()
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --boot)
                boot="${2:?--boot needs a mode}"
                shift 2
                ;;
            --board)
                board="${2:?--board needs a name}"
                shift 2
                ;;
            --stage)
                stage="${2:?--stage needs a name}"
                shift 2
                ;;
            --source)
                source=1
                shift
                ;;
            --json)
                json=1
                shift
                ;;
            --yes | -y)
                yes=1
                shift
                ;;
            -*) die "dt: unknown option '$1'" ;;
            *)
                words+=("$1")
                shift
                ;;
        esac
    done
    if [[ ${#words[@]} -gt 0 && " $_DT_ACTIONS " == *" ${words[0]} "* ]]; then
        action="${words[0]}"
        words=("${words[@]:1}")
    fi
    if [[ ${#words[@]} -gt 0 ]] && _dt_is_tree "${words[0]}"; then
        tree="${words[0]}"
        words=("${words[@]:1}")
    fi
    case "$action" in
        show | detach | reset | probe | edit | check)
            [[ ${#words[@]} -eq 0 ]] || die "dt $action: '${words[0]}' is neither a registered tree nor expected here"
            ;;
        apply | rebase)
            [[ ${#words[@]} -le 1 ]] || die "dt $action: one file at a time (kbuildlab dt $action [TREE] [FILE])"
            ;;
        diff)
            [[ ${#words[@]} -le 2 ]] || die "dt diff: two stages or blobs at most (stages: $_DT_STAGES)"
            ;;
    esac
    tree="$(kbl_tree "$tree")" || exit 1
    _dt_context "$tree" "$boot" "$board"
    [[ "$action" == show || (-z "$stage" && $source -eq 0 && $json -eq 0) ]] \
        || die "dt $action: --stage, --source and --json belong to dt show"
    [[ "$action" == reset || $yes -eq 0 ]] || die "dt $action: --yes belongs to dt reset"
    if [[ -n "$stage" ]]; then
        [[ " $_DT_STAGES " == *" $stage "* ]] || die "dt: no stage '$stage' (stages: $_DT_STAGES)"
    fi
    case "$action" in
        show) _dt_show "$stage" "$source" "$json" ;;
        edit) _dt_edit ;;
        apply) _dt_apply "${words[@]+"${words[@]}"}" ;;
        rebase) _dt_rebase "${words[@]+"${words[@]}"}" ;;
        detach) _dt_detach ;;
        reset) _dt_reset "$yes" ;;
        diff) _dt_diff "${words[@]+"${words[@]}"}" ;;
        check) _dt_check ;;
        probe)
            [[ "$_dt_boot" != uefi ]] || {
                _dt_ensure_base
                _dt_no_tree
            }
            _dt_lock
            if _dt_probe; then
                ok "asked        $_dt_dir holds what the $_dt_base hands on"
            else say "device tree  none for a $_dt_ctx boot: $(_dt_why_none)"; fi
            ;;
    esac
}
