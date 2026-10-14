#!/usr/bin/env bash
# Put the `kbuildlab` command on PATH with its bash and zsh completions, and
# record this checkout, so a program started without that PATH (an editor
# launched from a desktop menu) still finds it.  The command and the completions
# are symlinks into this checkout, so a `git pull` here updates them.
#
#   ./install.sh                  ~/.local/bin (XDG_BIN_HOME), for this user
#   ./install.sh --system         /usr/local/bin, for every user who can read
#                                 this checkout; run it as yourself and it asks
#                                 sudo for the links only, then records as you
#   ./install.sh --prefix DIR     DIR/bin (through sudo too when you cannot write it)
#   ./install.sh --sudo CMD       what asks for root: sudo, else doas, else run0
#   ./install.sh --alias NAME     also install it as NAME (repeatable), e.g. kbl
#   ./install.sh --record         only record this checkout (after `sudo ./install.sh --system`)
#   ./install.sh --check | --uninstall
#
# Both default directories are on PATH on Debian, Ubuntu, Fedora, RHEL and
# openSUSE once they exist; on Arch and Void ~/.local/bin is added by the
# user's shell setup, and this says how when it is missing.  The completions go
# where bash-completion and zsh look: share/bash-completion/completions and
# share/zsh/site-functions under /usr/local or DIR, or under XDG_DATA_HOME for
# this user (zsh reads that one only once it is on $fpath, and this says how).
# Shell files are never edited, and nothing is installed from a package
# manager: `kbuildlab doctor` names what is missing and the package for it.
set -euo pipefail
REPO="$(cd -P "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
NAME=kbuildlab
MODE=install; WHERE=user; PREFIX=""; SUDO_CMD=""
declare -a ALIASES=()

while [[ $# -gt 0 ]]; do
    case "$1" in
        --user) WHERE=user; shift ;;
        --system) WHERE=system; shift ;;
        --prefix) WHERE=prefix; PREFIX="${2:?--prefix needs a directory}"; shift 2 ;;
        --alias) ALIASES+=("${2:?--alias needs a name}"); shift 2 ;;
        --sudo) SUDO_CMD="${2:?--sudo needs a command}"; shift 2 ;;
        --record) MODE=record; shift ;;
        --check) MODE=check; shift ;;
        --uninstall) MODE=uninstall; shift ;;
        -h|--help) sed -n '2,/^[^#]/{/^#/s/^# \{0,1\}//p}' "$0"; exit 0 ;;
        *) echo "install.sh: unknown option '$1'" >&2; exit 2 ;;
    esac
done

if [[ -t 1 && -z "${NO_COLOR:-}" && "${TERM:-dumb}" != dumb ]]; then
    G=$'\e[32m' R=$'\e[31m' Y=$'\e[33m' C=$'\e[36m' D=$'\e[2m' N=$'\e[0m'
else G="" R="" Y="" C="" D="" N=""; fi
case "${LC_ALL:-${LC_CTYPE:-${LANG:-}}}" in
    *[Uu][Tt][Ff]-8*|*[Uu][Tt][Ff]8*) OKM="✓" NOM="✗" ;; *) OKM="+" NOM="x" ;;
esac
ok()  { printf '  %s%s%s %s\n' "$G" "$OKM" "$N" "$*"; }
bad() { printf '  %s%s%s %s\n' "$R" "$NOM" "$N" "$*"; }
warn() { printf '  %s!%s %s\n' "$Y" "$N" "$*"; }
say() { printf '  %s%-12s%s %s\n' "$C" "$1" "$N" "${*:2}"; }

case "$WHERE" in
    user)   BIN="${XDG_BIN_HOME:-$HOME/.local/bin}"; SHARE="${XDG_DATA_HOME:-$HOME/.local/share}"
            BASHC="${BASH_COMPLETION_USER_DIR:-$SHARE/bash-completion}/completions" ;;
    system) BIN=/usr/local/bin; SHARE=/usr/local/share; BASHC="$SHARE/bash-completion/completions" ;;
    prefix) BIN="${PREFIX%/}/bin"; SHARE="${PREFIX%/}/share"; BASHC="$SHARE/bash-completion/completions" ;;
esac
ZSHC="$SHARE/zsh/site-functions"
TARGET="$REPO/bin/$NAME"
POINTER="${XDG_CONFIG_HOME:-$HOME/.config}/kbuildlab/root"

