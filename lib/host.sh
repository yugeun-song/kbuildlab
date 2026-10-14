# The host: doctor, the container image, and what the container runtime holds.
# Sourced by bin/kbuildlab after lib/common.sh.

# ---------------------------------------------------------------- doctor
cmd_doctor() {
    local rc=0 c v a q g t w
    head1 "kbuildlab doctor  ($(kbl_distro))"
    say "repo         ${KBL_REPO}"
    if (( BASH_VERSINFO[0] > 4 || (BASH_VERSINFO[0] == 4 && BASH_VERSINFO[1] >= 4) )); then
        ok "bash $BASH_VERSION"
    else bad "bash $BASH_VERSION -- 4.4 or newer is needed"; rc=1; fi
    v="$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null)"
    if [[ -n "$v" ]] && python3 -c 'import sys; sys.exit(sys.version_info < (3, 12))' 2>/dev/null; then
        ok "python3 $v"
    else bad "python3 ${v:-missing} -- 3.12 or newer is needed$(kbl_pkg_hint python3)"; rc=1; fi
    for t in git ss flock timeout; do
        if command -v "$t" >/dev/null 2>&1; then ok "$t"; else bad "$t missing$(kbl_pkg_hint "$t")"; rc=1; fi
    done
    if c="$(kbl_container 2>/dev/null)"; then
        ok "container    $c"
        if "$c" image inspect "$KBL_IMAGE" >/dev/null 2>&1; then ok "image        $KBL_IMAGE"
        else bad "image        $KBL_IMAGE is not built -- kbuildlab image"; rc=1; fi
    else bad "container    none of podman, docker, nerdctl$(kbl_pkg_hint podman)"; rc=1; fi
    # The floors are what kbuildlab passes to qemu: riscv's sv39/sv48/sv57 CPU
    # properties (CPU_PAGING) arrived in 8.0, and virtio-sound-pci, which
    # --emulate attaches, in 8.2.
    _vge() { printf '%s\n%s\n' "$2" "$1" | sort -V -C; }
    for a in x86_64 arm64 riscv64; do
        case "$a" in arm64) q=qemu-system-aarch64 ;; *) q="qemu-system-$a" ;; esac
        if command -v "$q" >/dev/null 2>&1; then
            v="$("$q" --version 2>/dev/null | sed -n 's/.*version \([0-9][0-9.]*\).*/\1/p' | head -1)"
            if [[ -z "$v" ]]; then bad "$q -- its version could not be read"; rc=1
            elif [[ "$a" == riscv64 ]] && ! _vge "$v" 8.0; then
                bad "$q $v -- 8.0 or newer is needed (the sv48/sv57 CPU properties)"; rc=1
            elif ! _vge "$v" 8.2; then ok "$q $v ${KBL_D}(--emulate needs 8.2: virtio-sound-pci)${KBL_0}"
            else ok "$q $v"; fi
        else bad "$q missing$(kbl_pkg_hint "$q")"; rc=1; fi
        if g="$(kbl_gdb "$a" 2>/dev/null)"; then ok "gdb for $a: $g"
        else bad "gdb for $a: none with a working Python$(kbl_pkg_hint gdb)"; rc=1; fi
    done
    for t in mkimage mcopy clangd drgn; do
        if command -v "$t" >/dev/null 2>&1; then ok "$t"
        else printf '  %s-%s %s (optional)%s\n' "$KBL_D" "$KBL_0" "$t" "$(kbl_pkg_hint "$t")"; fi
    done
    if [[ -r /dev/kvm && -w /dev/kvm ]]; then ok "kvm (x86 guests can use it)"
    else printf '  %s-%s kvm not accessible; x86 guests run under TCG\n' "$KBL_D" "$KBL_0"; fi
    say "registry     $KBL_TREES"
    t="$(kbl_trees | tr '\n' ' ')"
    say "trees        ${t:-(none)}"
    w="$(kbl_legacy_trees | tr '\n' ' ')"
    [[ -n "$w" ]] && { bad "not migrated: $w-- kbuildlab migrate"; rc=1; }
    if w="$(kbl_workspace_opt)"; then say "workspace    $w"
    else say "workspace    not recorded (needed only to clone: kbuildlab init --workspace DIR)"; fi
    [[ $rc -eq 0 ]] || warn "something above is missing; install it and re-run"
    return $rc
}

