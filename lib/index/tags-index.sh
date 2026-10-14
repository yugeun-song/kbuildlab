#!/bin/bash
# ctags, cscope and gtags over the view, in the build image: tags-index.sh ARCH full|merge
# Exit 0 when all three indexed; 1 when one failed, after withdrawing what it
# wrote so the tree keeps its previous index for that one.
set -uo pipefail
{
here=$(dirname "$(readlink -f "$0")")
arch=${1:?} mode=${2:?}
st=${KBL_TAGS_STATE:?}
: > "$st/started"
ctags_full() { make ARCH="$arch" tags && python3 "$here/tagview.py" address; }
ctags_merge() {
    bash "$here/tags-merge.sh" && rm -f "$st/emitted" \
        && KBL_TAGS_EMIT=1 PATH="$here/ctags-shim:$PATH" make ARCH="$arch" tags \
        && python3 "$here/tagview.py" address
}
declare -A pid
if [[ $mode == merge ]]; then
    { ctags_merge || { echo "tags-index: the ctags merge failed; indexing in full" >&2; ctags_full; }; } &
else
    ctags_full &
fi
pid[ctags]=$!
make ARCH="$arch" cscope & pid[cscope]=$!
if command -v gtags >/dev/null 2>&1; then make ARCH="$arch" gtags & pid[gtags]=$!
else echo "tags-index: no 'global' in the build image; gtags skipped" >&2; fi
rc=0
for n in "${!pid[@]}"; do
    wait "${pid[$n]}" && continue
    echo "tags-index: $n failed; the tree keeps its previous $n index" >&2
    mapfile -t out < <(python3 "$here/tagview.py" outputs "$n")
    rm -f -- "${out[@]}"
    rc=1
done
exit $rc
}
