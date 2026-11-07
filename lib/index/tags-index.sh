#!/bin/bash
# ctags, cscope and gtags over the view, in the build image: tags-index.sh ARCH full|merge|fresh
# Exit 0 when all three indexed; 1 when one failed, after withdrawing what it
# wrote so the tree keeps its previous index for that one.
set -uo pipefail
{
here=$(dirname "$(readlink -f "$0")")
arch=${1:?} mode=${2:?}
st=${KBL_TAGS_STATE:?}
: > "$st/started"
if [[ $mode == fresh ]]; then
    mapfile -t out < <(python3 "$here/tagview.py" outputs)
    rm -f -- "${out[@]}"
    mode=full
fi
if [[ $mode == merge ]] && ! cmp -s "$st/keys" "$st/tags.keys"; then
    echo "tags-index: tags was made with other ctags options; indexing it in full"
    rm -f tags
    mode=full
fi
# What the build defined and ctags did not see: never worth failing tags over, but
# said when it is missing.  Its memory is the first suspect.
build_symbols() {
    python3 "$here/build_symbols.py" && return
    echo "tags-index: reading the build's symbols failed; reading every object again" >&2
    rm -f "$st/symbols.json"
    python3 "$here/build_symbols.py" \
        || echo "tags-index: tags hold only what ctags saw; definitions it missed stay missing" >&2
}
ctags_done() { python3 "$here/tagview.py" address && build_symbols && cp "$st/keys" "$st/tags.keys"; }
ctags_serial() { KBL_TAGS_PASS=1 PATH="$here/ctags-shim:$PATH" make ARCH="$arch" tags && ctags_done; }
ctags_merge() {
    bash "$here/tags-merge.sh" && rm -f "$st/emitted" \
        && KBL_TAGS_EMIT=1 PATH="$here/ctags-shim:$PATH" make ARCH="$arch" tags \
        && ctags_done
}
declare -A pid
# tags.prev can be a hard link to the tree's tags, which keeps its content until
# new indexes are published.
if [[ $mode != merge ]]; then
    rm -f "$st/tags.prev"
    : > "$st/tags.prev"
fi
{ ctags_merge || { echo "tags-index: the ctags workers failed; indexing with one" >&2; ctags_serial; }; } &
pid[ctags]=$!
make ARCH="$arch" cscope & pid[cscope]=$!
if command -v gtags >/dev/null 2>&1; then make ARCH="$arch" gtags & pid[gtags]=$!
else echo "tags-index: no 'global' in the build image; gtags skipped" >&2; fi
rc=0
for n in "${!pid[@]}"; do
    wait "${pid[$n]}" && continue
    mapfile -t out < <(python3 "$here/tagview.py" outputs "$n")
    rm -f -- "${out[@]}"
    if [[ $n != ctags ]]; then
        echo "tags-index: $n failed; indexing it again from nothing" >&2
        make ARCH="$arch" "$n" && continue
        rm -f -- "${out[@]}"
    fi
    echo "tags-index: $n failed; the tree keeps its previous $n index" >&2
    rc=1
done
exit $rc
}