# ---------------------------------------------------------------- image
# image        -> containers/Containerfile, Ubuntu 26.04 ($KBL_IMAGE)
# image legacy -> containers/Containerfile.legacy, Ubuntu 24.04, gcc-9..13 ($KBL_LEGACY_IMAGE)
cmd_image() {
    local c file tag
    c="$(kbl_container)"
    case "${1:-}" in
        ""|default|latest) file="${KBL_REPO}/containers/Containerfile";        tag="$KBL_IMAGE" ;;
        legacy)            file="${KBL_REPO}/containers/Containerfile.legacy";  tag="$KBL_LEGACY_IMAGE" ;;
        *) die "image: unknown variant '$1' (known: legacy)" ;;
    esac
    [[ -r "$file" ]] || die "image: no Containerfile at $file"
    "$c" build -t "$tag" -f "$file" "${KBL_REPO}/containers" || die "image build failed"
    say "built        $tag"
}

# ---------------------------------------------------------------- containers
# What the container runtime holds for kbuildlab on this host, and whether any of
# it can collide with the system package manager, the host toolchain, or the
# environment a build runs in.  `status` only reports, though it runs throwaway
# containers to ask the images.  `clean` removes, and by
# default only what kbuildlab itself created: the runtime is shared with whatever
# else this machine runs, so a blanket prune is never the right answer here.

# Variables that would steer a kernel build if they reached the container.  The
# image sets none of them, so any of these seen INSIDE means the runtime is passing
# the host environment through and a build would depend on the shell it started from.
_KBL_BUILD_ENV=(CC CXX CPP LD AR NM OBJCOPY OBJDUMP HOSTCC HOSTCXX
                CFLAGS CXXFLAGS CPPFLAGS LDFLAGS KCFLAGS KBUILD_CFLAGS
                MAKEFLAGS ARCH CROSS_COMPILE LD_LIBRARY_PATH LD_PRELOAD
                PKG_CONFIG_PATH C_INCLUDE_PATH CPLUS_INCLUDE_PATH LIBRARY_PATH
                SOURCE_DATE_EPOCH CCACHE_DIR)

# Every image this workspace can use: the two defaults, plus whatever a tree
# names for itself.  status and clean both read this, so a tree that pins its
# own image is not invisible to either.
_kbl_image_tags() {
    { printf '%s\n' "$KBL_IMAGE" "$KBL_LEGACY_IMAGE"
      local t tr
      for t in $(kbl_trees 2>/dev/null); do
          tr="$(kbl_tree "$t" 2>/dev/null)" || continue
          _tree_image "$tr"
      done
    } | awk 'NF && !seen[$0]++'
}

# A gcc banner reduced to what actually identifies the toolchain: the vendor string
# in parentheses and the version after it.  The program name is dropped on purpose --
# a cross compiler is named aarch64-linux-gnu-gcc but is the same toolchain as gcc,
# while the host's "gcc (GCC) 15.1.0" and the image's "gcc (Ubuntu 13.3.0-...) 13.3.0"
# share the name and are the two that must never mix in one build tree.
_cc_fingerprint() { sed -n 's/.*(\(.*\)) *\([0-9][0-9.]*\).*/\1 \2/p' <<<"${1:-}" | head -1; }