on_path() { case ":$PATH:" in *":$1:"*) return 0 ;; esac; return 1; }
path_hint() {
    on_path "$BIN" && return 0
    warn "$BIN is not on PATH; add it in your shell's startup file:"
    printf '      bash, zsh:  export PATH="%s:$PATH"\n' "$BIN"
    printf '      fish:       fish_add_path %s\n' "$BIN"
}
# Root only links into /usr/local/bin: what else this does belongs to the user
# who runs kbuildlab (their config), and under sudo it would land in root's.
if [[ "$(id -u)" -eq 0 && "$MODE" != check ]] && ! [[ "$WHERE" == system && "$MODE" != record ]]; then
    bad "run this as yourself; only --system (linking into /usr/local/bin) needs root"; exit 1
fi
user_scope() { [[ "$(id -u)" -ne 0 ]]; }

mine() { [[ -L "$1" && "$(readlink -f "$1")" == "$TARGET" ]]; }
dangling() { [[ -L "$1" && ! -e "$1" ]]; }

# What goes where, as KIND SOURCE DEST triples: L links DEST to SOURCE, F copies
# SOURCE (a file written here) to DEST.  An --alias gets its own small completion
# file, since a completion is registered by the name it completes.
CMARK="# completion for an --alias of kbuildlab, written by its install.sh"
STAGE=""
declare -a PLAN=()
plan_all() {
    local n
    for n in "$NAME" "${ALIASES[@]+"${ALIASES[@]}"}"; do
        PLAN+=(L "$TARGET" "$BIN/$n")
        if [[ "$n" == "$NAME" ]]; then
            PLAN+=(L "$REPO/completions/kbuildlab.bash" "$BASHC/$n" L "$REPO/completions/_kbuildlab" "$ZSHC/_$n")
        else
            [[ -n "$STAGE" ]] || { STAGE="$(mktemp -d)"; trap 'rm -rf -- "$STAGE"' EXIT; }
            printf '%s\n. "${BASH_SOURCE[0]%%/*}/%s" && complete -F _kbuildlab %s\n' "$CMARK" "$NAME" "$n" > "$STAGE/$n.bash"
            printf '#compdef %s\n%s\n_kbuildlab "$@"\n' "$n" "$CMARK" > "$STAGE/_$n"
            PLAN+=(F "$STAGE/$n.bash" "$BASHC/$n" F "$STAGE/_$n" "$ZSHC/_$n")
        fi
    done
}
# Whether DEST already is what KIND SOURCE would make it.
placed() {
    case "$1" in
        L) [[ -L "$3" && "$(readlink -f "$3")" == "$(readlink -f "$2")" ]] ;;
        F) [[ -f "$3" && ! -L "$3" ]] && cmp -s "$2" "$3" ;;
    esac
}
# Whether DEST is something this script made (and may replace or remove).
ours() {
    [[ -L "$1" ]] && case "$(readlink -f "$1")" in "$TARGET"|"$REPO"/completions/*) return 0 ;; esac
    [[ -f "$1" && ! -L "$1" ]] && grep -qxF "$CMARK" "$1" 2>/dev/null
}

record() { mkdir -p "$(dirname "$POINTER")" && printf '%s\n' "$REPO" > "$POINTER" && ok "recorded in $POINTER"; }

# A link into a directory this user cannot write (--system, or a root-owned
# --prefix) goes through sudo, asked once before anything changes; the record
# stays this user's.  Never for ~/.local/bin: a root-owned link there is wrong.
declare -a ELEVATE=()
ask_root() {   # ask_root DEST...: elevate when any of them cannot be written
    local d="" p cmd
    [[ "$(id -u)" -ne 0 && "$WHERE" != user ]] || return 0
    for p in "$@"; do
        p="$(dirname "$p")"
        while [[ ! -d "$p" ]]; do p="$(dirname "$p")"; done
        [[ -w "$p" ]] || { d="$p"; break; }
    done
    [[ -n "$d" ]] || return 0
    if [[ -n "$SUDO_CMD" ]]; then
        read -ra ELEVATE <<<"$SUDO_CMD"
    else
        for cmd in sudo doas run0; do command -v "$cmd" >/dev/null 2>&1 && { ELEVATE=("$cmd"); break; }; done
    fi
    if [[ ${#ELEVATE[@]} -eq 0 ]]; then
        bad "cannot write $d, and none of sudo, doas, run0 is installed; --sudo CMD names another"; exit 1
    fi
    command -v "${ELEVATE[0]}" >/dev/null 2>&1 || { bad "--sudo: ${ELEVATE[0]} is not installed"; exit 1; }
    say root "cannot write $d; asking ${ELEVATE[0]##*/} for the links only"
    if [[ "${ELEVATE[0]}" == sudo ]]; then
        "${ELEVATE[@]}" -v || { bad "sudo did not authenticate; nothing was changed"; exit 1; }
    fi
}
as_root() { "${ELEVATE[@]+"${ELEVATE[@]}"}" "$@"; }

