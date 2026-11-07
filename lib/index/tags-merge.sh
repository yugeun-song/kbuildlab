#!/bin/bash
set -euo pipefail
{
d=${KBL_TAGS_STATE:?}
prev=$d/tags.prev
new=$d/tags.new
jobs=${KBL_JOBS:-$(nproc)}
[[ -e $prev ]] || { echo "tags-merge: no $prev" >&2; exit 1; }
[[ -s $d/keys ]] || { echo "tags-merge: the shim recorded no ctags run" >&2; exit 1; }
compgen -G "$d/files.*" >/dev/null || { echo "tags-merge: the shim recorded no file" >&2; exit 1; }
origin() { LC_ALL=C awk -F'\t' '$1 == "!_TAG_PROC_CWD" || $1 == "!_TAG_PROGRAM_VERSION"' | LC_ALL=C sort; }
[[ ! -s $prev || "$(origin < "$prev")" == "$(ctags --extras=+p -o - /dev/null 2>/dev/null | origin)" ]] \
    || { echo "tags-merge: $prev was made in another directory or by another ctags" >&2; exit 1; }

# build_symbols.py decides the lines marked origin:build anew on every run.
LC_ALL=C awk -F'\t' '!/^!_TAG_/ && !/\torigin:build$/ && !seen[$2]++ { print $2 }' "$prev" | LC_ALL=C sort -u > "$d/old.files"
: > "$d/now.files"
: > "$d/newer.files"
while read -r k; do
    LC_ALL=C sort -u "$d/files.$k" -o "$d/now.$k"
    cat "$d/now.$k" >> "$d/now.files"
    tr '\n' '\0' < "$d/now.$k" | xargs -0 -r sh -c 'find "$@" -maxdepth 0 -newer "$0"' "$prev" >> "$d/newer.files"
done < "$d/keys"
LC_ALL=C sort -u "$d/now.files" -o "$d/now.files"
{
    LC_ALL=C comm -23 "$d/old.files" "$d/now.files"
    LC_ALL=C comm -13 "$d/old.files" "$d/now.files"
    cat "$d/newer.files"
} | LC_ALL=C sort -u > "$d/drop.files"

LC_ALL=C awk -F'\t' -v dropf="$d/drop.files" '
    BEGIN { while ((getline f < dropf) > 0) drop[f] = 1 }
    /^!_TAG_/ || (!/\torigin:build$/ && !($2 in drop))' "$prev" > "$new"

# ctags re-sorts its whole output after every run, so each worker writes its own
# and they are sorted together once; an empty $prev indexes everything.
rm -f "$d"/part.*
while read -r k; do
    LC_ALL=C comm -12 "$d/now.$k" "$d/drop.files" > "$d/replay.$k"
    [[ -s $d/replay.$k ]] || continue
    mapfile -d '' -t opts < "$d/opts.$k"
    split -e -n "r/$jobs" "$d/replay.$k" "$d/part.$k."
    for p in "$d/part.$k."*; do
        { ctags "${opts[@]}" -f "$p.tags" -L "$p" || echo "tags-merge: ctags exited $? on pass $k" >&2; } &
    done
    wait
done < "$d/keys"
if compgen -G "$d/part.*.tags" >/dev/null; then
    LC_ALL=C sort -u -T "$d" -o "$new.sorted" "$new" "$d"/part.*.tags
    mv -f "$new.sorted" "$new"
fi
rm -f "$d"/part.*

echo "tags-merge: $(wc -l < "$d/now.files") files listed;" \
     "$(LC_ALL=C comm -13 "$d/old.files" "$d/now.files" | wc -l) new," \
     "$(LC_ALL=C comm -23 "$d/old.files" "$d/now.files" | wc -l) gone," \
     "$(LC_ALL=C sort -u "$d/newer.files" | wc -l) changed; $(LC_ALL=C sort -u "$d"/replay.* 2>/dev/null | wc -l) indexed again"
}