_containers_status() {
    local c rc=0; c="$(kbl_container)" || return 1
    echo "kbuildlab containers"
    say "runtime      $c $("$c" --version 2>/dev/null | head -1)"
    if [[ "$(basename "$c")" == podman ]]; then
        say "mode         rootless=$("$c" info --format '{{.Host.Security.Rootless}}' 2>/dev/null) store=$("$c" info --format '{{.Store.GraphRoot}}' 2>/dev/null)"
    fi
    # A daemon is a process, not a unit: report what is actually running rather than
    # what systemd would start, so this works the same for a rootless runtime.
    local d; d="$(pgrep -a -x dockerd 2>/dev/null | head -1)"
    say "daemon       ${d:-none running (nothing to start or stop)}"

    local tag id sz
    while read -r tag; do
        if id="$("$c" image inspect "$tag" --format '{{.Id}}' 2>/dev/null)"; then
            sz="$("$c" image inspect "$tag" --format '{{.Size}}' 2>/dev/null)"
            id="${id#sha256:}"                  # docker prefixes the digest, podman does not
            printf '  %-12s %s  %s  %s MiB\n' "image" "$tag" "${id:0:12}" "$(( ${sz:-0} / 1048576 ))"
        else
            printf '  %-12s %s  (not built)\n' "image" "$tag"
        fi
    done < <(_kbl_image_tags)

    local n=0
    while read -r tag; do
        [[ -n "$tag" ]] || continue
        n=$(( n + $("$c" ps -aq --filter "ancestor=$tag" 2>/dev/null | wc -l) ))
    done < <(_kbl_image_tags)
    say "containers   $n from kbuildlab images (kbuildlab runs with --rm, so this is normally 0)"
    say "untagged     $("$c" images -q -f dangling=true 2>/dev/null | wc -l) image(s) on this host, from any project"

    # Does the host environment reach a build?  Ask the container, do not assume.
    local leaked="" v
    if "$c" image inspect "$KBL_IMAGE" >/dev/null 2>&1; then
        local inside; inside="$("$c" run --rm "$KBL_IMAGE" env 2>/dev/null)"
        for v in "${_KBL_BUILD_ENV[@]}"; do
            grep -q "^${v}=" <<<"$inside" && leaked="$leaked $v"
        done
        say "env          ${leaked:-clean -- no build-steering host variable reaches the container}"
        [[ -n "$leaked" ]] && rc=1
        # Ubuntu in the container, Arch on the host: the same tree built by both mixes
        # objects from two compilers.  Compare what each tree records against the one
        # that would build it now.
        # One run for every compiler the image offers, keyed by the same CROSS_COMPILE
        # prefix kbuildlab passes to make.  Comparing a cross-built tree against the
        # image's NATIVE gcc compares two different compilers and answers nothing --
        # an image is free to carry a different version per target, and this one did
        # until the Containerfile pinned them together.
        local banners a pre rec t cfg
        banners="$("$c" run --rm "$KBL_IMAGE" sh -c '
            for p in "" aarch64-linux-gnu- riscv64-linux-gnu-; do
                printf "%s|%s\n" "$p" "$(${p}gcc --version 2>/dev/null | head -1)"
            done' 2>/dev/null)"
        # Only the default tag is built from containers/Containerfile, so only there
        # does comparing against its pin mean anything; a $KBL_IMAGE pointing
        # somewhere else is simply not checked rather than checked against the wrong
        # file.
        # The image carries several compilers on purpose, so the newest package
        # named in the Containerfile is not what an unpinned build uses.  What it
        # uses is whatever /usr/bin/gcc points at, and the ln that makes it point
        # there is the one line worth comparing against.
        local pinned=""
        [[ "$KBL_IMAGE" == "kbuildlab:latest" ]] && pinned="$(sed -n \
            's#.*ln -sf gcc-\([0-9][0-9]*\) /usr/bin/gcc.*#\1#p' \
            "${KBL_REPO}/containers/Containerfile" 2>/dev/null | head -1)"
        for a in x86_64 arm64 riscv64; do
            pre="$(_cross "$a")"
            local ban maj; ban="$(grep -m1 "^${pre}|" <<<"$banners" | cut -d'|' -f2-)"
            printf '  %-12s %-8s %s\n' "compiler" "$a" "$ban"
            # The Containerfile can move ahead of the image built from it, and nothing
            # else would say so.  The version comes from the fingerprint rather than
            # the last field: a banner can end in a datestamp ("gcc (GCC) 16.2.1 20260810").
            maj="$(_cc_fingerprint "$ban")"; maj="${maj##* }"; maj="${maj%%.*}"
            if [[ -n "$pinned" && -n "$maj" && "$maj" != "$pinned" ]]; then
                printf '  %-12s %s has gcc %s but containers/Containerfile pins gcc-%s -- rebuild with `kbuildlab image`\n' \
                    "STALE" "$a" "$maj" "$pinned"; rc=1
            fi
        done
    else
        say "env          not checked -- $KBL_IMAGE is not built (kbuildlab image)"
    fi

    # This is asked of the images the TREES name, so it is asked whether or not
    # $KBL_IMAGE itself is built: a tree pinned to another image is exactly the
    # case where the default one being absent says nothing about that tree.
    # What each tree recorded, against what would build it now.  A tree names
    # its own image and compiler version, so this is a per-tree question and
    # not a per-architecture one: asking $KBL_IMAGE's native gcc about a tree
    # that pins another image compares two unrelated compilers and reports a
    # mismatch that means nothing.  Probe once per distinct image, with every
    # compiler the trees on it ask for.
    local tr timg tprog probe line
    declare -A _progs=() _treeimg=() _treeprog=()
    for t in $(kbl_trees 2>/dev/null); do
        tr="$(kbl_tree "$t" 2>/dev/null)" || continue
        timg="$(_tree_image "$tr")"
        tprog="$(_cross "$(kbl_tree_arch "$tr" 2>/dev/null)")gcc"
        local g; g="$(_tree_gcc "$tr")"; [[ -n "$g" ]] && tprog="${tprog}-${g}"
        _treeimg["$t"]="$timg"; _treeprog["$t"]="$tprog"
        case " ${_progs[$timg]:-} " in *" $tprog "*) : ;; *) _progs["$timg"]="${_progs[$timg]:-} $tprog" ;; esac
    done
    declare -A _seen=()
    for timg in "${!_progs[@]}"; do
        "$c" image inspect "$timg" >/dev/null 2>&1 || {
            printf '  %-12s %s is named by a tree but is not built (kbuildlab image)\n' "MISSING" "$timg"; rc=1; continue; }
        probe="$("$c" run --rm "$timg" sh -c '
            for p in '"${_progs[$timg]}"'; do printf "%s|%s\n" "$p" "$($p --version 2>/dev/null | head -1)"; done' 2>/dev/null)"
        while IFS= read -r line; do _seen["$timg|${line%%|*}"]="${line#*|}"; done <<<"$probe"
    done
    for t in $(kbl_trees 2>/dev/null); do
        cfg="$(kbl_tree_src "$(kbl_tree "$t")" 2>/dev/null)/.config"
        [[ -r "$cfg" ]] || continue
        rec="$(sed -n 's/^CONFIG_CC_VERSION_TEXT="\(.*\)"$/\1/p' "$cfg" | head -1)"
        # Whether the named compiler EXISTS is a separate question from whether
        # this tree can be compared against it, and it has an answer for every
        # tree.  Ask it before the comparison bails out, or a tree too old to
        # compare is also a tree whose GCC= is never checked at all.
        if [[ -z "${_seen["${_treeimg[$t]}|${_treeprog[$t]}"]:-}" ]]; then
            printf '  %-12s %s asks for %s in %s, which is not there\n' \
                "NO CC" "$t" "${_treeprog[$t]}" "${_treeimg[$t]}"; rc=1; continue
        fi
        if [[ -z "$rec" ]]; then
            # CC_VERSION_TEXT arrived in 4.19.  An older tree records no
            # compiler anywhere in its .config, so this check has nothing to
            # compare and cannot be made to work by trying harder.  Saying
            # so beats skipping in silence, which is the failure this whole
            # command exists to prevent: an unchecked thing that reads as a
            # checked one.
            printf '  %-12s %-18s no CONFIG_CC_VERSION_TEXT (pre-4.19) -- mixed objects cannot be detected here; mrproper before changing GCC=\n' \
                "unchecked" "$t"
            continue
        fi
        local now; now="${_seen["${_treeimg[$t]}|${_treeprog[$t]}"]:-}"
        if [[ -z "$now" ]]; then
            printf '  %-12s %s asks for %s in %s, which is not there\n' \
                "NO CC" "$t" "${_treeprog[$t]}" "${_treeimg[$t]}"; rc=1; continue
        fi
        if [[ "$(_cc_fingerprint "$rec")" != "$(_cc_fingerprint "$now")" ]]; then
            printf '  %-12s %s built by "%s", %s now has "%s" -- rebuilding mixes toolchains (make mrproper first)\n' \
                "MIXED" "$t" "$rec" "${_treeimg[$t]}" "$now"; rc=1
        else
            printf '  %-12s %-18s %s  (%s)\n' "toolchain" "$t" "$now" "${_treeimg[$t]}"
        fi
    done

    # Host-side collisions the package manager would notice.
    local pn; pn="$(ls /etc/containers/*.pacnew 2>/dev/null | tr '\n' ' ')"
    say "pacman       ${pn:+unmerged config: $pn}${pn:-no .pacnew under /etc/containers}"
    [[ -n "$pn" ]] && rc=1
    if [[ "$(basename "$c")" == podman ]]; then
        # $USER is not guaranteed; a cron or systemd context has none.
        local who="${USER:-$(id -un)}"
        grep -q "^${who}:" /etc/subuid 2>/dev/null && grep -q "^${who}:" /etc/subgid 2>/dev/null \
            && say "subid        ${who} has subuid and subgid ranges (rootless is set up)" \
            || { say "subid        ${who} has NO subuid/subgid range -- rootless podman cannot map users"; rc=1; }
    fi
    # What a build mounts writable: each tree's source and data directory, put
    # through the same guard _container_run applies rather than described.
    local p root_owned nt=0 refused=0
    for t in $(kbl_trees 2>/dev/null); do
        nt=$((nt + 1))
        for p in "$(kbl_tree_src "$KBL_TREES/$t" 2>/dev/null)" "$(kbl_tree_data "$KBL_TREES/$t")"; do
            [[ -n "$p" && -e "$p" ]] || continue
            if ! kbl_assert_mountable "$p" >/dev/null 2>&1; then
                say "REFUSED      $t: $p -- the mount guard refuses it, so a build of $t would not start"
                refused=1; rc=1
            fi
            root_owned="$(find "$p" -maxdepth 4 -user root -print -quit 2>/dev/null)"
            [[ -n "$root_owned" ]] && { say "ROOT-OWNED   $root_owned -- a rootful run left it; chown it back"; rc=1; }
        done
    done
    [[ $refused -eq 0 ]] && say "mounts       the sources and data directories of $nt tree(s) pass the mount guard"
    [[ $rc -eq 0 ]] || echo "kbuildlab: something above needs attention" >&2
    return $rc
}

