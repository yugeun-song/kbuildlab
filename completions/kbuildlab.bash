# bash completion for kbuildlab.  It asks kbuildlab itself (`kbuildlab
# __complete`, lib/cli.py) what may come next, so the candidates follow the
# checkout: commands, options, registered trees, tree.conf keys and the ports of
# live guests.  bash-completion loads it by name; sourcing it works too.

_kbuildlab() {
    local cur words cword
    if declare -F _comp_get_words >/dev/null 2>&1; then
        _comp_get_words -n =: cur words cword
    elif declare -F _get_comp_words_by_ref >/dev/null 2>&1; then
        _get_comp_words_by_ref -n =: cur words cword
    else
        cur=${COMP_WORDS[COMP_CWORD]} words=("${COMP_WORDS[@]}") cword=$COMP_CWORD
    fi
    local line tag rest dirs=0 files=0 nospace=0
    local -a cands=()
    while IFS= read -r line; do
        tag=${line%%$'\t'*} rest=${line#*$'\t'}
        case $tag in
            @dirs) dirs=1 ;;
            @files) files=1 ;;
            @nospace) nospace=1 ;;
            @message|'') ;;
            *) cands+=("${rest%%$'\t'*}") ;;
        esac
    done < <("${words[0]}" __complete "${words[@]:1:cword-1}" "$cur" 2>/dev/null)
    local IFS=$'\n'
    COMPREPLY=($(compgen -W "${cands[*]}" -- "$cur"))
    if (( dirs || files )); then
        local -a paths=()
        if (( dirs )); then paths=($(compgen -d -- "$cur")); else paths=($(compgen -f -- "$cur")); fi
        COMPREPLY+=("${paths[@]}")
        compopt -o filenames 2>/dev/null
    fi
    (( nospace )) && compopt -o nospace 2>/dev/null
    return 0
}
complete -F _kbuildlab kbuildlab