# The first directory on the way to this checkout that other users cannot enter.
reach_block() {
    local d="$REPO/bin"
    while [[ "$d" != / && -n "$d" ]]; do
        [[ "$(stat -c %A "$d" 2>/dev/null)" == *[xt] ]] || { printf '%s\n' "$d"; return 0; }
        d="$(dirname "$d")"
    done
    return 1
}
reach_warn() {
    local blk
    [[ "$WHERE" != user ]] && blk="$(reach_block)" || return 0
    warn "other users cannot enter $blk ($(stat -c %A "$blk")), so the link works only for $(stat -c %U "$REPO")"
}

# zsh reads a completion only from a directory on $fpath.  The system and the
# usual prefixes are on the default one; a user's XDG directory is on it only if
# their startup files put it there, which is looked for, never written.
zsh_hint() {
    command -v zsh >/dev/null 2>&1 || return 0
    zsh -fc 'print -rl -- $fpath' 2>/dev/null | grep -qxF "$ZSHC" && return 0
    local rc="${ZDOTDIR:-$HOME}/.zshrc" short="${ZSHC/#$HOME/\~}"
    [[ -r "$rc" ]] && grep -qF -e "$ZSHC" -e "$short" "$rc" && return 0
    warn "zsh completes from $ZSHC only once it is on \$fpath; in ${rc/#$HOME/\~}, before compinit"
    printf '      (or before oh-my-zsh is sourced):  fpath=(%s $fpath)\n' "$short"
}

case "$MODE" in
    record) record ;;
    check)
        plan_all
        for ((i = 0; i < ${#PLAN[@]}; i += 3)); do
            if placed "${PLAN[i]}" "${PLAN[i+1]}" "${PLAN[i+2]}"; then ok "${PLAN[i+2]}"
            else bad "${PLAN[i+2]} is not this checkout's"; fi
        done
        reach_warn
        if [[ -r "$POINTER" && "$(<"$POINTER")" == "$REPO" ]]; then ok "recorded in $POINTER"
        else bad "not recorded in $POINTER"; fi
        path_hint
        [[ "$WHERE" == user ]] && zsh_hint ;;
    uninstall)
        plan_all
        declare -a gone=()
        for ((i = 0; i < ${#PLAN[@]}; i += 3)); do
            d="${PLAN[i+2]}"
            if ours "$d"; then gone+=("$d")
            elif [[ -e "$d" || -L "$d" ]]; then say kept "$d (not this checkout's)"; fi
        done
        if [[ ${#gone[@]} -gt 0 ]]; then
            ask_root "${gone[@]}"
            as_root rm -f -- "${gone[@]}" || { bad "could not remove what was installed"; exit 1; }
            for d in "${gone[@]}"; do ok "removed $d"; done
        fi
        if user_scope && [[ -r "$POINTER" && "$(<"$POINTER")" == "$REPO" ]]; then
            rm -f "$POINTER" && ok "removed $POINTER"
        fi ;;
    install)
        plan_all
        declare -a todo=() dests=()
        for ((i = 0; i < ${#PLAN[@]}; i += 3)); do
            k="${PLAN[i]}" s="${PLAN[i+1]}" d="${PLAN[i+2]}"
            if placed "$k" "$s" "$d"; then ok "$d"; continue; fi
            if [[ -e "$d" || -L "$d" ]] && ! ours "$d" && ! dangling "$d"; then
                if [[ -L "$d" ]]; then bad "$d points at $(readlink -f "$d"), not this checkout; skipped"
                else bad "$d exists and is not this checkout's; skipped"; fi
                continue
            fi
            dangling "$d" && say replaced "$d (it pointed at $(readlink "$d"), which is gone)"
            todo+=("$k" "$s" "$d"); dests+=("$d")
        done
        if [[ ${#todo[@]} -gt 0 ]]; then
            ask_root "${dests[@]}"
            # shellcheck disable=SC2016
            as_root sh -c 'while [ $# -gt 0 ]; do
                    mkdir -p "$(dirname "$3")" || exit
                    if [ "$1" = L ]; then ln -sfn "$2" "$3"; else cp -- "$2" "$3" && chmod 0644 "$3"; fi || exit
                    shift 3
                done' sh "${todo[@]}" || { bad "could not install into $BIN and $SHARE"; exit 1; }
            for d in "${dests[@]}"; do ok "$d"; done
        fi
        reach_warn
        if user_scope; then
            record
            path_hint
            zsh_hint
            printf '  %snext: kbuildlab doctor; a new shell completes kbuildlab with Tab%s\n' "$D" "$N"
        else
            say next "as yourself: $REPO/install.sh --record, then kbuildlab doctor"
        fi ;;
esac