_containers_clean() {
    local c drop_images=0 drop_dangling=0 a n=0 d=0 tag id
    c="$(kbl_container)" || return 1
    for a in "$@"; do
        case "$a" in
            --images)   drop_images=1 ;;
            --dangling) drop_dangling=1 ;;
            -h|--help)  echo "kbuildlab containers clean [--images] [--dangling]"; return 0 ;;
            *) die "containers clean: unknown option '$a' (known: --images, --dangling)" ;;
        esac
    done
    local busy=0
    while read -r tag; do
        [[ -n "$tag" ]] || continue
        # Stopped only.  A container made from this image may still be someone's
        # build running in another terminal, and `rm -f` would kill it; say it is
        # there instead and leave it alone.
        while read -r id; do
            [[ -n "$id" ]] || continue
            "$c" rm "$id" >/dev/null 2>&1 && n=$((n+1))
        done < <("$c" ps -aq --filter "ancestor=$tag" --filter "status=exited" \
                                --filter "status=created" 2>/dev/null)
        busy=$(( busy + $("$c" ps -q --filter "ancestor=$tag" 2>/dev/null | wc -l) ))
    done < <(_kbl_image_tags)
    say "containers   removed $n stopped container(s) made from kbuildlab images"
    [[ $busy -gt 0 ]] && say "running      $busy left alone -- stop them yourself if they are not wanted"
    if [[ $drop_dangling -eq 1 ]]; then
        # Untagged images belong to no project by name, so this cannot be narrowed to
        # ours; it is opt-in for that reason and says what it took.
        while read -r id; do
            [[ -n "$id" ]] || continue
            "$c" rmi "$id" >/dev/null 2>&1 && d=$((d+1))
        done < <("$c" images -q -f dangling=true 2>/dev/null)
        say "untagged     removed $d (every untagged image on this host, not only kbuildlab's)"
    fi
    if [[ $drop_images -eq 1 ]]; then
        # _kbl_image_tags also lists whatever a tree names for itself, and a tree is
        # free to name an image this tool did not build -- a distribution base image,
        # someone else's.  Removing those is not cleaning up after kbuildlab, so only
        # images whose name says kbuildlab built them are removed, and the rest are
        # named so the difference is visible rather than assumed.
        while read -r tag; do
            [[ -n "$tag" ]] || continue
            case "${tag##*/}" in
                kbuildlab:*|kbuildlab-*:*)
                    "$c" rmi "$tag" >/dev/null 2>&1 && say "image        removed $tag" ;;
                *)  say "kept         $tag is named by a tree but was not built here" ;;
            esac
        done < <(_kbl_image_tags)
        say "rebuild      kbuildlab image"
    fi
    if [[ $drop_dangling -eq 1 ]]; then
        say "kept         every TAGGED image and every container this tool did not create"
    else
        say "kept         every image and container this tool did not create"
    fi
}

cmd_containers() {
    case "${1:-status}" in
        status|"") shift || true; _containers_status "$@" ;;
        clean)     shift;         _containers_clean  "$@" ;;
        -h|--help) echo "kbuildlab containers [status|clean] -- see 'kbuildlab help'" ;;
        *) die "containers: unknown subcommand '$1' (known: status, clean)" ;;
    esac
}
