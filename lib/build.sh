# Building a tree: the container a build runs in and its toolchain, then sync,
# config, build and module staging.  Sourced by bin/kbuildlab after lib/common.sh.

# ---------------------------------------------------------------- helpers
# The command prefix that runs a program in the active image with WORKDIR as
# its working directory, left in _KBL_RUN; RUN-OPTIONs go to `run` itself.
_container_run() {   # _container_run WORKDIR [RUN-OPTION...]
    local c wd img p q; c="$(kbl_container)"; wd="$1"; shift
    # A tree that pins IMAGE= builds in that one; everything else in $KBL_IMAGE.
    # The caller sets _KBL_ACTIVE_IMAGE because only a caller holding a tree can
    # know which, and _in_container is also called with no tree in hand.
    img="${_KBL_ACTIVE_IMAGE:-$KBL_IMAGE}"
    "$c" image inspect "$img" >/dev/null 2>&1 \
        || die "container image $img is missing. Build it: kbuildlab image"
    # Run as the host user so build artifacts aren't root-owned; HOME to a
    # writable path (mapped uid has no passwd entry).  Rootless podman already maps
    # the invoking user onto the container's root, so --user there would put the
    # build on a subuid instead and the tree would come back owned by nobody.
    # KBL_DOCKER_USER states one explicitly; KBL_DOCKER_USER="" opts out entirely.
    local -a _idopt=()
    if [[ -z "${KBL_DOCKER_USER+x}" ]]; then
        if [[ "$(basename "$c")" != podman || "$(id -u)" -eq 0 ]]; then
            _idopt=(--user "$(id -u):$(id -g)" -e HOME=/tmp)
        fi
    elif [[ -n "${KBL_DOCKER_USER}" ]]; then
        _idopt=(--user "${KBL_DOCKER_USER}" -e HOME=/tmp)
    fi
    # What the container may write is what the build writes: the active tree's
    # source and data directory, and the working directory when it lies outside
    # both.  The tree's registry directory (its config-fragment) and this
    # repository (the presets) are read-only, and so is any object store the
    # source borrows through git alternates.  Each is mounted at the path it
    # resolves to and, when the caller spells it through a symlink, at that
    # spelling too, so every path handed in (-w, make arguments) names it there.
    # Only a writable mount must pass the system-directory guard.
    local -a rw=() ro=("${KBL_REPO}") mounts=() taken=()
    if [[ -n "${_KBL_ACTIVE_TREE:-}" ]]; then
        p="$(kbl_tree_src "$_KBL_ACTIVE_TREE")" || exit 1
        rw+=("$p")
        q="$(kbl_tree_data "$_KBL_ACTIVE_TREE")"; mkdir -p "$q" && rw+=("$q")
        ro+=("$_KBL_ACTIVE_TREE")
        if [[ -r "$p/.git/objects/info/alternates" ]]; then
            while IFS= read -r q; do [[ -d "$q" ]] && ro+=("$q"); done < "$p/.git/objects/info/alternates"
        fi
    fi
    rw+=("$wd")
    # A destination the caller mounts itself (the tags view overlays the source)
    # is not mounted a second time, under either spelling.
    local -a args=("$@"); local i
    for ((i = 0; i < ${#args[@]}; i++)); do
        [[ "${args[i]}" == -v ]] && { q="${args[i+1]#*:}"; taken+=("${q%%:*}"); }
    done
    _kbl_covered() {   # is $1 inside something already mounted?
        local m
        for m in "${taken[@]+"${taken[@]}"}"; do [[ "$1" == "$m" || "$1" == "$m"/* ]] && return 0; done
        return 1
    }
    _kbl_mount() {   # _kbl_mount PATH rw|ro
        local given real d opt=""
        given="$(realpath -m -s -- "$1")"
        if [[ "$2" == rw ]]; then
            real="$(kbl_assert_mountable "$1")" || exit 1
        else
            real="$(readlink -f -- "$1" 2>/dev/null)"; opt=":ro"
            [[ -n "$real" && "$real" != / ]] || die "'$1' does not resolve to a path that can be mounted"
        fi
        _kbl_covered "$given" && return 0
        for d in "$real" "$given"; do
            _kbl_covered "$d" && continue
            mounts+=(-v "$real:$d$opt"); taken+=("$d")
        done
    }
    for p in "${rw[@]}"; do _kbl_mount "$p" rw; done
    for p in "${ro[@]}"; do _kbl_mount "$p" ro; done
    _KBL_RUN=("$c" run --rm "$@" "${_idopt[@]+"${_idopt[@]}"}" "${mounts[@]}" -w "$wd" "$img")
}

_in_container() {   # _in_container WORKDIR CMD...
    local wd="$1"; shift
    _container_run "$wd" || exit 1
    "${_KBL_RUN[@]}" "$@"
}

_cross() {
    case "$1" in
        arm64|aarch64) echo "aarch64-linux-gnu-" ;;
        riscv64)       echo "riscv64-linux-gnu-" ;;
        x86_64)        echo "" ;;
    esac
}
_karch() { case "$1" in arm64|aarch64) echo arm64 ;; riscv64) echo riscv ;; x86_64) echo x86_64 ;; esac; }

# Which image, and which compiler inside it, a tree builds with.
#
# Two optional tree.conf keys: IMAGE names the container image, GCC names the
# compiler version within it.  They exist because "the newest toolchain" is not
# one answer across seven kernels.  gcc-10 turned -fno-common on by default and
# a pre-5.6 kernel does not link under it; mainline tracks compilers a stable
# series was never tested against.  So the ceiling belongs to the tree, and a
# tree that has a reason to pin one says so where the rest of its build is
# described rather than in an environment variable someone has to remember.
#
# Unset means the image default -- the unversioned gcc/g++ the image symlinks.
_tree_image() { local v; v="$(kbl_tree_get "$1" IMAGE)"; printf '%s\n' "${v:-$KBL_IMAGE}"; }
_tree_gcc()   { kbl_tree_get "$1" GCC; }

# The make arguments that select it, one per line, or nothing when the tree
# names no version.  Only the compiler is versioned: CROSS_COMPILE still
# supplies ld/as/objcopy, which the distribution ships in one version per
# target.  HOSTCC moves with CC so the host tools that parse the kernel's own
# output (fixdep, modpost, the BTF pass) are not a generation apart from it.
#
# HOSTCC_FLAGS is appended to HOSTCC rather than set as HOSTCFLAGS, because
# overriding HOSTCFLAGS on the command line replaces the kernel's own list
# (-O2, -std=, the warning set) instead of adding to it, and a host tool built
# without those is a second difference nobody asked for.  It exists for one
# real case: a kernel older than 5.6 has tentative definitions its host tools
# cannot link under gcc-10's -fno-common default, and `-fcommon` there raises
# the whole tree by several compiler generations while leaving the kernel's own
# code generation untouched -- which is the property that makes it acceptable.
# KCFLAGS is the kernel's own append-to-KBUILD_CFLAGS variable, so a value here
# lands after everything the tree's Makefile set and wins where the two conflict.
# That is the point: it is how a flag the kernel hard-codes gets undone without
# editing the kernel.  Keep it to flags that change only debug information --
# gcc guarantees those do not change code generation -- because anything that
# changes the objects belongs in the config, where the read-back check sees it.
# One such flag needs no tree to ask for it: a kernel whose Makefile turns off
# var-tracking-assignments, as older ones did for compilers long gone, gets it
# back, since it is what lets gdb say where a local lives after optimisation.
# The one exception to the rule above is also read off the Makefile: a kernel
# that never turns PIE off was written for compilers that did not default to
# it, and under one that does its arm64 modules cannot build at all
# (-mcmodel=large has no PIC form).  It gets the -fno-PIE later kernels give
# themselves, which puts back the code generation it was written for.
#
# One make variable is read off the tree the same way.  A kernel whose
# Kbuild.include escapes '#' in .cmd files as make 4.2 wanted has every command
# read back cut short by make 4.3 and later, so each build finds every command
# changed and compiles everything again.  Later kernels fixed make-cmd with a
# $(pound); given on the command line, that definition fixes this kernel too.
_cc_args() {   # _cc_args TREE ARCH
    local g pre hf kf mk; g="$(_tree_gcc "$1")"
    kf="$(kbl_tree_get "$1" KCFLAGS)"; hf="$(kbl_tree_get "$1" HOSTCC_FLAGS)"
    mk="$(kbl_tree_src "$1")/Makefile"
    if grep -q -- '-fno-var-tracking-assignments' "$mk" 2>/dev/null \
       && [[ " $kf " != *" -fvar-tracking-assignments "* ]]; then
        kf="${kf:+$kf }-fvar-tracking-assignments"
    fi
    if [[ -f "$mk" ]] && ! grep -q -- '-fno-PIE' "$mk" && [[ " $kf " != *" -fno-PIE "* ]]; then
        kf="${kf:+$kf }-fno-PIE"
    fi
    # Each key stands on its own.  HOSTCC_FLAGS in particular must not need GCC= to
    # take effect: a tree can want -fcommon on the image's own compiler without
    # naming a version, and a flag that is read, accepted and then dropped is worse
    # than one that is refused.
    if [[ -n "$g" ]]; then
        pre="$(_cross "$2")"
        printf '%s\n' "CC=${pre}gcc-${g}" "HOSTCC=gcc-${g}${hf:+ $hf}" "HOSTCXX=g++-${g}"
    elif [[ -n "$hf" ]]; then
        printf '%s\n' "HOSTCC=gcc $hf"
    fi
    [[ -n "$kf" ]] && printf '%s\n' "KCFLAGS=$kf"
    local inc; inc="$(kbl_tree_src "$1")/scripts/Kbuild.include"
    if grep -qF 'subst \#,\\\#,' "$inc" 2>/dev/null && ! grep -q '^pound' "$inc"; then
        printf '%s\n' 'pound=#' \
            'make-cmd=$(call escsq,$(subst $(pound),$$(pound),$(subst $$,$$$$,$(cmd_$(1)))))'
    fi
    return 0
}

# The make command line for a tree's arch and compiler, left in _KBL_MAKE.
_kmake_args() {   # _kmake_args TREE
    local arch; arch="$(kbl_tree_arch "$1")" || exit 1
    local -a cc=(); mapfile -t cc < <(_cc_args "$1" "$arch")
    _KBL_MAKE=(make ARCH="$(_karch "$arch")" CROSS_COMPILE="$(_cross "$arch")" "${cc[@]+"${cc[@]}"}")
}

# That make, run in the tree's image with its source as the working directory.
_kmake() {   # _kmake TREE MAKE-ARG...
    local tree="$1" src _KBL_ACTIVE_IMAGE _KBL_ACTIVE_TREE; shift
    src="$(kbl_tree_src "$tree")" || exit 1
    _KBL_ACTIVE_IMAGE="$(_tree_image "$tree")"; _KBL_ACTIVE_TREE="$tree"
    _kmake_args "$tree"
    _in_container "$src" "${_KBL_MAKE[@]}" "$@"
}

# ---------------------------------------------------------------- sync
cmd_sync() {
    local tree; tree="$(kbl_tree "${1:-}")" || exit 1
    local ver url src
    ver="$(kbl_tree_get "$tree" VERSION)"
    [[ -n "$ver" ]] || die "$tree/tree.conf states no VERSION"
    case "$(kbl_tree_get "$tree" UPSTREAM)" in
        mainline) url="$MAINLINE_URL" ;;
        *)        url="$STABLE_URL" ;;
    esac
    src="$(kbl_tree_src "$tree")" || exit 1

    if [[ ! -d "$src/.git" ]]; then
        say "cloning      $ver from $url"
        # KBL_FULL_HISTORY: full clone (keeps git log/blame/diffs); borrow a local
        # KBL_MIRROR to skip re-downloading, then dissociate. Else depth-1.
        if [[ -n "${KBL_FULL_HISTORY:-}" ]]; then
            local -a _refopt=()
            [[ -n "${KBL_MIRROR:-}" && -d "$KBL_MIRROR" ]] && _refopt=(--reference-if-able "$KBL_MIRROR" --dissociate)
            git clone "${_refopt[@]+"${_refopt[@]}"}" --branch "$ver" "$url" "$src" \
                || die "clone failed: is '$ver' a real tag or branch?"
        else
            git clone --depth 1 --branch "$ver" "$url" "$src" \
                || die "clone failed: is '$ver' a real tag or branch?"
        fi
        return 0
    fi

    say "fetching     $ver"
    # depth-1 onto a full clone would shallow-truncate its history; keep depth-1
    # only when the repo is already shallow.
    local shallow=0 tracked=""
    if [[ "$(git -C "$src" rev-parse --is-shallow-repository 2>/dev/null)" == "true" ]]; then
        shallow=1
        tracked="$(git -C "$src" rev-parse --verify --quiet "refs/remotes/origin/$ver")"
        git -C "$src" fetch --depth 1 origin "$ver" || die "fetch failed"
    else
        git -C "$src" fetch --tags origin "$ver" || die "fetch failed"
    fi
    # A tag never moves, so fetching one is a no-op; a stable BRANCH does move,
    # and that is the follow-up-patch case this command exists for.
    if git -C "$src" rev-parse --verify --quiet "refs/tags/$ver" >/dev/null; then
        say "pinned       $ver is a tag; nothing to advance"
    else
        local before after
        before="$(git -C "$src" rev-parse HEAD)"
        if [[ $shallow -eq 1 ]] && ! git -C "$src" merge-base --is-ancestor HEAD FETCH_HEAD 2>/dev/null \
           && ! git -C "$src" merge-base --is-ancestor FETCH_HEAD HEAD 2>/dev/null; then
            # A depth-1 fetch cuts history at the new tip, so ancestry cannot be
            # shown; HEAD moves only if it is exactly what the last fetch brought.
            [[ -n "$tracked" && "$before" == "$tracked" ]] \
                || die "$src is shallow and its HEAD is not origin/$ver as last fetched, so it may
       carry local commits; nothing was changed.  git -C $src fetch --unshallow, then sync"
            git -C "$src" reset --keep FETCH_HEAD || die "local changes in $src conflict with the new $ver"
        else
            git -C "$src" merge --ff-only FETCH_HEAD || die "not a fast-forward -- the tree has local commits"
        fi
        after="$(git -C "$src" rev-parse HEAD)"
        if [[ "$before" == "$after" ]]; then say "up to date   $(git -C "$src" describe --tags 2>/dev/null || echo "$after")"
        else say "advanced     $(git -C "$src" describe --tags 2>/dev/null || echo "$after")"; fi
    fi
}

# ---------------------------------------------------------------- config
cmd_config() {
    local tree preset=0
    local -a rest=()
    while [[ $# -gt 0 ]]; do
        case "$1" in --preset) preset=1; shift ;; *) rest+=("$1"); shift ;; esac
    done
    tree="$(kbl_tree "${rest[0]:-}")" || exit 1
    local arch src; arch="$(kbl_tree_arch "$tree")" || exit 1; src="$(kbl_tree_src "$tree")" || exit 1
    [[ -d "$src" ]] || die "no kernel source at $src -- run: kbuildlab sync $(basename "$tree")"
    local _KBL_ACTIVE_IMAGE _KBL_ACTIVE_TREE="$tree"; _KBL_ACTIVE_IMAGE="$(_tree_image "$tree")"

    local -a frags=() soft=()
    [[ -f "$tree/config-fragment" ]] && frags+=("$tree/config-fragment")
    if [[ $preset -eq 1 ]]; then
        # A mainline tree may need an arch preset that differs from the stable one
        # (e.g. riscv dropped compile-time PREEMPT_NONE and reworked ftrace). Use
        # presets/arch/<arch>-mainline.config when it exists and UPSTREAM=mainline.
        local archpreset="${KBL_REPO}/presets/arch/${arch}.config"
        if [[ "$(kbl_tree_get "$tree" UPSTREAM)" == "mainline" \
              && -f "${KBL_REPO}/presets/arch/${arch}-mainline.config" ]]; then
            archpreset="${KBL_REPO}/presets/arch/${arch}-mainline.config"
        fi
        soft=("${KBL_REPO}"/presets/soft/*.config)
        [[ -f "${KBL_REPO}/presets/arch/${arch}.soft.config" ]] \
            && soft+=("${KBL_REPO}/presets/arch/${arch}.soft.config")
        if _older_than_preset "$src"; then
            # The hard preset names symbols a kernel older than the one it was
            # verified on lacks or restricts; there it is taken as wishes.
            soft=("${KBL_REPO}/presets/common.config" "$archpreset" "${soft[@]}")
            say "preset       $(basename "$tree") predates the preset's reference kernel; taken softly"
        else
            frags=("${KBL_REPO}/presets/common.config" "$archpreset" "${frags[@]+"${frags[@]}"}")
        fi
    fi

    local rec="${KBL_LAB}/trees/$(basename "$tree")/defconfig"
    if [[ ! -f "$src/.config" && -f "$rec" ]]; then
        say "restoring    the recorded config, $rec"
        _lab_copy "$rec" "$src/.config" @KBL_WORKSPACE@ "$(kbl_workspace_opt || echo @KBL_WORKSPACE@)" \
            || die "cannot copy $rec"
        _kmake "$tree" olddefconfig || die "olddefconfig failed"
    elif [[ ! -f "$src/.config" ]]; then
        say "defconfig    $(_karch "$arch")"
        _kmake "$tree" defconfig || die "defconfig failed"
    else
        say "keeping      the existing .config"
    fi

    if [[ ${#frags[@]} -gt 0 && -n "${frags[0]:-}" ]]; then
        say "merging      ${#frags[@]} fragment(s)"
        _in_container "$src" ./scripts/kconfig/merge_config.sh -m .config "${frags[@]}" \
            || die "merge_config failed"
        _kmake "$tree" olddefconfig || die "olddefconfig failed"
        # The whole point: read it back.  A fragment line whose depends-on was
        # unmet is dropped in silence, and the build then succeeds without it.
        "${KBL_REPO}/presets/verify.sh" "$src/.config" "${frags[@]}" \
            || die "the .config does not match what was asked for (above)"
    fi

    if [[ ${#soft[@]} -gt 0 ]]; then
        say "enabling     what the soft presets name, moving no existing answer"
        _container_run "$src" || exit 1
        _kmake_args "$tree"
        python3 "${KBL_REPO}/lib/softconfig.py" --config "$src/.config" --source "$src" \
            --olddefconfig "$(printf '%q ' "${_KBL_RUN[@]}" "${_KBL_MAKE[@]}" olddefconfig)" \
            "${soft[@]}" || die "the soft presets could not be applied"
    fi
    _record "$tree"
}

# A kernel's VERSION.PATCHLEVEL as one number, read from its own Makefile.
_kversion() {   # _kversion SRC
    awk '/^VERSION *=/ {v = $3} /^PATCHLEVEL *=/ {p = $3} END {if (v != "") print v * 1000 + p}' \
        "$1/Makefile"
}

# Whether a kernel predates the one the hard presets were verified on, which
# presets/common.config names in its own header.
_older_than_preset() {   # _older_than_preset SRC
    local ref kv
    ref="$(sed -n 's/.*[Vv]erified against Linux \([0-9]*\)\.\([0-9]*\).*/\1 \2/p' \
             "${KBL_REPO}/presets/common.config" | head -1 | awk '{print $1 * 1000 + $2}')"
    [[ -n "$ref" ]] || die "presets/common.config does not say which kernel it was verified against"
    kv="$(_kversion "$1")"
    [[ -n "$kv" && "$kv" -lt "$ref" ]]
}

# ---------------------------------------------------------------- build
cmd_build() {
    local tree
    local -a mkargs=()
    local -a rest=()
    while [[ $# -gt 0 ]]; do
        if [[ "$1" == "--" ]]; then shift; mkargs=("$@"); break; fi
        rest+=("$1"); shift
    done
    tree="$(kbl_tree "${rest[0]:-}")" || exit 1
    local arch src jobs
    arch="$(kbl_tree_arch "$tree")" || exit 1; src="$(kbl_tree_src "$tree")" || exit 1
    [[ -f "$src/.config" ]] || die "no .config in $src -- run: kbuildlab config $(basename "$tree") --preset"
    jobs="${KBL_JOBS:-$(( $(nproc) > 2 ? $(nproc) - 2 : 1 ))}"
    _kmake_args "$tree"
    say "building     $(basename "$tree") ($arch, -j$jobs${_KBL_MAKE[3]+, ${_KBL_MAKE[3]#CC=}})"
    _kmake "$tree" -j"$jobs" "${mkargs[@]+"${mkargs[@]}"}" || die "build failed"
    [[ -f "$src/vmlinux" ]] || die "build reported success but produced no vmlinux"
    say "vmlinux      $src/vmlinux"
    _stage_modules "$tree"
}

# The guests boot a rootfs that carries none of this build's modules, so the
# build leaves them beside the tree as a cpio archive that run appends to the
# initramfs; the kernel unpacks concatenated archives as one.  Stripped, since
# the guest only loads them and gdb reads the build's own copies.  With them
# goes an init script that loads what the devices present at boot ask for, as
# udev would, so an emulated controller whose driver is a module binds.  Only
# lib/modules and that script are named, never a directory the rootfs already
# has: the kernel replaces a path whose type differs, so a rootfs whose /lib
# is a symlink would lose it.
_stage_modules() {   # _stage_modules TREE
    local tree="$1" src out stage hook=etc/init.d/S10kbuildlab-coldplug
    src="$(kbl_tree_src "$tree")" || exit 1; out="$(kbl_tree_data "$tree")/modules.cpio.gz"
    mkdir -p "$(dirname "$out")" || { warn "modules: cannot write $(dirname "$out")"; return 1; }
    if [[ ! -s "$src/modules.order" ]]; then rm -f "$out"; return 0; fi
    [[ -f "$out" && ! "$src/modules.order" -nt "$out" \
       && -z "$(find "$src" -name '*.ko' -newer "$out" -print -quit)" ]] && return 0
    stage="$(kbl_tree_data "$tree")/.modules-stage"
    local _KBL_ACTIVE_IMAGE _KBL_ACTIVE_TREE="$tree"; _KBL_ACTIVE_IMAGE="$(_tree_image "$tree")"
    rm -rf "$stage" && mkdir -p "$stage/$(dirname "$hook")" \
        || { warn "modules: cannot write $stage"; return 1; }
    cat > "$stage/$hook" <<'EOF'
#!/bin/sh
[ "$1" = start ] || exit 0
printf 'Loading modules for the devices present: '
find /sys/devices -name modalias | while read -r f; do cat "$f"; done 2>/dev/null \
    | sort -u | xargs -r modprobe -a -q 2>/dev/null
echo OK
EOF
    chmod 755 "$stage/$hook"
    local rc=0
    if ! _kmake "$tree" INSTALL_MOD_PATH="$stage" INSTALL_MOD_STRIP=1 modules_install >/dev/null; then
        warn "modules: modules_install failed (above); the guest gets none of this build's modules"; rc=1
    elif [[ -z "$(find "$stage/lib/modules" -name '*.ko*' -print -quit 2>/dev/null)" ]]; then
        warn "modules: modules_install wrote no module into $stage, though $src/modules.order
       lists $(grep -c . "$src/modules.order"); the guest gets none of this build's modules"; rc=1
    elif _in_container "$stage" sh -c "find lib/modules $hook | cpio -o -H newc -R 0:0 --quiet | gzip -9" \
           > "$out.tmp"; then
        mv -f "$out.tmp" "$out"
        say "modules      $out ($(grep -c . "$src/modules.order") for the guest, $(du -h "$out" | cut -f1))"
    else
        warn "modules: the archive could not be written (above); the guest gets none of this build's modules"; rc=1
    fi
    rm -f "$out.tmp"
    rm -rf "$stage"
    return $rc
}
